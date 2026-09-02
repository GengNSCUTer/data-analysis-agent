#!/usr/bin/env python3
"""Compare paired Spider/CSpider candidates to gold denotations after generation.

This is an external, bounded diagnostic. It is not the official Spider
evaluator, does not produce an official metric, and never writes questions,
gold SQL, candidate SQL, database identifiers, or result rows to its report.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

from data_analysis_agent.external_artifacts import ensure_path_outside_repository


ROOT = Path(__file__).resolve().parents[3]
MATCH_STATES = {"exact_ordered_match", "bag_match_order_differs"}


class DenotationAuditError(ValueError):
    """Raised when paired diagnostics cannot be safely audited."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DenotationAuditError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DenotationAuditError(f"{label} is not valid JSON: {path}") from exc


def load_list(path: Path, label: str) -> list[Mapping[str, Any]]:
    value = load_json(path, label)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise DenotationAuditError(f"{label} must be a JSON list of objects")
    return value


def execution_state(connection: sqlite3.Connection, gold_sql: str, execution: Mapping[str, Any]) -> str:
    if execution.get("status") != "executed":
        return "not_executable"
    candidate_sql = execution.get("final_sql")
    if not isinstance(candidate_sql, str) or not candidate_sql.strip():
        raise DenotationAuditError("executed candidate has no final SQL")
    gold_rows = connection.execute(gold_sql).fetchall()
    candidate_rows = connection.execute(candidate_sql).fetchall()
    if candidate_rows == gold_rows:
        return "exact_ordered_match"
    if Counter(candidate_rows) == Counter(gold_rows):
        return "bag_match_order_differs"
    return "mismatch"


def paired_records(
    *,
    base_report: Mapping[str, Any],
    adapter_report: Mapping[str, Any],
    audit_cases: list[Mapping[str, Any]],
    database_root: Path,
    case_id_prefix: str = "spider_dev",
) -> list[dict[str, str]]:
    base_records = base_report.get("records")
    adapter_records = adapter_report.get("records")
    if not isinstance(base_records, list) or not isinstance(adapter_records, list):
        raise DenotationAuditError("diagnostic reports must contain records")
    if len(base_records) != len(adapter_records) or len(base_records) != len(audit_cases):
        raise DenotationAuditError("base, adapter and audit case counts must match")
    database_root = database_root.resolve(strict=True)
    records: list[dict[str, str]] = []
    for index, (base_record, adapter_record, audit_case_row) in enumerate(
        zip(base_records, adapter_records, audit_cases, strict=True)
    ):
        if not isinstance(base_record, Mapping) or not isinstance(adapter_record, Mapping):
            raise DenotationAuditError("diagnostic record must be an object")
        expected_case_id = f"{case_id_prefix}:{index:05d}"
        if base_record.get("case_id") != expected_case_id or adapter_record.get("case_id") != expected_case_id:
            raise DenotationAuditError("paired diagnostic case IDs do not match audit order")
        database_id = audit_case_row.get("db_id")
        gold_sql = audit_case_row.get("query")
        database_path = base_record.get("database_path")
        if not isinstance(database_id, str) or not isinstance(gold_sql, str):
            raise DenotationAuditError("audit case lacks database ID or gold SQL")
        if database_path != f"{database_id}/{database_id}.sqlite":
            raise DenotationAuditError("diagnostic database path does not match audit case")
        path = (database_root / database_path).resolve(strict=True)
        if not path.is_relative_to(database_root) or not path.is_file():
            raise DenotationAuditError("audit database path escapes the configured root")
        base_execution = base_record.get("execution")
        adapter_execution = adapter_record.get("execution")
        if not isinstance(base_execution, Mapping) or not isinstance(adapter_execution, Mapping):
            raise DenotationAuditError("diagnostic record lacks execution evidence")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            # Spider SQLite files can contain TEXT values that are not valid UTF-8.
            # Compare their original bytes so result decoding cannot abort an
            # otherwise read-only denotation audit or alter equality semantics.
            connection.text_factory = bytes
            base_state = execution_state(connection, gold_sql, base_execution)
            adapter_state = execution_state(connection, gold_sql, adapter_execution)
        records.append(
            {
                "case_id": expected_case_id,
                "base": base_state,
                "adapter": adapter_state,
            }
        )
    return records


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-report", type=Path, required=True)
    parser.add_argument("--adapter-report", type=Path, required=True)
    parser.add_argument("--audit-cases", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--dataset-id",
        choices=("spider_dev", "cspider_validation"),
        default="spider_dev",
        help="Case-ID namespace; CSpider uses its official dev/validation split.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base_path = ensure_path_outside_repository(args.base_report, ROOT)
        adapter_path = ensure_path_outside_repository(args.adapter_report, ROOT)
        audit_path = ensure_path_outside_repository(args.audit_cases, ROOT)
        database_root = ensure_path_outside_repository(args.database_root, ROOT)
        output_path = ensure_path_outside_repository(args.output, ROOT)
        base_report = load_json(base_path, "base report")
        adapter_report = load_json(adapter_path, "adapter report")
        if not isinstance(base_report, Mapping) or not isinstance(adapter_report, Mapping):
            raise DenotationAuditError("diagnostic reports must be JSON objects")
        audit_cases = load_list(audit_path, "Spider audit cases")
        records = paired_records(
            base_report=base_report,
            adapter_report=adapter_report,
            audit_cases=audit_cases,
            database_root=database_root,
            case_id_prefix=args.dataset_id,
        )
    except (DenotationAuditError, ValueError, sqlite3.Error) as exc:
        print(f"Spider denotation audit error: {exc}", file=sys.stderr)
        return 2
    report = {
        "report_version": "1",
        "scope": {
            "mode": "post_generation_bounded_denotation_audit",
            "dataset": args.dataset_id,
            "case_limit": len(records),
            "gold_sql_read_only_after_generation": True,
            "gold_sql_used_for_training_or_prompt": False,
            "official_test_suite_run": False,
            "official_leaderboard_claim": False,
            "raw_question_or_sql_written": False,
            "database_identifiers_written": False,
            "result_rows_written": False,
        },
        "input_evidence": {
            "base_sqlite_diagnostic_sha256": sha256_file(base_path),
            "adapter_sqlite_diagnostic_sha256": sha256_file(adapter_path),
            "audit_cases_sha256": sha256_file(audit_path),
        },
        "summary": {
            "total_cases": len(records),
            "base_exact_or_bag_matches": sum(record["base"] in MATCH_STATES for record in records),
            "adapter_exact_or_bag_matches": sum(record["adapter"] in MATCH_STATES for record in records),
            "base_state_counts": dict(sorted(Counter(record["base"] for record in records).items())),
            "adapter_state_counts": dict(sorted(Counter(record["adapter"] for record in records).items())),
            "semantic_transition_counts": dict(
                sorted(Counter(f"{record['base']} -> {record['adapter']}" for record in records).items())
            ),
        },
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
