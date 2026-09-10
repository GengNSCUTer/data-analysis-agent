#!/usr/bin/env python3
"""Materialize the protected TheLook v2 cross-schema final evaluation set.

This is an evaluation-only builder.  It creates reviewed QuerySpecs, renders
deterministic Gold SQL, then admits each case through SqlPolicy, the isolated
reader role and ResultValidator before writing raw questions and SQL only
outside the Git worktree.  It never reads Olist training data or calls a model.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Iterable
import uuid

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.result_validator import ResultValidator  # noqa: E402
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_queryspec import TheLookQueryTime  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v2_queryspec import (  # noqa: E402
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)
from data_analysis_agent.thelook_v2_renderer import render_thelook_v2_gold_sql  # noqa: E402


EVALUATION_VERSION = "thelook-cross-schema-final-test-v2"
QUESTION_VARIANT_VERSION = "thelook-question-variants-v2"
QUESTION_LANGUAGE = "zh"
EXPECTED_CASES = 600
MAX_CASES = 600

_YEAR_WINDOWS = (
    ("2019-01-01", "2020-01-01"),
    ("2020-01-01", "2021-01-01"),
    ("2021-01-01", "2022-01-01"),
    ("2022-01-01", "2023-01-01"),
    ("2023-01-01", "2024-01-01"),
)
_STATE_WINDOWS = (
    ("2019-01-01", "2020-01-01"),
    ("2020-01-01", "2021-01-01"),
    ("2021-01-01", "2021-07-01"),
)
# `average_dispatch_days` is eligible for every order with a valid shipment
# timestamp, not only completed orders.  In the latter two general state
# windows it yields 206 and 200 groups respectively, exceeding the strict
# 200-row result contract.  These three 2019 windows preserve the same
# state-by-dispatch business question without truncation; their group counts
# are bounded by the already-admitted 2019 full-year window.
_DISPATCH_STATE_WINDOWS = (
    ("2019-01-01", "2020-01-01"),
    ("2019-01-01", "2019-07-01"),
    ("2019-07-01", "2020-01-01"),
)
_ORDER_METRICS = (
    "completed_order_count",
    "average_order_value",
    "average_items_per_completed_order",
    "cancelled_order_count",
    "returned_order_count",
    "average_fulfillment_days",
    "average_dispatch_days",
    "average_transit_days",
    "average_post_delivery_return_days",
    "return_rate",
    "completed_customer_count",
)
_ITEM_METRICS = ("completed_sale_amount", "completed_item_count")
_INVENTORY_TIMED_METRICS = (
    "received_inventory_unit_count",
    "sold_inventory_unit_count",
    "average_days_to_sale",
)
_EVENT_METRICS = ("event_count", "unique_session_count")
_EVENT_DIMENSIONS = ("event_type", "event_browser", "event_traffic_source")
_ALL_TIMED_METRICS = (
    *_ORDER_METRICS,
    *_ITEM_METRICS,
    *_INVENTORY_TIMED_METRICS,
    *_EVENT_METRICS,
    "registered_user_count",
)
_METRIC_LABELS = {
    "completed_sale_amount": "已完成成交额",
    "completed_item_count": "已完成商品行数",
    "completed_order_count": "已完成订单数",
    "average_order_value": "平均完成订单金额",
    "average_items_per_completed_order": "平均每完成订单商品行数",
    "cancelled_order_count": "已取消订单数",
    "returned_order_count": "已退货订单数",
    "average_fulfillment_days": "平均履约天数",
    "average_dispatch_days": "平均发货时长",
    "average_transit_days": "平均运输时长",
    "average_post_delivery_return_days": "平均送达后退货等待时长",
    "return_rate": "退货率",
    "completed_customer_count": "已完成客户数",
    "received_inventory_unit_count": "入库库存单元数",
    "sold_inventory_unit_count": "已售库存单元数",
    "current_unsold_inventory_unit_count": "当前未售库存单元数",
    "average_days_to_sale": "平均售出周期",
    "event_count": "事件数",
    "unique_session_count": "去重会话数",
    "registered_user_count": "注册用户数",
}
_DIMENSION_LABELS = {
    "customer_state": "客户州/地区",
    "customer_city": "客户城市",
    "customer_traffic_source": "客户注册流量来源",
    "product_category": "商品品类",
    "product_brand": "商品品牌",
    "product_department": "商品部门",
    "distribution_center": "配送中心",
    "event_type": "事件类型",
    "event_browser": "浏览器",
    "event_traffic_source": "事件流量来源",
    "user_country": "用户国家",
    "user_traffic_source": "用户注册流量来源",
    "user_state": "用户注册州/地区",
}
_GRAIN_LABELS = {
    "day": "日",
    "week": "周",
    "month": "月",
    "quarter": "季度",
    "year": "年",
}


class TheLookV2EvaluationBuildError(ValueError):
    """The frozen v2 evaluation release cannot be trusted."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise TheLookV2EvaluationBuildError(
            "evaluation output must stay outside the Git worktree"
        )
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _metric_phrase(metrics: Iterable[str]) -> str:
    return "、".join(_METRIC_LABELS[metric] for metric in metrics)


