"""Runtime QueryPlan Gold SQL renderer for the TheLook workspace.

This is the TheLook counterpart of ``runtime_queryplan_renderer``.  It keeps
the existing reviewed v2 formula registry, but validates only the online
QueryPlan, ResultContract, Catalog and Workspace before compiling SQL.  The
offline TheLook QuerySpec is used as an internal formula bridge and its ID is
never exposed in the runtime artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from sqlglot import parse_one

from .query_plan import MetricPlan, QueryPlan
from .semantic_catalog import Catalog, ResultContract
from .thelook_v2_queryspec import (
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
    TheLookV2QuerySpecValidationError,
    TheLookV2WorkspacePin,
    validate_thelook_v2_query_spec,
)
from .thelook_v2_renderer import render_thelook_v2_gold_sql
from .thelook_v2_context import THELOOK_V2_WORKSPACE
from .thelook_queryspec import TheLookQueryTime
from .workspace import WorkspaceProfile


RUNTIME_THELOOK_QUERYPLAN_GOLD_RENDERER_VERSION = "runtime-thelook-queryplan-gold-renderer-v1"
_MAX_METRICS = 4
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]+$")
_TIME_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})


class RuntimeTheLookGoldRenderError(ValueError):
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class RuntimeTheLookGoldSqlArtifact:
    sql: str
    sql_sha256: str
    query_plan_sha256: str
    result_contract_sha256: str
    renderer_version: str
    workspace_id: str
    metric_ids: tuple[str, ...]
    dimensions: tuple[str, ...]
    time_grain: str | None
    required_result_columns: tuple[str, ...]
    join_program_id: str
    evidence: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "sql_sha256": self.sql_sha256,
            "query_plan_sha256": self.query_plan_sha256,
            "result_contract_sha256": self.result_contract_sha256,
            "renderer_version": self.renderer_version,
            "workspace_id": self.workspace_id,
            "metric_ids": list(self.metric_ids),
            "dimensions": list(self.dimensions),
            "time_grain": self.time_grain,
            "required_result_columns": list(self.required_result_columns),
            "join_program_id": self.join_program_id,
            "evidence": dict(self.evidence),
        }


def _sha(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _err(code: str, message: str) -> RuntimeTheLookGoldRenderError:
    return RuntimeTheLookGoldRenderError(code, message)


def _validate_snapshot(catalog: Catalog, contract: ResultContract, workspace: WorkspaceProfile) -> None:
    catalog_snapshot = (
        catalog.catalog_version, catalog.dataset_version,
        catalog.metric_version, catalog.policy_version,
    )
    workspace_snapshot = (
        workspace.catalog_version, workspace.dataset_version,
        workspace.metric_version, workspace.policy_version,
    )
    contract_snapshot = (
        contract.catalog_version, contract.dataset_version,
        contract.metric_version, contract.policy_version,
    )
    if catalog_snapshot != workspace_snapshot or contract_snapshot != catalog_snapshot:
        raise _err("workspace_version_mismatch", "TheLook Catalog/Contract/Workspace versions differ")
    if workspace.sql_dialect != "postgres":
        raise _err("workspace_version_mismatch", "TheLook Gold SQL requires PostgreSQL")
    if catalog.workspace is not None and catalog.workspace != workspace:
        raise _err("workspace_version_mismatch", "Catalog was loaded under another WorkspaceProfile")


def _validate_metrics(plan: QueryPlan, contract: ResultContract, catalog: Catalog) -> tuple[str, ...]:
    metric_ids = tuple(plan.metric_ids)
    if not metric_ids or len(metric_ids) > _MAX_METRICS or len(set(metric_ids)) != len(metric_ids):
        raise _err("invalid_metric_ids", "QueryPlan must contain 1-4 unique metrics")
    if any(not isinstance(metric, str) or not _SAFE_IDENTIFIER.fullmatch(metric) for metric in metric_ids):
        raise _err("invalid_metric_ids", "TheLook metric IDs must be safe identifiers")
    if set(metric_ids) != set(contract.metric_ids) or set(metric_ids) != set(contract.metric_result_columns):
        raise _err("metric_contract_mismatch", "QueryPlan and ResultContract metric sets differ")
    unknown = set(metric_ids) - set(catalog.metrics_by_id)
    if unknown:
        raise _err("invalid_metric_ids", f"TheLook Catalog does not define {sorted(unknown)}")
    plans = {item.metric_id: item for item in plan.metric_plans}
    if set(plans) != set(metric_ids):
        raise _err("metric_plan_mismatch", "metric_plans must cover exactly metric_ids")
    for metric_id in metric_ids:
        metric = catalog.metrics_by_id[metric_id]
        expected = MetricPlan.from_metric(metric)
        if plans[metric_id] != expected:
            raise _err("metric_plan_mismatch", f"metric plan drift for {metric_id}")
        if contract.metric_fact_grains.get(metric_id) != metric.grain:
            raise _err("metric_contract_mismatch", f"grain drift for {metric_id}")
        if dict(contract.metric_value_constraints.get(metric_id, {})) != dict(metric.result_value_constraints):
            raise _err("metric_contract_mismatch", f"value constraint drift for {metric_id}")
        if metric_id not in METRIC_FACT_DOMAINS:
            raise _err("metric_plan_mismatch", f"unsupported TheLook metric formula {metric_id}")
    return metric_ids


def _shape_and_columns(plan: QueryPlan, contract: ResultContract, metric_ids: tuple[str, ...]) -> str:
    dimensions = tuple(plan.dimensions)
    if len(dimensions) > 1 or any(not _SAFE_IDENTIFIER.fullmatch(item) for item in dimensions):
        raise _err("unsupported_queryplan_dimension", "TheLook renderer supports at most one dimension")
    if plan.top_n:
        raise _err("unsupported_queryplan_feature", "TheLook Top-N has no deterministic runtime renderer")
    if plan.comparison_baseline:
        raise _err("unsupported_queryplan_feature", "TheLook comparisons have no deterministic runtime renderer")
    if plan.time_grain is not None:
        if plan.time_grain not in _TIME_GRAINS:
            raise _err("invalid_time_contract", "unsupported TheLook time grain")
        shape = "time_series"
    elif not dimensions:
        shape = "scalar"
    else:
        shape = "dimension_grouped"
    expected = [*dimensions]
    if plan.time_grain:
        expected.append("time")
    expected.extend(metric_ids)
    if tuple(plan.required_result_columns) != tuple(expected):
        raise _err("result_columns_do_not_match_contract", "QueryPlan result columns are not canonical")
    if tuple(contract.required_result_columns) != tuple(plan.required_result_columns):
        raise _err("result_columns_do_not_match_contract", "QueryPlan and ResultContract columns differ")
    if plan.time_grain and contract.result_time_column is None:
        raise _err("result_columns_do_not_match_contract", "time series requires a result time column")
    if not plan.time_grain and contract.result_time_column is not None:
        raise _err("result_columns_do_not_match_contract", "non-series plan cannot require time")
    return shape


def _runtime_time(plan: QueryPlan, contract: ResultContract) -> TheLookQueryTime:
    raw = plan.time_range
    if raw is None:
        if contract.requested_start is not None or contract.requested_end is not None or contract.requested_end_exclusive:
            raise _err("invalid_time_contract", "unbounded TheLook plan has a contract range")
        if plan.time_grain:
            raise _err("invalid_time_contract", "TheLook series requires an explicit exclusive range")
        return TheLookQueryTime("all_time")
    if raw.get("end_semantics") != "exclusive":
        raise _err("invalid_time_contract", "TheLook runtime ranges require an exclusive end")
    start, end = raw.get("start"), raw.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise _err("invalid_time_contract", "TheLook range requires ISO start/end")
    try:
        if date.fromisoformat(end) <= date.fromisoformat(start):
            raise _err("invalid_time_contract", "TheLook end must be later than start")
    except ValueError as exc:
        raise _err("invalid_time_contract", "TheLook range requires ISO dates") from exc
    if (contract.requested_start, contract.requested_end) != (start, end) or not contract.requested_end_exclusive:
        raise _err("invalid_time_contract", "TheLook ResultContract endpoints differ from QueryPlan")
    if plan.time_grain:
        return TheLookQueryTime("series", start, end, plan.time_grain)
    return TheLookQueryTime("absolute_range", start, end)


def _runtime_projection(sql: str, columns: tuple[str, ...]) -> str:
    statement = parse_one(sql, read="postgres")
    expressions = {expression.alias_or_name: expression for expression in statement.expressions}
    if set(expressions) != set(columns):
        raise _err("result_columns_do_not_match_contract", "TheLook renderer projection differs from runtime columns")
    statement.set("expressions", [expressions[column] for column in columns])
    return statement.sql(dialect="postgres") + ";"


def render_runtime_thelook_queryplan_gold_sql(
    query_plan: QueryPlan,
    result_contract: ResultContract,
    catalog: Catalog,
    *,
    workspace: WorkspaceProfile | None = None,
) -> RuntimeTheLookGoldSqlArtifact:
    if not isinstance(query_plan, QueryPlan):
        raise _err("invalid_query_plan", "expected QueryPlan")
    if not isinstance(result_contract, ResultContract):
        raise _err("invalid_result_contract", "expected ResultContract")
    active_workspace = workspace or catalog.workspace
    if active_workspace is None:
        raise _err("workspace_version_mismatch", "TheLook renderer requires a WorkspaceProfile")
    _validate_snapshot(catalog, result_contract, active_workspace)
    metric_ids = _validate_metrics(query_plan, result_contract, catalog)
    shape = _shape_and_columns(query_plan, result_contract, metric_ids)
    for metric_id in metric_ids:
        metric = catalog.metrics_by_id[metric_id]
        for dimension in query_plan.dimensions:
            if dimension not in metric.allowed_dimensions:
                raise _err("dimension_not_supported_by_metric", f"{metric_id} does not allow {dimension}")
            policy = metric.dimension_policies.get(dimension)
            if policy is not None and policy.mode != "safe_direct":
                raise _err("attribution_not_frozen", f"{metric_id} × {dimension} requires attribution")
    if query_plan.attribution_requirements:
        raise _err("attribution_not_frozen", "TheLook attribution rules are not supported by this renderer")
    query_time = _runtime_time(query_plan, result_contract)
    try:
        spec = TheLookV2QuerySpec.create(
            metric_ids=metric_ids,
            result_shape=shape,
            dimension=query_plan.dimensions[0] if query_plan.dimensions else None,
            time=query_time,
            workspace=TheLookV2WorkspacePin.current(),
            required_result_columns=(
                (*metric_ids, "time") if shape == "time_series" else tuple(query_plan.required_result_columns)
            ),
        )
        spec = validate_thelook_v2_query_spec(spec, catalog)
        internal = render_thelook_v2_gold_sql(spec, catalog)
    except TheLookV2QuerySpecValidationError as exc:
        raise _err(exc.reason_code, str(exc)) from exc
    sql = internal.sql
    if shape == "time_series":
        sql = _runtime_projection(sql, tuple(query_plan.required_result_columns))
    plan_sha = _sha(query_plan.as_dict())
    contract_sha = _sha(result_contract.as_evidence())
    sql_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    evidence = MappingProxyType({
        "source": "runtime_query_plan",
        "renderer_version": RUNTIME_THELOOK_QUERYPLAN_GOLD_RENDERER_VERSION,
        "internal_formula_renderer_version": internal.renderer_version,
        "workspace_id": active_workspace.workspace_id,
        "catalog_version": catalog.catalog_version,
        "dataset_version": catalog.dataset_version,
        "metric_version": catalog.metric_version,
        "policy_version": catalog.policy_version,
        "query_plan_sha256": plan_sha,
        "result_contract_sha256": contract_sha,
        "sql_sha256": sql_sha,
        "metric_ids": list(metric_ids),
        "dimensions": list(query_plan.dimensions),
        "time_grain": query_plan.time_grain,
        "time_range": dict(query_plan.time_range) if query_plan.time_range else None,
        "end_semantics": query_plan.time_range.get("end_semantics") if query_plan.time_range else None,
        "required_result_columns": list(query_plan.required_result_columns),
        "join_program_id": spec.join_program_id,
        "plan_adherent": True,
    })
    return RuntimeTheLookGoldSqlArtifact(
        sql=sql,
        sql_sha256=sql_sha,
        query_plan_sha256=plan_sha,
        result_contract_sha256=contract_sha,
        renderer_version=RUNTIME_THELOOK_QUERYPLAN_GOLD_RENDERER_VERSION,
        workspace_id=active_workspace.workspace_id,
        metric_ids=metric_ids,
        dimensions=tuple(query_plan.dimensions),
        time_grain=query_plan.time_grain,
        required_result_columns=tuple(query_plan.required_result_columns),
        join_program_id=spec.join_program_id,
        evidence=evidence,
    )


__all__ = [
    "RUNTIME_THELOOK_QUERYPLAN_GOLD_RENDERER_VERSION",
    "RuntimeTheLookGoldRenderError",
    "RuntimeTheLookGoldSqlArtifact",
    "render_runtime_thelook_queryplan_gold_sql",
]
