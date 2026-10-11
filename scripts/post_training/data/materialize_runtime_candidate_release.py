#!/usr/bin/env python3
"""Materialize and execute a pinned 32-case train/validation alignment pilot.

Outputs are always outside Git. No model, final test, or original release is
modified. This entry intentionally does not yet expose full-release mode.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE  # noqa: E402
from data_analysis_agent.plan_sql_guard import sql_plan_mismatch  # noqa: E402
from data_analysis_agent.postgres_runner import execution_probe  # noqa: E402
from data_analysis_agent.query_plan import QueryPlan  # noqa: E402
from data_analysis_agent.query_time import DATE_RANGE, EXCLUSIVE_END  # noqa: E402
from data_analysis_agent.result_validator import ResultValidator  # noqa: E402
from data_analysis_agent.runtime_candidate_materializer import (  # noqa: E402
    MATERIALIZER_VERSION, RuntimeMaterializationError, materialize_runtime_candidate_row, text_sha256,
)
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit import compare_denotations  # noqa: E402

SOURCE_HASHES = {
    "train": "e5790e8f6d67321a37e1d031e7088718b6b9a292b71a73d84b5a44917a8616df",
    "validation": "8793b18689eca6ca83530988be4bd227f4193ccd06d9f3aa4a45084f78646329",
}
WORKSPACES = {"olist": OLIST_RUNTIME_WORKSPACE, "thelook": THELOOK_V2_WORKSPACE}
DATABASES = {"olist": "data_analysis_agent", "thelook": "thelook_analytics"}
TOKENIZER_PATH = "/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"


def features(row):
    spec = row["query_spec"]
    result = {f"shape:{spec['result_shape']}", f"dimension:{spec.get('dimension')}",
              f"grain:{spec['time'].get('grain')}", f"family:{row['scenario_family_id']}",
              f"variant:{row['primary_variant_id']}"}
    result.update(f"risk:{risk}" for risk in row.get("risk_tags", ()))
    result.update(f"metric:{metric}" for metric in spec["metric_ids"])
    if DATE_RANGE.search(row["question"]) and not EXCLUSIVE_END.search(row["question"]):
        result.add("edit:exclusive_end")
    if spec["time"].get("grain") and QueryPlan._time_grain(row["question"]) is None:
        result.add("edit:time_grain")
    return result


def select_pilot(rows):
    """8 per workspace/split, shape coverage first, then deterministic diversity."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row["workspace_key"], row["split"]["name"])].append(row)
    selected = []
    for key in ((w, s) for w in WORKSPACES for s in SOURCE_HASHES):
        pool = sorted(groups[key], key=lambda row: text_sha256(row["sample_id"]))
        chosen, covered = [], set()
        shapes = sorted({row["query_spec"]["result_shape"] for row in pool})
        for shape in shapes:
            candidate = next(row for row in pool if row["query_spec"]["result_shape"] == shape)
            chosen.append(candidate)
            covered.update(features(candidate))
        while len(chosen) < 8:
            candidates = [row for row in pool if row not in chosen]
            if not candidates:
                raise ValueError("source group cannot supply the fixed pilot quota")
            candidate = max(candidates, key=lambda row: len(features(row) - covered))
            chosen.append(candidate)
            covered.update(features(candidate))
        selected.extend(chosen)
    if len(selected) != 32 or len({row["sample_id"] for row in selected}) != 32:
        raise ValueError("pilot selection must have 32 unique sources")
    return selected


def execute_gold(cursor, sql, policy):
    decision = policy.evaluate(sql, "analyst")
    probe_sql, probe_count = execution_probe(decision.final_sql, decision.row_limit,
                                            policy_limit_applied=decision.policy_limit_applied)
    cursor.execute(probe_sql)
    rows = cursor.fetchmany(probe_count)
    truncated = decision.policy_limit_applied and len(rows) > decision.row_limit
    frame = pd.DataFrame(rows[:decision.row_limit], columns=[col.name for col in cursor.description])
    return frame, truncated, text_sha256(decision.final_sql)


def frame_sha256(frame):
    payload = {"columns": list(frame.columns), "rows": frame.values.tolist()}
    return text_sha256(json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))


