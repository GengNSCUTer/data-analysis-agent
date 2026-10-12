"""Evaluate Base/Adapter outputs against the aligned runtime Gold release.

Generation is deliberately separated from this module.  The model process reads
only ``inputs.jsonl``; this post-generation process reads the protected Gold
sidecar and executes candidate/Gold SQL under the same workspace gates.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from data_analysis_agent.candidate_sql_generator import CandidateSqlGenerationError, unwrap_sql_completion
from data_analysis_agent.plan_sql_guard import sql_plan_mismatch
from data_analysis_agent.runtime_candidate_matching import execute_sql
from data_analysis_agent.sql_policy import PolicyViolation
from scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit import (
    compare_denotations as _compare_denotations,
)


class EvaluationError(ValueError):
    """A frozen aligned evaluation contract was violated."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvaluationError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise EvaluationError(f"expected non-empty JSONL objects: {path}")
    return rows


def load_inputs(path: Path) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = row.get("source_id")
        prompt = row.get("prompt")
        if not isinstance(source_id, str) or source_id in result:
            raise EvaluationError(f"duplicate or missing source_id: {source_id}")
        if not isinstance(prompt, str) or sha256_text(prompt) != row.get("prompt_sha256"):
            raise EvaluationError(f"prompt hash drift: {source_id}")
        if {"query_spec", "candidate_sql", "gold_sql", "training_text"} & row.keys():
            raise EvaluationError(f"offline label leaked into aligned input: {source_id}")
        result[source_id] = row
    return result


def load_gold(path: Path, expected_ids: set[str]) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = row.get("source_id")
        sql = row.get("sql")
        if not isinstance(source_id, str) or source_id not in expected_ids or source_id in result:
            raise EvaluationError(f"Gold source identity drift: {source_id}")
        if not isinstance(sql, str) or not sql.strip():
            raise EvaluationError(f"Gold SQL missing: {source_id}")
        if sha256_text(sql) != row.get("sql_sha256"):
            raise EvaluationError(f"Gold SQL hash drift: {source_id}")
        result[source_id] = row
    if result.keys() != expected_ids:
        raise EvaluationError("Gold/input identity sets differ")
    return result


def load_generation(directory: Path, expected_ids: set[str]) -> dict[str, dict[str, Any]]:
    evidence = read_json(directory / "generation-evidence.json")
    if evidence.get("gold_read_for_generation") is not False or evidence.get("database_rows_read") is not False:
        raise EvaluationError("generation evidence claims Gold/database access")
    rows = read_jsonl(directory / "raw-completions.jsonl")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = row.get("source_id")
        raw = row.get("raw_completion")
        if not isinstance(source_id, str) or source_id not in expected_ids or source_id in result:
            raise EvaluationError(f"generated source identity drift: {source_id}")
        if not isinstance(raw, str) or sha256_text(raw) != row.get("raw_completion_sha256"):
            raise EvaluationError(f"generated raw hash drift: {source_id}")
        if row.get("generated_tokens", 0) < 1:
            raise EvaluationError(f"empty generation in full aligned run: {source_id}")
        result[source_id] = row
    if result.keys() != expected_ids:
        raise EvaluationError("generation/input identity sets differ")
    return result


def compare_denotations(gold: Any, candidate: Any) -> str:
    """Use the repository's tolerance-aware ordered/bag denotation contract."""

    return _compare_denotations(gold, candidate)


