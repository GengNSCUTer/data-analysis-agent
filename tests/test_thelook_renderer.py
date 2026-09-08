from __future__ import annotations

import pytest
from sqlglot import parse_one

from data_analysis_agent.sql_policy import SqlPolicy
from data_analysis_agent.thelook_queryspec import TheLookQuerySpec, TheLookQueryTime
from data_analysis_agent.thelook_context import THELOOK_WORKSPACE
from data_analysis_agent.thelook_renderer import (
    RENDERER_VERSION,
    render_thelook_gold_sql,
)


def _spec(**kwargs) -> TheLookQuerySpec:
    return TheLookQuerySpec.create_validated(**kwargs)


def test_scalar_gold_is_stable_and_policy_approved() -> None:
    spec = _spec(metric_ids=("completed_sale_amount",), result_shape="scalar")
    first = render_thelook_gold_sql(spec)
    second = render_thelook_gold_sql(spec)

    assert first.sql == second.sql
    assert first.sql_sha256 == second.sql_sha256
    assert first.renderer_version == RENDERER_VERSION
    assert first.required_result_columns == ("completed_sale_amount",)
    assert SqlPolicy(workspace=THELOOK_WORKSPACE).evaluate(first.sql).status == "allowed"
    assert parse_one(first.sql, read="postgres") is not None


@pytest.mark.parametrize(
    ("metrics", "shape", "dimension", "time", "columns"),
    [
        (("completed_order_count", "return_rate"), "dimension_grouped", "state", None, ("state", "completed_order_count", "return_rate")),
        (("completed_sale_amount",), "dimension_grouped", "category", None, ("category", "completed_sale_amount")),
        (("completed_order_count", "average_fulfillment_days"), "time_series", None, TheLookQueryTime("series", "2019-01-01", "2020-01-01", "month"), ("completed_order_count", "average_fulfillment_days", "time")),
    ],
)
def test_grouped_and_series_gold_have_expected_keys_and_policy(
    metrics: tuple[str, ...],
    shape: str,
    dimension: str | None,
    time: TheLookQueryTime | None,
    columns: tuple[str, ...],
) -> None:
    spec = _spec(metric_ids=metrics, result_shape=shape, dimension=dimension, time=time)
    artifact = render_thelook_gold_sql(spec)

    assert artifact.required_result_columns == columns
    assert SqlPolicy(workspace=THELOOK_WORKSPACE).evaluate(artifact.sql).status == "allowed"
    assert "thelook_raw" not in artifact.sql
    selected = artifact.sql.split(" SELECT ", 1)[-1]
    if shape == "time_series":
        assert selected.index("completed_order_count") < selected.index(" AS time")


def test_aov_gold_preserves_order_grain_before_average() -> None:
    artifact = render_thelook_gold_sql(
        _spec(metric_ids=("average_order_value",), result_shape="scalar")
    )

    assert "tl_aov_orders_01" in artifact.sql
    assert "SUM(oi.sale_price) AS order_total" in artifact.sql
    assert "AVG(order_total) AS average_order_value" in artifact.sql


def test_evidence_is_bounded_and_does_not_include_questions_or_rows() -> None:
    artifact = render_thelook_gold_sql(
        _spec(
            metric_ids=("completed_sale_amount", "completed_order_count"),
            result_shape="dimension_grouped",
            dimension="traffic_source",
        )
    )
    assert "question" not in artifact.evidence
    assert "rows" not in artifact.evidence
    assert artifact.evidence["workspace"]["workspace_id"] == THELOOK_WORKSPACE.workspace_id
