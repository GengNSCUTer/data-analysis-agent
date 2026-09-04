"""Offline Olist QuerySpec validation and deterministic PostgreSQL Gold SQL.

This module is deliberately separate from the online Agent runtime.  A
``QuerySpec`` is a reviewed construction artifact, not a natural-language
parser result and not a SQL string.  The renderer consumes only validated
identifiers and fixed registry fragments; it never executes SQL or calls a
model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from .candidate_sql_generator import OLIST_CANDIDATE_SQL_PROMPT_VERSION
from .metric_context import OLIST_WORKSPACE
from .semantic_catalog import Catalog, CatalogLoader


QUERY_SPEC_SCHEMA_VERSION = "olist-query-spec-v1"
RENDERER_VERSION = "olist-postgres-gold-renderer-v1"
_MAX_METRICS = 4
_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})
_SHAPES = frozenset({"scalar", "state_grouped", "category_grouped", "time_series"})
_ITEM_METRICS = frozenset({"gmv", "item_count", "freight_amount"})
_REVIEW_METRICS = frozenset({"positive_review_rate", "average_review_score"})
_PURCHASE_METRICS = frozenset(
    {
        "gmv",
        "item_count",
        "freight_amount",
        "paid_order_count",
        "average_delivery_days",
        "average_order_value",
        "on_time_delivery_rate",
        "cancellation_rate",
    }
)
_DIMENSION_FOR_SHAPE = {
    "scalar": None,
    "state_grouped": "customer_state",
    "category_grouped": "product_category_name",
    "time_series": None,
}
_SENSITIVE_DIMENSIONS = frozenset({"seller_id", "seller_city", "seller_state"})
_ATTRIBUTION_DIMENSIONS = frozenset({"payment_type"})
_CANONICAL_DIMENSION_EXPRESSIONS = {
    "customer_state": "c.customer_state",
    "product_category_name": "p.product_category_name",
}


class QuerySpecValidationError(ValueError):
    """A QuerySpec violates the frozen offline construction contract."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class WorkspacePin:
    workspace_id: str
    catalog_version: str
    dataset_version: str
    metric_version: str
    policy_version: str
    prompt_version: str
    dialect: str = "postgres"

    @classmethod
    def current(cls) -> "WorkspacePin":
        return cls(
            workspace_id=OLIST_WORKSPACE.workspace_id,
            catalog_version=OLIST_WORKSPACE.catalog_version,
            dataset_version=OLIST_WORKSPACE.dataset_version,
            metric_version=OLIST_WORKSPACE.metric_version,
            policy_version=OLIST_WORKSPACE.policy_version,
            prompt_version=OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            dialect=OLIST_WORKSPACE.sql_dialect,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "catalog_version": self.catalog_version,
            "dataset_version": self.dataset_version,
            "metric_version": self.metric_version,
            "policy_version": self.policy_version,
            "prompt_version": self.prompt_version,
            "dialect": self.dialect,
        }


@dataclass(frozen=True)
class QueryTime:
    mode: Literal["all_time", "absolute_range", "series"]
    start: str | None = None
    end_exclusive: str | None = None
    grain: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "start": self.start,
            "end_exclusive": self.end_exclusive,
            "grain": self.grain,
        }


