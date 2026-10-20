import pytest
import yaml
from vanna.core.user import User

from data_analysis_agent.runtime_paths import RESOURCES
from data_analysis_agent.runtime_workspace import load_runtime_workspace, build_runtime_system_prompt
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.semantic_intent import SemanticIntentInterpreter, SemanticIntent, select_intent_catalog, route_semantic_intent
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.sql_policy import SqlPolicy, PolicyViolation
from data_analysis_agent.trusted_workflow import AnalyticsWorkflowHandler


@pytest.mark.parametrize("name,metric", [("olist", "gmv"), ("thelook", "completed_sale_amount")])
def test_packaged_workspaces_build_scoped_catalog_and_sql_policy(name, metric, monkeypatch):
    monkeypatch.delenv("DATA_ANALYSIS_CATALOG_PATH", raising=False)
    monkeypatch.delenv("DATA_ANALYSIS_DATASET_VERSION", raising=False)
    runtime = load_runtime_workspace(RESOURCES / f"workspaces/{name}.yaml")
    catalog = CatalogLoader(runtime.profile).load()
    assert metric in catalog.metrics_by_id
    policy = SqlPolicy(workspace=runtime.profile)
    table = catalog.tables[0]
    assert table.physical_name in policy.evaluate(f"SELECT COUNT(*) AS n FROM {table.physical_name}", role="analyst").tables
    for query in ("SELECT COUNT(*) FROM app.accounts", "SELECT COUNT(*) FROM thelook_raw.users", "DELETE FROM analytics.orders"):
        with pytest.raises(PolicyViolation):
            policy.evaluate(query, role="analyst")


