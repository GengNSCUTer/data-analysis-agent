from __future__ import annotations

from dataclasses import replace

import pytest
from vanna.core.user import User

from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.runtime_thelook_queryplan_renderer import (
    RuntimeTheLookGoldRenderError,
    render_runtime_thelook_queryplan_gold_sql,
)
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE


@pytest.fixture(scope="module")
def retriever() -> CatalogRetriever:
    return CatalogRetriever(CatalogLoader(THELOOK_V2_WORKSPACE).load())


def _parts(retriever: CatalogRetriever, question: str):
    user = User(id="thelook-runtime-gold-test", group_memberships=["analyst"])
    selection = retriever.retrieve(question, user)
    router = QuestionRouter(retriever, max_result_rows=1_000)
    route = router.classify(question, user=user, selection=selection)
    if route.should_generate_sql:
        route = router.admit_sql_request(question, user=user, selection=selection)
    assert route.should_generate_sql, route
    plan = QueryPlan.from_selection(selection, question, route)
    contract = ResultContract.from_selection(
        selection,
        question,
        plan.time_range,
        catalog=retriever.catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    return plan, contract


def test_scalar_runtime_thelook_gold_is_stable(retriever):
    plan, contract = _parts(retriever, "统计注册用户数")
    first = render_runtime_thelook_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=THELOOK_V2_WORKSPACE
    )
    second = render_runtime_thelook_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=THELOOK_V2_WORKSPACE
    )
    assert first.sql == second.sql
    assert first.sql_sha256 == second.sql_sha256
    assert first.required_result_columns == ("registered_user_count",)
    assert "query_spec_id" not in first.evidence


def test_dimension_runtime_thelook_gold_uses_catalog_column(retriever):
    plan, contract = _parts(retriever, "按注册用户国家统计注册用户数")
    artifact = render_runtime_thelook_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=THELOOK_V2_WORKSPACE
    )
    assert plan.required_result_columns == ("user_country", "registered_user_count")
    assert "u.country AS user_country" in artifact.sql
    assert "analytics.users AS u" in artifact.sql


def test_series_runtime_thelook_gold_is_time_first_and_half_open(retriever):
    plan, contract = _parts(
        retriever, "2021-01-01至2021-04-01（不含结束日）按月统计事件数"
    )
    artifact = render_runtime_thelook_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=THELOOK_V2_WORKSPACE
    )
    assert plan.required_result_columns == ("time", "event_count")
    assert artifact.sql.rsplit("SELECT ", 1)[-1].startswith("m01_event_count.time AS time")
    assert "e.created_at < CAST('2021-04-01' AS TIMESTAMP)" in artifact.sql


def test_series_metric_order_difference_is_allowed(retriever):
    plan, contract = _parts(
        retriever, "2021-01-01至2021-04-01（不含结束日）按月统计事件数和去重会话数"
    )
    reordered = replace(
        contract,
        metric_ids=tuple(reversed(contract.metric_ids)),
        metric_result_columns=tuple(reversed(contract.metric_result_columns)),
    )
    artifact = render_runtime_thelook_queryplan_gold_sql(
        plan, reordered, retriever.catalog, workspace=THELOOK_V2_WORKSPACE
    )
    assert artifact.metric_ids == plan.metric_ids


def test_unsupported_top_n_fails_closed(retriever):
    plan, contract = _parts(retriever, "按注册用户国家统计注册用户数")
    plan = replace(plan, top_n=(("user_country", 10),))
    with pytest.raises(RuntimeTheLookGoldRenderError) as exc:
        render_runtime_thelook_queryplan_gold_sql(
            plan, contract, retriever.catalog, workspace=THELOOK_V2_WORKSPACE
        )
    assert exc.value.reason_code == "unsupported_queryplan_feature"
