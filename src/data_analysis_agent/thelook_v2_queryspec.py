"""Offline, version-pinned QuerySpec contract for TheLook v2 evaluation.

The v2 plan stays deliberately smaller than a SQL AST.  It names reviewed
metrics, one result shape, one semantic dimension and one time contract.  The
renderer compiles only this validated plan; it never parses natural language.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from typing import Any, Literal, Mapping, Sequence

from .semantic_catalog import Catalog, CatalogLoader
from .thelook_queryspec import TheLookQueryTime
from .thelook_v2_context import THELOOK_V2_WORKSPACE


QUERY_SPEC_SCHEMA_VERSION = "thelook-query-spec-v2"
QUERY_PROMPT_VERSION = "thelook-candidate-sql-v2"
_MAX_METRICS = 4
_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})
_SHAPES = frozenset({"scalar", "dimension_grouped", "time_series"})

METRIC_FACT_DOMAINS: Mapping[str, str] = {
    "completed_sale_amount": "order_items",
    "completed_item_count": "order_items",
    "completed_order_count": "orders",
    "average_order_value": "orders",
    "average_items_per_completed_order": "orders",
    "cancelled_order_count": "orders",
    "returned_order_count": "orders",
    "average_fulfillment_days": "orders",
    "average_dispatch_days": "orders",
    "average_transit_days": "orders",
    "average_post_delivery_return_days": "orders",
    "return_rate": "orders",
    "completed_customer_count": "orders",
    "received_inventory_unit_count": "inventory_received",
    "sold_inventory_unit_count": "inventory_sold",
    "current_unsold_inventory_unit_count": "inventory_snapshot",
    "average_days_to_sale": "inventory_sold",
    "event_count": "events",
    "unique_session_count": "events",
    "registered_user_count": "users",
}

_DIMENSION_DOMAINS: Mapping[str, str] = {
    "customer_state": "orders",
    "customer_city": "orders",
    "customer_traffic_source": "orders",
    "product_category": "product",
    "product_brand": "product",
    "product_department": "product",
    "distribution_center": "inventory",
    "event_type": "events",
    "event_browser": "events",
    "event_traffic_source": "events",
    "user_country": "users",
    "user_traffic_source": "users",
    "user_state": "users",
}


class TheLookV2QuerySpecValidationError(ValueError):
    """Raised when a v2 QuerySpec violates a frozen evaluation boundary."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class TheLookV2WorkspacePin:
    workspace_id: str
    catalog_version: str
    dataset_version: str
    metric_version: str
    policy_version: str
    prompt_version: str = QUERY_PROMPT_VERSION
    dialect: str = "postgres"

    @classmethod
    def current(cls) -> "TheLookV2WorkspacePin":
        return cls(
            workspace_id=THELOOK_V2_WORKSPACE.workspace_id,
            catalog_version=THELOOK_V2_WORKSPACE.catalog_version,
            dataset_version=THELOOK_V2_WORKSPACE.dataset_version,
            metric_version=THELOOK_V2_WORKSPACE.metric_version,
            policy_version=THELOOK_V2_WORKSPACE.policy_version,
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
class TheLookV2QuerySpec:
    schema_version: str
    query_spec_id: str
    workspace: TheLookV2WorkspacePin
    metric_ids: tuple[str, ...]
    result_shape: Literal["scalar", "dimension_grouped", "time_series"]
    dimension: str | None
    time: TheLookQueryTime
    join_program_id: str
    required_result_columns: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        metric_ids: Sequence[str],
        result_shape: str,
        time: TheLookQueryTime | None = None,
        dimension: str | None = None,
        join_program_id: str | None = None,
        workspace: TheLookV2WorkspacePin | None = None,
        required_result_columns: Sequence[str] | None = None,
    ) -> "TheLookV2QuerySpec":
        metrics = tuple(metric_ids)
        shape = str(result_shape)
        derived_columns = _derive_result_columns(metrics, shape, dimension)
        provisional = cls(
            schema_version=QUERY_SPEC_SCHEMA_VERSION,
            query_spec_id="",
            workspace=workspace or TheLookV2WorkspacePin.current(),
            metric_ids=metrics,
            result_shape=shape,  # type: ignore[arg-type]
            dimension=dimension,
            time=time or TheLookQueryTime("all_time"),
            join_program_id=join_program_id
            or _default_join_program(metrics, shape, dimension),
            required_result_columns=tuple(required_result_columns or derived_columns),
        )
        return cls(
            **{
                **provisional.__dict__,
                "query_spec_id": provisional.expected_query_spec_id(),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TheLookV2QuerySpec":
        if not isinstance(value, Mapping):
            raise TheLookV2QuerySpecValidationError(
                "invalid_query_spec", "QuerySpec must be an object"
            )
        allowed = {
            "schema_version",
            "query_spec_id",
            "workspace",
            "metric_ids",
            "result_shape",
            "dimension",
            "time",
            "join_program_id",
            "required_result_columns",
        }
        unknown = set(value) - allowed
        if unknown:
            raise TheLookV2QuerySpecValidationError(
                "unsupported_query_feature",
                f"QuerySpec has unsupported fields: {sorted(unknown)}",
            )
        try:
            return cls(
                schema_version=str(value["schema_version"]),
                query_spec_id=str(value["query_spec_id"]),
                workspace=TheLookV2WorkspacePin(**dict(value["workspace"])),
                metric_ids=tuple(value["metric_ids"]),
                result_shape=value["result_shape"],
                dimension=value.get("dimension"),
                time=TheLookQueryTime(**dict(value["time"])),
                join_program_id=str(value["join_program_id"]),
                required_result_columns=tuple(value["required_result_columns"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TheLookV2QuerySpecValidationError(
                "invalid_query_spec", "QuerySpec has invalid field types"
            ) from exc

    @classmethod
    def create_validated(cls, **kwargs: Any) -> "TheLookV2QuerySpec":
        return validate_thelook_v2_query_spec(cls.create(**kwargs))

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
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def expected_query_spec_id(self) -> str:
        return (
            "tqs2_"
            + hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:24]
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self.canonical_payload(), "query_spec_id": self.query_spec_id}


def _derive_result_columns(
    metric_ids: Sequence[str], shape: str, dimension: str | None
) -> tuple[str, ...]:
    if shape == "dimension_grouped":
        return (dimension or "", *metric_ids)
    if shape == "time_series":
        return (*metric_ids, "time")
    return tuple(metric_ids)


def _default_join_program(
    metric_ids: Sequence[str], shape: str, dimension: str | None
) -> str:
    domain = (
        METRIC_FACT_DOMAINS.get(metric_ids[0], "unknown") if metric_ids else "unknown"
    )
    return f"tlv2_{domain}_{shape}_{dimension or 'none'}"


def _date_value(value: str, field: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", f"{field} must be an ISO date"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", f"{field} is not a valid date"
        ) from exc


def _validate_time(spec: TheLookV2QuerySpec) -> None:
    query_time = spec.time
    if query_time.mode not in {"all_time", "absolute_range", "series"}:
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", "unsupported time mode"
        )
    if query_time.mode == "all_time":
        if any(
            value is not None
            for value in (query_time.start, query_time.end_exclusive, query_time.grain)
        ):
            raise TheLookV2QuerySpecValidationError(
                "invalid_time_contract", "all_time cannot contain range or grain"
            )
        return
    if query_time.start is None or query_time.end_exclusive is None:
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract",
            "absolute time modes require start and end_exclusive",
        )
    if _date_value(query_time.end_exclusive, "end_exclusive") <= _date_value(
        query_time.start, "start"
    ):
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", "end_exclusive must be later than start"
        )
    if query_time.mode == "absolute_range" and query_time.grain is not None:
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", "absolute_range cannot contain grain"
        )
    if query_time.mode == "series" and query_time.grain not in _GRAINS:
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", "series requires a supported grain"
        )


