from __future__ import annotations

import pytest

from data_analysis_agent.result_presentation import (
    ResultPresentation,
    build_result_presentation,
    display_records,
    display_value,
)


def test_result_presentation_distinguishes_total_and_preview_rows() -> None:
    presentation = build_result_presentation(
        total_row_count=250,
        preview_row_count=200,
        columns=["customer_state", "gmv"],
        labels={"customer_state": "客户州", "gmv": "商品成交额"},
        units={"gmv": "BRL"},
        workspace_id="olist-demo",
        dataset_version="olist-v1",
        metric_version="metrics-v3",
        audit_ref="request-123",
    )

    assert presentation.display_truncated is True
    assert presentation.as_dict() == {
        "total_row_count": 250,
        "preview_row_count": 200,
        "display_truncated": True,
        "columns": ["customer_state", "gmv"],
        "column_labels": {"customer_state": "客户州", "gmv": "商品成交额"},
        "column_units": {"gmv": "BRL"},
        "preview_export_allowed": False,
        "full_export_allowed": False,
        "workspace_id": "olist-demo",
        "dataset_version": "olist-v1",
        "metric_version": "metrics-v3",
        "audit_ref": "request-123",
        "artifact_id": None,
    }


def test_result_presentation_rejects_inconsistent_truncation() -> None:
    with pytest.raises(ValueError, match="display_truncated"):
        ResultPresentation(
            total_row_count=20,
            preview_row_count=20,
            display_truncated=True,
            columns=(),
            column_labels={},
            column_units={},
            preview_export_allowed=False,
            full_export_allowed=False,
            workspace_id="w",
            dataset_version="d",
            metric_version="m",
            audit_ref="a",
        )


def test_all_registered_rate_metrics_render_as_percentages() -> None:
    assert display_value("positive_review_rate", 0.7611) == "76.11%"
    assert display_value("on_time_delivery_rate", 0.96) == "96.00%"
    assert display_value("cancellation_rate", 0.0125) == "1.25%"


def test_display_records_relabels_translated_category_without_mutating_values() -> None:
    rows, columns = display_records(
        [{"product_category_name": "health_beauty", "gmv": 12.5}],
        columns=["product_category_name", "gmv"],
        labels={"product_category_name": "商品品类（英文展示名）", "gmv": "商品成交额"},
    )

    assert columns == ["商品品类（英文展示名）", "商品成交额"]
    assert rows == [{"商品品类（英文展示名）": "health_beauty", "商品成交额": "12.50"}]
