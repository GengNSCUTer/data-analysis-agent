from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from scripts.run_product_capability_routing import evaluate_routing_case, evaluate_suite
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from vanna.core.user import User


ROOT = Path(__file__).resolve().parents[1]
CASE_FILE = ROOT / "evals/cases/product_capability_v1.yaml"


def _router() -> QuestionRouter:
    return QuestionRouter(CatalogRetriever(CatalogLoader().load()))


def _user() -> User:
    return User(id="product-capability-test", group_memberships=["analyst"])


def test_phase_a_has_the_frozen_routing_coverage() -> None:
    suite = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
    cases = suite["routing_cases"]

    assert len(cases) == 12
    assert len({case["case_id"] for case in cases}) == 12
    assert {case["category"] for case in cases} == {
        "answerable_database",
        "help_or_definition",
        "clarification",
        "security_refusal",
        "result_followup_without_context",
    }
    assert sum(case["category"] == "answerable_database" for case in cases) == 3
    assert sum(case["category"] == "help_or_definition" for case in cases) == 2
    assert sum(case["category"] == "clarification" for case in cases) == 3
    assert sum(case["category"] == "security_refusal" for case in cases) == 3
    assert sum(case["category"] == "result_followup_without_context" for case in cases) == 1


def test_phase_a_runner_is_database_and_llm_free() -> None:
    report = evaluate_suite(CASE_FILE)

    assert report["case_count"] == 12
    assert report["passed_count"] == 12
    assert report["failed_count"] == 0
    assert report["database_calls"] == 0
    assert report["llm_calls"] == 0


def test_phase_a_safe_report_excludes_questions_sql_and_route_payloads() -> None:
    report = evaluate_suite(CASE_FILE)
    forbidden = {"question", "sql", "sql_text", "actual", "expected", "result_rows", "completion"}

    for record in report["records"]:
        assert not (set(record) & forbidden)
        assert record["status"] == "pass"
        assert record["evidence_codes"] == ["route_matches"]


def test_routing_mismatch_becomes_a_structured_failure_not_a_runner_crash() -> None:
    suite = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
    case = deepcopy(suite["routing_cases"][0])
    case["expected"]["terminal_state"] = "help"

    record, mismatches = evaluate_routing_case(case, _router(), _user())

    assert record["status"] == "fail"
    assert record["failure_layer"] == "router"
    assert record["evidence_codes"] == ["route_mismatch:terminal_state"]
    assert set(mismatches) == {"terminal_state"}
