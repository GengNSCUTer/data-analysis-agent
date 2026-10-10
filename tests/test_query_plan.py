from __future__ import annotations

from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
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
    assert "顶层最终 SELECT 的结果列白名单" in plan.prompt_context()
    assert "必须且只能返回上述白名单列" in plan.prompt_context()
    assert "不得原样出现在顶层最终 SELECT" in plan.prompt_context()
    assert "内部 CTE 如需按关联键保持事实粒度" in plan.prompt_context()
    assert plan.as_dict()["metric_plans"][0]["grain"]


def test_calendar_year_is_a_filter_not_a_time_series() -> None:
    router = _router()
    question = "统计2018年各地区的GMV"
    selection = router.retriever.retrieve(question, _user())
    route = router.admit_sql_request(question, user=_user(), selection=selection)
    memory = {"time_range": {"start": "2018-01-01", "end": "2018-12-31"}}
    plan = QueryPlan.from_selection(selection, question, route, memory)

    assert route.should_generate_sql is True
    assert plan.dimensions == ("customer_state",)
    assert plan.time_grain is None
    assert plan.time_range == {"start": "2018-01-01", "end": "2018-12-31"}
    assert plan.required_result_columns == ("customer_state", "gmv")
    assert "`time`" not in plan.prompt_context().split("时间粒度")[0]
    contract = ResultContract.from_selection(
        selection,
        question,
        memory["time_range"],
        catalog=router.retriever.catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    assert contract.result_time_column is None
    assert "time" not in contract.required_result_columns


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
    prompt = plan.prompt_context()
    assert "`customer_state`, `paid_order_count`, `gmv`, `time`" in prompt
    assert "最终结果别名、GROUP BY 或 ORDER BY 中" in prompt


def test_sp_customer_filter_keeps_customer_state_in_the_catalog_slice() -> None:
    router = _router()
    question = "只看 SP 客户，按商品品类看 GMV 最高的前几名"
    selection = router.retriever.retrieve(question, _user())
    route = router.admit_sql_request(question, user=_user(), selection=selection)
    plan = QueryPlan.from_selection(selection, question, route)

    assert "dim_customers" in selection.trace.selected_tables
    assert "customer_state" in {
        column
        for table_id, columns in selection.trace.selected_columns
        if table_id == "dim_customers"
        for column in columns
    }
    assert "customer_state" not in plan.dimensions
    assert "dim_customers.customer_state = 'SP'" in plan.prompt_context()


def test_specific_state_filter_does_not_become_a_state_breakdown() -> None:
    router = _router()
    question = "SP州哪种商品销量最好"
    selection = router.retriever.retrieve(question, _user())
    route = router.classify(question, user=_user(), selection=selection)
    plan = QueryPlan.from_selection(selection, question, route)

    assert route.should_generate_sql
    assert plan.metric_ids == ("item_count",)
    assert plan.dimensions == ("product_category_name",)


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


def test_query_plan_preflight_rejects_unbounded_or_over_budget_shapes() -> None:
    router = _router()
    cases = (
        ("2017年按月统计各州 GMV", "result_row_budget_exceeded"),
        ("按日统计 GMV", "daily_series_requires_explicit_range"),
    )
    city = router.retriever.retrieve("按客户城市统计 GMV", _user())
    city_decision = QueryPlan.preflight(city.metrics, "按客户城市统计 GMV")
    assert city_decision.allowed is True
    assert city_decision.estimated_max_rows == 20

    for question, reason_code in cases:
        selection = router.retriever.retrieve(question, _user())
        decision = QueryPlan.preflight(
            selection.metrics, question, conversation_state={}
        )
        assert decision.reason_code == reason_code
        assert decision.allowed is False


def test_query_plan_preflight_uses_an_explicit_range_for_daily_row_budget() -> None:
    router = _router()
    selection = router.retriever.retrieve("按日统计 GMV", _user())

    short_range = QueryPlan.preflight(
        selection.metrics,
        "2027-01-01 至 2027-01-31按日统计 GMV",
        conversation_state={},
    )
    long_range = QueryPlan.preflight(
        selection.metrics,
        "2027-01-01 至 2027-12-31按日统计 GMV",
        conversation_state={},
    )

    assert short_range.allowed is True
    assert short_range.estimated_max_rows == 31
    assert long_range.allowed is False
    assert long_range.reason_code == "result_row_budget_exceeded"
    assert long_range.estimated_max_rows == 365


def test_ranking_category_uses_top_n_instead_of_full_table() -> None:
    router = _router()
    hottest = router.retriever.retrieve("卖得最火的货", _user())
    hottest_decision = QueryPlan.preflight(hottest.metrics, "卖得最火的货")
    assert hottest_decision.allowed is True
    assert hottest_decision.estimated_max_rows == 20

    by_state = router.retriever.retrieve("各州卖得最好的货", _user())
    by_state_decision = QueryPlan.preflight(by_state.metrics, "各州卖得最好的货")
    assert by_state_decision.allowed is True
    assert by_state_decision.estimated_max_rows == 27

    census = router.retriever.retrieve("按品类统计 GMV", _user())
    census_decision = QueryPlan.preflight(census.metrics, "按品类统计 GMV")
    assert census_decision.allowed is True
    assert census_decision.estimated_max_rows == 73


def test_query_plan_can_use_online_execution_cap_above_display_budget() -> None:
    router = _router()
    selection = router.retriever.retrieve("2017年按月统计各州 GMV", _user())

    decision = QueryPlan.preflight(
        selection.metrics,
        "2017年按月统计各州 GMV",
        conversation_state={},
        max_result_rows=1_000,
    )

    assert decision.allowed is True
    assert decision.estimated_max_rows == 324
