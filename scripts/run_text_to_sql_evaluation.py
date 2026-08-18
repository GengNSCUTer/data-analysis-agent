#!/usr/bin/env python3
"""Run the deterministic v2 routing and QueryPlan golden suite.

The default runner is deliberately offline: it loads the server-owned Catalog,
routes each question, and builds a QueryPlan when SQL would be allowed. It does
not call SiliconFlow, PostgreSQL, or any external service. This keeps routing
regressions separate from online model quality and from database execution
goldens.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml

from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.working_memory import WorkingMemory
from vanna.core.user import User


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = ROOT / "evals/cases/text_to_sql_v2.yaml"


def _evaluation_user() -> User:
    return User(id="deterministic-eval-analyst", group_memberships=["analyst"])


def _expected_value(case: dict[str, Any], key: str, default: Any = None) -> Any:
    return case[key] if key in case else default


def evaluate_case(
    case: dict[str, Any],
    router: QuestionRouter,
    user: User,
) -> dict[str, Any]:
    started = perf_counter()
    question = case.get("question", "")
    state = case.get("conversation_state") or {}
    memory = WorkingMemory.from_mapping(state)
    retrieval_question = memory.retrieval_context(question) if question.strip() else question
    selection = (
        router.retriever.retrieve(retrieval_question, user)
        if question.strip()
        else None
    )
    route = router.classify(
        question,
        user=user,
        selection=selection,
        conversation_state=memory.as_dict(),
    )
    updated_memory = memory.apply(question, route)
    plan = None
    if route.requires_database:
        if selection is None:  # pragma: no cover - defensive contract guard
            raise AssertionError("database route requires a Catalog selection")
        plan = QueryPlan.from_selection(
            selection,
            question,
            route,
            updated_memory.as_dict(),
        )

    actual = {
        "state": route.state,
        "intent": route.intent,
        "requires_database": route.requires_database,
        "evidence_mode": route.evidence_mode,
        "metric_ids": list(route.metric_ids),
        "reason_code": route.reason_code,
        "plan_type": plan.plan_type if plan else None,
        "required_result_columns": list(plan.required_result_columns) if plan else [],
    }
    expected = {
        "state": case.get("expected_state"),
        "intent": case.get("expected_intent"),
        "requires_database": case.get("requires_database"),
        "evidence_mode": case.get("expected_evidence_mode"),
        "metric_ids": list(case.get("expected_metric_ids", [])),
        "reason_code": case.get("expected_reason_code"),
        "plan_type": case.get("expected_plan_type"),
        "required_result_columns": list(case.get("expected_required_result_columns", [])),
    }
    mismatches = {
        key: {"expected": expected[key], "actual": actual[key]}
        for key in expected
        if expected[key] != actual[key]
    }
    # Reports intentionally contain only IDs and structured route/plan fields;
    # the original question and any model/database content are not copied.
    return {
        "id": case["id"],
        "category": case.get("category", "uncategorized"),
        "passed": not mismatches,
        "mismatches": mismatches,
        "actual": actual,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
    }


def evaluate_suite(case_file: Path = DEFAULT_CASE_FILE) -> dict[str, Any]:
    suite = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    cases = suite.get("cases", [])
    identifiers = [case.get("id") for case in cases]
    router = QuestionRouter(CatalogRetriever(CatalogLoader().load()))
    user = _evaluation_user()
    results = [evaluate_case(case, router, user) for case in cases]
    passed = sum(1 for result in results if result["passed"])
    failures = [result for result in results if not result["passed"]]
    return {
        "suite_version": suite.get("version"),
        "dataset": suite.get("dataset"),
        "dataset_version": suite.get("dataset_version"),
        "catalog_version": suite.get("catalog_version"),
        "metric_version": suite.get("metric_version"),
        "policy_version": suite.get("policy_version"),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "deterministic_offline",
        "total_cases": len(cases),
        "passed_cases": passed,
        "failed_cases": len(failures),
        "unique_ids": len(identifiers) == len(set(identifiers)),
        "categories": dict(Counter(case.get("category", "uncategorized") for case in cases)),
        "results": results,
        "failures": failures,
        "scope_note": suite.get("scope_note"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASE_FILE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_suite(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{result['passed_cases']}/{result['total_cases']} cases passed "
        f"({result['failed_cases']} failed)"
    )
    if not result["unique_ids"] or result["failed_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
