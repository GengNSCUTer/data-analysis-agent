#!/usr/bin/env python3
"""Materialize and execute pinned train/validation runtime-alignment data.

Outputs are always outside Git. No model, final test, or original release is
modified. Release mode requires a completed pilot and freezes a length contract
from the full admitted population before publishing the new directory.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
import uuid

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
    MATERIALIZER_VERSION, RuntimeMaterializationError, materialize_runtime_candidate_row,
    normalized_question_sha256, split_family_id, text_sha256,
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
SOURCE_MANIFEST_HASH = "4afb0eeb9a5962eaa8dea9af4a30894ac5841f3ea270c98313c256f52f2c52d3"
RELEASE_VERSION = "runtime-candidate-sql-v2-r1"
LENGTH_CEILINGS = (3072, 4096, 5120, 6144, 8192)


def runtime_asset_hashes():
    paths = [Path(__file__).resolve(), *[ROOT / "src/data_analysis_agent" / name for name in (
        "runtime_candidate_materializer.py", "runtime_alignment_audit.py", "candidate_sql_generator.py",
        "semantic_catalog.py", "query_plan.py", "query_time.py", "question_router.py", "working_memory.py",
        "workspace.py", "metric_context.py", "thelook_v2_context.py", "plan_sql_guard.py", "result_validator.py",
        "postgres_runner.py", "sql_policy.py",
    )], *[workspace.catalog_path for workspace in WORKSPACES.values()],
        ROOT / "data/catalog/thelook_runtime_shape_bounds_v1.json"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


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


def audit_identities(rows):
    """Reject exact duplicates/leakage; retain shared coverage categories."""
    ids = [row["sample_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample_id")
    by_split = {split: [row for row in rows if row["split"] == {"name": split}] for split in SOURCE_HASHES}
    if sum(map(len, by_split.values())) != len(rows):
        raise ValueError("only train/validation rows are allowed")
    report = {}
    extractors = {
        "query_spec_id": lambda row: row["query_spec_id"],
        "gold_sql_sha256": lambda row: text_sha256(row["candidate_sql"]),
        "normalized_question": lambda row: normalized_question_sha256(row["question"]),
        "prompt_sha256": lambda row: text_sha256(row["rendered_prompt"]),
        "split_family_id": split_family_id,
    }
    for key, extract in extractors.items():
        values = {split: [extract(row) for row in group] for split, group in by_split.items()}
        overlap = set(values["train"]) & set(values["validation"])
        duplicates = {split: len(items) - len(set(items)) for split, items in values.items()}
        if overlap or key != "split_family_id" and any(duplicates.values()):
            raise ValueError(f"duplicate/cross-split identity: {key}")
        report[key] = {"cross_split_overlap": len(overlap), "within_split_repeated": duplicates}
    # Scenario categories intentionally overlap in TheLook. They are not its
    # program-level split identity and must not be advertised as disjoint.
    categories = {split: {f"{r['workspace_key']}:{r['scenario_family_id']}" for r in group}
                  for split, group in by_split.items()}
    report["shared_scenario_category_count"] = len(categories["train"] & categories["validation"])
    return report


def freeze_max_seq_length(lengths):
    if not lengths:
        raise ValueError("cannot freeze an empty release")
    longest = max(lengths)
    for ceiling in LENGTH_CEILINGS:
        if longest <= ceiling:
            return ceiling
    raise ValueError(f"observed sequence {longest} exceeds supported length ceilings; release remains draft")


def length_stats(lengths):
    ordered = sorted(lengths)
    if not ordered:
        return {"count": 0}
    return {"count": len(ordered), "min": ordered[0], "max": ordered[-1],
            "mean": round(sum(ordered) / len(ordered), 2),
            **{f"p{percent}": ordered[((len(ordered) - 1) * percent + 99) // 100]
               for percent in (50, 90, 95, 99)},
            "over_ceiling": {str(ceiling): sum(n > ceiling for n in ordered) for ceiling in LENGTH_CEILINGS}}


def coverage_stats(rows):
    def counts(values):
        return dict(sorted(Counter(str(value) for value in values).items()))
    return {
        "rows": len(rows), "query_spec_count": len({r["query_spec_id"] for r in rows}),
        "scenario_family_count": len({r["scenario_family_id"] for r in rows}),
        "split_family_count": len({split_family_id(r) for r in rows}),
        "program_count": len({r["program_id"] for r in rows}),
        "by_result_shape": counts(r["query_spec"]["result_shape"] for r in rows),
        "by_dimension": counts(r["query_spec"].get("dimension") for r in rows),
        "by_time_grain": counts(r["query_spec"]["time"].get("grain") for r in rows),
        "by_metric": counts(metric for r in rows for metric in r["query_spec"]["metric_ids"]),
        "by_metric_count": counts(len(r["query_spec"]["metric_ids"]) for r in rows),
        "by_risk_tag": counts(tag for r in rows for tag in r.get("risk_tags", ())),
        "missing_risk_tags": sum(not r.get("risk_tags") for r in rows),
        "by_primary_variant": counts(r["primary_variant_id"] for r in rows),
    }


def verify_completed_pilot(root):
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("selected"), manifest.get("admitted"), manifest.get("excluded")) != (32, 32, 0):
        raise ValueError("full release requires the completed 32-case pilot")
    for split, expected in SOURCE_HASHES.items():
        if manifest["source_files"][split]["sha256"] != expected:
            raise ValueError("pilot/source hash drift")
    for name, info in manifest["artifacts"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("pilot artifact hash drift")
    records = [json.loads(line) for line in (root / "evidence.jsonl").read_text().splitlines()]
    if len(records) != 32 or len({row["sample_id"] for row in records}) != 32:
        raise ValueError("pilot execution evidence incomplete")
    for row in records:
        execution = row["execution"]
        if (row["state"] != "admitted" or not execution["transaction_read_only"]
                or execution["execution_truncated"] or execution["result_state"] != "valid"
                or execution["denotation"] not in {"ordered_denotation_match", "bag_denotation_match"}):
            raise ValueError("pilot execution gate did not pass")
    return {"path": str(root.resolve()), "sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()}


def build_one(source, retrievers, tokenizer):
    record = {"sample_id": source["sample_id"], "workspace_key": source["workspace_key"], "split": source["split"]}
    try:
        row = materialize_runtime_candidate_row(source, retrievers[source["workspace_key"]], WORKSPACES[source["workspace_key"]])
        execution = admit_execution(source, row, WORKSPACES[source["workspace_key"]])
        row["token_length"] = len(tokenizer.encode(row["training_text"], add_special_tokens=False)) + 1
        row["admission_status"] = "admitted"
        row["execution_evidence"] = execution
        return row, {**record, "state": "admitted", "provenance": row["provenance"],
                     "token_length": row["token_length"], "execution": execution}
    except psycopg2.errors.QueryCanceled:
        return None, {**record, "state": "excluded", "reason": "statement_timeout"}
    except psycopg2.OperationalError:
        raise  # A lost database is a batch failure, not a dataset exclusion.
    except (RuntimeMaterializationError, PolicyViolation, psycopg2.Error) as error:
        code = error.code if isinstance(error, RuntimeMaterializationError) else type(error).__name__
        if "drift" in code or code == "readonly_reader_identity_mismatch":
            raise
        return None, {**record, "state": "excluded", "reason": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("pilot", "release"), default="pilot")
    parser.add_argument("--pilot-dir", type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args()
    output = ensure_path_outside_repository(args.output_dir, ROOT)
    if output.exists():
        raise ValueError("output already exists; frozen releases cannot be overwritten")
    pilot = None
    if args.mode == "release":
        if args.pilot_dir is None:
            parser.error("release requires --pilot-dir")
        pilot = verify_completed_pilot(args.pilot_dir)
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
    source_audit = audit_identities(source_rows)
    manifest_path = args.source_dir / "manifest.json"
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != SOURCE_MANIFEST_HASH:
        raise ValueError("source manifest hash drift")
    selected = select_pilot(source_rows) if args.mode == "pilot" else source_rows
    pinned_assets = runtime_asset_hashes()
    destination = output
    output = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    output.mkdir(parents=True, exist_ok=False)
    print(json.dumps({"event": "started", "mode": args.mode, "selected": len(selected),
                      "workers": args.workers, "staging": str(output)}, ensure_ascii=False), flush=True)
    write_jsonl(output / "selection-manifest.jsonl", [{
        "sample_id": row["sample_id"], "workspace_key": row["workspace_key"], "split": row["split"],
        "shape": row["query_spec"]["result_shape"], "features": sorted(features(row)),
    } for row in selected])
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_PATH, local_files_only=True)
    retrievers = {key: CatalogRetriever(CatalogLoader(workspace).load()) for key, workspace in WORKSPACES.items()}
    accepted, excluded, evidence = [], [], []
    with (output / "collection-journal.jsonl").open("x", encoding="utf-8") as journal:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for row, record in executor.map(lambda source: build_one(source, retrievers, tokenizer), selected):
                evidence.append(record)
                if row is None:
                    excluded.append({key: value for key, value in record.items() if key != "state"})
                else:
                    accepted.append(row)
                journal.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                if args.mode == "pilot" or len(evidence) % 100 == 0 or len(evidence) == len(selected):
                    journal.flush()
                    print(json.dumps({"processed": len(evidence), "admitted": len(accepted), "excluded": len(excluded),
                                      "exclusion_reasons": dict(Counter(r["reason"] for r in excluded))}, ensure_ascii=False), flush=True)
    output_audit = audit_identities(accepted)
    max_seq_length = freeze_max_seq_length([row["token_length"] for row in accepted]) if args.mode == "release" else None
    for row in accepted:
        row["training_length_status"] = ("within_frozen_contract" if max_seq_length else
                                         "within_3072" if row["token_length"] <= 3072 else "requires_length_decision")
    for split in SOURCE_HASHES:
        write_jsonl(output / f"{split}.jsonl", [row for row in accepted if row["split"] == {"name": split}])
    write_jsonl(output / "evidence.jsonl", evidence)
    write_jsonl(output / "exclusions.jsonl", excluded)
    if accepted:
        examples = [accepted[0]]
        edited_grain = next((row for row in accepted if "clarify_time_grain" in row["provenance"]["question_changes"]), None)
        if edited_grain:
            examples.append(edited_grain)
        lines = ["# 运行时候选输入对齐：审阅样本", "",
                 f"模式：{args.mode}。输入是 rendered_prompt；标签是 candidate_sql 加 EOS。",
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
            raise ValueError("source changed during materialization")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != SOURCE_MANIFEST_HASH:
        raise ValueError("source manifest changed during materialization")
    if runtime_asset_hashes() != pinned_assets:
        raise ValueError("runtime assets changed during materialization")
    coverage = {}
    for workspace in WORKSPACES:
        for split in SOURCE_HASHES:
            key = f"{workspace}/{split}"
            source_group = [r for r in selected if r["workspace_key"] == workspace and r["split"] == {"name": split}]
            admitted_group = [r for r in accepted if r["workspace_key"] == workspace and r["split"] == {"name": split}]
            coverage[key] = {"source": coverage_stats(source_group), "admitted": coverage_stats(admitted_group),
                             "sequence_tokens": length_stats([r["token_length"] for r in admitted_group])}
    (output / "coverage-audit.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "version": MATERIALIZER_VERSION, "release_version": RELEASE_VERSION if args.mode == "release" else None,
        "prompt_format_version": accepted[0]["prompt_format_version"] if accepted else None,
        "scope": args.mode + "; train/validation only; no final test", "pilot_evidence": pilot,
        "generated_at": datetime.now(timezone.utc).isoformat(), "source_files": source_files,
        "selected": len(selected), "admitted": len(accepted), "excluded": len(excluded),
        "exclusion_reasons": dict(Counter(row["reason"] for row in excluded)),
        "groups": {f"{w}/{s}": sum(row["workspace_key"] == w and row["split"] == {"name": s} for row in accepted)
                   for w in WORKSPACES for s in SOURCE_HASHES},
        "question_changes": dict(Counter(change for row in accepted for change in row["provenance"]["question_changes"])),
        "coverage": {key: dict(Counter(str(row["query_spec"].get(key)) for row in accepted))
                     for key in ("result_shape", "dimension")},
        "tokenizer": {"path": TOKENIZER_PATH, "eos_token_id": tokenizer.eos_token_id,
                      "max_seq_length": max_seq_length, "length_selection": "smallest supported ceiling retaining all admitted sequences",
                      "min_observed_with_eos": min((row["token_length"] for row in accepted), default=0),
                      "max_observed_with_eos": max((row["token_length"] for row in accepted), default=0),
                      "over_3072": sum(row["token_length"] > 3072 for row in accepted), "silent_truncation": False},
        "source_manifest_sha256": SOURCE_MANIFEST_HASH,
        "runtime_asset_sha256": pinned_assets,
        "source_identity_audit": source_audit, "output_identity_audit": output_audit,
        "outputs": {split: {"rows": sum(r["split"] == {"name": split} for r in accepted),
                             "sha256": hashlib.sha256((output / f"{split}.jsonl").read_bytes()).hexdigest()}
                    for split in SOURCE_HASHES},
        "split_audit": {split: {"rows": sum(r["split"] == {"name": split} for r in accepted),
                                 "by_workspace": dict(Counter(r["workspace_key"] for r in accepted if r["split"] == {"name": split})),
                                 "sequence_tokens": length_stats([r["token_length"] for r in accepted if r["split"] == {"name": split}])}
                        for split in SOURCE_HASHES},
        "execution_contract": {"analyst_execution_row_cap": 1000, "display_preview_row_budget": 200,
                               "query_timeout_ms": 5000, "workers": args.workers},
        "execution_summary": {"result_states": dict(Counter(r["execution_evidence"]["result_state"] for r in accepted)),
                              "denotation": dict(Counter(r["execution_evidence"]["denotation"] for r in accepted))},
        "checks": {"status": "pass", "source_and_split_preserved": True,
                   "runtime_derived_inputs": True, "readonly_gold_comparison": True,
                   "train_validation_identity_audit": "pass", "final_test_read": False, "silent_truncation": False},
        "artifacts": {path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                      for path in sorted(output.iterdir()) if path.is_file()},
        "not_a_formal_training_release": args.mode != "release",
    }
    (output / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.rename(destination)
    print(json.dumps({"event": "release_published", "output_dir": str(destination),
                      "admitted": summary["admitted"], "excluded": summary["excluded"],
                      "groups": summary["groups"], "tokenizer": summary["tokenizer"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