def validate_thelook_v2_query_spec(
    spec: TheLookV2QuerySpec, catalog: Catalog | None = None
) -> TheLookV2QuerySpec:
    """Reject unpinned, cross-fact or non-displayable evaluation plans."""
    if not isinstance(spec, TheLookV2QuerySpec):
        raise TheLookV2QuerySpecValidationError(
            "invalid_query_spec", "expected TheLookV2QuerySpec"
        )
    if spec.schema_version != QUERY_SPEC_SCHEMA_VERSION:
        raise TheLookV2QuerySpecValidationError(
            "workspace_version_mismatch", "unsupported QuerySpec schema version"
        )
    if (
        spec.workspace != TheLookV2WorkspacePin.current()
        or spec.workspace.dialect != "postgres"
    ):
        raise TheLookV2QuerySpecValidationError(
            "workspace_version_mismatch", "workspace snapshot is not current"
        )
    if (
        not spec.metric_ids
        or len(spec.metric_ids) > _MAX_METRICS
        or len(set(spec.metric_ids)) != len(spec.metric_ids)
    ):
        raise TheLookV2QuerySpecValidationError(
            "invalid_metric_ids", "metric_ids must contain 1-4 unique metrics"
        )
    if any(
        not isinstance(metric, str) or not re.fullmatch(r"[a-z][a-z0-9_]+", metric)
        for metric in spec.metric_ids
    ):
        raise TheLookV2QuerySpecValidationError(
            "invalid_metric_ids", "metric_ids must be safe identifiers"
        )

    active_catalog = catalog or CatalogLoader(THELOOK_V2_WORKSPACE).load()
    snapshot = (
        active_catalog.catalog_version,
        active_catalog.dataset_version,
        active_catalog.metric_version,
        active_catalog.policy_version,
    )
    pinned = (
        spec.workspace.catalog_version,
        spec.workspace.dataset_version,
        spec.workspace.metric_version,
        spec.workspace.policy_version,
    )
    if snapshot != pinned:
        raise TheLookV2QuerySpecValidationError(
            "workspace_version_mismatch", "Catalog snapshot does not match QuerySpec"
        )
    # Both the frozen Catalog *and* the renderer-facing fact-domain registry
    # must know a metric.  Checking only the Catalog would allow a Catalog
    # addition to reach ``METRIC_FACT_DOMAINS`` below without a deterministic
    # fact-domain/renderer implementation; checking only the registry would
    # make a removed Catalog metric silently usable.
    unknown = (set(spec.metric_ids) - set(active_catalog.metrics_by_id)) | (
        set(spec.metric_ids) - set(METRIC_FACT_DOMAINS)
    )
    if unknown:
        raise TheLookV2QuerySpecValidationError(
            "invalid_metric_ids", f"unknown metrics: {sorted(unknown)}"
        )
    fact_domains = {METRIC_FACT_DOMAINS[metric] for metric in spec.metric_ids}
    if len(fact_domains) != 1:
        raise TheLookV2QuerySpecValidationError(
            "cross_fact_combination_not_permitted",
            "multi-metric plans must remain in one fact domain",
        )
    fact_domain = next(iter(fact_domains))
    if spec.result_shape not in _SHAPES:
        raise TheLookV2QuerySpecValidationError(
            "coverage_shape_not_permitted", "unsupported result shape"
        )
    if spec.result_shape == "dimension_grouped":
        if spec.dimension not in _DIMENSION_DOMAINS:
            raise TheLookV2QuerySpecValidationError(
                "coverage_shape_not_permitted", "unsupported grouping dimension"
            )
        if any(
            spec.dimension
            not in active_catalog.metrics_by_id[metric].allowed_dimensions
            for metric in spec.metric_ids
        ):
            raise TheLookV2QuerySpecValidationError(
                "coverage_shape_not_permitted",
                "dimension is not allowed by every metric",
            )
        dimension_domain = _DIMENSION_DOMAINS[spec.dimension]
        if fact_domain == "order_items":
            permitted_dimension_domains = {"orders", "product"}
        elif fact_domain.startswith("inventory"):
            permitted_dimension_domains = {"inventory", "product"}
        else:
            permitted_dimension_domains = {fact_domain}
        if dimension_domain not in permitted_dimension_domains:
            raise TheLookV2QuerySpecValidationError(
                "coverage_shape_not_permitted",
                "dimension fact domain does not match metrics",
            )
    elif spec.dimension is not None:
        raise TheLookV2QuerySpecValidationError(
            "coverage_shape_not_permitted",
            "scalar/time series cannot contain a dimension",
        )
    if spec.result_shape == "time_series" and spec.time.mode != "series":
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", "time_series requires time.mode=series"
        )
    if spec.result_shape != "time_series" and spec.time.mode == "series":
        raise TheLookV2QuerySpecValidationError(
            "invalid_time_contract", "series grain is only valid for time_series"
        )
    _validate_time(spec)
    if fact_domain == "inventory_snapshot" and spec.time.mode != "all_time":
        raise TheLookV2QuerySpecValidationError(
            "snapshot_time_not_permitted",
            "inventory snapshot metrics only support all_time",
        )
    expected_program = _default_join_program(
        spec.metric_ids, spec.result_shape, spec.dimension
    )
    if spec.join_program_id != expected_program:
        raise TheLookV2QuerySpecValidationError(
            "coverage_shape_not_permitted",
            "join program does not match shape/dimension",
        )
    expected_columns = _derive_result_columns(
        spec.metric_ids, spec.result_shape, spec.dimension
    )
    if spec.required_result_columns != expected_columns:
        raise TheLookV2QuerySpecValidationError(
            "result_columns_do_not_match_contract",
            "required_result_columns must be derived and ordered",
        )
    if spec.query_spec_id != spec.expected_query_spec_id():
        raise TheLookV2QuerySpecValidationError(
            "invalid_query_spec", "query_spec_id does not match canonical content"
        )
    return spec
