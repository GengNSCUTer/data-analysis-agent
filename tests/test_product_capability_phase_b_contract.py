from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

from data_analysis_agent.chart_contract import ChartContract
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
from vanna.core.user import User


ROOT = Path(__file__).resolve().parents[1]
CASE_FILE = ROOT / "evals/cases/product_capability_v1.yaml"


def _router() -> QuestionRouter:
    return QuestionRouter(CatalogRetriever(CatalogLoader().load()))


def _user() -> User:
    return User(id="product-capability-phase-b-contract", group_memberships=["analyst"])


def _cases() -> list[dict[str, object]]:
    raw = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
    return raw["single_turn_cases"]


def test_phase_b_freezes_the_required_16_case_distribution() -> None:
    cases = _cases()
    assert len(cases) == 16
    assert len({case["case_id"] for case in cases}) == 16
    assert Counter(case["category"] for case in cases) == {
        "scalar": 2,
        "dimension_grouped": 3,
        "time_series": 3,
        "multi_metric": 3,
        "two_stage_aggregate": 2,
        "chart_request": 3,
    }


def test_phase_b_cases_match_offline_router_and_query_plan_contracts() -> None:
    router = _router()
    user = _user()
    for case in _cases():
        expected = case["expected"]
        selection = router.retriever.retrieve(case["question"], user)
        route = router.classify(
            case["question"], user=user, selection=selection, conversation_state={}
        )
        if route.should_generate_sql:
            route = router.admit_sql_request(
                case["question"], user=user, selection=selection, conversation_state={}
            )

        assert route.state == expected["terminal_state"], case["case_id"]
        assert route.intent == expected["expected_intent"], case["case_id"]
        assert route.requires_database is expected["requires_database"], case["case_id"]
        assert route.evidence_mode == expected["expected_evidence_mode"], case["case_id"]
        assert list(route.metric_ids) == expected["expected_metric_ids"], case["case_id"]
        assert route.reason_code == expected["expected_reason_code"], case["case_id"]
        assert route.requires_database is True, case["case_id"]

        plan = QueryPlan.from_selection(selection, case["question"], route, {})
        assert list(plan.required_result_columns) == expected["expected_required_result_columns"], case["case_id"]
        assert list(plan.dimensions) == expected.get("expected_dimensions", []), case["case_id"]
        assert plan.time_grain == expected.get("time_grain"), case["case_id"]
        if "expected_plan_type" in expected:
            assert plan.plan_type == expected["expected_plan_type"], case["case_id"]


def test_phase_b_chart_cases_have_a_server_owned_valid_chart_contract() -> None:
    router = _router()
    user = _user()
    for case in (case for case in _cases() if case["category"] == "chart_request"):
        expected = case["expected"]
        selection = router.retriever.retrieve(case["question"], user)
        route = router.classify(
            case["question"], user=user, selection=selection, conversation_state={}
        )
        plan = QueryPlan.from_selection(selection, case["question"], route, {})
        result_contract = ResultContract.from_selection(
            selection,
            case["question"],
            None,
            catalog=router.retriever.catalog,
            required_result_columns=plan.required_result_columns,
            requested_dimensions=plan.dimensions,
        )
        chart = ChartContract.from_query_plan(case["question"], plan, result_contract)

        assert chart.safe_to_visualize, case["case_id"]
        assert chart.chart_type == expected["chart_type"], case["case_id"]
        assert chart.x_column == expected["chart_x"], case["case_id"]
        assert list(chart.y_columns) == expected["chart_y"], case["case_id"]
