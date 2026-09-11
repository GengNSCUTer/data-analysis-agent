"""Deterministic, training-only Olist schema-link program labels.

``SchemaLinkPlan`` is derived from a validated offline :class:`QuerySpec`.
It exposes the table/column, join, grain, time-owner, filter, deduplication,
grouping, and result-alias decisions that canonical Gold SQL embodies, without
parsing SQL or becoming a second online Agent protocol.  It never calls an LLM
or database and must remain independent from the renderer implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any

from .olist_queryspec import (
    METRIC_SQL_REGISTRY,
    QuerySpec,
    QuerySpecValidationError,
    QueryTime,
    WorkspacePin,
    validate_query_spec,
)
from .semantic_catalog import Catalog, CatalogLoader


SCHEMA_LINK_PLAN_SCHEMA_VERSION = "olist-schema-link-plan-v1"
SCHEMA_LINK_REGISTRY_VERSION = "olist-schema-link-registry-v1"


class SchemaLinkPlanValidationError(ValueError):
    """Raised when a schema-link label differs from its deterministic source."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class RelationAlias:
    """One fixed relation and the renderer-compatible alias used for it."""

    relation: str
    alias: str

    def as_dict(self) -> dict[str, str]:
        return {"relation": self.relation, "alias": self.alias}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RelationAlias":
        _require_exact_keys(value, {"relation", "alias"}, "relation alias")
        relation = _require_text(value.get("relation"), "relation")
        alias = _require_text(value.get("alias"), "relation alias")
        return cls(relation=relation, alias=alias)


@dataclass(frozen=True)
class MetricProgram:
    """One metric CTE's deterministic schema and aggregation decisions."""

    metric_id: str
    cte_name: str
    source_grain: str
    relation_aliases: tuple[RelationAlias, ...]
    join_ids: tuple[str, ...]
    required_column_refs: tuple[str, ...]
    time_owner: str
    filter_rule_ids: tuple[str, ...]
    dedup_rule_id: str
    group_key_refs: tuple[str, ...]
    aggregation_rule_id: str
    result_alias: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "cte_name": self.cte_name,
            "source_grain": self.source_grain,
            "relation_aliases": [item.as_dict() for item in self.relation_aliases],
            "join_ids": list(self.join_ids),
            "required_column_refs": list(self.required_column_refs),
            "time_owner": self.time_owner,
            "filter_rule_ids": list(self.filter_rule_ids),
            "dedup_rule_id": self.dedup_rule_id,
            "group_key_refs": list(self.group_key_refs),
            "aggregation_rule_id": self.aggregation_rule_id,
            "result_alias": self.result_alias,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MetricProgram":
        _require_exact_keys(
            value,
            {
                "metric_id",
                "cte_name",
                "source_grain",
                "relation_aliases",
                "join_ids",
                "required_column_refs",
                "time_owner",
                "filter_rule_ids",
                "dedup_rule_id",
                "group_key_refs",
                "aggregation_rule_id",
                "result_alias",
            },
            "metric program",
        )
        aliases_raw = _require_sequence(
            value.get("relation_aliases"), "relation_aliases"
        )
        aliases = tuple(
            RelationAlias.from_mapping(_require_mapping(item, "relation_aliases item"))
            for item in aliases_raw
        )
        return cls(
            metric_id=_require_text(value.get("metric_id"), "metric_id"),
            cte_name=_require_text(value.get("cte_name"), "cte_name"),
            source_grain=_require_text(value.get("source_grain"), "source_grain"),
            relation_aliases=aliases,
            join_ids=_require_text_sequence(
                value.get("join_ids"), "join_ids", allow_empty=True
            ),
            required_column_refs=_require_text_sequence(
                value.get("required_column_refs"), "required_column_refs"
            ),
            time_owner=_require_text(value.get("time_owner"), "time_owner"),
            filter_rule_ids=_require_text_sequence(
                value.get("filter_rule_ids"), "filter_rule_ids"
            ),
            dedup_rule_id=_require_text(value.get("dedup_rule_id"), "dedup_rule_id"),
            group_key_refs=_require_text_sequence(
                value.get("group_key_refs"), "group_key_refs", allow_empty=True
            ),
            aggregation_rule_id=_require_text(
                value.get("aggregation_rule_id"), "aggregation_rule_id"
            ),
            result_alias=_require_text(value.get("result_alias"), "result_alias"),
        )


