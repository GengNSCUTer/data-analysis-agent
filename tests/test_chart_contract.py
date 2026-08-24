"""Deterministic chart contract derivation tests."""

from __future__ import annotations

from data_analysis_agent.chart_contract import ChartContract
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import (
    CatalogLoader,
    CatalogRetriever,
    ResultContract,
)
from vanna.core.user import User


def _chart_contract(question: str) -> ChartContract:
    user = User(id="chart-contract-user", group_memberships=["analyst"])
    catalog = CatalogLoader().load()
    router = QuestionRouter(CatalogRetriever(catalog))
    selection = router.retriever.retrieve(question, user)
    route = router.classify(question, user=user, selection=selection)
    assert route.should_generate_sql
    plan = QueryPlan.from_selection(selection, question, route)
    result_contract = ResultContract.from_selection(
        selection,
        question,
        catalog=catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    return ChartContract.from_query_plan(question, plan, result_contract)


def test_monthly_metric_line_chart_has_fixed_time_axis() -> None:
    contract = _chart_contract("2017年按月统计 GMV，并生成折线图")

    assert contract.status == "valid"
    assert contract.chart_type == "line"
    assert contract.x_column == "time"
    assert contract.y_columns == ("gmv",)
    assert contract.series_column is None


def test_dimension_metric_bar_chart_has_fixed_dimension_axis() -> None:
    contract = _chart_contract("按州统计 GMV，并生成柱状图")

    assert contract.status == "valid"
    assert contract.chart_type == "bar"
    assert contract.x_column == "customer_state"
    assert contract.y_columns == ("gmv",)


def test_explicit_line_without_time_grain_requires_clarification() -> None:
    contract = _chart_contract("按州统计 GMV，并生成折线图")

    assert contract.status == "clarification_required"
    assert "时间粒度" in contract.clarification


def test_unspecified_type_uses_line_for_time_and_bar_for_dimension() -> None:
    temporal = _chart_contract("2017年按月统计 GMV，并生成图表")
    grouped = _chart_contract("按州统计 GMV，并生成图表")

    assert temporal.status == "valid"
    assert temporal.chart_type == "line"
    assert grouped.status == "valid"
    assert grouped.chart_type == "bar"


def test_scalar_chart_requires_an_axis() -> None:
    contract = _chart_contract("统计 GMV，并生成图表")

    assert contract.status == "clarification_required"
    assert "横轴" in contract.clarification


def test_unsupported_type_is_not_silently_downgraded() -> None:
    contract = _chart_contract("按州统计 GMV，并生成饼图")

    assert contract.status == "unsupported"
    assert "饼图" in contract.clarification
