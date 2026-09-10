from __future__ import annotations

import pytest

from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.thelook_context import THELOOK_ANALYTICS_COLUMNS
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE


@pytest.fixture()
def catalog():
    return CatalogLoader(THELOOK_V2_WORKSPACE).load()


def test_v2_catalog_matches_the_isolated_workspace_snapshot(catalog) -> None:
    assert catalog.catalog_version == THELOOK_V2_WORKSPACE.catalog_version
    assert catalog.dataset_version == THELOOK_V2_WORKSPACE.dataset_version
    assert catalog.metric_version == THELOOK_V2_WORKSPACE.metric_version
    assert catalog.policy_version == THELOOK_V2_WORKSPACE.policy_version
    assert set(catalog.tables_by_id) == set(THELOOK_ANALYTICS_COLUMNS)
    assert len(catalog.tables) == 7
    assert len(catalog.metrics) == 20
    assert len(catalog.joins) == 8

    for table in catalog.tables:
        assert {column.name for column in table.columns} == set(
            THELOOK_ANALYTICS_COLUMNS[table.table_id]
        )


def test_v2_catalog_freezes_all_five_business_fact_domains(catalog) -> None:
    metrics = catalog.metrics_by_id

    assert {
        "completed_sale_amount",
        "completed_item_count",
    } <= set(metrics)
    assert {
        "completed_order_count",
        "average_order_value",
        "average_items_per_completed_order",
        "return_rate",
    } <= set(metrics)
    assert {
        "received_inventory_unit_count",
        "sold_inventory_unit_count",
        "current_unsold_inventory_unit_count",
        "average_days_to_sale",
    } <= set(metrics)
    assert {"event_count", "unique_session_count"} <= set(metrics)
    assert "registered_user_count" in metrics

    assert metrics["completed_sale_amount"].time_field == "orders.created_at"
    assert metrics["average_order_value"].grain == "order"
    assert metrics["sold_inventory_unit_count"].time_field == (
        "inventory_items.sold_at"
    )
    assert metrics["received_inventory_unit_count"].time_field == (
        "inventory_items.created_at"
    )
    assert metrics["event_count"].time_field == "events.created_at"
    assert metrics["registered_user_count"].time_field == "users.created_at"


def test_v2_catalog_encodes_metric_boundaries_and_safe_dimensions(catalog) -> None:
    metrics = catalog.metrics_by_id

    assert metrics["average_order_value"].default_filters == (
        "orders.status = 'Complete'",
    )
    assert metrics["average_items_per_completed_order"].default_filters == (
        "orders.status = 'Complete'",
    )
    assert metrics["return_rate"].default_filters == (
        "orders.status IN ('Complete', 'Returned')",
    )
    assert metrics["current_unsold_inventory_unit_count"].allowed_dimensions == (
        "distribution_center",
        "product_category",
        "product_brand",
        "product_department",
    )
    assert (
        "date" not in metrics["current_unsold_inventory_unit_count"].allowed_dimensions
    )
    assert "event_state" not in metrics["event_count"].allowed_dimensions
    assert "event_state" not in metrics["unique_session_count"].allowed_dimensions

    for metric in metrics.values():
        assert not (
            set(metric.allowed_dimensions)
            & THELOOK_V2_WORKSPACE.sensitive_projection_columns
        )