def admit_execution(source, row, workspace):
    mismatch = sql_plan_mismatch(row["candidate_sql"], row["query_plan"])
    if mismatch:
        raise RuntimeMaterializationError("gold_conflicts_with_query_plan_guard")
    policy = SqlPolicy(analyst_limit=1000, workspace=workspace)
    connection = psycopg2.connect(host="/tmp", port=35434, database=DATABASES[source["workspace_key"]],
                                  user=workspace.reader_role)
    connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
    started = perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = analytics, pg_catalog")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute("SELECT current_user, current_database(), current_setting('transaction_read_only')")
            user, database, readonly = cursor.fetchone()
            if user != workspace.reader_role or readonly != "on":
                raise RuntimeMaterializationError("readonly_reader_identity_mismatch")
            original, original_truncated, parent_policy_hash = execute_gold(cursor, source["candidate_sql"], policy)
            normalized, truncated, policy_hash = execute_gold(cursor, row["candidate_sql"], policy)
    finally:
        connection.close()
    if original_truncated or truncated:
        raise RuntimeMaterializationError("gold_execution_truncated")
    contract = row["result_contract"]
    validation = ResultValidator(max_rows=1000, display_row_budget=200).validate(
        normalized, required_columns=contract["required_result_columns"],
        metric_columns=contract["metric_result_columns"], time_column=contract["result_time_column"],
        time_column_aliases=contract["result_time_column_aliases"],
        time_bucket_grain=row["query_plan"]["time_grain"], requested_start=contract["requested_start"],
        requested_end=contract["requested_end"], requested_end_exclusive=contract["requested_end_exclusive"],
        exact_columns=contract["exact_result_columns"], metric_value_constraints=contract["metric_value_constraints"],
        execution_truncated=False,
    )
    if not validation.safe_to_answer or not validation.safe_to_display:
        raise RuntimeMaterializationError("runtime_result_contract:" + validation.state)
    aligned_original = original.loc[:, contract["required_result_columns"]]
    denotation = compare_denotations(aligned_original, normalized)
    if denotation not in {"ordered_denotation_match", "bag_denotation_match"}:
        raise RuntimeMaterializationError("projection_reorder_changed_denotation:" + denotation)
    return {
        "database": database, "reader_role": user, "transaction_read_only": True,
        "workspace_id": workspace.workspace_id, "dataset_version": workspace.dataset_version,
        "policy_state": "allowed", "query_plan_guard": "passed", "result_state": validation.state,
        "row_count": len(normalized), "preview_row_count": min(len(normalized), 200),
        "execution_truncated": False, "denotation": denotation,
        "parent_policy_sql_sha256": parent_policy_hash, "policy_sql_sha256": policy_hash,
        "parent_aligned_result_sha256": frame_sha256(aligned_original), "result_sha256": frame_sha256(normalized),
        "elapsed_ms": round((perf_counter() - started) * 1000),
    }


