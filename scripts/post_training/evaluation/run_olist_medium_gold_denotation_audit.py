#!/usr/bin/env python3
"""Audit a frozen Olist candidate pair against isolated Gold SQL.

This command runs only after a completed Base/Adapter generation evaluation.
Gold SQL is read only after generation has completed and is never exposed to a
model, prompt, training input, Git report, or result artifact. Candidate SQL
is re-executed only when its original safe report proves that it already
passed the ResultContract; both candidate and Gold use the same policy,
readonly PostgreSQL role, QueryPlan, and ResultContract metadata.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import (
    FORBIDDEN_REPORT_FIELDS,
    OlistCandidateEvaluationError,
    build_safe_comparison,
)
from data_analysis_agent.post_training_comparison import sha256_file
from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.sql_policy import PolicyViolation
from data_analysis_agent.sql_repair import SafeSqlExecutionError
from scripts.post_training.evaluation.run_olist_medium_matching_evaluation import (
    EXPECTED_SPLIT,
    load_test_contract,
)
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory


ABSOLUTE_NUMERIC_TOLERANCE = Decimal("0.000001")
RELATIVE_NUMERIC_TOLERANCE = Decimal("0.000001")
MATCH_STATES = frozenset({"ordered_denotation_match", "bag_denotation_match"})


class GoldDenotationAuditError(ValueError):
    """The post-generation Gold denotation contract was violated."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-jsonl", type=Path, required=True)
    parser.add_argument("--runtime-candidates", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--base-safe-report", type=Path, required=True)
    parser.add_argument("--adapter-safe-report", type=Path, required=True)
    parser.add_argument("--base-raw-candidates", type=Path, required=True)
    parser.add_argument("--adapter-raw-candidates", type=Path, required=True)
    parser.add_argument("--completed-marker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldDenotationAuditError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise GoldDenotationAuditError(f"{label} must be an object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldDenotationAuditError(f"{label} is unavailable or invalid") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise GoldDenotationAuditError(f"{label} must contain non-empty object rows")
    return rows


def _safe_records(
    report: Mapping[str, Any], label: str, *, expected_count: int
) -> dict[str, Mapping[str, Any]]:
    raw_records = report.get("records")
    if not isinstance(raw_records, list) or len(raw_records) != expected_count:
        raise GoldDenotationAuditError(
            f"{label} safe report must contain {expected_count} records"
        )
    records: dict[str, Mapping[str, Any]] = {}
    for record in raw_records:
        if not isinstance(record, Mapping):
            raise GoldDenotationAuditError(f"{label} safe report has an invalid record")
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in records:
            raise GoldDenotationAuditError(f"{label} safe report has invalid source IDs")
        if not isinstance(record.get("result_contract_satisfied"), bool):
            raise GoldDenotationAuditError(f"{label} safe report lacks ResultContract state")
        records[source_id] = record
    return records


def _raw_candidates(path: Path, expected_ids: set[str], label: str) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for row in _read_jsonl(path, label):
        source_id = row.get("source_id")
        sql = row.get("candidate_sql")
        if not isinstance(source_id, str) or not isinstance(sql, str) or not sql.strip():
            raise GoldDenotationAuditError(f"{label} contains an invalid candidate")
        if source_id in candidates:
            raise GoldDenotationAuditError(f"{label} contains duplicate source IDs")
        candidates[source_id] = sql.strip()
    if set(candidates) != expected_ids:
        raise GoldDenotationAuditError(f"{label} source IDs differ from its safe report")
    return candidates


def load_audit_inputs(
    *,
    test_jsonl: Path,
    runtime_candidates: Path,
    split_audit: Path,
    base_safe_report: Path,
    adapter_safe_report: Path,
    base_raw_candidates: Path,
    adapter_raw_candidates: Path,
    completed_marker: Path,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]], dict[str, str], dict[str, str], dict[str, str], dict[str, Mapping[str, Any]], dict[str, Any]]:
    """Bind completed paired outputs before reading Gold SQL for post-generation use."""
    if not completed_marker.is_file():
        raise GoldDenotationAuditError("generation evaluation is not marked completed")
    runtime_rows = load_test_contract(test_jsonl, runtime_candidates, split_audit)
    runtime_by_id = {str(row["seed_id"]): row for row in runtime_rows}
    expected_count = len(runtime_by_id)
    if expected_count < 1:
        raise GoldDenotationAuditError("runtime final test is empty")
    contract_case_count = None

    base_report = _read_json(base_safe_report, "base safe report")
    adapter_report = _read_json(adapter_safe_report, "adapter safe report")
    try:
        paired = build_safe_comparison(base_report, adapter_report)
    except OlistCandidateEvaluationError as exc:
        raise GoldDenotationAuditError("Base/Adapter safe reports are not a matching pair") from exc
    contract = paired.get("comparison_contract")
    if not isinstance(contract, Mapping):
        raise GoldDenotationAuditError("paired safe reports lack a comparison contract")
    contract_case_count = contract.get("test_case_count")
    if contract_case_count != expected_count:
        raise GoldDenotationAuditError(
            "safe reports and final test disagree on case count"
        )
    if contract.get("test_jsonl_sha256") != sha256_file(test_jsonl):
        raise GoldDenotationAuditError("safe reports do not bind the selected final test")
    if contract.get("runtime_candidates_sha256") != sha256_file(runtime_candidates):
        raise GoldDenotationAuditError("safe reports do not bind the selected runtime prompts")
    if contract.get("split_audit_sha256") != sha256_file(split_audit):
        raise GoldDenotationAuditError("safe reports do not bind the selected split audit")
    if contract.get("gold_sql_read_for_generation") is not False:
        raise GoldDenotationAuditError("safe reports do not prove Gold isolation during generation")

    base_records = _safe_records(base_report, "base", expected_count=expected_count)
    adapter_records = _safe_records(
        adapter_report, "adapter", expected_count=expected_count
    )
    expected_ids = set(runtime_by_id)
    if set(base_records) != expected_ids or set(adapter_records) != expected_ids:
        raise GoldDenotationAuditError("safe report IDs differ from the final test")
    for report, raw_path, label in (
        (base_report, base_raw_candidates, "base"),
        (adapter_report, adapter_raw_candidates, "adapter"),
    ):
        raw_hash = report.get("raw_artifacts", {}).get("raw_candidates_sha256")
        if raw_hash != sha256_file(raw_path):
            raise GoldDenotationAuditError(f"{label} raw candidates do not match its safe report")

    base_sql = _raw_candidates(base_raw_candidates, expected_ids, "base raw candidates")
    adapter_sql = _raw_candidates(adapter_raw_candidates, expected_ids, "adapter raw candidates")

    # This is the sole Gold read, deliberately after completed paired generation
    # and all input-contract checks above.
    gold_sql: dict[str, str] = {}
    for row in _read_jsonl(test_jsonl, "final test Gold rows"):
        if row.get("split", {}).get("name") != EXPECTED_SPLIT:
            raise GoldDenotationAuditError("Gold test contains a non-test split row")
        source_id = row.get("seed_id")
        sql = row.get("candidate_sql")
        if not isinstance(source_id, str) or not isinstance(sql, str) or not sql.strip():
            raise GoldDenotationAuditError("Gold test row is malformed")
        if source_id in gold_sql:
            raise GoldDenotationAuditError("Gold test contains duplicate source IDs")
        gold_sql[source_id] = sql.strip()
    if set(gold_sql) != expected_ids:
        raise GoldDenotationAuditError("Gold test IDs differ from the final test contract")
    return base_records, adapter_records, base_sql, adapter_sql, gold_sql, runtime_by_id, dict(contract)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    return None


