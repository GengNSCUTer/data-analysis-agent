from __future__ import annotations

from dataclasses import replace

import pytest
from vanna.core.user import User

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.runtime_queryplan_renderer import (
    RuntimeGoldRenderError,
    render_runtime_queryplan_gold_sql,
)
from data_analysis_agent.semantic_catalog import (
    CatalogLoader,
    CatalogRetriever,
    ResultContract,
)


@pytest.fixture(scope="module")
def retriever() -> CatalogRetriever:
    return CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())


def _runtime_parts(retriever: CatalogRetriever, question: str):
    user = User(id="runtime-gold-test", group_memberships=["analyst"])
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


def test_runtime_scalar_gold_is_stable_and_has_runtime_provenance(retriever):
    plan, contract = _runtime_parts(retriever, "统计 GMV")
    first = render_runtime_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
    )
    second = render_runtime_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
    )

    assert first.sql == second.sql
    assert first.sql_sha256 == second.sql_sha256
    assert first.query_plan_sha256 == second.query_plan_sha256
    assert first.required_result_columns == ("gmv",)
    assert first.evidence["source"] == "runtime_query_plan"
    assert "query_spec_id" not in first.evidence
    assert "query_spec_id" not in first.as_dict()


def test_runtime_dimension_and_time_series_gold_preserves_contract_order(retriever):
    question = "2017年按月统计各州 GMV 和有效订单数"
    plan, contract = _runtime_parts(retriever, question)
    artifact = render_runtime_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
    )

    assert plan.required_result_columns == (
        "customer_state",
        "time",
        "gmv",
        "paid_order_count",
    )
    assert artifact.required_result_columns == plan.required_result_columns
    assert "date_trunc('month'" in artifact.sql
    assert "customer_state, time" in artifact.sql
    assert "< TIMESTAMP '2018-01-01'" in artifact.sql
    assert ">= TIMESTAMP '2017-01-01'" in artifact.sql


def test_runtime_time_series_uses_time_first_projection(retriever):
    plan, contract = _runtime_parts(retriever, "2017年按月统计 GMV 和有效订单数")
    artifact = render_runtime_queryplan_gold_sql(
        plan, contract, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
    )
    assert plan.required_result_columns == ("time", "gmv", "paid_order_count")
    assert artifact.sql.rsplit("SELECT ", 1)[-1].startswith(
        "COALESCE(m01_gmv.time, m02_paid_order_count.time) AS time, "
        "m01_gmv.gmv AS gmv"
    )


def test_runtime_renderer_allows_result_contract_metric_order_difference(retriever):
    plan, contract = _runtime_parts(retriever, "概览 GMV 和有效订单数")
    reordered = replace(
        contract,
        metric_ids=tuple(reversed(contract.metric_ids)),
        metric_result_columns=tuple(reversed(contract.metric_result_columns)),
    )
    artifact = render_runtime_queryplan_gold_sql(
        plan, reordered, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
    )
    assert artifact.metric_ids == plan.metric_ids
    assert artifact.required_result_columns == plan.required_result_columns


def test_runtime_renderer_rejects_ambiguous_inclusive_endpoint(retriever):
    plan, contract = _runtime_parts(retriever, "2017年按月统计 GMV")
    legacy_plan = replace(
        plan,
        time_range={
            "start": plan.time_range["start"],
            "end": plan.time_range["end"],
            "end_semantics": "inclusive",
        },
    )
    with pytest.raises(RuntimeGoldRenderError) as exc:
        render_runtime_queryplan_gold_sql(
            legacy_plan, contract, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
        )
    assert exc.value.reason_code == "invalid_time_contract"


def test_runtime_renderer_rejects_unsupported_dimension(retriever):
    plan, contract = _runtime_parts(retriever, "统计 GMV")
    unsupported_plan = replace(
        plan,
        dimensions=("customer_city",),
        required_result_columns=("customer_city", "gmv"),
    )
    unsupported_contract = replace(
        contract,
        required_result_columns=("customer_city", "gmv"),
    )
    with pytest.raises(RuntimeGoldRenderError) as exc:
        render_runtime_queryplan_gold_sql(
            unsupported_plan,
            unsupported_contract,
            retriever.catalog,
            workspace=OLIST_RUNTIME_WORKSPACE,
        )
    assert exc.value.reason_code == "unsupported_queryplan_dimension"


def test_runtime_renderer_rejects_metric_plan_drift(retriever):
    plan, contract = _runtime_parts(retriever, "统计 GMV")
    drifted_metric = replace(plan.metric_plans[0], time_field="fact_orders.bad_timestamp")
    drifted_plan = replace(plan, metric_plans=(drifted_metric,))
    with pytest.raises(RuntimeGoldRenderError) as exc:
        render_runtime_queryplan_gold_sql(
            drifted_plan, contract, retriever.catalog, workspace=OLIST_RUNTIME_WORKSPACE
        )
    assert exc.value.reason_code == "metric_plan_mismatch"
