"""Deterministic PostgreSQL Gold SQL renderer for TheLook v2 QuerySpecs.

The renderer has no database connection and no natural-language handling.  It
compiles only a previously validated offline plan, leaving AST policy, reader
role and ResultValidator as independent downstream gates.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Any, Mapping

from .semantic_catalog import Catalog
from .thelook_queryspec import TheLookQueryTime
from .thelook_v2_queryspec import (
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)


RENDERER_VERSION = "thelook-postgres-gold-renderer-v2"


@dataclass(frozen=True)
class TheLookV2MetricSqlDefinition:
    metric_id: str
    expression: str
    filters: tuple[str, ...]
    fact_domain: str
    order_level: bool = False
    inner_expression: str | None = None
    inner_alias: str | None = None


_METRICS: Mapping[str, TheLookV2MetricSqlDefinition] = MappingProxyType(
    {
        "completed_sale_amount": TheLookV2MetricSqlDefinition(
            "completed_sale_amount",
            "SUM(oi.sale_price)",
            ("o.status = 'Complete'",),
            "order_items",
        ),
        "completed_item_count": TheLookV2MetricSqlDefinition(
            "completed_item_count",
            "COUNT(oi.id)",
            ("o.status = 'Complete'",),
            "order_items",
        ),
        "completed_order_count": TheLookV2MetricSqlDefinition(
            "completed_order_count",
            "COUNT(o.order_id)",
            ("o.status = 'Complete'",),
            "orders",
        ),
        "average_order_value": TheLookV2MetricSqlDefinition(
            "average_order_value",
            "AVG(order_values.order_value)",
            ("o.status = 'Complete'",),
            "orders",
            order_level=True,
            inner_expression="SUM(oi.sale_price)",
            inner_alias="order_value",
        ),
        "average_items_per_completed_order": TheLookV2MetricSqlDefinition(
            "average_items_per_completed_order",
            "AVG(order_item_counts.item_count)",
            ("o.status = 'Complete'",),
            "orders",
            order_level=True,
            inner_expression="COUNT(oi.id)",
            inner_alias="item_count",
        ),
        "cancelled_order_count": TheLookV2MetricSqlDefinition(
            "cancelled_order_count",
            "COUNT(o.order_id)",
            ("o.status = 'Cancelled'",),
            "orders",
        ),
        "returned_order_count": TheLookV2MetricSqlDefinition(
            "returned_order_count",
            "COUNT(o.order_id)",
            ("o.status = 'Returned'",),
            "orders",
        ),
        "average_fulfillment_days": TheLookV2MetricSqlDefinition(
            "average_fulfillment_days",
            "AVG(EXTRACT(EPOCH FROM (o.delivered_at - o.created_at)) / 86400.0)",
            (
                "o.status = 'Complete'",
                "o.delivered_at IS NOT NULL",
                "o.delivered_at >= o.created_at",
            ),
            "orders",
        ),
        "average_dispatch_days": TheLookV2MetricSqlDefinition(
            "average_dispatch_days",
            "AVG(EXTRACT(EPOCH FROM (o.shipped_at - o.created_at)) / 86400.0)",
            ("o.shipped_at IS NOT NULL", "o.shipped_at >= o.created_at"),
            "orders",
        ),
        "average_transit_days": TheLookV2MetricSqlDefinition(
            "average_transit_days",
            "AVG(EXTRACT(EPOCH FROM (o.delivered_at - o.shipped_at)) / 86400.0)",
            (
                "o.shipped_at IS NOT NULL",
                "o.delivered_at IS NOT NULL",
                "o.delivered_at >= o.shipped_at",
            ),
            "orders",
        ),
        "average_post_delivery_return_days": TheLookV2MetricSqlDefinition(
            "average_post_delivery_return_days",
            "AVG(EXTRACT(EPOCH FROM (o.returned_at - o.delivered_at)) / 86400.0)",
            (
                "o.returned_at IS NOT NULL",
                "o.delivered_at IS NOT NULL",
                "o.returned_at >= o.delivered_at",
            ),
            "orders",
        ),
        "return_rate": TheLookV2MetricSqlDefinition(
            "return_rate",
            "AVG(CASE WHEN o.status = 'Returned' THEN 1.0 ELSE 0.0 END)",
            ("o.status IN ('Complete', 'Returned')",),
            "orders",
        ),
        "completed_customer_count": TheLookV2MetricSqlDefinition(
            "completed_customer_count",
            "COUNT(DISTINCT o.user_id)",
            ("o.status = 'Complete'",),
            "orders",
        ),
        "received_inventory_unit_count": TheLookV2MetricSqlDefinition(
            "received_inventory_unit_count",
            "COUNT(ii.id)",
            ("ii.created_at IS NOT NULL",),
            "inventory_received",
        ),
        "sold_inventory_unit_count": TheLookV2MetricSqlDefinition(
            "sold_inventory_unit_count",
            "COUNT(ii.id)",
            ("ii.sold_at IS NOT NULL",),
            "inventory_sold",
        ),
        "current_unsold_inventory_unit_count": TheLookV2MetricSqlDefinition(
            "current_unsold_inventory_unit_count",
            "COUNT(ii.id)",
            ("ii.sold_at IS NULL",),
            "inventory_snapshot",
        ),
        "average_days_to_sale": TheLookV2MetricSqlDefinition(
            "average_days_to_sale",
            "AVG(EXTRACT(EPOCH FROM (ii.sold_at - ii.created_at)) / 86400.0)",
            ("ii.sold_at IS NOT NULL", "ii.sold_at >= ii.created_at"),
            "inventory_sold",
        ),
        "event_count": TheLookV2MetricSqlDefinition(
            "event_count", "COUNT(e.id)", ("e.created_at IS NOT NULL",), "events"
        ),
        "unique_session_count": TheLookV2MetricSqlDefinition(
            "unique_session_count",
            "COUNT(DISTINCT e.session_id)",
            ("e.created_at IS NOT NULL",),
            "events",
        ),
        "registered_user_count": TheLookV2MetricSqlDefinition(
            "registered_user_count",
            "COUNT(u.id)",
            ("u.created_at IS NOT NULL",),
            "users",
        ),
    }
)

_DIMENSION_EXPRESSIONS: Mapping[str, str] = MappingProxyType(
    {
        "customer_state": "u.state",
        "customer_city": "u.city",
        "customer_traffic_source": "u.traffic_source",
        "product_category": "p.category",
        "product_brand": "p.brand",
        "product_department": "p.department",
        "distribution_center": "dc.name",
        "event_type": "e.event_type",
        "event_browser": "e.browser",
        "event_traffic_source": "e.traffic_source",
        "user_country": "u.country",
        "user_traffic_source": "u.traffic_source",
        "user_state": "u.state",
    }
)

_TIME_EXPRESSIONS: Mapping[str, str] = MappingProxyType(
    {
        "order_items": "o.created_at",
        "orders": "o.created_at",
        "inventory_received": "ii.created_at",
        "inventory_sold": "ii.sold_at",
        "events": "e.created_at",
        "users": "u.created_at",
    }
)


@dataclass(frozen=True)
class TheLookV2GoldSqlArtifact:
    query_spec_id: str
    sql: str
    sql_sha256: str
    renderer_version: str
    metric_ids: tuple[str, ...]
    join_program_id: str
    required_result_columns: tuple[str, ...]
    evidence: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_spec_id": self.query_spec_id,
            "sql_sha256": self.sql_sha256,
            "renderer_version": self.renderer_version,
            "metric_ids": list(self.metric_ids),
            "join_program_id": self.join_program_id,
            "required_result_columns": list(self.required_result_columns),
            "evidence": dict(self.evidence),
        }


def _time_expression(domain: str) -> str:
    try:
        return _TIME_EXPRESSIONS[domain]
    except KeyError as exc:
        raise AssertionError(
            f"v2 metric domain has no time expression: {domain}"
        ) from exc


def _time_filters(domain: str, query_time: TheLookQueryTime) -> list[str]:
    if query_time.start is None:
        return []
    field = _time_expression(domain)
    return [
        f"{field} >= TIMESTAMP '{query_time.start}'",
        f"{field} < TIMESTAMP '{query_time.end_exclusive}'",
    ]


def _where(filters: list[str]) -> str:
    return " AND ".join(filters) if filters else "TRUE"


def _base_from(domain: str, dimension: str | None, *, order_level: bool = False) -> str:
    if domain == "orders":
        joins: list[str] = []
        # Order-level averages are calculated from order_items first (one
        # value per order) before their outer AVG.  They therefore need the
        # item fact even though their public fact domain, time contract and
        # grouping key remain ``orders``.
        if order_level:
            joins.append("JOIN analytics.order_items AS oi ON o.order_id = oi.order_id")
        if dimension and dimension.startswith("customer_"):
            joins.append("JOIN analytics.users AS u ON o.user_id = u.id")
        return "FROM analytics.orders AS o" + (" " + " ".join(joins) if joins else "")
    if domain == "order_items":
        joins = ["JOIN analytics.order_items AS oi ON o.order_id = oi.order_id"]
        if dimension and dimension.startswith("customer_"):
            joins.append("JOIN analytics.users AS u ON o.user_id = u.id")
        if dimension and dimension.startswith("product_"):
            joins.append("JOIN analytics.products AS p ON oi.product_id = p.id")
        return "FROM analytics.orders AS o " + " ".join(joins)
    if domain.startswith("inventory"):
        joins = []
        if dimension and dimension.startswith("product_"):
            joins.append("JOIN analytics.products AS p ON ii.product_id = p.id")
        if dimension == "distribution_center":
            joins.append(
                "JOIN analytics.distribution_centers AS dc ON ii.product_distribution_center_id = dc.id"
            )
        return "FROM analytics.inventory_items AS ii" + (
            " " + " ".join(joins) if joins else ""
        )
    if domain == "events":
        return "FROM analytics.events AS e"
    if domain == "users":
        return "FROM analytics.users AS u"
    raise AssertionError(f"unsupported v2 metric domain: {domain}")


def _group_parts(spec: TheLookV2QuerySpec, domain: str) -> tuple[str, ...]:
    parts: list[str] = []
    if spec.result_shape == "dimension_grouped":
        parts.append(_DIMENSION_EXPRESSIONS[spec.dimension or ""])
    if spec.result_shape == "time_series":
        parts.append(f"date_trunc('{spec.time.grain}', {_time_expression(domain)})")
    return tuple(parts)


def _metric_ctes(
    metric_id: str, spec: TheLookV2QuerySpec, index: int
) -> tuple[str, ...]:
    definition = _METRICS[metric_id]
    domain = definition.fact_domain
    dimension = spec.dimension if spec.result_shape == "dimension_grouped" else None
    group_parts = _group_parts(spec, domain)
    select_groups: list[str] = []
    if dimension:
        select_groups.append(f"{group_parts[0]} AS {dimension}")
    if spec.result_shape == "time_series":
        select_groups.append(f"{group_parts[-1]} AS time")
    filters = [*definition.filters, *_time_filters(domain, spec.time)]
    if dimension:
        filters.append(f"{_DIMENSION_EXPRESSIONS[dimension]} IS NOT NULL")
    group_by = ", ".join(str(position) for position in range(1, len(select_groups) + 1))
    base_from = _base_from(domain, dimension, order_level=definition.order_level)

    if definition.order_level:
        if definition.inner_expression is None or definition.inner_alias is None:
            raise AssertionError(f"order-level definition incomplete: {metric_id}")
        inner_groups = ["o.order_id", *group_parts]
        inner_select = [
            "o.order_id",
            f"{definition.inner_expression} AS {definition.inner_alias}",
        ]
        if dimension:
            inner_select.insert(1, f"{group_parts[0]} AS {dimension}")
        if spec.result_shape == "time_series":
            inner_select.insert(1, f"{group_parts[-1]} AS time")
        inner_sql = (
            f"SELECT {', '.join(inner_select)} {base_from} WHERE {_where(filters)} "
            f"GROUP BY {', '.join(inner_groups)}"
        )
        inner_name = f"tlv2_{definition.inner_alias}_{index:02d}"
        outer_alias = (
            "order_values"
            if definition.inner_alias == "order_value"
            else "order_item_counts"
        )
        outer_source = f"{inner_name} AS {outer_alias}"
        outer_groups = [
            *([dimension] if dimension else []),
            *(["time"] if spec.result_shape == "time_series" else []),
        ]
        outer_select = [*outer_groups, f"{definition.expression} AS {metric_id}"]
        outer_sql = f"SELECT {', '.join(outer_select)} FROM {outer_source}"
        if outer_groups:
            outer_sql += f" GROUP BY {', '.join(outer_groups)}"
        # Keep the order-grain stage as a top-level CTE.  SqlPolicy can then
        # prove its exported alias and reject unknown columns without treating
        # a nested WITH body as opaque.
        return (
            f"{inner_name} AS ({inner_sql})",
            f"m{index:02d}_{metric_id} AS ({outer_sql})",
        )

    select_parts = [*select_groups, f"{definition.expression} AS {metric_id}"]
    sql = f"m{index:02d}_{metric_id} AS (SELECT {', '.join(select_parts)} {base_from} WHERE {_where(filters)}"
    if group_parts:
        sql += f" GROUP BY {group_by}"
    return (sql + ")",)


def _final_select(spec: TheLookV2QuerySpec) -> str:
    names = [
        f"m{index:02d}_{metric}" for index, metric in enumerate(spec.metric_ids, 1)
    ]
    if spec.result_shape == "scalar":
        columns = [
            f"{name}.{metric} AS {metric}"
            for name, metric in zip(names, spec.metric_ids)
        ]
        return f"SELECT {', '.join(columns)} FROM " + " CROSS JOIN ".join(names)
    key = spec.dimension if spec.result_shape == "dimension_grouped" else "time"
    columns = [
        f"{name}.{metric} AS {metric}" for name, metric in zip(names, spec.metric_ids)
    ]
    if len(names) == 1:
        key_expression = f"{names[0]}.{key} AS {key}"
    else:
        key_expression = (
            "COALESCE(" + ", ".join(f"{name}.{key}" for name in names) + f") AS {key}"
        )
    if spec.result_shape == "dimension_grouped":
        columns.insert(0, key_expression)
    else:
        columns.append(key_expression)
    sql = f"SELECT {', '.join(columns)} FROM {names[0]}"
    for name in names[1:]:
        sql += f" FULL OUTER JOIN {name} USING ({key})"
    return sql


def render_thelook_v2_gold_sql(
    spec: TheLookV2QuerySpec, catalog: Catalog | None = None
) -> TheLookV2GoldSqlArtifact:
    """Compile one validated v2 plan into canonical, side-effect-free SQL."""
    validated = validate_thelook_v2_query_spec(spec, catalog)
    for metric in validated.metric_ids:
        if (
            metric not in _METRICS
            or METRIC_FACT_DOMAINS[metric] != _METRICS[metric].fact_domain
        ):
            raise AssertionError(f"Catalog/renderer metric registry drift: {metric}")
    ctes = tuple(
        cte
        for index, metric in enumerate(validated.metric_ids, 1)
        for cte in _metric_ctes(metric, validated, index)
    )
    sql = "WITH " + ", ".join(ctes) + " " + _final_select(validated) + ";"
    sql_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    evidence = MappingProxyType(
        {
            "schema_version": validated.schema_version,
            "workspace": validated.workspace.as_dict(),
            "query_spec_id": validated.query_spec_id,
            "metric_ids": list(validated.metric_ids),
            "join_program_id": validated.join_program_id,
            "required_result_columns": list(validated.required_result_columns),
            "renderer_version": RENDERER_VERSION,
            "sql_sha256": sql_sha256,
        }
    )
    return TheLookV2GoldSqlArtifact(
        query_spec_id=validated.query_spec_id,
        sql=sql,
        sql_sha256=sql_sha256,
        renderer_version=RENDERER_VERSION,
        metric_ids=validated.metric_ids,
        join_program_id=validated.join_program_id,
        required_result_columns=validated.required_result_columns,
        evidence=evidence,
    )