def _period_text(spec: TheLookV2QuerySpec) -> str:
    if spec.time.mode == "all_time":
        return "全部可用时间"
    return f"{spec.time.start} 至 {spec.time.end_exclusive}（不含结束日）"


def question_variants(spec: TheLookV2QuerySpec) -> dict[str, str]:
    """Return five controlled Chinese surface forms for exactly one QuerySpec."""
    metrics = _metric_phrase(spec.metric_ids)
    period = _period_text(spec)
    if spec.result_shape == "scalar":
        texts = (
            f"请汇总{period}的{metrics}。",
            f"统计{period}内的{metrics}，按已冻结口径返回。",
            f"我想了解{period}的{metrics}。",
            f"{period}的{metrics}是多少？",
            f"请给出{period}范围内{metrics}的汇总结果。",
        )
    elif spec.result_shape == "dimension_grouped":
        dimension = _DIMENSION_LABELS[spec.dimension or ""]
        texts = (
            f"请按{dimension}统计{period}的{metrics}。",
            f"展示{period}内各{dimension}的{metrics}。",
            f"我想对比不同{dimension}在{period}的{metrics}。",
            f"按{dimension}拆分，{period}的{metrics}分别是多少？",
            f"请给出{period}按{dimension}汇总的{metrics}。",
        )
    else:
        grain = _GRAIN_LABELS[spec.time.grain or ""]
        texts = (
            f"请按{grain}展示{period}的{metrics}。",
            f"给出{period}内{metrics}的{grain}度时间序列。",
            f"我想查看{period}的{metrics}按{grain}汇总结果。",
            f"请按{grain}统计{period}内的{metrics}。",
            f"{period}的{metrics}按{grain}如何分布？",
        )
    return {f"v{index}": text for index, text in enumerate(texts, 1)}


def primary_variant_id(query_spec_id: str) -> str:
    bucket = int(hashlib.sha256(query_spec_id.encode("utf-8")).hexdigest(), 16) % 5 + 1
    return f"v{bucket}"


def _append_case(
    cases: list[dict[str, Any]],
    catalog: Catalog,
    *,
    metric_ids: tuple[str, ...],
    result_shape: str,
    time: TheLookQueryTime,
    dimension: str | None = None,
) -> None:
    spec = validate_thelook_v2_query_spec(
        TheLookV2QuerySpec.create(
            metric_ids=metric_ids,
            result_shape=result_shape,
            time=time,
            dimension=dimension,
        ),
        catalog,
    )
    cases.append({"query_spec": spec, "family_id": spec.query_spec_id})


def _all_time_or_year_windows() -> tuple[TheLookQueryTime, ...]:
    return (TheLookQueryTime("all_time"),) + tuple(
        TheLookQueryTime("absolute_range", start, end) for start, end in _YEAR_WINDOWS
    )


