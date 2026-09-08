from __future__ import annotations

import pytest

from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogValidationError
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.thelook_context import (
    THELOOK_ANALYTICS_COLUMNS,
    THELOOK_WORKSPACE,
)


@pytest.fixture()
def catalog():
    return CatalogLoader(THELOOK_WORKSPACE).load()


def test_thelook_catalog_matches_isolated_workspace_contract(catalog) -> None:
    assert catalog.catalog_version == THELOOK_WORKSPACE.catalog_version
    assert catalog.dataset_version == THELOOK_WORKSPACE.dataset_version
    assert catalog.metric_version == THELOOK_WORKSPACE.metric_version
    assert catalog.policy_version == THELOOK_WORKSPACE.policy_version
    assert set(catalog.tables_by_id) == set(THELOOK_ANALYTICS_COLUMNS)
    assert len(catalog.tables) == 7
    assert len(catalog.metrics) == 6
    assert len(catalog.joins) == 8

    for table in catalog.tables:
        assert {column.name for column in table.columns} == set(
            THELOOK_ANALYTICS_COLUMNS[table.table_id]
        )


def test_thelook_metrics_have_explicit_order_and_status_semantics(catalog) -> None:
    assert catalog.metrics_by_id["completed_sale_amount"].default_filters == (
        "orders.status = 'Complete'",
    )
    assert catalog.metrics_by_id["return_rate"].default_filters == (
        "orders.status IN ('Complete', 'Returned')",
    )
    assert catalog.metrics_by_id["average_order_value"].grain == "order"
    assert catalog.metrics_by_id["average_fulfillment_days"].time_field == (
        "orders.created_at"
    )


def test_thelook_sensitive_identifiers_are_not_display_dimensions(catalog) -> None:
    for metric in catalog.metrics:
        assert not (
            set(metric.allowed_dimensions)
            & THELOOK_WORKSPACE.sensitive_projection_columns
        )
    for table in catalog.tables:
        for column in table.columns:
            if column.name in THELOOK_WORKSPACE.sensitive_projection_columns:
                assert column.sensitive is True


def test_thelook_policy_uses_analytics_views_and_rejects_raw_schema() -> None:
    policy = SqlPolicy(workspace=THELOOK_WORKSPACE)
    decision = policy.evaluate(
        "SELECT p.category, SUM(oi.sale_price) AS completed_sale_amount "
        "FROM order_items oi "
        "JOIN orders o ON oi.order_id = o.order_id "
        "JOIN products p ON oi.product_id = p.id "
        "WHERE o.status = 'Complete' GROUP BY p.category"
    )
    assert decision.role == "analyst"
    assert decision.tables == ("order_items", "orders", "products")
    assert "analytics.order_items" in decision.final_sql

    with pytest.raises(PolicyViolation, match="schema is not allowed"):
        policy.evaluate("SELECT count(*) FROM thelook_raw.orders")


def test_thelook_catalog_rejects_unknown_policy_column(tmp_path) -> None:
    path = tmp_path / "thelook.yaml"
    raw = THELOOK_WORKSPACE.catalog_path.read_text(encoding="utf-8")
    path.write_text(
        raw.replace("order_items.sale_price", "order_items.not_a_column"),
        encoding="utf-8",
    )
    with pytest.raises(CatalogValidationError):
        CatalogLoader(THELOOK_WORKSPACE).load(path)
