#!/usr/bin/env python3
"""Evaluate frozen TheLook Base/Adapter generation outputs.

This command is intentionally separate from model generation.  It is the only
stage that reads ``gold_sql`` and runs candidate and Gold SQL through the
TheLook AST policy, readonly role and ResultValidator.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.candidate_sql_generator import unwrap_sql_completion
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.thelook_context import THELOOK_WORKSPACE
from data_analysis_agent.thelook_queryspec import TheLookQuerySpec


class TheLookOutputError(ValueError):
    """Generated output does not satisfy the frozen evaluation contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _load_cases(path: Path, manifest: Path) -> dict[str, dict[str, Any]]:
    frozen = json.loads(manifest.read_text(encoding="utf-8"))
    if frozen.get("evaluation_version") != "thelook-cross-schema-final-test-v1":
        raise TheLookOutputError("unexpected evaluation manifest")
    expected = frozen.get("output", {}).get("cases_jsonl", {})
    if expected.get("sha256") != sha256_file(path) or expected.get("rows") != 206:
        raise TheLookOutputError("cases do not match frozen manifest")
    cases: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise TheLookOutputError("invalid case row")
        if item["case_id"] in cases:
            raise TheLookOutputError("duplicate case ID")
        # Gold is deliberately loaded only in this post-generation command.
        for key in ("gold_sql", "query_spec", "required_result_columns"):
            if key not in item:
                raise TheLookOutputError(f"case lacks {key}")
        cases[item["case_id"]] = item
    if len(cases) != 206:
        raise TheLookOutputError("expected 206 cases")
    return cases


def _load_raw(path: Path, expected_ids: set[str]) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        case_id, sql = item.get("case_id"), item.get("candidate_sql")
        if not isinstance(case_id, str) or not isinstance(sql, str) or case_id in rows:
            raise TheLookOutputError("invalid or duplicate raw candidate row")
        rows[case_id] = sql
    if set(rows) != expected_ids:
        raise TheLookOutputError("candidate output does not cover exactly all cases")
    return rows


def _frame(sql: str, policy: SqlPolicy) -> tuple[str, pd.DataFrame | None, str | None]:
    try:
        decision = policy.evaluate(sql, role="analyst")
    except PolicyViolation as exc:
        return "rejected", None, "policy_rejected"
    connection = psycopg2.connect(host="/tmp", port=35434, database="thelook_analytics", user="postgres")
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SET LOCAL ROLE daa_thelook_reader")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(decision.final_sql)
            return "accepted", pd.DataFrame([dict(row) for row in cursor.fetchmany(200)]), None
    except Exception:
        connection.rollback()
        return "accepted", None, "postgres_execution_error"
    finally:
        connection.close()


def _validate(frame: pd.DataFrame, case: Mapping[str, Any]) -> bool:
    spec = TheLookQuerySpec.from_mapping(case["query_spec"])
    catalog = __import__("data_analysis_agent.semantic_catalog", fromlist=["CatalogLoader"]).CatalogLoader(THELOOK_WORKSPACE).load()
    constraints = {metric: dict(catalog.metrics_by_id[metric].result_value_constraints) for metric in spec.metric_ids}
    result = ResultValidator(max_rows=200).validate(
        frame,
        required_columns=spec.required_result_columns,
        metric_columns=spec.metric_ids,
        time_column="time" if spec.result_shape == "time_series" else None,
        time_bucket_grain=spec.time.grain,
        requested_start=spec.time.start,
        requested_end=spec.time.end_exclusive,
        exact_columns=True,
        metric_value_constraints=constraints,
    )
    return bool(result.safe_to_answer)


def _cell(value: Any) -> str:
    if pd.isna(value):
        return "<NULL>"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _denotation(candidate: pd.DataFrame, gold: pd.DataFrame) -> tuple[bool, bool]:
    if list(candidate.columns) != list(gold.columns):
        return False, False
    left = [[_cell(value) for value in row] for row in candidate.itertuples(index=False, name=None)]
    right = [[_cell(value) for value in row] for row in gold.itertuples(index=False, name=None)]
    return left == right, Counter(map(tuple, left)) == Counter(map(tuple, right))