@dataclass(frozen=True)
class FinalMerge:
    """The deterministic final combination of metric CTEs."""

    strategy: str
    key_alias: str | None
    output_columns: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "key_alias": self.key_alias,
            "output_columns": list(self.output_columns),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FinalMerge":
        _require_exact_keys(
            value, {"strategy", "key_alias", "output_columns"}, "final merge"
        )
        key_alias = value.get("key_alias")
        if key_alias is not None:
            key_alias = _require_text(key_alias, "final merge key_alias")
        return cls(
            strategy=_require_text(value.get("strategy"), "final merge strategy"),
            key_alias=key_alias,
            output_columns=_require_text_sequence(
                value.get("output_columns"), "final merge output_columns"
            ),
        )


@dataclass(frozen=True)
class SchemaLinkPlan:
    """Versioned schema-link label for one validated Olist QuerySpec."""

    schema_version: str
    schema_link_plan_id: str
    query_spec_id: str
    workspace: WorkspacePin
    registry_version: str
    join_program_id: str
    result_shape: str
    time: QueryTime
    required_result_columns: tuple[str, ...]
    metric_programs: tuple[MetricProgram, ...]
    final_merge: FinalMerge

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query_spec_id": self.query_spec_id,
            "workspace": self.workspace.as_dict(),
            "registry_version": self.registry_version,
            "join_program_id": self.join_program_id,
            "result_shape": self.result_shape,
            "time": self.time.as_dict(),
            "required_result_columns": list(self.required_result_columns),
            "metric_programs": [item.as_dict() for item in self.metric_programs],
            "final_merge": self.final_merge.as_dict(),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def expected_schema_link_plan_id(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return "slp_" + digest[:24]

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.canonical_payload(),
            "schema_link_plan_id": self.schema_link_plan_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SchemaLinkPlan":
        _require_exact_keys(
            value,
            {
                "schema_version",
                "schema_link_plan_id",
                "query_spec_id",
                "workspace",
                "registry_version",
                "join_program_id",
                "result_shape",
                "time",
                "required_result_columns",
                "metric_programs",
                "final_merge",
            },
            "schema link plan",
        )
        try:
            workspace = WorkspacePin(
                **dict(_require_mapping(value.get("workspace"), "workspace"))
            )
            time = QueryTime(**dict(_require_mapping(value.get("time"), "time")))
        except (TypeError, ValueError) as exc:
            raise SchemaLinkPlanValidationError(
                "invalid_schema_link_plan", "workspace or time has invalid fields"
            ) from exc
        programs = tuple(
            MetricProgram.from_mapping(_require_mapping(item, "metric_programs item"))
            for item in _require_sequence(
                value.get("metric_programs"), "metric_programs"
            )
        )
        return cls(
            schema_version=_require_text(value.get("schema_version"), "schema_version"),
            schema_link_plan_id=_require_text(
                value.get("schema_link_plan_id"), "schema_link_plan_id"
            ),
            query_spec_id=_require_text(value.get("query_spec_id"), "query_spec_id"),
            workspace=workspace,
            registry_version=_require_text(
                value.get("registry_version"), "registry_version"
            ),
            join_program_id=_require_text(
                value.get("join_program_id"), "join_program_id"
            ),
            result_shape=_require_text(value.get("result_shape"), "result_shape"),
            time=time,
            required_result_columns=_require_text_sequence(
                value.get("required_result_columns"), "required_result_columns"
            ),
            metric_programs=programs,
            final_merge=FinalMerge.from_mapping(
                _require_mapping(value.get("final_merge"), "final_merge")
            ),
        )


@dataclass(frozen=True)
class SchemaLinkMetricDefinition:
    """A reviewed, SQL-free structural definition for one Olist metric."""

    source_grain: str
    relation_aliases: tuple[RelationAlias, ...]
    join_ids: tuple[str, ...]
    required_column_refs: tuple[str, ...]
    filter_rule_ids: tuple[str, ...]
    dedup_rule_id: str
    aggregation_rule_id: str


_ORDERS = RelationAlias("analytics.fact_orders", "o")
_ITEMS = RelationAlias("analytics.fact_order_items", "i")
_REVIEWS = RelationAlias("analytics.fact_reviews", "r")
_CUSTOMERS = RelationAlias("analytics.dim_customers", "c")
_PRODUCTS = RelationAlias("analytics.dim_products", "p")


def _registry() -> dict[str, SchemaLinkMetricDefinition]:
    """Return structural metadata kept independent from renderer SQL fragments."""

    return {
        "gmv": SchemaLinkMetricDefinition(
            "order_item",
            (_ORDERS, _ITEMS),
            ("orders_items",),
            (
                "o.order_id",
                "o.order_status",
                "o.order_purchase_timestamp",
                "i.order_id",
                "i.price",
            ),
            ("exclude_canceled_unavailable",),
            "none",
            "sum_item_price",
        ),
        "item_count": SchemaLinkMetricDefinition(
            "order_item",
            (_ORDERS, _ITEMS),
            ("orders_items",),
            (
                "o.order_id",
                "o.order_status",
                "o.order_purchase_timestamp",
                "i.order_id",
                "i.order_item_id",
            ),
            ("exclude_canceled_unavailable",),
            "none",
            "count_order_item_rows",
        ),
        "freight_amount": SchemaLinkMetricDefinition(
            "order_item",
            (_ORDERS, _ITEMS),
            ("orders_items",),
            (
                "o.order_id",
                "o.order_status",
                "o.order_purchase_timestamp",
                "i.order_id",
                "i.freight_value",
            ),
            ("exclude_canceled_unavailable",),
            "none",
            "sum_freight_value",
        ),
        "paid_order_count": SchemaLinkMetricDefinition(
            "order",
            (_ORDERS,),
            (),
            ("o.order_id", "o.order_status", "o.order_purchase_timestamp"),
            ("exclude_canceled_unavailable",),
            "count_distinct_order_id",
            "count_distinct_order_id",
        ),
        "average_delivery_days": SchemaLinkMetricDefinition(
            "order",
            (_ORDERS,),
            (),
            ("o.order_purchase_timestamp", "o.order_delivered_customer_date"),
            ("require_purchase_and_delivery_timestamps",),
            "none",
            "average_delivery_interval_days",
        ),
        "average_order_value": SchemaLinkMetricDefinition(
            "order_total",
            (_ORDERS, _ITEMS),
            ("orders_items",),
            (
                "o.order_id",
                "o.order_status",
                "o.order_purchase_timestamp",
                "i.order_id",
                "i.price",
            ),
            ("exclude_canceled_unavailable",),
            "preaggregate_order_price",
            "average_order_total",
        ),
        "on_time_delivery_rate": SchemaLinkMetricDefinition(
            "order",
            (_ORDERS,),
            (),
            (
                "o.order_status",
                "o.order_purchase_timestamp",
                "o.order_delivered_customer_date",
                "o.order_estimated_delivery_date",
            ),
            ("delivered_with_complete_delivery_dates",),
            "none",
            "on_time_delivery_fraction",
        ),
        "cancellation_rate": SchemaLinkMetricDefinition(
            "order",
            (_ORDERS,),
            (),
            ("o.order_status", "o.order_purchase_timestamp"),
            ("require_purchase_timestamp",),
            "none",
            "cancellation_fraction",
        ),
        "positive_review_rate": SchemaLinkMetricDefinition(
            "review",
            (_REVIEWS,),
            (),
            ("r.review_score", "r.review_creation_date"),
            ("valid_review_score",),
            "none",
            "positive_review_fraction",
        ),
        "average_review_score": SchemaLinkMetricDefinition(
            "review",
            (_REVIEWS,),
            (),
            ("r.review_score", "r.review_creation_date"),
            ("valid_review_score",),
            "none",
            "average_review_score",
        ),
    }


SCHEMA_LINK_REGISTRY: Mapping[str, SchemaLinkMetricDefinition] = MappingProxyType(
    _registry()
)
_RELATION_TO_CATALOG_TABLE_ID = MappingProxyType(
    {
        "analytics.fact_orders": "fact_orders",
        "analytics.fact_order_items": "fact_order_items",
        "analytics.fact_reviews": "fact_reviews",
        "analytics.dim_customers": "dim_customers",
        "analytics.dim_products": "dim_products",
    }
)
_REQUIRED_CATALOG_JOIN_IDS = frozenset(
    {"orders_customers", "orders_items", "orders_reviews", "items_products"}
)


def _unique(items: Sequence[Any]) -> tuple[Any, ...]:
    output: list[Any] = []
    for item in items:
        if item not in output:
            output.append(item)
    return tuple(output)


def _append_state_dimension(
    relations: tuple[RelationAlias, ...],
    joins: tuple[str, ...],
    columns: tuple[str, ...],
    filters: tuple[str, ...],
) -> tuple[
    tuple[RelationAlias, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]
]:
    if any(item.alias == "r" for item in relations):
        additions = (
            (_ORDERS, _CUSTOMERS),
            ("orders_reviews", "orders_customers"),
            (
                "r.order_id",
                "o.order_id",
                "o.customer_id",
                "c.customer_id",
                "c.customer_state",
            ),
        )
    else:
        additions = (
            (_CUSTOMERS,),
            ("orders_customers",),
            ("o.customer_id", "c.customer_id", "c.customer_state"),
        )
    return (
        _unique((*relations, *additions[0])),
        _unique((*joins, *additions[1])),
        _unique((*columns, *additions[2])),
        _unique((*filters, "exclude_null_customer_state")),
    )


def _append_category_dimension(
    relations: tuple[RelationAlias, ...],
    joins: tuple[str, ...],
    columns: tuple[str, ...],
    filters: tuple[str, ...],
) -> tuple[
    tuple[RelationAlias, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]
]:
    return (
        _unique((*relations, _PRODUCTS)),
        _unique((*joins, "items_products")),
        _unique((*columns, "i.product_id", "p.product_id", "p.product_category_name")),
        _unique((*filters, "exclude_null_product_category")),
    )


def _metric_program(metric_id: str, spec: QuerySpec, index: int) -> MetricProgram:
    try:
        definition = SCHEMA_LINK_REGISTRY[metric_id]
        metric_sql = METRIC_SQL_REGISTRY[metric_id]
    except (
        KeyError
    ) as exc:  # guarded by validate_query_spec; preserve fail-closed behavior.
        raise SchemaLinkPlanValidationError(
            "unknown_metric", f"schema-link registry lacks {metric_id!r}"
        ) from exc
    relations = definition.relation_aliases
    joins = definition.join_ids
    columns = definition.required_column_refs
    filters = definition.filter_rule_ids
    group_keys: tuple[str, ...] = ()
    if spec.result_shape == "state_grouped":
        relations, joins, columns, filters = _append_state_dimension(
            relations, joins, columns, filters
        )
        group_keys = ("c.customer_state",)
    elif spec.result_shape == "category_grouped":
        relations, joins, columns, filters = _append_category_dimension(
            relations, joins, columns, filters
        )
        group_keys = ("p.product_category_name",)
    elif spec.result_shape == "time_series":
        group_keys = (metric_sql.time_field,)
    return MetricProgram(
        metric_id=metric_id,
        cte_name=f"m{index:02d}_{metric_id}",
        source_grain=definition.source_grain,
        relation_aliases=relations,
        join_ids=joins,
        required_column_refs=columns,
        time_owner=metric_sql.time_field,
        filter_rule_ids=filters,
        dedup_rule_id=definition.dedup_rule_id,
        group_key_refs=group_keys,
        aggregation_rule_id=definition.aggregation_rule_id,
        result_alias=metric_id,
    )


def _final_merge(spec: QuerySpec) -> FinalMerge:
    metric_count = len(spec.metric_ids)
    if metric_count == 1:
        key_alias = (
            "customer_state"
            if spec.result_shape == "state_grouped"
            else "product_category_name"
            if spec.result_shape == "category_grouped"
            else "time"
            if spec.result_shape == "time_series"
            else None
        )
        return FinalMerge("single_metric_cte", key_alias, spec.required_result_columns)
    if spec.result_shape == "scalar":
        return FinalMerge("cross_join_metric_ctes", None, spec.required_result_columns)
    key_alias = "customer_state" if spec.result_shape == "state_grouped" else "time"
    return FinalMerge(
        f"full_outer_join_on_{key_alias}", key_alias, spec.required_result_columns
    )


def _derive_validated(spec: QuerySpec) -> SchemaLinkPlan:
    programs = tuple(
        _metric_program(metric_id, spec, index)
        for index, metric_id in enumerate(spec.metric_ids, start=1)
    )
    provisional = SchemaLinkPlan(
        schema_version=SCHEMA_LINK_PLAN_SCHEMA_VERSION,
        schema_link_plan_id="",
        query_spec_id=spec.query_spec_id,
        workspace=spec.workspace,
        registry_version=SCHEMA_LINK_REGISTRY_VERSION,
        join_program_id=spec.join_program_id,
        result_shape=spec.result_shape,
        time=spec.time,
        required_result_columns=spec.required_result_columns,
        metric_programs=programs,
        final_merge=_final_merge(spec),
    )
    return SchemaLinkPlan(
        **{
            **provisional.__dict__,
            "schema_link_plan_id": provisional.expected_schema_link_plan_id(),
        }
    )


def derive_schema_link_plan(
    spec: QuerySpec, catalog: Catalog | None = None
) -> SchemaLinkPlan:
    """Derive a plan from a validated Olist QuerySpec without SQL execution."""

    active_catalog = catalog or CatalogLoader().load()
    try:
        validated = validate_query_spec(spec, active_catalog)
    except QuerySpecValidationError as exc:
        raise SchemaLinkPlanValidationError(exc.reason_code, str(exc)) from exc
    _validate_registry_catalog(active_catalog)
    return _derive_validated(validated)


def validate_schema_link_plan(
    plan: SchemaLinkPlan, spec: QuerySpec, catalog: Catalog | None = None
) -> SchemaLinkPlan:
    """Fail closed unless ``plan`` is exactly the plan derived from ``spec``."""

    if not isinstance(plan, SchemaLinkPlan):
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan", "expected a SchemaLinkPlan instance"
        )
    if plan.schema_version != SCHEMA_LINK_PLAN_SCHEMA_VERSION:
        raise SchemaLinkPlanValidationError(
            "schema_version_mismatch", "unsupported schema-link plan version"
        )
    if plan.registry_version != SCHEMA_LINK_REGISTRY_VERSION:
        raise SchemaLinkPlanValidationError(
            "registry_version_mismatch", "schema-link registry version differs"
        )
    if plan.schema_link_plan_id != plan.expected_schema_link_plan_id():
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan", "schema_link_plan_id does not match content"
        )
    expected = derive_schema_link_plan(spec, catalog)
    if plan.as_dict() != expected.as_dict():
        raise SchemaLinkPlanValidationError(
            "schema_link_plan_mismatch",
            "schema-link plan differs from deterministic QuerySpec-derived program",
        )
    return plan


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan", f"{label} must be an object"
        )
    return value


