from __future__ import annotations

from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from vanna.core.user import User


def _router() -> QuestionRouter:
    return QuestionRouter(CatalogRetriever(CatalogLoader().load()))


def _user() -> User:
    return User(id="query-plan-analyst", group_memberships=["analyst"])


def test_scalar_multi_metric_plan_requires_one_cte_per_metric() -> None:
    router = _router()
    question = "概览 GMV、有效订单数、平均履约天数和好评率，并说明统计口径"
    selection = router.retriever.retrieve(question, _user())
    route = router.classify(question, user=_user(), selection=selection)

    plan = QueryPlan.from_selection(selection, question, route)

    assert route.intent == "mixed_request"
    assert plan.plan_type == "scalar_multi_metric_overview"
    assert plan.execution_strategy == "one_cte_per_metric_then_cross_join"
    assert plan.metric_ids == (
        "average_delivery_days",
        "paid_order_count",
        "gmv",
        "positive_review_rate",
    )
    assert plan.required_result_columns == plan.metric_ids
    assert "每个指标先在自己的事实粒度独立聚合" in plan.prompt_context()
    assert plan.as_dict()["metric_plans"][0]["grain"]


def test_grouped_plan_detects_dimension_and_time_grain() -> None:
    router = _router()
    question = "2017年按月统计各州 GMV 和有效订单数"
    selection = router.retriever.retrieve(question, _user())
    route = router.classify(question, user=_user(), selection=selection)

    plan = QueryPlan.from_selection(
        selection,
        question,
        route,
        {"time_range": {"start": "2017-01-01", "end": "2017-12-31"}},
    )

    assert plan.plan_type == "grouped_multi_metric"
    assert plan.execution_strategy == "one_cte_per_metric_then_join_on_group_keys"
    assert plan.dimensions == ("customer_state",)
    assert plan.time_grain == "month"
    assert plan.required_result_columns[-1] == "time"
    assert plan.time_range == {"start": "2017-01-01", "end": "2017-12-31"}


def test_single_metric_plan_does_not_invent_dimensions() -> None:
    router = _router()
    question = "统计 GMV"
    selection = router.retriever.retrieve(question, _user())
    route = router.classify(question, user=_user(), selection=selection)

    plan = QueryPlan.from_selection(selection, question, route)

    assert plan.plan_type == "single_metric"
    assert plan.dimensions == ()
    assert plan.time_grain is None
    assert plan.required_result_columns == ("gmv",)