@dataclass(frozen=True)
class QuerySpec:
    schema_version: str
    query_spec_id: str
    workspace: WorkspacePin
    metric_ids: tuple[str, ...]
    result_shape: Literal["scalar", "state_grouped", "category_grouped", "time_series"]
    dimension: str | None
    time: QueryTime
    join_program_id: str
    required_result_columns: tuple[str, ...]
    attribution_rule_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        metric_ids: Sequence[str],
        result_shape: str,
        time: QueryTime | None = None,
        dimension: str | None = None,
        join_program_id: str | None = None,
        workspace: WorkspacePin | None = None,
        required_result_columns: Sequence[str] | None = None,
        attribution_rule_id: str | None = None,
    ) -> "QuerySpec":
        metrics = tuple(metric_ids)
        shape = str(result_shape)
        derived_dimension = _DIMENSION_FOR_SHAPE.get(shape)
        if dimension is None and shape in _DIMENSION_FOR_SHAPE:
            dimension = derived_dimension
        derived_columns = _derive_result_columns(metrics, shape, dimension)
        spec = cls(
            schema_version=QUERY_SPEC_SCHEMA_VERSION,
            query_spec_id="",
            workspace=workspace or WorkspacePin.current(),
            metric_ids=metrics,
            result_shape=shape,  # type: ignore[arg-type]
            dimension=dimension,
            time=time or QueryTime("all_time"),
            join_program_id=join_program_id or _default_join_program(metrics, shape),
            required_result_columns=tuple(required_result_columns or derived_columns),
            attribution_rule_id=attribution_rule_id,
        )
        return cls(
            **{
                **spec.__dict__,
                "query_spec_id": spec.expected_query_spec_id(),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "QuerySpec":
        if not isinstance(value, Mapping):
            raise QuerySpecValidationError("invalid_query_spec", "QuerySpec must be an object")
        allowed = {
            "schema_version", "query_spec_id", "workspace", "metric_ids", "result_shape",
            "dimension", "time", "join_program_id", "required_result_columns",
            "attribution_rule_id",
        }
        unknown = set(value) - allowed
        if unknown:
            raise QuerySpecValidationError(
                "unsupported_query_feature", f"QuerySpec has unsupported fields: {sorted(unknown)}"
            )
        try:
            workspace_raw = value["workspace"]
            time_raw = value["time"]
            workspace = WorkspacePin(**dict(workspace_raw))
            query_time = QueryTime(**dict(time_raw))
            spec = cls(
                schema_version=str(value["schema_version"]),
                query_spec_id=str(value["query_spec_id"]),
                workspace=workspace,
                metric_ids=tuple(value["metric_ids"]),
                result_shape=value["result_shape"],
                dimension=value.get("dimension"),
                time=query_time,
                join_program_id=str(value["join_program_id"]),
                required_result_columns=tuple(value["required_result_columns"]),
                attribution_rule_id=value.get("attribution_rule_id"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QuerySpecValidationError("invalid_query_spec", "QuerySpec has invalid field types") from exc
        return spec

    @classmethod
    def create_validated(cls, **kwargs: Any) -> "QuerySpec":
        """Construct and immediately validate a QuerySpec for the current snapshot."""
        return validate_query_spec(cls.create(**kwargs))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace": self.workspace.as_dict(),
            "metric_ids": list(self.metric_ids),
            "result_shape": self.result_shape,
            "dimension": self.dimension,
            "time": self.time.as_dict(),
            "join_program_id": self.join_program_id,
            "required_result_columns": list(self.required_result_columns),
            "attribution_rule_id": self.attribution_rule_id,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def expected_query_spec_id(self) -> str:
        return "qs_" + hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:24]

    def as_dict(self) -> dict[str, Any]:
        return {**self.canonical_payload(), "query_spec_id": self.query_spec_id}


@dataclass(frozen=True)
class MetricSqlDefinition:
    metric_id: str
    time_field: str
    time_family: Literal["purchase", "review"]
    base_tables: tuple[str, ...]
    scalar_expression: str
    grouped_expression: str
    where_sql: tuple[str, ...]
    value_notes: str


def _metric_definitions() -> dict[str, MetricSqlDefinition]:
    purchase = "o.order_purchase_timestamp"
    status = "o.order_status NOT IN ('canceled', 'unavailable')"
    return {
        "gmv": MetricSqlDefinition("gmv", purchase, "purchase", ("fact_orders", "fact_order_items"), "SUM(i.price)", "SUM(i.price)", (status,), "price only"),
        "item_count": MetricSqlDefinition("item_count", purchase, "purchase", ("fact_orders", "fact_order_items"), "COUNT(i.order_item_id)", "COUNT(i.order_item_id)", (status,), "order item rows"),
        "freight_amount": MetricSqlDefinition("freight_amount", purchase, "purchase", ("fact_orders", "fact_order_items"), "SUM(i.freight_value)", "SUM(i.freight_value)", (status,), "freight_value only"),
        "paid_order_count": MetricSqlDefinition("paid_order_count", purchase, "purchase", ("fact_orders",), "COUNT(DISTINCT o.order_id)", "COUNT(DISTINCT o.order_id)", (status,), "distinct valid orders"),
        "average_delivery_days": MetricSqlDefinition("average_delivery_days", purchase, "purchase", ("fact_orders",), "AVG(EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_purchase_timestamp)) / 86400.0)", "AVG(EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_purchase_timestamp)) / 86400.0)", ("o.order_purchase_timestamp IS NOT NULL", "o.order_delivered_customer_date IS NOT NULL"), "delivery minus purchase"),
        "average_order_value": MetricSqlDefinition("average_order_value", purchase, "purchase", ("fact_orders", "fact_order_items"), "AVG(order_totals.order_total)", "AVG(order_totals.order_total)", (status,), "order-level price sum then average"),
        "on_time_delivery_rate": MetricSqlDefinition("on_time_delivery_rate", purchase, "purchase", ("fact_orders",), "AVG(CASE WHEN o.order_delivered_customer_date <= o.order_estimated_delivery_date THEN 1.0 ELSE 0.0 END)", "AVG(CASE WHEN o.order_delivered_customer_date <= o.order_estimated_delivery_date THEN 1.0 ELSE 0.0 END)", ("o.order_status = 'delivered'", "o.order_purchase_timestamp IS NOT NULL", "o.order_delivered_customer_date IS NOT NULL", "o.order_estimated_delivery_date IS NOT NULL"), "eligible delivered orders"),
        "cancellation_rate": MetricSqlDefinition("cancellation_rate", purchase, "purchase", ("fact_orders",), "AVG(CASE WHEN o.order_status = 'canceled' THEN 1.0 ELSE 0.0 END)", "AVG(CASE WHEN o.order_status = 'canceled' THEN 1.0 ELSE 0.0 END)", ("o.order_purchase_timestamp IS NOT NULL",), "canceled over all purchased orders"),
        "positive_review_rate": MetricSqlDefinition("positive_review_rate", "r.review_creation_date", "review", ("fact_reviews",), "AVG(CASE WHEN r.review_score >= 4 THEN 1.0 ELSE 0.0 END)", "AVG(CASE WHEN r.review_score >= 4 THEN 1.0 ELSE 0.0 END)", ("r.review_score BETWEEN 1 AND 5",), "review rows with score >= 4"),
        "average_review_score": MetricSqlDefinition("average_review_score", "r.review_creation_date", "review", ("fact_reviews",), "AVG(r.review_score)", "AVG(r.review_score)", ("r.review_score BETWEEN 1 AND 5",), "valid review rows"),
    }


METRIC_SQL_REGISTRY: Mapping[str, MetricSqlDefinition] = MappingProxyType(_metric_definitions())


def _derive_result_columns(metric_ids: Sequence[str], shape: str, dimension: str | None) -> tuple[str, ...]:
    if shape == "state_grouped":
        return ("customer_state", *metric_ids)
    if shape == "category_grouped":
        return ("product_category_name", *metric_ids)
    if shape == "time_series":
        return (*metric_ids, "time")
    return tuple(metric_ids)


def _default_join_program(metric_ids: Sequence[str], shape: str) -> str:
    if shape == "category_grouped":
        return "JP07_category_item"
    if shape == "state_grouped":
        if len(metric_ids) > 1:
            return "JP10_state_multi_metric"
        metric = metric_ids[0] if metric_ids else ""
        return "JP06_customer_geo_review" if metric in _REVIEW_METRICS else "JP05_customer_geo_order" if metric not in _ITEM_METRICS else "JP04_customer_geo_item"
    if shape == "time_series":
        return "JP12_review_time_multi_metric" if set(metric_ids) <= _REVIEW_METRICS else "JP11_purchase_time_multi_metric"
    if len(metric_ids) > 1:
        return "JP09_scalar_multi_metric"
    metric = metric_ids[0] if metric_ids else ""
    return "JP03_review_scalar" if metric in _REVIEW_METRICS else "JP02_order_scalar" if metric not in _ITEM_METRICS else "JP01_item_scalar"


def _date_value(value: str, field: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise QuerySpecValidationError("invalid_time_contract", f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise QuerySpecValidationError("invalid_time_contract", f"{field} is not a valid date") from exc


def _validate_time(spec: QuerySpec) -> None:
    time = spec.time
    if time.mode not in {"all_time", "absolute_range", "series"}:
        raise QuerySpecValidationError("invalid_time_contract", "unsupported time mode")
    if time.mode == "all_time":
        if any(value is not None for value in (time.start, time.end_exclusive, time.grain)):
            raise QuerySpecValidationError("invalid_time_contract", "all_time cannot contain range or grain")
        return
    if time.start is None or time.end_exclusive is None:
        raise QuerySpecValidationError("invalid_time_contract", "absolute time modes require start and end_exclusive")
    start = _date_value(time.start, "start")
    end = _date_value(time.end_exclusive, "end_exclusive")
    if end <= start:
        raise QuerySpecValidationError("invalid_time_contract", "end_exclusive must be later than start")
    if time.mode == "absolute_range" and time.grain is not None:
        raise QuerySpecValidationError("invalid_time_contract", "absolute_range cannot contain grain")
    if time.mode == "series" and time.grain not in _GRAINS:
        raise QuerySpecValidationError("invalid_time_contract", "series requires a supported grain")


def validate_query_spec(spec: QuerySpec, catalog: Catalog | None = None) -> QuerySpec:
    """Validate a QuerySpec against the current Catalog and frozen coverage."""
    if not isinstance(spec, QuerySpec):
        raise QuerySpecValidationError("invalid_query_spec", "expected QuerySpec")
    if spec.schema_version != QUERY_SPEC_SCHEMA_VERSION:
        raise QuerySpecValidationError("workspace_version_mismatch", "unsupported QuerySpec schema version")
    expected_workspace = WorkspacePin.current()
    if spec.workspace != expected_workspace:
        raise QuerySpecValidationError("workspace_version_mismatch", "workspace/catalog/prompt versions do not match the pinned snapshot")
    if spec.workspace.dialect != "postgres":
        raise QuerySpecValidationError("workspace_version_mismatch", "only PostgreSQL is supported")
    if not spec.metric_ids or len(spec.metric_ids) > _MAX_METRICS or len(set(spec.metric_ids)) != len(spec.metric_ids):
        raise QuerySpecValidationError("invalid_metric_ids", "metric_ids must contain 1-4 unique metrics")
    if any(not isinstance(metric, str) or not re.fullmatch(r"[a-z][a-z0-9_]+", metric) for metric in spec.metric_ids):
        raise QuerySpecValidationError("invalid_metric_ids", "metric_ids must be safe identifiers")
    active_catalog = catalog or CatalogLoader().load()
    catalog_snapshot = (
        active_catalog.catalog_version,
        active_catalog.dataset_version,
        active_catalog.metric_version,
        active_catalog.policy_version,
    )
    pinned_snapshot = (
        spec.workspace.catalog_version,
        spec.workspace.dataset_version,
        spec.workspace.metric_version,
        spec.workspace.policy_version,
    )
    if catalog_snapshot != pinned_snapshot:
        raise QuerySpecValidationError(
            "workspace_version_mismatch",
            "provided Catalog does not match the QuerySpec workspace snapshot",
        )
    unknown_metrics = set(spec.metric_ids) - set(active_catalog.metrics_by_id)
    if unknown_metrics or any(metric not in METRIC_SQL_REGISTRY for metric in spec.metric_ids):
        raise QuerySpecValidationError("invalid_metric_ids", f"unknown metrics: {sorted(unknown_metrics)}")
    if spec.result_shape not in _SHAPES:
        raise QuerySpecValidationError("coverage_shape_not_permitted", "unsupported result shape")
    if spec.dimension in _SENSITIVE_DIMENSIONS:
        raise QuerySpecValidationError("sensitive_dimension_not_displayable", "dimension is not displayable in v1")
    if spec.dimension in _ATTRIBUTION_DIMENSIONS:
        raise QuerySpecValidationError("attribution_not_frozen", "dimension requires an unfrozen attribution rule")
    expected_dimension = _DIMENSION_FOR_SHAPE[spec.result_shape]
    if spec.dimension != expected_dimension:
        raise QuerySpecValidationError("coverage_shape_not_permitted", "dimension does not match result shape")
    if spec.attribution_rule_id is not None:
        raise QuerySpecValidationError("attribution_not_frozen", "attribution rules are not frozen in v1")
    if spec.result_shape == "category_grouped":
        if any(metric not in _ITEM_METRICS for metric in spec.metric_ids):
            raise QuerySpecValidationError(
                "attribution_not_frozen",
                "category grouping for order or review metrics requires an attribution rule",
            )
        if len(spec.metric_ids) != 1:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "category grouping only supports one item metric")
    families = {METRIC_SQL_REGISTRY[metric].time_family for metric in spec.metric_ids}
    if spec.result_shape == "time_series":
        if len(families) != 1:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "mixed purchase/review time fields are not supported")
        time_fields = {METRIC_SQL_REGISTRY[metric].time_field for metric in spec.metric_ids}
        if len(time_fields) != 1:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "time series metrics must share one canonical time field")
        if families == {"review"}:
            if not set(spec.metric_ids) <= _REVIEW_METRICS:
                raise QuerySpecValidationError("coverage_shape_not_permitted", "unsupported review series metric set")
        elif not set(spec.metric_ids) <= _PURCHASE_METRICS:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "unsupported purchase series metric set")
        if spec.time.mode != "series":
            raise QuerySpecValidationError("invalid_time_contract", "time_series requires time.mode=series")
    elif spec.time.mode == "series":
        raise QuerySpecValidationError("invalid_time_contract", "series grain is only valid for time_series shape")
    _validate_time(spec)
    if spec.time.mode == "series" and spec.time.grain == "day" and spec.time.start is None:
        raise QuerySpecValidationError("invalid_time_contract", "daily series requires an absolute range")
    if spec.result_shape == "state_grouped":
        expected_program = _default_join_program(spec.metric_ids, spec.result_shape)
        if spec.join_program_id != expected_program:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "state_grouped join program does not match metric set")
    if spec.result_shape == "category_grouped" and spec.join_program_id != "JP07_category_item":
        raise QuerySpecValidationError("coverage_shape_not_permitted", "category_grouped requires JP07_category_item")
    if spec.result_shape == "time_series":
        expected_program = "JP12_review_time_multi_metric" if families == {"review"} else "JP11_purchase_time_multi_metric"
        if spec.join_program_id != expected_program:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "time series join program does not match metric family")
    if spec.result_shape == "scalar":
        expected_program = _default_join_program(spec.metric_ids, spec.result_shape)
        if spec.join_program_id != expected_program:
            raise QuerySpecValidationError("coverage_shape_not_permitted", "scalar join program does not match metric set")
    expected_columns = _derive_result_columns(spec.metric_ids, spec.result_shape, spec.dimension)
    if spec.required_result_columns != expected_columns:
        raise QuerySpecValidationError("result_columns_do_not_match_contract", "required_result_columns must be derived and ordered")
    if spec.query_spec_id != spec.expected_query_spec_id():
        raise QuerySpecValidationError("invalid_query_spec", "query_spec_id does not match canonical QuerySpec content")
    return spec