def build_cases() -> list[dict[str, Any]]:
    """Enumerate exactly 600 distinct, business-meaningful v2 plans.

    The count is a release cap, not a templating target: every block below adds
    a different fact domain, metric, shape, dimension, grain or time contract.
    Five Chinese surface forms are attached later and never multiply this count.
    """
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    cases: list[dict[str, Any]] = []
    full_windows = _all_time_or_year_windows()

    # 163 scalar cases: all first-class facts plus compatible multi-metric plans.
    for metric in _ALL_TIMED_METRICS:
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="scalar",
                time=query_time,
            )
    _append_case(
        cases,
        catalog,
        metric_ids=("current_unsold_inventory_unit_count",),
        result_shape="scalar",
        time=TheLookQueryTime("all_time"),
    )
    for metrics in (
        ("completed_order_count", "completed_customer_count"),
        ("average_order_value", "average_items_per_completed_order"),
        ("average_fulfillment_days", "average_dispatch_days", "average_transit_days"),
        ("returned_order_count", "return_rate"),
        ("cancelled_order_count", "completed_order_count"),
        ("completed_sale_amount", "completed_item_count"),
        ("sold_inventory_unit_count", "average_days_to_sale"),
        ("event_count", "unique_session_count"),
    ):
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=metrics,
                result_shape="scalar",
                time=query_time,
            )

    # 240 grouped single-metric cases across order, item, inventory, event and user facts.
    for metric in _ORDER_METRICS:
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="dimension_grouped",
                dimension="customer_traffic_source",
                time=query_time,
            )
        state_windows = (
            _DISPATCH_STATE_WINDOWS
            if metric == "average_dispatch_days"
            else _STATE_WINDOWS
        )
        for start, end in state_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="dimension_grouped",
                dimension="customer_state",
                time=TheLookQueryTime("absolute_range", start, end),
            )
    for metric in _ITEM_METRICS:
        for dimension in ("product_category", "product_department"):
            for query_time in full_windows:
                _append_case(
                    cases,
                    catalog,
                    metric_ids=(metric,),
                    result_shape="dimension_grouped",
                    dimension=dimension,
                    time=query_time,
                )
    for metric in (
        "received_inventory_unit_count",
        "sold_inventory_unit_count",
        "average_days_to_sale",
    ):
        for dimension in (
            "distribution_center",
            "product_category",
            "product_department",
        ):
            for query_time in full_windows:
                _append_case(
                    cases,
                    catalog,
                    metric_ids=(metric,),
                    result_shape="dimension_grouped",
                    dimension=dimension,
                    time=query_time,
                )
    for dimension in ("distribution_center", "product_category", "product_department"):
        _append_case(
            cases,
            catalog,
            metric_ids=("current_unsold_inventory_unit_count",),
            result_shape="dimension_grouped",
            dimension=dimension,
            time=TheLookQueryTime("all_time"),
        )
    for metric in _EVENT_METRICS:
        for dimension in _EVENT_DIMENSIONS:
            for query_time in full_windows:
                _append_case(
                    cases,
                    catalog,
                    metric_ids=(metric,),
                    result_shape="dimension_grouped",
                    dimension=dimension,
                    time=query_time,
                )
    for dimension in ("user_country", "user_traffic_source"):
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=("registered_user_count",),
                result_shape="dimension_grouped",
                dimension=dimension,
                time=query_time,
            )

    # 81 grouped multi-metric cases.  Each pair stays in one fact and time domain.
    order_pairs = (
        ("completed_order_count", "completed_customer_count"),
        ("average_order_value", "average_items_per_completed_order"),
        ("average_fulfillment_days", "average_dispatch_days", "average_transit_days"),
    )
    for metrics in order_pairs:
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=metrics,
                result_shape="dimension_grouped",
                dimension="customer_traffic_source",
                time=query_time,
            )
        state_windows = (
            _DISPATCH_STATE_WINDOWS
            if "average_dispatch_days" in metrics
            else _STATE_WINDOWS
        )
        for start, end in state_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=metrics,
                result_shape="dimension_grouped",
                dimension="customer_state",
                time=TheLookQueryTime("absolute_range", start, end),
            )
    for dimension in ("product_category", "product_department"):
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=("completed_sale_amount", "completed_item_count"),
                result_shape="dimension_grouped",
                dimension=dimension,
                time=query_time,
            )
    for dimension in ("distribution_center", "product_category", "product_department"):
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=("sold_inventory_unit_count", "average_days_to_sale"),
                result_shape="dimension_grouped",
                dimension=dimension,
                time=query_time,
            )
    for dimension in _EVENT_DIMENSIONS:
        for query_time in full_windows:
            _append_case(
                cases,
                catalog,
                metric_ids=("event_count", "unique_session_count"),
                result_shape="dimension_grouped",
                dimension=dimension,
                time=query_time,
            )

    # 116 time-series cases.  Together they cover day/week/month/quarter/year.
    # There are five time contracts for every order metric below.  The final
    # (year) contract is intentionally replaced by compatible multi-metric
    # series further down.  This retains 116 total time-series cases while
    # testing the renderer's same-fact CTE merge rather than treating every
    # trend as a one-metric template.
    order_series = (
        ("2019-01-01", "2020-01-01", "month"),
        ("2020-01-01", "2021-01-01", "month"),
        ("2021-01-01", "2022-01-01", "month"),
        ("2019-01-01", "2020-01-01", "quarter"),
        ("2020-01-01", "2021-01-01", "quarter"),
    )
    for metric in _ORDER_METRICS:
        for start, end, grain in order_series:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="time_series",
                time=TheLookQueryTime("series", start, end, grain),
            )

    # Eleven order-metric year-series were removed from ``order_series``.
    # Replace them one-for-one with business-meaningful, same-fact multi-
    # metric series.  Cross-fact combinations remain deliberately forbidden
    # by the QuerySpec contract.
    order_multi_series = (
        (
            ("completed_order_count", "completed_customer_count"),
            "2019-01-01",
            "2020-01-01",
            "month",
        ),
        (
            ("completed_order_count", "completed_customer_count"),
            "2020-01-01",
            "2021-01-01",
            "quarter",
        ),
        (
            ("completed_order_count", "completed_customer_count"),
            "2019-01-01",
            "2024-01-01",
            "year",
        ),
        (
            ("average_order_value", "average_items_per_completed_order"),
            "2019-01-01",
            "2020-01-01",
            "month",
        ),
        (
            ("average_order_value", "average_items_per_completed_order"),
            "2021-01-01",
            "2022-01-01",
            "quarter",
        ),
        (
            ("average_order_value", "average_items_per_completed_order"),
            "2019-01-01",
            "2024-01-01",
            "year",
        ),
        (
            (
                "average_fulfillment_days",
                "average_dispatch_days",
                "average_transit_days",
            ),
            "2020-01-01",
            "2021-01-01",
            "month",
        ),
        (
            (
                "average_fulfillment_days",
                "average_dispatch_days",
                "average_transit_days",
            ),
            "2021-01-01",
            "2022-01-01",
            "quarter",
        ),
        (
            (
                "average_fulfillment_days",
                "average_dispatch_days",
                "average_transit_days",
            ),
            "2019-01-01",
            "2024-01-01",
            "year",
        ),
        (("returned_order_count", "return_rate"), "2019-01-01", "2020-01-01", "month"),
        (("returned_order_count", "return_rate"), "2019-01-01", "2024-01-01", "year"),
    )
    for metrics, start, end, grain in order_multi_series:
        _append_case(
            cases,
            catalog,
            metric_ids=metrics,
            result_shape="time_series",
            time=TheLookQueryTime("series", start, end, grain),
        )

    # Completion/cancellation analysis is independent from the customer-volume,
    # basket, fulfillment and return series above.  These six plans replace
    # high-cardinality event-state groups without duplicating a single-metric
    # date template.
    for start, end, grain in (
        ("2019-01-01", "2020-01-01", "month"),
        ("2020-01-01", "2021-01-01", "month"),
        ("2021-01-01", "2022-01-01", "month"),
        ("2019-01-01", "2020-01-01", "quarter"),
        ("2020-01-01", "2021-01-01", "quarter"),
        ("2019-01-01", "2024-01-01", "year"),
    ):
        _append_case(
            cases,
            catalog,
            metric_ids=("cancelled_order_count", "completed_order_count"),
            result_shape="time_series",
            time=TheLookQueryTime("series", start, end, grain),
        )
    for metric in _ITEM_METRICS:
        for start, end in _YEAR_WINDOWS:
            for grain in ("month", "quarter"):
                _append_case(
                    cases,
                    catalog,
                    metric_ids=(metric,),
                    result_shape="time_series",
                    time=TheLookQueryTime("series", start, end, grain),
                )
    inventory_series = (
        ("2019-01-01", "2020-01-01", "month"),
        ("2020-01-01", "2021-01-01", "month"),
        ("2021-01-01", "2022-01-01", "quarter"),
        ("2022-01-01", "2023-01-01", "quarter"),
    )
    for metric in _INVENTORY_TIMED_METRICS:
        for start, end, grain in inventory_series:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="time_series",
                time=TheLookQueryTime("series", start, end, grain),
            )
    for start, end, grain in (
        ("2019-01-01", "2020-01-01", "month"),
        ("2020-01-01", "2021-01-01", "month"),
        ("2021-01-01", "2022-01-01", "quarter"),
        ("2022-01-01", "2023-01-01", "quarter"),
        ("2023-01-01", "2024-01-01", "quarter"),
        ("2019-01-01", "2024-01-01", "year"),
    ):
        _append_case(
            cases,
            catalog,
            metric_ids=("sold_inventory_unit_count", "average_days_to_sale"),
            result_shape="time_series",
            time=TheLookQueryTime("series", start, end, grain),
        )
    event_series = (
        ("2019-01-01", "2020-01-01", "month"),
        ("2020-01-01", "2021-01-01", "month"),
        ("2021-01-01", "2022-01-01", "month"),
        ("2021-01-01", "2022-01-01", "quarter"),
        ("2022-01-01", "2023-01-01", "quarter"),
        ("2019-01-01", "2024-01-01", "year"),
    )
    for metric in _EVENT_METRICS:
        for start, end, grain in event_series:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="time_series",
                time=TheLookQueryTime("series", start, end, grain),
            )
    for start, end, grain in event_series:
        _append_case(
            cases,
            catalog,
            metric_ids=("event_count", "unique_session_count"),
            result_shape="time_series",
            time=TheLookQueryTime("series", start, end, grain),
        )
    user_series = (
        ("2019-01-01", "2019-04-01", "day"),
        ("2019-01-01", "2019-04-01", "week"),
        ("2019-01-01", "2020-01-01", "month"),
        ("2020-01-01", "2021-01-01", "month"),
        ("2021-01-01", "2022-01-01", "quarter"),
        ("2019-01-01", "2024-01-01", "year"),
    )
    for start, end, grain in user_series:
        _append_case(
            cases,
            catalog,
            metric_ids=("registered_user_count",),
            result_shape="time_series",
            time=TheLookQueryTime("series", start, end, grain),
        )

    if len(cases) != EXPECTED_CASES:
        raise AssertionError(f"expected {EXPECTED_CASES} cases, got {len(cases)}")
    query_spec_ids = [case["query_spec"].query_spec_id for case in cases]
    family_ids = [case["family_id"] for case in cases]
    if len(set(query_spec_ids)) != len(cases) or len(set(family_ids)) != len(cases):
        raise AssertionError("v2 evaluation QuerySpecs and families must be unique")
    return cases


