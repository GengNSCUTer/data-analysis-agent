"""Online metric capabilities, separate from frozen training catalogs."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import os
import uuid

import pandas as pd
import pytest
from sqlglot import parse_one
from vanna.core.user import User

from data_analysis_agent.chart_contract import ChartContract
from data_analysis_agent.plan_sql_guard import sql_plan_mismatch
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.runtime_paths import RESOURCES
from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, CatalogValidationError, ResultContract
from data_analysis_agent.semantic_intent import SemanticIntent, route_semantic_intent, select_intent_catalog

ROOT = Path(__file__).resolve().parents[1]
USER = User(id="online-metric-test", group_memberships=["analyst"])
NEW = {
    "olist": ("total_order_count", "payment_amount", "freight_share", "period_repeat_customer_rate"),
    "thelook": ("total_order_count", "cancellation_rate", "period_repeat_customer_rate", "current_unsold_inventory_cost"),
}


def catalog_for(name):
    return CatalogLoader(load_runtime_workspace(RESOURCES / f"workspaces/{name}.yaml").profile).load()


def test_clean_product_asset_build_contains_both_online_catalogs(tmp_path):
    from data_analysis_agent._build import copy_assets
    copy_assets(tmp_path)
    for name, count in [("olist", 23), ("thelook", 24)]:
        workspace = load_runtime_workspace(tmp_path / f"workspaces/{name}.yaml").profile
        catalog = CatalogLoader(workspace).load()
        assert len(catalog.metrics) == count


def selected(catalog, metric_ids, dimensions=(), **kwargs):
    intent = SemanticIntent(request_kind="data_query", metric_ids=metric_ids, dimensions=dimensions, **kwargs)
    retriever = CatalogRetriever(catalog)
    selection = select_intent_catalog(intent, retriever, USER, question="查看指标")
    route = route_semantic_intent(intent, QuestionRouter(retriever, max_result_rows=100_000, full_results=True), selection)
    return intent, selection, route


@pytest.mark.parametrize("name,count", [("olist", 23), ("thelook", 24)])
def test_online_catalogs_match_operator_copies_and_experiment_catalogs_stay_separate(name, count):
    catalog = catalog_for(name)
    assert len(catalog.metrics) == count
    assert set(NEW[name]) <= catalog.metrics_by_id.keys()
    assert set(catalog.metric_semantics) == catalog.metrics_by_id.keys()
    for directory in ("catalog", "workspaces"):
        filename = f"{name}_online_v1.yaml" if directory == "catalog" else f"{name}.yaml"
        assert (RESOURCES / directory / filename).read_bytes() == (ROOT / "data" / directory / filename).read_bytes()
    old_file, old_version, old_count = (("olist_catalog_v3.yaml", "olist-catalog-v3", 19) if name == "olist"
                                       else ("thelook_catalog_v2.yaml", "thelook-catalog-v2", 20))
    old = CatalogLoader(replace(catalog.workspace, catalog_version=old_version)).load(RESOURCES / "catalog" / old_file)
    assert len(old.metrics) == old_count and not (set(NEW[name]) & old.metrics_by_id.keys())


@pytest.mark.parametrize("name,metric", [(n, m) for n, metrics in NEW.items() for m in metrics])
def test_new_metrics_route_to_plan_and_contract_without_gold_renderer(name, metric):
    catalog = catalog_for(name)
    intent, selection, route = selected(catalog, (metric,))
    assert route.state == "answerable"
    assert selection.trace.selected_metrics == (metric,)
    state = {"semantic_intent": intent.as_dict()}
    plan = QueryPlan.from_selection(selection, "查看指标", route, state)
    contract = ResultContract.from_selection(selection, "查看指标", catalog=catalog,
                                           required_result_columns=plan.required_result_columns,
                                           result_budget=plan.result_budget)
    assert plan.required_result_columns == (metric,)
    assert contract.metric_result_columns == (metric,)
    assert catalog.metrics_by_id[metric].description in selection.prompt
    assert all(catalog.workspace.dimension_bindings.get(d) for d in catalog.metrics_by_id[metric].allowed_dimensions if d != "date")
    good = ResultValidator().validate(pd.DataFrame({metric: [0]}), required_columns=(metric,), metric_columns=(metric,),
                                      metric_value_constraints=contract.metric_value_constraints)
    assert good.state == "valid"
    bad = ResultValidator().validate(pd.DataFrame({metric: [-1]}), required_columns=(metric,), metric_columns=(metric,),
                                     metric_value_constraints=contract.metric_value_constraints)
    assert bad.state == "refuse"
    if catalog.metric_semantics[metric]["kind"] == "rate":
        assert metric not in contract.additive_metric_ids
        assert ResultValidator().validate(pd.DataFrame({metric: [1.01]}), metric_columns=(metric,),
            metric_value_constraints=contract.metric_value_constraints).state == "refuse"


@pytest.mark.parametrize("metric", ["current_unsold_inventory_unit_count", "current_unsold_inventory_cost"])
@pytest.mark.parametrize("options", [
    {"time_range": {"start": "2023-01-01", "end": "2024-01-01"}},
    {"time_grain": "month"},
])
def test_snapshot_history_refused_before_sql_but_current_grouping_allowed(metric, options):
    catalog = catalog_for("thelook")
    _, _, route = selected(catalog, (metric,), **options)
    assert route.state == "clarification_required"
    assert route.reason_code == "current_snapshot_has_no_history"
    assert "快照" in route.clarification
    _, _, route = selected(catalog, (metric,), ("product_department",))
    assert route.state == "answerable"


def test_snapshot_constraint_is_catalog_owned_not_a_special_metric_name():
    catalog = catalog_for("thelook")
    metric = replace(catalog.metrics_by_id["total_order_count"], temporal_mode="current_snapshot")
    result = QueryPlan.preflight((metric,), "", {"semantic_intent": {"time_grain": "month"}})
    assert not result.allowed and result.reason_code == "current_snapshot_has_no_history"


@pytest.mark.parametrize("name", ["olist", "thelook"])
def test_repeat_is_not_additive_and_regular_period_grouping_is_allowed(name):
    catalog = catalog_for(name)
    metric = "period_repeat_customer_rate"
    _, selection, route = selected(catalog, (metric,), ("customer_state",), time_grain="month",
                                   time_range={"start": "2017-01-01", "end": "2018-01-01"})
    assert route.state == "answerable"
    result = ResultContract.from_selection(selection, "查看指标", catalog=catalog,
        required_result_columns=("customer_state", metric), requested_dimensions=("customer_state",))
    chart = ChartContract._from_shape("", ("customer_state",), None, (metric,), ("customer_state", metric), result,
                                      semantic_intent={"chart_request": "donut"})
    assert chart.status == "unsupported"
    _, _, bad = selected(catalog, (metric,), ("product_category_name" if name == "olist" else "product_category",))
    assert bad.state == "clarification_required"


@pytest.mark.parametrize("name,metric,good,bad", [
    ("olist", "payment_amount", "SELECT SUM(payment_value) AS payment_amount FROM analytics.fact_payments",
     "SELECT SUM(price) AS payment_amount FROM analytics.fact_order_items"),
    ("thelook", "current_unsold_inventory_cost", "SELECT SUM(cost) AS current_unsold_inventory_cost FROM analytics.inventory_items",
     "SELECT SUM(retail_price) AS current_unsold_inventory_cost FROM analytics.products"),
])
def test_new_amounts_enforce_physical_measure_source(name, metric, good, bad):
    catalog = catalog_for(name)
    _, selection, _ = selected(catalog, (metric,))
    contract = ResultContract.from_selection(selection, "查看指标", catalog=catalog,
        required_result_columns=(metric,), result_budget=ResultBudget.aggregate())
    assert metric in contract.metric_value_sources
    schema = {table.physical_name: {c.name: c.type for c in table.columns} for table in catalog.tables}
    plan = {"metric_ids": [metric]}
    assert sql_plan_mismatch(good, plan, metric_value_sources=contract.metric_value_sources, column_schema=schema) is None
    assert sql_plan_mismatch(bad, plan, metric_value_sources=contract.metric_value_sources, column_schema=schema)


@pytest.mark.parametrize("name", ["olist", "thelook"])
def test_all_order_count_does_not_inherit_valid_status_filter(name):
    catalog = catalog_for(name)
    assert catalog.metrics_by_id["total_order_count"].default_filters == ()
    assert catalog.metrics_by_id["period_repeat_customer_rate"].default_filters


def test_snapshot_metadata_typo_and_date_grouping_fail_catalog_loading():
    import yaml
    catalog = catalog_for("thelook")
    raw = yaml.safe_load(catalog.workspace.catalog_path.read_text())
    raw["metrics"][0]["temporal_mode"] = "snpashot"
    with pytest.raises(CatalogValidationError, match="temporal_mode"):
        CatalogLoader(catalog.workspace)._parse(raw, Path("test"))
    raw["metrics"][0]["temporal_mode"] = "current_snapshot"
    with pytest.raises(CatalogValidationError, match="date grouping"):
        CatalogLoader(catalog.workspace)._parse(raw, Path("test"))


def test_no_population_ratio_is_not_promoted_to_trusted_zero():
    metric = "freight_share"
    result = ResultValidator().validate(pd.DataFrame({metric: [None]}), metric_columns=(metric,))
    assert not result.safe_to_answer and result.null_metric_columns == (metric,)


@pytest.mark.parametrize("via_patch", [False, True])
def test_virtual_date_dimension_is_not_counted_twice_and_saved_state_matches_plan(via_patch):
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.semantic_intent import parse_semantic_intent, resolve_state_intent
    import json
    scope = {"metric_ids": ["period_repeat_customer_rate"], "dimensions": ["date"], "time_grain": "month",
             "time_range": {"start": "2017-01-01", "end": "2018-01-01"}}
    payload = {"request_kind": "data_query", "action": "new_task"}
    if via_patch:
        payload["state_patch"] = {"operations": [{"op": "set", "field": k, "value": v} for k, v in scope.items()]}
    else:
        payload.update(scope)
    catalog = catalog_for("olist")
    intent = parse_semantic_intent(json.dumps(payload), catalog)
    resolved, state = resolve_state_intent(intent, AnalysisState.new(catalog.workspace), catalog, USER, message_count=1)
    assert resolved.dimensions == state.dimensions == ()
    assert resolved.time_grain == state.time_grain == "month"
    _, selection, route = selected(catalog, resolved.metric_ids, resolved.dimensions,
        time_grain=resolved.time_grain, time_range=resolved.time_range)
    assert route.state == "answerable"
    plan = QueryPlan.from_selection(selection, "按月统计", route, {"semantic_intent": resolved.as_dict(), "time_range": state.time_range})
    assert plan.required_result_columns == ("time", "period_repeat_customer_rate")


@pytest.mark.parametrize("name", ["olist", "thelook"])
def test_rollout_question_sets_have_independent_sql_and_all_eight_metric_coverage(name):
    spec = importlib.util.spec_from_file_location("verify_workspace", ROOT / "scripts/verify_workspace_onboarding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = module.load_cases(ROOT / f"evals/cases/{name}_online_metrics_v1.yaml")
    catalog = catalog_for(name)
    tested = {col for c in suite["cases"] if c.get("reference_sql") for col in parse_one(c["reference_sql"], read="postgres").named_selects}
    assert set(NEW[name]) <= tested
    assert suite["metric_count"] == len(catalog.metrics)
    assert len([c for c in suite["cases"] if c.get("conversation_group")]) == 5
    assert any(c.get("refresh") and c.get("reuse") for c in suite["cases"])


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("name,database", [("olist", "data_analysis_agent"), ("thelook", "thelook_analytics")])
async def test_reviewed_new_metric_sql_passes_real_policy_reader_diagnostics_and_result_contract(name, database):
    """Opt-in actual snapshots; CI synthetic Olist cannot represent TheLook."""
    if os.getenv("RUN_ONLINE_METRICS_DB") != "1":
        pytest.skip("set RUN_ONLINE_METRICS_DB=1 for the two local business snapshots")
    import yaml
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
    from data_analysis_agent.trusted_sql_tool import TrustedRunSqlTool
    from vanna.capabilities.sql_runner import RunSqlToolArgs
    from vanna.core.tool import ToolContext
    from vanna.integrations.local.agent_memory import DemoAgentMemory

    catalog = catalog_for(name)
    runner = SecurePostgresRunner(settings=PostgresConnectionSettings(database=database, statement_timeout_ms=15_000),
                                  workspace=catalog.workspace)
    suite = yaml.safe_load((ROOT / f"evals/cases/{name}_online_metrics_v1.yaml").read_text())
    for case in suite["cases"]:
        if not case.get("reference_sql"):
            continue
        statement = case["reference_sql"]
        columns = tuple(parse_one(statement, read="postgres").named_selects)
        metrics = tuple(col for col in columns if col in catalog.metrics_by_id)
        dimensions = tuple(col for col in columns if col not in metrics and col != "time")
        intent, selection, route = selected(catalog, metrics, dimensions, time_grain="month" if "time" in columns else None)
        assert route.state == "answerable", case["id"]
        budget = ResultBudget.aggregate(statement_timeout_ms=15_000)
        contract = ResultContract.from_selection(selection, case["question"], catalog=catalog,
            required_result_columns=columns, requested_dimensions=dimensions, result_budget=budget)
        context = ToolContext(user=USER, conversation_id="new-metrics-" + uuid.uuid4().hex,
            request_id=uuid.uuid4().hex, agent_memory=DemoAgentMemory(),
            metadata={**contract.as_tool_metadata(), "query_plan": {**budget.as_metadata(), "metric_ids": list(metrics)}})
        assert TrustedRunSqlTool(runner)._candidate_mismatch(statement, context) is None, case["id"]
        frame = await runner.run_sql(RunSqlToolArgs(sql=statement), context)
        assert not frame.empty, case["id"]
        assert context.metadata["result_validation"]["state"] == "valid", (case["id"], context.metadata["result_validation"])