def write_jsonl(path, rows):
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output = ensure_path_outside_repository(args.output_dir, ROOT)
    output.mkdir(parents=True, exist_ok=False)
    source_rows, source_files = [], {}
    for split, expected in SOURCE_HASHES.items():
        path = args.source_dir / f"{split}.jsonl"
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            raise ValueError("source file hash differs from frozen alignment source")
        rows = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        if any(row["split"] != {"name": split} for row in rows):
            raise ValueError("split identity disagrees with source filename")
        source_rows.extend(rows)
        source_files[split] = {"sha256": digest, "rows": len(rows)}
    selected = select_pilot(source_rows)
    write_jsonl(output / "selection-manifest.jsonl", [{
        "sample_id": row["sample_id"], "workspace_key": row["workspace_key"], "split": row["split"],
        "shape": row["query_spec"]["result_shape"], "features": sorted(features(row)),
    } for row in selected])
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_PATH, local_files_only=True)
    retrievers = {key: CatalogRetriever(CatalogLoader(workspace).load()) for key, workspace in WORKSPACES.items()}
    accepted, excluded, evidence = [], [], []
    for source in selected:
        record = {"sample_id": source["sample_id"], "workspace_key": source["workspace_key"], "split": source["split"]}
        try:
            row = materialize_runtime_candidate_row(source, retrievers[source["workspace_key"]], WORKSPACES[source["workspace_key"]])
            execution = admit_execution(source, row, WORKSPACES[source["workspace_key"]])
            row["token_length"] = len(tokenizer.encode(row["training_text"], add_special_tokens=False)) + 1
            row["training_length_status"] = "within_3072" if row["token_length"] <= 3072 else "requires_length_decision"
            row["admission_status"] = "admitted"
            row["execution_evidence"] = execution
            accepted.append(row)
            evidence.append({**record, "state": "admitted", "provenance": row["provenance"],
                             "token_length": row["token_length"], "execution": execution})
        except (RuntimeMaterializationError, PolicyViolation, psycopg2.Error) as error:
            code = error.code if isinstance(error, RuntimeMaterializationError) else type(error).__name__
            if "drift" in code:
                raise  # Source corruption is a batch stop, not an exclusion.
            excluded.append({**record, "reason": code})
            evidence.append({**record, "state": "excluded", "reason": code})
        print(json.dumps({**record, "processed": len(evidence), "admitted": len(accepted), "excluded": len(excluded)}, ensure_ascii=False), flush=True)
    for split in SOURCE_HASHES:
        write_jsonl(output / f"{split}.jsonl", [row for row in accepted if row["split"] == {"name": split}])
    write_jsonl(output / "evidence.jsonl", evidence)
    write_jsonl(output / "exclusions.jsonl", excluded)
    if accepted:
        examples = [accepted[0]]
        edited_grain = next((row for row in accepted if "clarify_time_grain" in row["provenance"]["question_changes"]), None)
        if edited_grain:
            examples.append(edited_grain)
        lines = ["# 运行时候选输入对齐：小批审阅样本", "",
                 "这是 train/validation 小批，尚非正式训练 release。输入是 rendered_prompt；标签是 candidate_sql 加 EOS。",
                 "query_spec、provenance 和 execution_evidence 是审计信息，不进入模型输入；training_text 是输入与 SQL 标签的拼接。", ""]
        for i, row in enumerate(examples):
            lines.extend([f"## {row['sample_id']}", "", f"原问题：{row['provenance']['parent_question']}", "",
                          f"新问题：{row['question']}", "", f"修改原因：{', '.join(row['provenance']['question_changes'])}", "",
                          "问题解析得到的 QueryPlan：", "", "```json",
                          json.dumps(row["query_plan"], ensure_ascii=False, indent=2), "```", "",
                          "SQL 标签：", "", "```sql", row["candidate_sql"], "```", "",
                          f"执行 {row['execution_evidence']['row_count']} 行；原/新 Gold：{row['execution_evidence']['denotation']}；含 EOS 长度 {row['token_length']}。", ""])
            if i == 0:
                lines.extend(["完整模型输入（到 ### SQL 为止，不含下方标签）：", "", "```text", row["rendered_prompt"], "```", ""])
        (output / "review-preview.md").write_text("\n".join(lines), encoding="utf-8")
    for split, original in source_files.items():
        if hashlib.sha256((args.source_dir / f"{split}.jsonl").read_bytes()).hexdigest() != original["sha256"]:
            raise ValueError("source changed during pilot")
    summary = {
        "version": MATERIALIZER_VERSION, "scope": "32-case train/validation pilot; no final test",
        "generated_at": datetime.now(timezone.utc).isoformat(), "source_files": source_files,
        "selected": len(selected), "admitted": len(accepted), "excluded": len(excluded),
        "exclusion_reasons": dict(Counter(row["reason"] for row in excluded)),
        "groups": {f"{w}/{s}": sum(row["workspace_key"] == w and row["split"] == {"name": s} for row in accepted)
                   for w in WORKSPACES for s in SOURCE_HASHES},
        "question_changes": dict(Counter(change for row in accepted for change in row["provenance"]["question_changes"])),
        "coverage": {key: dict(Counter(str(row["query_spec"].get(key)) for row in accepted))
                     for key in ("result_shape", "dimension")},
        "tokenizer": {"path": TOKENIZER_PATH, "eos_token_id": tokenizer.eos_token_id,
                      "min_observed_with_eos": min((row["token_length"] for row in accepted), default=0),
                      "max_observed_with_eos": max((row["token_length"] for row in accepted), default=0),
                      "over_3072": sum(row["token_length"] > 3072 for row in accepted), "silent_truncation": False},
        "artifacts": {path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                      for path in sorted(output.iterdir()) if path.is_file()},
        "not_a_formal_training_release": True,
    }
    (output / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