def _is_missing(value: Any) -> bool:
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def _values_equal(left: Any, right: Any) -> bool:
    if _is_missing(left) or _is_missing(right):
        return _is_missing(left) and _is_missing(right)
    left_number, right_number = _decimal(left), _decimal(right)
    if left_number is not None and right_number is not None:
        return abs(left_number - right_number) <= ABSOLUTE_NUMERIC_TOLERANCE + (
            RELATIVE_NUMERIC_TOLERANCE * max(abs(left_number), abs(right_number))
        )
    if isinstance(left, (datetime, date, time)) and isinstance(right, (datetime, date, time)):
        return left.isoformat() == right.isoformat()
    return left == right


def _value_sort_key(value: Any) -> tuple[str, str]:
    if _is_missing(value):
        return ("0", "")
    number = _decimal(value)
    if number is not None:
        return ("1", format(number.normalize(), "f"))
    if isinstance(value, (datetime, date, time)):
        return ("2", value.isoformat())
    return ("3", f"{type(value).__qualname__}:{value!r}")


def _row_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return len(left) == len(right) and all(_values_equal(a, b) for a, b in zip(left, right, strict=True))


def compare_denotations(gold: pd.DataFrame, candidate: pd.DataFrame) -> str:
    """Compare only query outputs; SQL text is intentionally not considered."""
    if tuple(map(str, gold.columns)) != tuple(map(str, candidate.columns)):
        return "column_mismatch"
    gold_rows = [tuple(row) for row in gold.itertuples(index=False, name=None)]
    candidate_rows = [tuple(row) for row in candidate.itertuples(index=False, name=None)]
    if len(gold_rows) != len(candidate_rows):
        return "row_count_mismatch"
    if all(_row_equal(a, b) for a, b in zip(gold_rows, candidate_rows, strict=True)):
        return "ordered_denotation_match"
    try:
        gold_rows.sort(key=lambda row: tuple(_value_sort_key(value) for value in row))
        candidate_rows.sort(key=lambda row: tuple(_value_sort_key(value) for value in row))
    except (InvalidOperation, TypeError, ValueError):
        return "denotation_mismatch"
    if all(_row_equal(a, b) for a, b in zip(gold_rows, candidate_rows, strict=True)):
        return "bag_denotation_match"
    return "denotation_mismatch"