def _one(case: Mapping[str, Any], candidate_sql: str, policy: SqlPolicy) -> dict[str, Any]:
    try:
        candidate_sql = unwrap_sql_completion(candidate_sql)
    except Exception:
        return {
            "case_id": case["case_id"], "candidate_policy": "not_run",
            "candidate_executed": False, "candidate_result_contract_valid": False,
            "candidate_failure": "candidate_normalization_error", "gold_policy": "not_run",
            "gold_executed": False, "ordered_denotation_match": False,
            "bag_denotation_match": False, "gold_failure": None,
        }
    candidate_policy, candidate, candidate_failure = _frame(candidate_sql, policy)
    candidate_valid = False
    if candidate is not None:
        try:
            candidate_valid = _validate(candidate, case)
        except Exception:
            candidate_failure = "result_contract_rejected"
    gold_policy, gold, gold_failure = _frame(str(case["gold_sql"]), policy)
    if gold is None:
        raise TheLookOutputError(f"Gold SQL failed for {case['case_id']}: {gold_failure}")
    ordered = bag = False
    if candidate is not None and gold is not None:
        ordered, bag = _denotation(candidate, gold)
    return {
        "case_id": case["case_id"],
        "candidate_policy": candidate_policy,
        "candidate_executed": candidate is not None,
        "candidate_result_contract_valid": candidate_valid,
        "candidate_failure": candidate_failure,
        "gold_policy": gold_policy,
        "gold_executed": gold is not None,
        "ordered_denotation_match": ordered,
        "bag_denotation_match": bag,
        "gold_failure": gold_failure,
    }


def main() -> int:
    args = parse_args()
    cases = _load_cases(args.cases_jsonl, args.manifest)
    base_raw = _load_raw(args.base_dir / "raw-candidates.jsonl", set(cases))
    adapter_raw = _load_raw(args.adapter_dir / "raw-candidates.jsonl", set(cases))
    base_report = json.loads((args.base_dir / "safe-report.json").read_text(encoding="utf-8"))
    adapter_report = json.loads((args.adapter_dir / "safe-report.json").read_text(encoding="utf-8"))
    if base_report.get("comparison_contract") != adapter_report.get("comparison_contract"):
        raise TheLookOutputError("Base and Adapter contracts differ")
    policy = SqlPolicy(workspace=THELOOK_WORKSPACE)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookOutputError("output directory must be new")
    output_dir.mkdir(parents=True)
    base_records = [_one(cases[case_id], base_raw[case_id], policy) for case_id in sorted(cases)]
    adapter_records = [_one(cases[case_id], adapter_raw[case_id], policy) for case_id in sorted(cases)]
    for label, raw in (("base", base_raw), ("adapter", adapter_raw)):
        with (output_dir / f"{label}-normalized-candidates.jsonl").open("x", encoding="utf-8") as handle:
            for case_id in sorted(raw):
                try:
                    normalized = unwrap_sql_completion(raw[case_id])
                except Exception:
                    normalized = ""
                handle.write(json.dumps({"case_id": case_id, "candidate_sql": normalized}, ensure_ascii=False) + "\n")
    base_by = {r["case_id"]: r for r in base_records}
    adapter_by = {r["case_id"]: r for r in adapter_records}
    transitions = Counter(
        f"{base_by[case_id]['ordered_denotation_match']} -> {adapter_by[case_id]['ordered_denotation_match']}"
        for case_id in sorted(cases)
    )
    report = {
        "report_schema_version": "thelook-matching-evaluation-v1",
        "dataset": "thelook-cross-schema-final-test-v1",
        "cases_sha256": sha256_file(args.cases_jsonl),
        "base_raw_sha256": sha256_file(args.base_dir / "raw-candidates.jsonl"),
        "adapter_raw_sha256": sha256_file(args.adapter_dir / "raw-candidates.jsonl"),
        "case_count": len(cases),
        "base_summary": {"candidate_executed": sum(r["candidate_executed"] for r in base_records), "result_contract_valid": sum(r["candidate_result_contract_valid"] for r in base_records), "ordered_denotation_match": sum(r["ordered_denotation_match"] for r in base_records), "bag_denotation_match": sum(r["bag_denotation_match"] for r in base_records)},
        "adapter_summary": {"candidate_executed": sum(r["candidate_executed"] for r in adapter_records), "result_contract_valid": sum(r["candidate_result_contract_valid"] for r in adapter_records), "ordered_denotation_match": sum(r["ordered_denotation_match"] for r in adapter_records), "bag_denotation_match": sum(r["bag_denotation_match"] for r in adapter_records)},
        "ordered_match_transitions": dict(sorted(transitions.items())),
        "records": {"base": base_records, "adapter": adapter_records},
    }
    (output_dir / "evaluation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("case_count", "base_summary", "adapter_summary", "ordered_match_transitions")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookOutputError, ValueError) as exc:
        print(f"thelook output evaluation error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
