"""Offline QuerySpec contract for the isolated TheLook workspace.

TheLook QuerySpec is a small, versioned query plan.  It contains no natural
language, generated SQL, or database connection.  A later deterministic
renderer may compile a validated plan into PostgreSQL Gold SQL; this module
only validates the plan and its snapshot/shape boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from typing import Any, Literal, Mapping, Sequence

from .semantic_catalog import Catalog, CatalogLoader
from .thelook_context import THELOOK_WORKSPACE


QUERY_SPEC_SCHEMA_VERSION = "thelook-query-spec-v1"
QUERY_PROMPT_VERSION = "thelook-query-prompt-v1-design"
_MAX_METRICS = 4
_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})
_SHAPES = frozenset({"scalar", "dimension_grouped", "time_series"})
_GROUP_DIMENSIONS = frozenset(
    {"state", "city", "traffic_source", "category", "brand", "department"}
)
_SUPPORTED_METRICS = frozenset(
    {
        "completed_sale_amount",
        "completed_order_count",
        "average_order_value",
        "average_fulfillment_days",
        "return_rate",
        "completed_customer_count",
    }
)


class TheLookQuerySpecValidationError(ValueError):
    """A TheLook QuerySpec violates the frozen construction contract."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class TheLookWorkspacePin:
    """Immutable snapshot identity carried by every TheLook QuerySpec."""

    workspace_id: str
    catalog_version: str
    dataset_version: str
    metric_version: str
    policy_version: str
    prompt_version: str = QUERY_PROMPT_VERSION
    dialect: str = "postgres"

    @classmethod
    def current(cls) -> "TheLookWorkspacePin":
        return cls(
            workspace_id=THELOOK_WORKSPACE.workspace_id,
            catalog_version=THELOOK_WORKSPACE.catalog_version,
            dataset_version=THELOOK_WORKSPACE.dataset_version,
            metric_version=THELOOK_WORKSPACE.metric_version,
            policy_version=THELOOK_WORKSPACE.policy_version,
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
class TheLookQueryTime:
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
class TheLookQuerySpec:
    schema_version: str
    query_spec_id: str
    workspace: TheLookWorkspacePin
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
        workspace: TheLookWorkspacePin | None = None,
        required_result_columns: Sequence[str] | None = None,
    ) -> "TheLookQuerySpec":
        metrics = tuple(metric_ids)
        shape = str(result_shape)
        derived_columns = _derive_result_columns(metrics, shape, dimension)
        spec = cls(
            schema_version=QUERY_SPEC_SCHEMA_VERSION,
            query_spec_id="",
            workspace=workspace or TheLookWorkspacePin.current(),
            metric_ids=metrics,
            result_shape=shape,  # type: ignore[arg-type]
            dimension=dimension,
            time=time or TheLookQueryTime("all_time"),
            join_program_id=join_program_id or _default_join_program(metrics, shape, dimension),
            required_result_columns=tuple(required_result_columns or derived_columns),
        )
        return cls(**{**spec.__dict__, "query_spec_id": spec.expected_query_spec_id()})

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TheLookQuerySpec":
        if not isinstance(value, Mapping):
            raise TheLookQuerySpecValidationError("invalid_query_spec", "QuerySpec must be an object")
        allowed = {
            "schema_version", "query_spec_id", "workspace", "metric_ids", "result_shape",
            "dimension", "time", "join_program_id", "required_result_columns",
        }
        unknown = set(value) - allowed
        if unknown:
            raise TheLookQuerySpecValidationError(
                "unsupported_query_feature", f"QuerySpec has unsupported fields: {sorted(unknown)}"
            )
        try:
            spec = cls(
                schema_version=str(value["schema_version"]),
                query_spec_id=str(value["query_spec_id"]),
                workspace=TheLookWorkspacePin(**dict(value["workspace"])),
                metric_ids=tuple(value["metric_ids"]),
                result_shape=value["result_shape"],
                dimension=value.get("dimension"),
                time=TheLookQueryTime(**dict(value["time"])),
                join_program_id=str(value["join_program_id"]),
                required_result_columns=tuple(value["required_result_columns"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TheLookQuerySpecValidationError(
                "invalid_query_spec", "QuerySpec has invalid field types"
            ) from exc
        return spec

    @classmethod
    def create_validated(cls, **kwargs: Any) -> "TheLookQuerySpec":
        return validate_thelook_query_spec(cls.create(**kwargs))

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
        return json.dumps(self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def expected_query_spec_id(self) -> str:
        return "tqs_" + hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:24]

    def as_dict(self) -> dict[str, Any]:
        return {**self.canonical_payload(), "query_spec_id": self.query_spec_id}


def _derive_result_columns(metric_ids: Sequence[str], shape: str, dimension: str | None) -> tuple[str, ...]:
    if shape == "dimension_grouped":
        return (dimension or "", *metric_ids)
    if shape == "time_series":
        return (*metric_ids, "time")
    return tuple(metric_ids)


def _default_join_program(metric_ids: Sequence[str], shape: str, dimension: str | None) -> str:
    del metric_ids
    if shape == "scalar":
        return "TLJP01_scalar"
    if shape == "time_series":
        return "TLJP03_order_time_series"
    return f"TLJP02_group_{dimension}"


def _date_value(value: str, field: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise TheLookQuerySpecValidationError("invalid_time_contract", f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TheLookQuerySpecValidationError("invalid_time_contract", f"{field} is not a valid date") from exc


def _validate_time(spec: TheLookQuerySpec) -> None:
    time = spec.time
    if time.mode not in {"all_time", "absolute_range", "series"}:
        raise TheLookQuerySpecValidationError("invalid_time_contract", "unsupported time mode")
    if time.mode == "all_time":
        if any(value is not None for value in (time.start, time.end_exclusive, time.grain)):
            raise TheLookQuerySpecValidationError("invalid_time_contract", "all_time cannot contain range or grain")
        return
    if time.start is None or time.end_exclusive is None:
        raise TheLookQuerySpecValidationError("invalid_time_contract", "absolute time modes require start and end_exclusive")
    if _date_value(time.end_exclusive, "end_exclusive") <= _date_value(time.start, "start"):
        raise TheLookQuerySpecValidationError("invalid_time_contract", "end_exclusive must be later than start")
    if time.mode == "absolute_range" and time.grain is not None:
        raise TheLookQuerySpecValidationError("invalid_time_contract", "absolute_range cannot contain grain")
    if time.mode == "series" and time.grain not in _GRAINS:
        raise TheLookQuerySpecValidationError("invalid_time_contract", "series requires a supported grain")


def validate_thelook_query_spec(
    spec: TheLookQuerySpec, catalog: Catalog | None = None
) -> TheLookQuerySpec:
    """Validate a TheLook plan against the current Catalog snapshot."""
    if not isinstance(spec, TheLookQuerySpec):
        raise TheLookQuerySpecValidationError("invalid_query_spec", "expected TheLookQuerySpec")
    if spec.schema_version != QUERY_SPEC_SCHEMA_VERSION:
        raise TheLookQuerySpecValidationError("workspace_version_mismatch", "unsupported QuerySpec schema version")
    if spec.workspace != TheLookWorkspacePin.current():
        raise TheLookQuerySpecValidationError("workspace_version_mismatch", "workspace snapshot is not current")
    if spec.workspace.dialect != "postgres":
        raise TheLookQuerySpecValidationError("workspace_version_mismatch", "only PostgreSQL is supported")
    if not spec.metric_ids or len(spec.metric_ids) > _MAX_METRICS or len(set(spec.metric_ids)) != len(spec.metric_ids):
        raise TheLookQuerySpecValidationError("invalid_metric_ids", "metric_ids must contain 1-4 unique metrics")
    if any(not isinstance(metric, str) or not re.fullmatch(r"[a-z][a-z0-9_]+", metric) for metric in spec.metric_ids):
        raise TheLookQuerySpecValidationError("invalid_metric_ids", "metric_ids must be safe identifiers")

    active_catalog = catalog or CatalogLoader(THELOOK_WORKSPACE).load()
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
        raise TheLookQuerySpecValidationError("workspace_version_mismatch", "Catalog snapshot does not match QuerySpec")
    unknown = (set(spec.metric_ids) - set(active_catalog.metrics_by_id)) | (
        set(spec.metric_ids) - _SUPPORTED_METRICS
    )
    if unknown:
        raise TheLookQuerySpecValidationError("invalid_metric_ids", f"unknown metrics: {sorted(unknown)}")
    if spec.result_shape not in _SHAPES:
        raise TheLookQuerySpecValidationError("coverage_shape_not_permitted", "unsupported result shape")
    if spec.result_shape == "dimension_grouped":
        if spec.dimension not in _GROUP_DIMENSIONS:
            raise TheLookQuerySpecValidationError("coverage_shape_not_permitted", "unsupported TheLook grouping dimension")
        if any(spec.dimension not in active_catalog.metrics_by_id[m].allowed_dimensions for m in spec.metric_ids):
            raise TheLookQuerySpecValidationError("coverage_shape_not_permitted", "dimension is not allowed by every metric")
    elif spec.dimension is not None:
        raise TheLookQuerySpecValidationError("coverage_shape_not_permitted", "scalar/time series cannot contain a dimension")
    if spec.result_shape == "time_series" and spec.time.mode != "series":
        raise TheLookQuerySpecValidationError("invalid_time_contract", "time_series requires time.mode=series")
    if spec.result_shape != "time_series" and spec.time.mode == "series":
        raise TheLookQuerySpecValidationError("invalid_time_contract", "series grain is only valid for time_series")
    _validate_time(spec)
    if spec.result_shape == "time_series" and any(
        active_catalog.metrics_by_id[m].time_field != "orders.created_at"
        for m in spec.metric_ids
    ):
        raise TheLookQuerySpecValidationError(
            "coverage_shape_not_permitted",
            "TheLook time series currently requires orders.created_at for every metric",
        )
    expected_program = _default_join_program(spec.metric_ids, spec.result_shape, spec.dimension)
    if spec.join_program_id != expected_program:
        raise TheLookQuerySpecValidationError("coverage_shape_not_permitted", "join program does not match shape/dimension")
    expected_columns = _derive_result_columns(spec.metric_ids, spec.result_shape, spec.dimension)
    if spec.required_result_columns != expected_columns:
        raise TheLookQuerySpecValidationError("result_columns_do_not_match_contract", "required_result_columns must be derived and ordered")
    if spec.query_spec_id != spec.expected_query_spec_id():
        raise TheLookQuerySpecValidationError("invalid_query_spec", "query_spec_id does not match canonical content")
    return spec