async def _execute_trusted(
    runner: SecurePostgresRunner,
    *,
    sql: str,
    runtime_row: Mapping[str, Any],
    source_id: str,
    kind: str,
) -> tuple[str, pd.DataFrame | None]:
    context = ToolContext(
        user=User(id="olist-medium-gold-auditor", group_memberships=["analyst"]),
        conversation_id=f"olist-medium-gold-{kind}-{source_id}-{uuid.uuid4().hex}",
        request_id=f"olist-medium-gold-{kind}-{source_id}-{uuid.uuid4().hex}",
        agent_memory=DemoAgentMemory(),
        metadata={
            "question": "offline post-generation Gold denotation audit",
            "query_plan": dict(runtime_row["query_plan"]),
            **dict(runtime_row["result_contract"]),
        },
    )
    try:
        return "executed", await runner.run_sql(RunSqlToolArgs(sql=sql), context)
    except PolicyViolation:
        return "policy_rejected", None
    except ResultValidationError:
        return "result_contract_rejected", None
    except SafeSqlExecutionError:
        return "postgres_execution_error", None
    except Exception:
        return "unexpected_execution_error", None


def _safe_report(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = FORBIDDEN_REPORT_FIELDS.intersection(value)
        if forbidden:
            raise GoldDenotationAuditError(f"Gold audit report contains unsafe field(s): {sorted(forbidden)}")
        for nested in value.values():
            _safe_report(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _safe_report(nested)


def _summary(records: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    base_states = Counter(record["base"] for record in records)
    adapter_states = Counter(record["adapter"] for record in records)
    transitions = Counter(f"{record['base']} -> {record['adapter']}" for record in records)
    return {
        "case_count": len(records),
        "base_eligible_for_gold_denotation": sum(record["base"] != "not_result_contract_valid" for record in records),
        "adapter_eligible_for_gold_denotation": sum(record["adapter"] != "not_result_contract_valid" for record in records),
        "base_gold_denotation_matches": sum(record["base"] in MATCH_STATES for record in records),
        "adapter_gold_denotation_matches": sum(record["adapter"] in MATCH_STATES for record in records),
        "base_state_counts": dict(sorted(base_states.items())),
        "adapter_state_counts": dict(sorted(adapter_states.items())),
        "semantic_transition_counts": dict(sorted(transitions.items())),
    }


async def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    paths = (
        args.test_jsonl,
        args.runtime_candidates,
        args.split_audit,
        args.base_safe_report,
        args.adapter_safe_report,
        args.base_raw_candidates,
        args.adapter_raw_candidates,
        args.completed_marker,
    )
    for path in paths:
        ensure_path_outside_repository(path, ROOT)
    output_path = ensure_path_outside_repository(args.output, ROOT)
    if output_path.exists():
        raise GoldDenotationAuditError("Gold denotation output already exists")
    (
        base_records,
        adapter_records,
        base_sql,
        adapter_sql,
        gold_sql,
        runtime_by_id,
        comparison_contract,
    ) = load_audit_inputs(
        test_jsonl=args.test_jsonl,
        runtime_candidates=args.runtime_candidates,
        split_audit=args.split_audit,
        base_safe_report=args.base_safe_report,
        adapter_safe_report=args.adapter_safe_report,
        base_raw_candidates=args.base_raw_candidates,
        adapter_raw_candidates=args.adapter_raw_candidates,
        completed_marker=args.completed_marker,
    )
    settings = PostgresConnectionSettings.from_environment()
    runner = SecurePostgresRunner(
        settings=settings,
        result_validator=ResultValidator(settings.max_rows),
        model_name="post-training/olist-medium-gold-denotation-audit",
    )
    records: list[dict[str, str]] = []
    gold_cache: dict[str, pd.DataFrame] = {}
    for source_id in sorted(runtime_by_id):
        candidates = {"base": (base_records[source_id], base_sql[source_id]), "adapter": (adapter_records[source_id], adapter_sql[source_id])}
        eligible = {label: bool(record["result_contract_satisfied"]) for label, (record, _) in candidates.items()}
        states = dict.fromkeys(candidates, "not_result_contract_valid")
        if any(eligible.values()):
            gold_state, gold_frame = await _execute_trusted(
                runner,
                sql=gold_sql[source_id],
                runtime_row=runtime_by_id[source_id],
                source_id=source_id,
                kind="gold",
            )
            if gold_state != "executed" or gold_frame is None:
                raise GoldDenotationAuditError(f"frozen Gold SQL failed trusted execution for {source_id}")
            gold_cache[source_id] = gold_frame
            for label, (record, sql) in candidates.items():
                if not eligible[label]:
                    continue
                candidate_state, candidate_frame = await _execute_trusted(
                    runner,
                    sql=sql,
                    runtime_row=runtime_by_id[source_id],
                    source_id=source_id,
                    kind=label,
                )
                if candidate_state != "executed" or candidate_frame is None:
                    states[label] = f"candidate_reexecution_{candidate_state}"
                else:
                    states[label] = compare_denotations(gold_cache[source_id], candidate_frame)
        records.append({"source_id": source_id, "base": states["base"], "adapter": states["adapter"]})
    report = {
        "report_version": "1",
        "scope": {
            "mode": "post_generation_olist_gold_denotation_audit",
            "dataset": comparison_contract.get("dataset", "olist_in_domain_test"),
            "case_count": len(records),
            "candidate_eligibility": "original_result_contract_valid_only",
            "gold_sql_read_only_after_generation": True,
            "gold_sql_used_for_training_or_prompt": False,
            "production_default_unchanged": True,
            "raw_question_or_sql_written": False,
            "result_rows_written": False,
            "numeric_absolute_tolerance": str(ABSOLUTE_NUMERIC_TOLERANCE),
            "numeric_relative_tolerance": str(RELATIVE_NUMERIC_TOLERANCE),
            "row_order": "ordered_then_bag_comparison",
        },
        "input_evidence": {
            "comparison_contract": comparison_contract,
            "test_jsonl_sha256": sha256_file(args.test_jsonl),
            "runtime_candidates_sha256": sha256_file(args.runtime_candidates),
            "split_audit_sha256": sha256_file(args.split_audit),
            "base_safe_report_sha256": sha256_file(args.base_safe_report),
            "adapter_safe_report_sha256": sha256_file(args.adapter_safe_report),
            "base_raw_candidates_sha256": sha256_file(args.base_raw_candidates),
            "adapter_raw_candidates_sha256": sha256_file(args.adapter_raw_candidates),
        },
        "summary": _summary(records),
        "records": records,
    }
    _safe_report(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = asyncio.run(run_audit(args))
    except (GoldDenotationAuditError, OlistCandidateEvaluationError, ValueError) as exc:
        print(f"Olist Gold denotation audit error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
