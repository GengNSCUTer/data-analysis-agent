from __future__ import annotations

import pytest

from data_analysis_agent.metric_context import OLIST_WORKSPACE
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.workspace import WorkspaceProfile


def _small_profile() -> WorkspaceProfile:
    return WorkspaceProfile(
        workspace_id="fixture-workspace",
        dataset_id="fixture-dataset",
        dataset_version="fixture-v1",
        metric_version="metrics-v1",
        catalog_version="catalog-v1",
        policy_version="policy-v1",
        analytics_schema="sales",
        allowed_columns={
            "fact_orders": frozenset({"order_id", "order_status"}),
        },
        analyst_tables=frozenset({"fact_orders"}),
        sensitive_projection_columns=frozenset({"order_id"}),
    )


def test_policy_uses_workspace_objects_and_schema_instead_of_olist_constants() -> None:
    profile = _small_profile()
    policy = SqlPolicy(workspace=profile)

    decision = policy.evaluate(
        "SELECT COUNT(order_id) AS order_count FROM fact_orders"
    )

    assert decision.final_sql.endswith("FROM sales.fact_orders LIMIT 200")
    with pytest.raises(PolicyViolation, match="table is not allowed"):
        policy.evaluate("SELECT order_status FROM dim_customers")


def test_olist_is_an_adapter_profile_for_the_generic_catalog_loader() -> None:
    catalog = CatalogLoader(OLIST_WORKSPACE).load()

    assert catalog.dataset_version == OLIST_WORKSPACE.dataset_version
    assert catalog.metric_version == OLIST_WORKSPACE.metric_version
    assert catalog.catalog_version == OLIST_WORKSPACE.catalog_version
    assert set(catalog.tables_by_id) == set(OLIST_WORKSPACE.allowed_columns)


def test_profile_rejects_an_analyst_table_outside_workspace_objects() -> None:
    with pytest.raises(ValueError, match="analyst_tables"):
        WorkspaceProfile(
            workspace_id="invalid",
            dataset_id="fixture",
            dataset_version="v1",
            metric_version="m1",
            catalog_version="c1",
            policy_version="p1",
            allowed_columns={"fact_orders": frozenset({"order_id"})},
            analyst_tables=frozenset({"dim_customers"}),
            sensitive_projection_columns=frozenset(),
        )