def _execute_as_reader(sql: str) -> pd.DataFrame:
    connection = psycopg2.connect(
        host="/tmp", port=35434, database="thelook_analytics", user="postgres"
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SET LOCAL ROLE daa_thelook_reader")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(sql)
            rows = [dict(row) for row in cursor.fetchmany(200)]
    finally:
        connection.close()
    return pd.DataFrame(rows)


def _validate_execution(
    spec: TheLookV2QuerySpec,
    frame: pd.DataFrame,
    metric_constraints: dict[str, dict[str, float | bool]],
) -> dict[str, Any]:
    validation = ResultValidator(max_rows=200).validate(
        frame,
        required_columns=spec.required_result_columns,
        metric_columns=spec.metric_ids,
        time_column="time" if spec.result_shape == "time_series" else None,
        time_bucket_grain=spec.time.grain,
        requested_start=spec.time.start,
        requested_end=spec.time.end_exclusive,
        exact_columns=True,
        metric_value_constraints=metric_constraints,
    )
    if not validation.safe_to_answer:
        raise TheLookV2EvaluationBuildError(
            f"{spec.query_spec_id} failed ResultValidator: {validation.reason}"
        )
    return validation.as_dict()


def materialize(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    execute: Callable[[str], pd.DataFrame] = _execute_as_reader,
) -> dict[str, Any]:
    """Admit every v2 case and atomically write only to a new external directory."""
    output_path = _external_new_dir(output_dir)
    cases = build_cases()
    if len(cases) > MAX_CASES:
        raise TheLookV2EvaluationBuildError("case count exceeds frozen release cap")
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        spec = validate_thelook_v2_query_spec(case["query_spec"], catalog)
        artifact = render_thelook_v2_gold_sql(spec, catalog)
        decision = policy.evaluate(artifact.sql)
        frame = execute(decision.final_sql)
        constraints = {
            metric: dict(catalog.metrics_by_id[metric].result_value_constraints)
            for metric in spec.metric_ids
        }
        validation = _validate_execution(spec, frame, constraints)
        variants = question_variants(spec)
        variant_id = primary_variant_id(spec.query_spec_id)
        records.append(
            {
                "case_id": f"thelook-final-v2-{index:03d}",
                "split": "cross_schema_final_test",
                "language": QUESTION_LANGUAGE,
                "question_variant_version": QUESTION_VARIANT_VERSION,
                "primary_variant_id": variant_id,
                "question": variants[variant_id],
                "question_variants": variants,
                "family_id": case["family_id"],
                "query_spec": spec.as_dict(),
                "gold_sql": artifact.sql,
                "gold_sql_sha256": artifact.sql_sha256,
                "renderer_version": artifact.renderer_version,
                "required_result_columns": list(spec.required_result_columns),
                "execution": {
                    "policy_status": decision.status,
                    "policy_limit_applied": decision.policy_limit_applied,
                    "reader_role": THELOOK_V2_WORKSPACE.reader_role,
                    "result_validation": validation,
                },
            }
        )

    generated_at = (
        generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        cases_path = staging / "cases.jsonl"
        with cases_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
        shape_counts = Counter(row["query_spec"]["result_shape"] for row in records)
        fact_counts = Counter(
            METRIC_FACT_DOMAINS[metric]
            for row in records
            for metric in row["query_spec"]["metric_ids"]
        )
        grain_counts = Counter(
            row["query_spec"]["time"]["grain"]
            for row in records
            if row["query_spec"]["time"]["grain"] is not None
        )
        manifest = {
            "evaluation_version": EVALUATION_VERSION,
            "generated_at": generated_at,
            "workspace": records[0]["query_spec"]["workspace"],
            "source": {
                "catalog_path": str(
                    THELOOK_V2_WORKSPACE.catalog_path.relative_to(ROOT)
                ),
                "snapshot_manifest_sha256": sha256_file(
                    Path("/disk2/gengnan/data-analysis-agent-data/datasets/thelook/")
                    / "kaggle-mirror-v1-20260908/audit/snapshot_manifest.json"
                ),
            },
            "output": {
                "cases_jsonl": {"rows": len(records), "sha256": sha256_file(cases_path)}
            },
            "coverage": {
                "result_shapes": dict(sorted(shape_counts.items())),
                "fact_domains": dict(sorted(fact_counts.items())),
                "metrics": sorted(
                    {
                        metric
                        for row in records
                        for metric in row["query_spec"]["metric_ids"]
                    }
                ),
                "dimensions": sorted(
                    {
                        row["query_spec"]["dimension"]
                        for row in records
                        if row["query_spec"]["dimension"]
                    }
                ),
                "time_grains": dict(sorted(grain_counts.items())),
                "multi_metric_cases": sum(
                    len(row["query_spec"]["metric_ids"]) > 1 for row in records
                ),
                "five_surface_variants_per_query_spec": True,
            },
            "checks": {
                "all_cases_queryspec_renderer_policy_reader_result_contract": True,
                "case_ids_unique": len({row["case_id"] for row in records})
                == len(records),
                "query_spec_ids_unique": len(
                    {row["query_spec"]["query_spec_id"] for row in records}
                )
                == len(records),
                "family_ids_unique": len({row["family_id"] for row in records})
                == len(records),
                "five_question_variants_present": all(
                    len(row["question_variants"]) == 5 for row in records
                ),
                "olist_training_or_prompt_input": False,
                "base_or_adapter_generation_run": False,
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    args = parse_args()
    manifest = materialize(args.output_dir, generated_at=args.generated_at)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
