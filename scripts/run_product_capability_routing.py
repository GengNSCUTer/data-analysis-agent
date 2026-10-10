#!/usr/bin/env python3
"""Run Phase A's deterministic, database-free routing baseline.

The runner intentionally stops at ``QuestionRouter``.  It neither generates
SQL nor opens a PostgreSQL connection, calls an online provider, or launches a
browser.  The JSON report is deliberately safe to retain: it includes only
case identifiers, pass/fail status, opaque evidence codes, hashes and timing.
Questions, route payloads, SQL and result rows never leave process memory.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

import yaml

from data_analysis_agent.product_capability_contract import (
    SUITE_ID,
    validate_product_capability_suite,
    validate_safe_evidence,
)
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from vanna.core.user import User


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = ROOT / "evals/cases/product_capability_v1.yaml"
DEFAULT_OUTPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "product-capability-baseline-v1/phase-a/routing-safe-report.json"
)
_EXPECTED_FIELDS = (
    "terminal_state",
    "expected_intent",
    "requires_database",
    "expected_evidence_mode",
    "expected_metric_ids",
    "expected_reason_code",
)


def _evaluation_user() -> User:
    return User(id="product-capability-phase-a", group_memberships=["analyst"])


def _route_projection(route: object) -> dict[str, object]:
    """Keep only structural fields that define the Phase-A contract."""
    return {
        "terminal_state": getattr(route, "state"),
        "expected_intent": getattr(route, "intent"),
        "requires_database": getattr(route, "requires_database"),
        "expected_evidence_mode": getattr(route, "evidence_mode"),
        "expected_metric_ids": list(getattr(route, "metric_ids")),
        "expected_reason_code": getattr(route, "reason_code"),
    }


def _artifact_sha256(case_id: str, expected: Mapping[str, Any], actual: Mapping[str, object]) -> str:
    """Hash structural evidence without storing question or router response text."""
    canonical = json.dumps(
        {"case_id": case_id, "expected": expected, "actual": actual},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_routing_case(
    case: Mapping[str, Any], router: QuestionRouter, user: User
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Evaluate one case and return a safe record plus in-memory diagnostics.

    The second return value exists for unit tests and CLI exit decisions only;
    it is never written to the safe report because it contains actual route
    values which are useful for debugging but unnecessary for durable evidence.
    """
    started = perf_counter()
    question = case["question"]
    expected = case["expected"]
    selection = router.retriever.retrieve(question, user)
    route = router.classify(
        question,
        user=user,
        selection=selection,
        conversation_state={},
    )
    if route.should_generate_sql:
        route = router.admit_sql_request(
            question,
            user=user,
            selection=selection,
            conversation_state={},
        )
    actual = _route_projection(route)
    mismatches = {
        field: {"expected": expected.get(field), "actual": actual[field]}
        for field in _EXPECTED_FIELDS
        if expected.get(field) != actual[field]
    }
    record: dict[str, object] = {
        "case_id": case["case_id"],
        "status": "pass" if not mismatches else "fail",
        "failure_layer": None if not mismatches else "router",
        "evidence_codes": ["route_matches"]
        if not mismatches
        else [f"route_mismatch:{field}" for field in mismatches],
        "artifact_sha256": _artifact_sha256(str(case["case_id"]), expected, actual),
        "latency_ms": round((perf_counter() - started) * 1000),
    }
    return record, mismatches


def evaluate_suite(case_file: Path = DEFAULT_CASE_FILE) -> dict[str, object]:
    raw = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    validate_product_capability_suite(raw, require_complete=False)
    cases = raw["routing_cases"]
    if len(cases) != 12:
        raise ValueError("Phase A requires exactly 12 routing_cases")

    router = QuestionRouter(CatalogRetriever(CatalogLoader().load()))
    user = _evaluation_user()
    records: list[dict[str, object]] = []
    failure_counts: Counter[str] = Counter()
    for case in cases:
        record, mismatches = evaluate_routing_case(case, router, user)
        records.append(record)
        failure_counts.update(mismatches.keys())

    passed_count = sum(record["status"] == "pass" for record in records)
    report: dict[str, object] = {
        "suite_id": SUITE_ID,
        "suite_version": raw["suite_version"],
        "phase": "phase_a",
        "mode": "deterministic_offline",
        "case_count": len(records),
        "passed_count": passed_count,
        "failed_count": len(records) - passed_count,
        "database_calls": 0,
        "llm_calls": 0,
        "failure_counts": dict(sorted(failure_counts.items())),
        "records": records,
    }
    validate_safe_evidence(
        {"suite_id": report["suite_id"], "phase": report["phase"], "records": records},
        known_case_ids={case["case_id"] for case in cases},
        phase="phase_a",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASE_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    result = evaluate_suite(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Phase A routing: {result['passed_count']}/{result['case_count']} passed; "
        f"report={args.output}"
    )
    if result["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
