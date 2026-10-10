"""Handoffs between routing, the Catalog prompt and multi-turn state.

These use the Catalog the web demo loads (``OLIST_RUNTIME_WORKSPACE``), not
the default v1 file, because the two disagree on grouping policies.
"""

from __future__ import annotations

import pytest

from data_analysis_agent.budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.question_router import (
    OVERVIEW_METRIC_IDS,
    QuestionRouter,
    expand_retrieval_question,
    is_result_refinement,
)
from data_analysis_agent.semantic_catalog import (
    CatalogContextEnhancer,
    CatalogLoader,
    CatalogRetriever,
)
from vanna.core.user import User


@pytest.fixture(scope="module")
def router() -> QuestionRouter:
    return QuestionRouter(CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()))


def _user() -> User:
    return User(id="runtime-handoff", group_memberships=["analyst"])


def _route(router: QuestionRouter, question: str, state: dict | None = None):
    state = state or {}
    selection = router.retriever.retrieve(question, _user())
    route = router.classify(question, user=_user(), selection=selection, conversation_state=state)
    if route.should_generate_sql:
        route = router.admit_sql_request(
            question, user=_user(), selection=selection, conversation_state=state
        )
    return route


@pytest.mark.asyncio
async def test_enhancer_reuses_the_routed_catalog_slice(router) -> None:
    grounded = router.retriever.retrieve("平均配送要几天\n平均履约天数", _user())
    assert "average_delivery_days" in grounded.trace.selected_metrics
    usage = BudgetUsage(RequestBudget())
    usage.set_catalog_context("平均配送要几天", None, catalog_prompt=grounded.prompt)
    token = CURRENT_BUDGET.set(usage)
    try:
        prompt = await CatalogContextEnhancer(router.retriever).enhance_system_prompt(
            "base", "平均配送要几天", _user()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert grounded.prompt in prompt
    assert "`average_delivery_days`" in prompt
    assert "analytics.fact_orders" in prompt


def test_catalog_prompt_does_not_tell_the_model_to_block_admitted_groupings(router) -> None:
    question = "各品类好评率排名"
    selection = router.retriever.retrieve(question, _user())

    assert _route(router, question).should_generate_sql is True
    assert "尚未启用实际归因实现" not in selection.prompt
    assert "各组合计可以大于总体" in selection.prompt


def test_two_explicit_years_are_a_comparison_baseline(router) -> None:
    for question in ("2017年和2018年GMV对比", "2018年GMV相比2017年增长了多少"):
        assert _route(router, question).should_generate_sql is True
    assert _route(router, "GMV 和订单变化").state == "missing_comparison"


def test_explain_wording_is_a_definition_not_a_data_query(router) -> None:
    route = _route(router, "用一句话解释复购率")

    assert route.requires_database is False
    assert route.intent == "general_knowledge"


def test_refinement_needs_a_trusted_previous_result() -> None:
    state = {"metric_ids": ["gmv"], "dimensions": ["customer_state"]}

    assert is_result_refinement("按州拆分", state) is False
    trusted = {**state, "previous_result_summary": "2017 年 GMV 已验证。"}
    assert is_result_refinement("按州拆分", trusted) is True
    assert is_result_refinement("只看前五", trusted) is True
    assert is_result_refinement("改看有效订单数", trusted, has_metric_match=True) is True
    assert is_result_refinement("按月统计2017年GMV", trusted, has_metric_match=True) is False


def test_refinement_retrieves_the_previous_metric_and_grouping(router) -> None:
    state = {
        "metric_ids": ["gmv"],
        "dimensions": ["customer_state"],
        "previous_result_summary": "按州 GMV 已验证。",
    }
    question = expand_retrieval_question("只看前五", state, has_metric_match=False)
    selection = router.retriever.retrieve(question, _user())

    assert selection.trace.selected_metrics == ("gmv",)
    assert "dim_customers" in selection.trace.selected_tables
    assert _route(router, question, state).should_generate_sql is True


def test_overview_without_a_metric_uses_the_starter_metrics(router) -> None:
    question = expand_retrieval_question("概览一下经营情况", {}, has_metric_match=False)
    selection = router.retriever.retrieve(question, _user())

    assert set(selection.trace.selected_metrics) == set(OVERVIEW_METRIC_IDS)
    assert expand_retrieval_question("GMV 概览", {}, has_metric_match=True) == "GMV 概览"
