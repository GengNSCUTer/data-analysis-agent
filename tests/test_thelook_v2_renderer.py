from __future__ import annotations

import pytest
from sqlglot import parse_one

from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.sql_policy import SqlPolicy
from data_analysis_agent.thelook_queryspec import TheLookQueryTime
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.thelook_v2_queryspec import TheLookV2QuerySpec
from data_analysis_agent.thelook_v2_renderer import (
    RENDERER_VERSION,
    render_thelook_v2_gold_sql,
)


def _spec(**kwargs) -> TheLookV2QuerySpec:
    return TheLookV2QuerySpec.create_validated(**kwargs)


def test_every_v2_metric_has_a_policy_approved_scalar_renderer() -> None:
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)

    for metric_id in catalog.metrics_by_id:
        artifact = render_thelook_v2_gold_sql(
            _spec(metric_ids=(metric_id,), result_shape="scalar"), catalog
        )

        assert artifact.renderer_version == RENDERER_VERSION
        assert artifact.required_result_columns == (metric_id,)
        assert artifact.sql_sha256 == artifact.evidence["sql_sha256"]
        assert "thelook_raw" not in artifact.sql
        assert parse_one(artifact.sql, read="postgres") is not None
        assert policy.evaluate(artifact.sql).status == "allowed"


@pytest.mark.parametrize(
    ("metrics", "shape", "dimension", "time", "required"),
    [
        (
            ("average_order_value", "average_items_per_completed_order"),
            "dimension_grouped",
            "customer_traffic_source",
            TheLookQueryTime("all_time"),
            (
                "customer_traffic_source",
                "average_order_value",
                "average_items_per_completed_order",
            ),
        ),
        (
            ("sold_inventory_unit_count", "average_days_to_sale"),
            "dimension_grouped",
            "distribution_center",
            TheLookQueryTime("absolute_range", "2020-01-01", "2021-01-01"),
            (
                "distribution_center",
                "sold_inventory_unit_count",
                "average_days_to_sale",
            ),
        ),
        (
            ("event_count", "unique_session_count"),
            "time_series",
            None,
            TheLookQueryTime("series", "2019-01-01", "2024-01-01", "year"),
            ("event_count", "unique_session_count", "time"),
        ),
    ],
)
def test_v2_renderer_preserves_multi_metric_contracts(
    metrics: tuple[str, ...],
    shape: str,
    dimension: str | None,
    time: TheLookQueryTime,
    required: tuple[str, ...],
) -> None:
    artifact = render_thelook_v2_gold_sql(
        _spec(
            metric_ids=metrics,
            result_shape=shape,
            dimension=dimension,
            time=time,
        )
    )

    assert artifact.required_result_columns == required
    assert SqlPolicy(workspace=THELOOK_V2_WORKSPACE).evaluate(artifact.sql).status == (
        "allowed"
    )
    assert "question" not in artifact.evidence
    assert "rows" not in artifact.evidence


def test_order_level_renderer_keeps_order_grain_before_outer_average() -> None:
    artifact = render_thelook_v2_gold_sql(
        _spec(
            metric_ids=("average_order_value", "average_items_per_completed_order"),
            result_shape="scalar",
        )
    )

    assert "SUM(oi.sale_price) AS order_value" in artifact.sql
    assert "COUNT(oi.id) AS item_count" in artifact.sql
    assert (
        "JOIN analytics.order_items AS oi ON o.order_id = oi.order_id" in artifact.sql
    )
    assert "AVG(order_values.order_value) AS average_order_value" in artifact.sql
    assert "AVG(order_item_counts.item_count) AS average_items_per_completed_order" in (
        artifact.sql
    )
