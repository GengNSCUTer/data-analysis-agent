"""Deterministic PostgreSQL Gold SQL renderer for TheLook QuerySpecs.

The renderer is intentionally offline and side-effect free.  It consumes only
an already validated :class:`TheLookQuerySpec` and fixed metric fragments.  It
does not parse questions, call an LLM, connect to PostgreSQL, or bypass the
runtime SQL Policy and ResultValidator.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Any, Mapping

from .semantic_catalog import Catalog
from .thelook_queryspec import (
    TheLookQuerySpec,
    TheLookQueryTime,
    validate_thelook_query_spec,
)


RENDERER_VERSION = "thelook-postgres-gold-renderer-v1"


@dataclass(frozen=True)
class TheLookMetricSqlDefinition:
    metric_id: str
    expression: str
    filters: tuple[str, ...]
    needs_order_items: bool = False
    needs_users: bool = False
    needs_products: bool = False
    order_level: bool = False


_METRICS: Mapping[str, TheLookMetricSqlDefinition] = MappingProxyType(
    {
        "completed_sale_amount": TheLookMetricSqlDefinition(
            "completed_sale_amount",
            "SUM(oi.sale_price)",
            ("o.status = 'Complete'",),
            needs_order_items=True,
        ),
        "completed_order_count": TheLookMetricSqlDefinition(
            "completed_order_count", "COUNT(o.order_id)", ("o.status = 'Complete'",)
        ),
        "average_order_value": TheLookMetricSqlDefinition(
            "average_order_value",
            "AVG(order_totals.order_total)",
            ("o.status = 'Complete'",),
            needs_order_items=True,
            order_level=True,
        ),
        "average_fulfillment_days": TheLookMetricSqlDefinition(
            "average_fulfillment_days",
            "AVG(EXTRACT(EPOCH FROM (o.delivered_at - o.created_at)) / 86400.0)",
            (
                "o.status = 'Complete'",
                "o.created_at IS NOT NULL",
                "o.delivered_at IS NOT NULL",
            ),
        ),
        "return_rate": TheLookMetricSqlDefinition(
            "return_rate",
            "AVG(CASE WHEN o.status = 'Returned' THEN 1.0 ELSE 0.0 END)",
            ("o.status IN ('Complete', 'Returned')",),
        ),
        "completed_customer_count": TheLookMetricSqlDefinition(
            "completed_customer_count",
            "COUNT(DISTINCT o.user_id)",
            ("o.status = 'Complete'",),
        ),
    }
)

_DIMENSION_EXPRESSIONS = MappingProxyType(
    {
        "state": "u.state",
        "city": "u.city",
        "traffic_source": "u.traffic_source",
        "category": "p.category",
        "brand": "p.brand",
        "department": "p.department",
    }
)


@dataclass(frozen=True)
class TheLookGoldSqlArtifact:
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


def _time_filters(definition: TheLookMetricSqlDefinition, query_time: TheLookQueryTime) -> list[str]:
    filters = list(definition.filters)
    if query_time.start is not None:
        filters.extend(
            [
                f"o.created_at >= TIMESTAMP '{query_time.start}'",
                f"o.created_at < TIMESTAMP '{query_time.end_exclusive}'",
            ]
        )
    return filters


def _where(filters: list[str]) -> str:
    return " AND ".join(filters) if filters else "TRUE"


def _group_parts(spec: TheLookQuerySpec) -> tuple[str, ...]:
    parts: list[str] = []
    if spec.result_shape == "dimension_grouped":
        parts.append(_DIMENSION_EXPRESSIONS[spec.dimension or ""])
    if spec.result_shape == "time_series":
        parts.append(f"date_trunc('{spec.time.grain}', o.created_at)")
    return tuple(parts)


def _joins(definition: TheLookMetricSqlDefinition, dimension: str | None) -> str:
    clauses: list[str] = []
    if definition.needs_order_items or dimension in {"category", "brand", "department"}:
        clauses.append("JOIN analytics.order_items AS oi ON o.order_id = oi.order_id")
    if definition.needs_users or dimension in {"state", "city", "traffic_source"}:
        clauses.append("JOIN analytics.users AS u ON o.user_id = u.id")
    if dimension in {"category", "brand", "department"}:
        clauses.append("JOIN analytics.products AS p ON oi.product_id = p.id")
    return " " + " ".join(clauses) if clauses else ""


def _metric_cte(metric_id: str, spec: TheLookQuerySpec, index: int) -> str:
    definition = _METRICS[metric_id]
    dimension = spec.dimension if spec.result_shape == "dimension_grouped" else None
    group_parts = _group_parts(spec)
    grouped = bool(group_parts)
    select_groups = []
    if dimension:
        select_groups.append(f"{group_parts[0]} AS {dimension}")
    if spec.result_shape == "time_series":
        select_groups.append(f"{group_parts[-1]} AS time")
    group_by = ", ".join(str(i) for i in range(1, len(select_groups) + 1))
    filters = _time_filters(definition, spec.time)
    if dimension:
        filters.append(f"{_DIMENSION_EXPRESSIONS[dimension]} IS NOT NULL")

    if definition.order_level:
        inner_groups = ["o.order_id", *group_parts]
        inner_select = ["o.order_id", "SUM(oi.sale_price) AS order_total"]
        if dimension:
            inner_select.insert(1, f"{group_parts[0]} AS {dimension}")
        if spec.result_shape == "time_series":
            inner_select.insert(1, f"{group_parts[-1]} AS time")
        inner_sql = (
            f"SELECT {', '.join(inner_select)} FROM analytics.orders AS o"
            f"{_joins(definition, dimension)} WHERE {_where(filters)}"
            f" GROUP BY {', '.join(inner_groups)}"
        )
        outer_name = f"tl_aov_orders_{index:02d}"
        outer_groups = [*([dimension] if dimension else []), *(["time"] if spec.result_shape == "time_series" else [])]
        outer_select = [*outer_groups, f"AVG(order_total) AS {metric_id}"]
        outer_sql = f"SELECT {', '.join(outer_select)} FROM {outer_name}"
        if outer_groups:
            outer_sql += f" GROUP BY {', '.join(outer_groups)}"
        return f"m{index:02d}_{metric_id} AS (WITH {outer_name} AS ({inner_sql}) {outer_sql})"

    select_parts = [*select_groups, f"{definition.expression} AS {metric_id}"]
    sql = (
        f"m{index:02d}_{metric_id} AS (SELECT {', '.join(select_parts)} "
        f"FROM analytics.orders AS o{_joins(definition, dimension)} WHERE {_where(filters)}"
    )
    if grouped:
        sql += f" GROUP BY {group_by}"
    return sql + ")"


def _final_select(spec: TheLookQuerySpec) -> str:
    names = [f"m{index:02d}_{metric}" for index, metric in enumerate(spec.metric_ids, 1)]
    if spec.result_shape == "scalar":
        columns = [f"{name}.{metric} AS {metric}" for name, metric in zip(names, spec.metric_ids)]
        return f"SELECT {', '.join(columns)} FROM " + " CROSS JOIN ".join(names)

    key = spec.dimension if spec.result_shape == "dimension_grouped" else "time"
    columns: list[str] = [
        f"{name}.{metric} AS {metric}" for name, metric in zip(names, spec.metric_ids)
    ]
    if len(names) == 1:
        key_expression = f"{names[0]}.{key} AS {key}"
    else:
        key_expression = (
            f"COALESCE({names[0]}.{key}, "
            + ", ".join(f"{name}.{key}" for name in names[1:])
            + f") AS {key}"
        )
    # Keep the public order identical to QuerySpec.required_result_columns:
    # dimension-first for grouped results, metric-first then time for series.
    if spec.result_shape == "dimension_grouped":
        columns.insert(0, key_expression)
    else:
        columns.append(key_expression)
    sql = f"SELECT {', '.join(columns)} FROM {names[0]}"
    for name in names[1:]:
        sql += f" FULL OUTER JOIN {name} USING ({key})"
    return sql


def render_thelook_gold_sql(
    spec: TheLookQuerySpec, catalog: Catalog | None = None
) -> TheLookGoldSqlArtifact:
    """Compile a validated TheLook QuerySpec into canonical PostgreSQL SQL."""
    validated = validate_thelook_query_spec(spec, catalog)
    sql = "WITH " + ", ".join(
        _metric_cte(metric, validated, index)
        for index, metric in enumerate(validated.metric_ids, 1)
    ) + " " + _final_select(validated) + ";"
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
    return TheLookGoldSqlArtifact(
        query_spec_id=validated.query_spec_id,
        sql=sql,
        sql_sha256=sql_sha256,
        renderer_version=RENDERER_VERSION,
        metric_ids=validated.metric_ids,
        join_program_id=validated.join_program_id,
        required_result_columns=validated.required_result_columns,
        evidence=evidence,
    )