def _validate_registry_catalog(catalog: Catalog) -> None:
    """Guard static labels against a same-version-but-divergent Catalog object."""

    tables = catalog.tables_by_id
    for relation, table_id in _RELATION_TO_CATALOG_TABLE_ID.items():
        table = tables.get(table_id)
        expected_name = relation.removeprefix("analytics.")
        if table is None or table.physical_name != expected_name:
            raise SchemaLinkPlanValidationError(
                "registry_catalog_mismatch",
                f"catalog relation {relation!r} is not available under expected table ID",
            )
    actual_join_ids = {item.join_id for item in catalog.joins}
    missing_join_ids = _REQUIRED_CATALOG_JOIN_IDS - actual_join_ids
    if missing_join_ids:
        raise SchemaLinkPlanValidationError(
            "registry_catalog_mismatch",
            "catalog lacks required schema-link joins: "
            + ", ".join(sorted(missing_join_ids)),
        )


def _require_sequence(
    value: Any, label: str, *, allow_empty: bool = False
) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan", f"{label} must be an array"
        )
    if not value and not allow_empty:
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan", f"{label} must not be empty"
        )
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan", f"{label} must be non-empty text"
        )
    return value


def _require_text_sequence(
    value: Any, label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    return tuple(
        _require_text(item, label)
        for item in _require_sequence(value, label, allow_empty=allow_empty)
    )


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        raise SchemaLinkPlanValidationError(
            "invalid_schema_link_plan",
            f"{label} fields differ; missing={sorted(missing)}, unknown={sorted(unknown)}",
        )