def evaluate_one(
    row: Mapping[str, Any],
    gold_row: Mapping[str, Any],
    generated: Mapping[str, Any],
) -> dict[str, Any]:
    source_id = row["source_id"]
    plan = row["query_plan"]
    contract = row["result_contract"]
    gold_sql = gold_row["sql"]
    gold_evidence, gold_frame = execute_sql(gold_sql, row["workspace"], plan, contract)
    if gold_frame is None or gold_evidence.get("state") != "valid":
        raise EvaluationError(f"frozen Gold no longer passes runtime contract: {source_id}")

    try:
        sql = unwrap_sql_completion(generated["raw_completion"])
    except (CandidateSqlGenerationError, ValueError) as exc:
        return {
            "state": "completion_wrapper_rejected",
            "denotation": "not_eligible",
            "candidate_sql_sha256": None,
            "plan_adherent": False,
            "error_type": type(exc).__name__,
        }

    mismatch = sql_plan_mismatch(sql, plan)
    if mismatch:
        return {
            "state": "query_plan_guard_rejected",
            "denotation": "not_eligible",
            "candidate_sql_sha256": sha256_text(sql),
            "plan_adherent": False,
            "plan_mismatch_code": mismatch,
        }
    try:
        candidate_evidence, candidate_frame = execute_sql(sql, row["workspace"], plan, contract)
    except PolicyViolation:
        return {
            "state": "policy_rejected",
            "denotation": "not_eligible",
            "candidate_sql_sha256": sha256_text(sql),
            "plan_adherent": True,
        }
    result = {
        "state": candidate_evidence.get("state", "unknown"),
        "denotation": "not_eligible",
        "candidate_sql_sha256": sha256_text(sql),
        "plan_adherent": True,
        "candidate_execution": candidate_evidence,
    }
    if candidate_frame is not None:
        result["denotation"] = compare_denotations(gold_frame, candidate_frame)
    return result


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for workspace in ("olist", "thelook", "all"):
        group = [record for record in records if workspace == "all" or record["workspace"] == workspace]
        workspace_summary: dict[str, Any] = {"total": len(group)}
        for role in ("base", "adapter"):
            role_rows = [record[role] for record in group]
            workspace_summary[role] = {
                "states": dict(sorted(Counter(record["state"] for record in role_rows).items())),
                "denotations": dict(sorted(Counter(record["denotation"] for record in role_rows).items())),
                "plan_adherent": sum(record["plan_adherent"] is True for record in role_rows),
                "ordered_or_bag_matches": sum(
                    record["denotation"] in {"ordered_denotation_match", "bag_denotation_match"}
                    for record in role_rows
                ),
                "ordered_matches": sum(
                    record["denotation"] == "ordered_denotation_match" for record in role_rows
                ),
            }
        summary[workspace] = workspace_summary
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise EvaluationError(f"refusing to overwrite {args.output}")
    inputs = load_inputs(args.inputs)
    ids = set(inputs)
    gold = load_gold(args.gold, ids)
    base = load_generation(args.base_dir, ids)
    adapter = load_generation(args.adapter_dir, ids)
    args.output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    for index, source_id in enumerate(sorted(ids), start=1):
        row = inputs[source_id]
        base_result = evaluate_one(row, gold[source_id], base[source_id])
        adapter_result = evaluate_one(row, gold[source_id], adapter[source_id])
        records.append(
            {
                "source_id": source_id,
                "workspace": row["workspace"],
                "scenario_family_id": row.get("scenario_family_id"),
                "risk_tags": row.get("risk_tags", []),
                "base": base_result,
                "adapter": adapter_result,
            }
        )
        if index % 25 == 0:
            print(f"evaluated {index}/{len(ids)}", flush=True)
    summary = {
        "version": "runtime-queryplan-aligned-matching-v1",
        "inputs": {"path": str(args.inputs), "sha256": sha256_file(args.inputs), "rows": len(inputs)},
        "gold": {"path": str(args.gold), "sha256": sha256_file(args.gold), "rows": len(gold)},
        "base_generation": {"path": str(args.base_dir), "raw_sha256": sha256_file(args.base_dir / "raw-completions.jsonl")},
        "adapter_generation": {"path": str(args.adapter_dir), "raw_sha256": sha256_file(args.adapter_dir / "raw-completions.jsonl")},
        "summary": summarize(records),
    }
    with (args.output / "records.jsonl").open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvaluationError as exc:
        print(f"evaluation error: {exc}")
        raise SystemExit(2) from exc