@dataclass(frozen=True)
class GoldSqlArtifact:
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


def _time_filter(definition: MetricSqlDefinition, query_time: QueryTime, alias: str) -> list[str]:
    filters = [condition.replace("o.", f"{alias}.").replace("r.", f"{alias}.") for condition in definition.where_sql]
    if query_time.start is not None:
        field = definition.time_field.replace("o.", f"{alias}.").replace("r.", f"{alias}.")
        filters.extend([
            f"{field} >= TIMESTAMP '{query_time.start}'",
            f"{field} < TIMESTAMP '{query_time.end_exclusive}'",
        ])
    return filters


def _where(filters: Sequence[str]) -> str:
    return " AND ".join(filters) if filters else "TRUE"


def _dimension_expr(dimension: str | None, alias: str) -> str | None:
    del alias  # expressions are canonical and intentionally not caller-controlled
    return _CANONICAL_DIMENSION_EXPRESSIONS.get(dimension)


def _metric_cte(metric_id: str, spec: QuerySpec, index: int) -> str:
    definition = METRIC_SQL_REGISTRY[metric_id]
    grouped = spec.result_shape in {"state_grouped", "category_grouped", "time_series"}
    dimension = spec.dimension if spec.result_shape in {"state_grouped", "category_grouped"} else None
    time_expr = None
    if spec.result_shape == "time_series":
        time_expr = f"date_trunc('{spec.time.grain}', {definition.time_field})"
    group_expr = _dimension_expr(dimension, "")
    group_parts = [part for part in (group_expr, time_expr) if part]
    select_parts = [f"{part} AS {'time' if part == time_expr else dimension}" for part in group_parts]
    if metric_id == "average_order_value":
        inner_select = ["o.order_id", "SUM(i.price) AS order_total"]
        if group_expr:
            inner_select.insert(0, f"{group_expr} AS {dimension}")
        if time_expr:
            inner_select.insert(0, f"{time_expr} AS time")
        inner_group = ["o.order_id", *group_parts]
        joins = "JOIN analytics.fact_order_items AS i ON o.order_id = i.order_id"
        if dimension == "customer_state":
            joins += " JOIN analytics.dim_customers AS c ON o.customer_id = c.customer_id"
        filters = _time_filter(definition, spec.time, "o")
        body = (
            f"SELECT {', '.join(inner_select)} FROM analytics.fact_orders AS o {joins} "
            f"WHERE {_where(filters)} GROUP BY {', '.join(inner_group)}"
        )
        outer_alias = f"aov_order_totals_{index:02d}"
        outer_select = [
            *([f"{outer_alias}.customer_state"] if dimension else []),
            *([f"{outer_alias}.time"] if time_expr else []),
            f"AVG({outer_alias}.order_total) AS {metric_id}",
        ]
        outer_group = [value.split(".")[-1] for value in outer_select[:-1]]
        # Keep the order-level intermediate as a named CTE.  Besides making
        # the grain boundary explicit, this gives the AST policy a declared
        # output schema for the derived `order_total` column.
        inner_cte = f"aov_order_totals_{index:02d} AS ({body})"
        outer_query = f"SELECT {', '.join(outer_select)} FROM aov_order_totals_{index:02d}"
        if outer_group:
            outer_query += f" GROUP BY {', '.join(outer_group)}"
        return f"m{index:02d}_{metric_id} AS (WITH {inner_cte} {outer_query})"

    if definition.time_family == "review":
        from_sql = "analytics.fact_reviews AS r"
        joins = ""
        if dimension == "customer_state":
            joins = " JOIN analytics.fact_orders AS o ON r.order_id = o.order_id JOIN analytics.dim_customers AS c ON o.customer_id = c.customer_id"
        filters = _time_filter(definition, spec.time, "r")
    else:
        from_sql = "analytics.fact_orders AS o"
        joins = ""
        if "fact_order_items" in definition.base_tables:
            joins = " JOIN analytics.fact_order_items AS i ON o.order_id = i.order_id"
        if dimension == "customer_state":
            joins += " JOIN analytics.dim_customers AS c ON o.customer_id = c.customer_id"
        if dimension == "product_category_name":
            joins += " JOIN analytics.dim_products AS p ON i.product_id = p.product_id"
        filters = _time_filter(definition, spec.time, "o")
    if dimension:
        filters.append(f"{_dimension_expr(dimension, '')} IS NOT NULL")
    select_parts = [*([f"{group_expr} AS {dimension}"] if group_expr else []), *([f"{time_expr} AS time"] if time_expr else []), f"{definition.grouped_expression} AS {metric_id}"]
    group_by = [str(index + 1) for index in range(len(group_parts))]
    return f"m{index:02d}_{metric_id} AS (SELECT {', '.join(select_parts)} FROM {from_sql}{joins} WHERE {_where(filters)}" + (f" GROUP BY {', '.join(group_by)}" if grouped else "") + ")"