@pytest.fixture()
def workshop(tmp_path, monkeypatch):
    """Third schema: no commerce IDs, no dataset branch, a new logical dimension."""
    monkeypatch.delenv("DATA_ANALYSIS_CATALOG_PATH", raising=False)
    monkeypatch.delenv("DATA_ANALYSIS_DATASET_VERSION", raising=False)
    catalog = {
        "catalog_version": "workshop-v1", "dataset_version": "workshop-sample-v1",
        "metric_version": "1", "policy_version": "sql-policy-v1", "description": "工单业务",
        "tables": [{"table_id": "tickets", "physical_name": "tickets", "description": "工单", "grain": "ticket",
                    "aliases": ["工单"], "semantic_tags": ["工单"], "role_visibility": ["analyst", "admin"],
                    "context_columns": ["team", "opened_at", "ticket_id"], "columns": [
                        {"name": name, "type": kind, "description": label, "aliases": [label], "sensitive": sensitive}
                        for name, kind, label, sensitive in [("ticket_id", "bigint", "内部工单键", True), ("team", "text", "负责团队", False), ("opened_at", "timestamp", "创建时间", False)]
                    ]}],
        "metrics": [{"metric_id": "ticket_count", "name": "工单数", "description": "每行一工单，统计行数", "aliases": ["工单量"],
                     "grain": "ticket", "time_field": "tickets.opened_at", "source_tables": ["tickets"], "source_columns": ["tickets.ticket_id", "tickets.opened_at"],
                     "default_filters": ["tickets.opened_at IS NOT NULL"], "allowed_dimensions": ["date", "support_team"], "result_value_constraints": {"minimum": 0, "integer_like": True},
                     "recommended_chart": "bar", "role_visibility": ["analyst", "admin"]}], "joins": [],
    }
    (tmp_path / "catalog.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True))
    config = {"workspace_id": "workshop", "dataset_id": "workshop", "display_name": "工单分析", "description": "自构造测试业务",
              "catalog": "catalog.yaml", "analytics_schema": "support", "reader_role": "ticket_reader", "writer_role": "ticket_writer",
              "dimensions": {"support_team": {"aliases": ["负责团队"], "sources": [["tickets", "team"]], "max_cardinality": 12}},
              "starter_questions": [{"label": "团队工单", "question": "按负责团队统计工单数"}]}
    path = tmp_path / "workspace.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    return load_runtime_workspace(path)


@pytest.mark.asyncio
async def test_third_schema_reuses_prompt_intent_plan_policy_and_help(workshop):
    catalog = CatalogLoader(workshop.profile).load()
    interpreter = SemanticIntentInterpreter(catalog, lambda *_: None)
    user = User(id="test", group_memberships=["analyst"])
    intent = SemanticIntent(request_kind="data_query", metric_ids=("ticket_count",), dimensions=("support_team",), confidence=0.95)
    retriever = CatalogRetriever(catalog)
    selection = select_intent_catalog(intent, retriever, user)
    assert "support.tickets" in selection.prompt
    assert "analytics.tickets" not in selection.prompt
    from data_analysis_agent.query_plan import QueryPlan
    state = {"semantic_intent": intent.model_dump(), "metric_ids": ["ticket_count"], "dimensions": ["support_team"]}
    route = route_semantic_intent(intent, QuestionRouter(retriever, full_results=True), selection, state)
    plan = QueryPlan.from_selection(selection, "按负责团队统计工单数", route, state)
    assert plan.required_result_columns == ("support_team", "ticket_count")
    decision = SqlPolicy(workspace=workshop.profile).evaluate("SELECT team AS support_team, COUNT(*) AS ticket_count FROM tickets GROUP BY team", role="analyst")
    assert "support.tickets" in decision.final_sql
    with pytest.raises(PolicyViolation, match="sensitive"):
        SqlPolicy(workspace=workshop.profile).evaluate("SELECT ticket_id FROM tickets", role="analyst")
    prompt = build_runtime_system_prompt(workshop) + interpreter.prompt("按负责团队统计工单数", user)
    assert "工单分析" in prompt and "ticket_count" in prompt
    assert all(token not in prompt for token in ("paid_order_count", "product_category_name", "圣保罗", "Olist", "TheLook"))
    components = await AnalyticsWorkflowHandler(workshop).get_starter_ui(None, user, None)
    assert components[1].rich_component.data["buttons"][0]["action"] == "按负责团队统计工单数"


def test_server_uses_configured_workspace_for_storage_and_writer(tmp_path, monkeypatch):
    from data_analysis_agent import server
    stores = []
    original = server.PostgresConversationStore

    def store(settings, **kwargs):
        result = original(settings, **kwargs)
        stores.append(result)
        return result

    monkeypatch.setenv("SILICONFLOW_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("DATA_ANALYSIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ANALYSIS_WORKSPACE_CONFIG", str(RESOURCES / "workspaces/thelook.yaml"))
    monkeypatch.delenv("DATA_ANALYSIS_CATALOG_PATH", raising=False)
    monkeypatch.delenv("DATA_ANALYSIS_DATASET_VERSION", raising=False)
    monkeypatch.setattr(server, "PostgresConversationStore", store)
    monkeypatch.setattr(server, "ensure_bootstrap_account", lambda _: None)
    server.create_app()
    assert stores[0].default_workspace_id == "thelook-demo"
    assert stores[0].default_dataset_version == "thelook-kaggle-mirror-v1-20260908"
    assert stores[0].default_metric_version == "0.2-frozen"
    assert stores[0].settings.writer_user == "daa_thelook_app_writer"


def test_dimension_source_prefers_configured_product_dimension_over_inventory_fact():
    runtime = load_runtime_workspace(RESOURCES / "workspaces/thelook.yaml")
    catalog = CatalogLoader(runtime.profile).load()
    retriever = CatalogRetriever(catalog)
    user = User(id="test", group_memberships=["analyst"])
    sales = select_intent_catalog(SemanticIntent(request_kind="data_query", metric_ids=("completed_sale_amount",), dimensions=("product_category",)), retriever, user)
    assert "products" in sales.trace.selected_tables
    inventory = select_intent_catalog(SemanticIntent(request_kind="data_query", metric_ids=("current_unsold_inventory_unit_count",), dimensions=("product_category",)), retriever, user)
    assert "inventory_items" in inventory.trace.selected_tables


def test_join_key_guard_catches_observed_fanout_without_filters():
    from data_analysis_agent.plan_sql_guard import sql_plan_mismatch
    plan = {"query_plan_version": "query-plan-v2", "lookup_joins": [
        {"table": "inventory_items", "on": "order_items.inventory_item_id = inventory_items.id"},
        {"table": "orders", "on": "order_items.order_id = orders.order_id"},
    ]}
    prefix = "WITH amounts AS (SELECT oi.product_id, SUM(oi.sale_price) AS amount FROM analytics.order_items oi JOIN analytics.orders o ON oi.order_id=o.order_id GROUP BY oi.product_id) "
    bad = prefix + "SELECT i.product_category, SUM(a.amount) FROM amounts a JOIN analytics.inventory_items i ON a.product_id=i.product_id GROUP BY i.product_category"
    assert "关联键" in sql_plan_mismatch(bad, plan)
    good = "SELECT i.product_category,SUM(oi.sale_price) FROM analytics.order_items oi JOIN analytics.inventory_items i ON oi.inventory_item_id=i.id GROUP BY i.product_category"
    assert sql_plan_mismatch(good, plan) is None