def _render_final_select(spec: QuerySpec) -> str:
    names = [f"m{index:02d}_{metric}" for index, metric in enumerate(spec.metric_ids, 1)]
    if spec.result_shape == "scalar":
        columns = [f"{name}.{metric} AS {metric}" for name, metric in zip(names, spec.metric_ids)]
        return f"SELECT {', '.join(columns)} FROM " + " CROSS JOIN ".join(names)
    key = spec.dimension or "time"
    if spec.result_shape == "time_series":
        key = "time"
    if spec.result_shape == "time_series":
        columns = [
            f"{name}.{metric} AS {metric}"
            for metric, name in zip(spec.metric_ids, names)
        ]
        columns.append(
            f"{names[0]}.{key} AS {key}"
            if len(names) == 1
            else f"COALESCE({names[0]}.{key}, {', '.join(f'{name}.{key}' for name in names[1:])}) AS {key}"
        )
    else:
        if len(names) == 1:
            columns = [f"{names[0]}.{key} AS {key}"]
        else:
            columns = [
                f"COALESCE({names[0]}.{key}, {', '.join(f'{name}.{key}' for name in names[1:])}) AS {key}"
            ]
        for metric, name in zip(spec.metric_ids, names):
            columns.append(f"{name}.{metric} AS {metric}")
    sql = f"SELECT {', '.join(columns)} FROM {names[0]}"
    for name in names[1:]:
        sql += f" FULL OUTER JOIN {name} USING ({key})"
    return sql


def render_gold_sql(spec: QuerySpec, catalog: Catalog | None = None) -> GoldSqlArtifact:
    """Render canonical SQL from a validated QuerySpec without execution."""
    validated = validate_query_spec(spec, catalog)
    ctes = [_metric_cte(metric, validated, index) for index, metric in enumerate(validated.metric_ids, 1)]
    sql = "WITH " + ", ".join(ctes) + " " + _render_final_select(validated) + ";"
    evidence = MappingProxyType(
        {
            "schema_version": validated.schema_version,
            "workspace": validated.workspace.as_dict(),
            "query_spec_id": validated.query_spec_id,
            "metric_ids": list(validated.metric_ids),
            "join_program_id": validated.join_program_id,
            "required_result_columns": list(validated.required_result_columns),
            "renderer_version": RENDERER_VERSION,
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        }
    )
    return GoldSqlArtifact(
        query_spec_id=validated.query_spec_id,
        sql=sql,
        sql_sha256=evidence["sql_sha256"],
        renderer_version=RENDERER_VERSION,
        metric_ids=validated.metric_ids,
        join_program_id=validated.join_program_id,
        required_result_columns=validated.required_result_columns,
        evidence=evidence,
    )
