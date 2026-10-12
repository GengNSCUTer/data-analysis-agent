"""Deterministic Gold SQL for the online QueryPlan contract.

The offline ``QuerySpec`` renderer remains the owner of the reviewed Olist
formula registry.  This module is the runtime-facing boundary around it: it
accepts only the server-produced ``QueryPlan``, ``ResultContract`` and
versioned ``Catalog``.  It never reads an SFT row, an offline QuerySpec, or a
model-generated SQL candidate.

The small bridge to ``olist_queryspec`` is intentional.  It reuses the same
audited SQL formulas while requiring the online plan and result contract to
agree before rendering.  The internal QuerySpec identifier is not part of the
runtime artifact or its evidence.
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

from .olist_queryspec import (
    QuerySpec,
    QuerySpecValidationError,
    QueryTime,
    WorkspacePin,
    render_gold_sql,
)
from .query_plan import MetricPlan, QueryPlan
from .semantic_catalog import Catalog, ResultContract
from .workspace import WorkspaceProfile


RUNTIME_QUERYPLAN_GOLD_RENDERER_VERSION = "runtime-queryplan-gold-renderer-v1"
_MAX_METRICS = 4
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]+$")
_TIME_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})
_SUPPORTED_DIMENSIONS = frozenset({"customer_state", "product_category_name"})


class RuntimeGoldRenderError(ValueError):
    """A runtime plan cannot be rendered under the frozen Gold contract."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class RuntimeGoldSqlArtifact:
    """A runtime-plan-owned Gold SQL artifact and its reproducibility evidence."""

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


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error(reason_code: str, message: str) -> RuntimeGoldRenderError:
    return RuntimeGoldRenderError(reason_code, message)


def _validate_workspace_snapshot(
    catalog: Catalog, contract: ResultContract, workspace: WorkspaceProfile
) -> None:
    snapshot = (
        catalog.catalog_version,
        catalog.dataset_version,
        catalog.metric_version,
        catalog.policy_version,
    )
    expected = (
        workspace.catalog_version,
        workspace.dataset_version,
        workspace.metric_version,
        workspace.policy_version,
    )
    if snapshot != expected:
        raise _error(
            "workspace_version_mismatch",
            "Catalog versions do not match the runtime WorkspaceProfile",
        )
    contract_snapshot = (
        contract.catalog_version,
        contract.dataset_version,
        contract.metric_version,
        contract.policy_version,
    )
    if contract_snapshot != snapshot:
        raise _error(
            "workspace_version_mismatch",
            "ResultContract versions do not match the runtime Catalog",
        )
    if workspace.sql_dialect != "postgres":
        raise _error("workspace_version_mismatch", "runtime Gold SQL requires PostgreSQL")
    if catalog.workspace is not None and catalog.workspace != workspace:
        raise _error(
            "workspace_version_mismatch",
            "Catalog was loaded under a different WorkspaceProfile",
        )


def _validate_metric_contract(
    plan: QueryPlan, contract: ResultContract, catalog: Catalog
) -> tuple[str, ...]:
    metric_ids = tuple(plan.metric_ids)
    if not metric_ids or len(metric_ids) > _MAX_METRICS:
        raise _error("invalid_metric_ids", "QueryPlan must contain 1-4 metrics")
    if len(set(metric_ids)) != len(metric_ids) or any(
        not isinstance(metric_id, str) or not _SAFE_IDENTIFIER.fullmatch(metric_id)
        for metric_id in metric_ids
    ):
        raise _error("invalid_metric_ids", "QueryPlan metric IDs must be unique safe identifiers")
    if set(metric_ids) != set(contract.metric_ids):
        raise _error(
            "metric_contract_mismatch",
            "QueryPlan and ResultContract metric sets differ",
        )
    if set(metric_ids) != set(contract.metric_result_columns):
        raise _error(
            "metric_contract_mismatch",
            "ResultContract metric result columns differ from QueryPlan",
        )
    if not contract.exact_result_columns:
        raise _error(
            "result_columns_do_not_match_contract",
            "runtime Gold SQL requires exact result columns",
        )

    metrics_by_id = catalog.metrics_by_id
    unknown = set(metric_ids) - set(metrics_by_id)
    if unknown:
        raise _error("invalid_metric_ids", f"Catalog does not define metrics: {sorted(unknown)}")

    plans_by_id = {metric.metric_id: metric for metric in plan.metric_plans}
    if set(plans_by_id) != set(metric_ids):
        raise _error(
            "metric_plan_mismatch",
            "QueryPlan metric_plans must cover exactly metric_ids",
        )
    for metric_id in metric_ids:
        expected = MetricPlan.from_metric(metrics_by_id[metric_id])
        if plans_by_id[metric_id] != expected:
            raise _error(
                "metric_plan_mismatch",
                f"QueryPlan metric plan does not match Catalog metric {metric_id}",
            )
        if contract.metric_fact_grains.get(metric_id) != expected.grain:
            raise _error(
                "metric_contract_mismatch",
                f"ResultContract grain does not match Catalog metric {metric_id}",
            )
        expected_constraints = dict(metrics_by_id[metric_id].result_value_constraints)
        actual_constraints = dict(contract.metric_value_constraints.get(metric_id, {}))
        if actual_constraints != expected_constraints:
            raise _error(
                "metric_contract_mismatch",
                f"ResultContract value constraints do not match Catalog metric {metric_id}",
            )
    return metric_ids


def _validate_shape(plan: QueryPlan, contract: ResultContract, metric_ids: tuple[str, ...]) -> str:
    dimensions = tuple(plan.dimensions)
    if len(dimensions) > 1 or any(
        not isinstance(dimension, str) or not _SAFE_IDENTIFIER.fullmatch(dimension)
        for dimension in dimensions
    ):
        raise _error(
            "unsupported_queryplan_dimension",
            "runtime Gold SQL currently supports at most one safe dimension",
        )
    unsupported = set(dimensions) - _SUPPORTED_DIMENSIONS
    if unsupported:
        raise _error(
            "unsupported_queryplan_dimension",
            f"deterministic renderer has no formula for dimensions: {sorted(unsupported)}",
        )
    if plan.top_n:
        raise _error(
            "unsupported_queryplan_feature",
            "top-N plans require a dedicated deterministic ranking renderer",
        )
    if plan.comparison_baseline:
        raise _error(
            "unsupported_queryplan_feature",
            "comparison plans require a dedicated baseline contract",
        )

    if dimensions:
        for metric_id in metric_ids:
            # The detailed allowed-dimension check is performed in the metric
            # plan validation above; this branch only guards missing metadata.
            if metric_id not in contract.metric_fact_grains:
                raise _error("metric_contract_mismatch", f"missing grain for {metric_id}")

    if plan.time_grain is not None:
        if plan.time_grain not in _TIME_GRAINS:
            raise _error("invalid_time_contract", "QueryPlan has an unsupported time grain")
        shape = "time_series"
    elif dimensions == ():
        shape = "scalar"
    elif dimensions == ("customer_state",):
        shape = "state_grouped"
    elif dimensions == ("product_category_name",):
        shape = "category_grouped"
    else:  # pragma: no cover - guarded by _SUPPORTED_DIMENSIONS above
        raise _error("unsupported_queryplan_dimension", "unsupported grouped shape")

    expected_columns = list(dimensions)
    if plan.time_grain is not None:
        expected_columns.append("time")
    expected_columns.extend(metric_ids)
    if tuple(plan.required_result_columns) != tuple(expected_columns):
        raise _error(
            "result_columns_do_not_match_contract",
            "QueryPlan required_result_columns are not canonical for its shape",
        )
    if tuple(contract.required_result_columns) != tuple(plan.required_result_columns):
        raise _error(
            "result_columns_do_not_match_contract",
            "QueryPlan and ResultContract result columns differ",
        )
    if plan.time_grain is None and contract.result_time_column is not None:
        raise _error(
            "result_columns_do_not_match_contract",
            "a non-series plan cannot require a result time column",
        )
    if plan.time_grain is not None and contract.result_time_column is None:
        raise _error(
            "result_columns_do_not_match_contract",
            "a series plan must identify its Catalog time column",
        )
    return shape


def _runtime_time(plan: QueryPlan, contract: ResultContract) -> QueryTime:
    raw = plan.time_range
    if raw is None:
        if contract.requested_start is not None or contract.requested_end is not None:
            raise _error(
                "invalid_time_contract",
                "ResultContract contains a time range absent from QueryPlan",
            )
        if contract.requested_end_exclusive:
            raise _error(
                "invalid_time_contract",
                "unbounded QueryPlan cannot mark an exclusive end",
            )
        if plan.time_grain is not None:
            raise _error(
                "invalid_time_contract",
                "time series requires an explicit exclusive time range",
            )
        return QueryTime("all_time")

    if not isinstance(raw, Mapping):
        raise _error("invalid_time_contract", "QueryPlan time_range must be an object")
    start = raw.get("start")
    end = raw.get("end")
    if raw.get("end_semantics") != "exclusive":
        raise _error(
            "invalid_time_contract",
            "runtime QueryPlan ranges must use an exclusive end",
        )
    if not isinstance(start, str) or not isinstance(end, str):
        raise _error("invalid_time_contract", "runtime time range requires start and end")
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise _error("invalid_time_contract", "runtime time range must use ISO dates") from exc
    if end_date <= start_date:
        raise _error("invalid_time_contract", "runtime end must be later than start")
    if (contract.requested_start, contract.requested_end) != (start, end):
        raise _error(
            "invalid_time_contract",
            "ResultContract time endpoints differ from QueryPlan",
        )
    if not contract.requested_end_exclusive:
        raise _error(
            "invalid_time_contract",
            "bounded runtime QueryPlan requires ResultContract exclusive end",
        )
    if plan.time_grain is None:
        return QueryTime("absolute_range", start, end)
    return QueryTime("series", start, end, plan.time_grain)


def _runtime_projection_sql(sql: str, required_columns: tuple[str, ...]) -> str:
    """Reorder the legacy no-dimension series projection to runtime order.

    The reviewed offline renderer historically emitted ``metric..., time``.
    The online QueryPlan contract emits ``time, metric...``.  SQLGlot lets us
    change only the outer projection while leaving every CTE and predicate
    byte-for-byte semantically intact.
    """

    statement = parse_one(sql, read="postgres")
    expressions = list(statement.expressions)
    by_alias = {expression.alias_or_name: expression for expression in expressions}
    if set(by_alias) != set(required_columns):
        raise _error(
            "result_columns_do_not_match_contract",
            "deterministic renderer projection does not match runtime columns",
        )
    statement.set("expressions", [by_alias[column] for column in required_columns])
    return statement.sql(dialect="postgres") + ";"


def render_runtime_queryplan_gold_sql(
    query_plan: QueryPlan,
    result_contract: ResultContract,
    catalog: Catalog,
    *,
    workspace: WorkspaceProfile | None = None,
) -> RuntimeGoldSqlArtifact:
    """Render Gold SQL from the online runtime contract.

    The function fails closed for unsupported runtime features.  A rejected
    plan is recorded by its caller as ``gold_disagreement`` or
    ``renderer_rejected``; it is never silently converted into a different
    query shape.
    """

    if not isinstance(query_plan, QueryPlan):
        raise _error("invalid_query_plan", "expected a QueryPlan")
    if not isinstance(result_contract, ResultContract):
        raise _error("invalid_result_contract", "expected a ResultContract")
    if not isinstance(catalog, Catalog):
        raise _error("invalid_catalog", "expected a Catalog")
    active_workspace = workspace or catalog.workspace
    if active_workspace is None:
        raise _error("workspace_version_mismatch", "runtime renderer requires a WorkspaceProfile")

    _validate_workspace_snapshot(catalog, result_contract, active_workspace)
    metric_ids = _validate_metric_contract(query_plan, result_contract, catalog)
    shape = _validate_shape(query_plan, result_contract, metric_ids)
    query_time = _runtime_time(query_plan, result_contract)

    # Attribution rules are Catalog policy. The renderer only handles direct,
    # deterministic dimensions; payment share, multi-membership, and similar
    # rules must get their own reviewed renderer rather than being guessed.
    expected_requirements = set()
    for metric_id in metric_ids:
        definition = catalog.metrics_by_id[metric_id]
        for dimension in query_plan.dimensions:
            policy = definition.dimension_policies.get(dimension)
            if policy is not None:
                expected_requirements.add(
                    (
                        metric_id,
                        definition.grain,
                        dimension,
                        policy.mode,
                        policy.attribution_rule_id,
                    )
                )
            if policy is not None and policy.mode != "safe_direct":
                raise _error(
                    "attribution_not_frozen",
                    f"{metric_id} × {dimension} requires a reviewed attribution rule",
                )
            if dimension not in definition.allowed_dimensions:
                raise _error(
                    "dimension_not_supported_by_metric",
                    f"Catalog metric {metric_id} does not allow {dimension}",
                )
    actual_requirements = {
        (
            requirement.metric_id,
            requirement.metric_grain,
            requirement.dimension,
            requirement.policy_mode,
            requirement.attribution_rule_id,
        )
        for requirement in query_plan.attribution_requirements
    }
    if actual_requirements != expected_requirements:
        raise _error(
            "attribution_contract_mismatch",
            "QueryPlan attribution requirements do not match the Catalog",
        )

    try:
        internal_required_columns = tuple(query_plan.required_result_columns)
        if shape == "time_series" and not query_plan.dimensions:
            # The legacy offline contract is metrics-first; the runtime
            # contract is time-first. Reorder only the final projection below.
            internal_required_columns = (*metric_ids, "time")
        spec = QuerySpec.create(
            metric_ids=metric_ids,
            result_shape=shape,
            dimension=query_plan.dimensions[0] if query_plan.dimensions else None,
            time=query_time,
            workspace=WorkspacePin.current(active_workspace),
            required_result_columns=internal_required_columns,
        )
        internal_artifact = render_gold_sql(spec, catalog)
    except QuerySpecValidationError as exc:
        raise _error(exc.reason_code, str(exc)) from exc

    plan_sha = _canonical_sha256(query_plan.as_dict())
    contract_sha = _canonical_sha256(result_contract.as_evidence())
    sql = internal_artifact.sql
    if shape == "time_series" and not query_plan.dimensions:
        sql = _runtime_projection_sql(sql, tuple(query_plan.required_result_columns))
    sql_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    evidence = MappingProxyType(
        {
            "source": "runtime_query_plan",
            "renderer_version": RUNTIME_QUERYPLAN_GOLD_RENDERER_VERSION,
            "internal_formula_renderer_version": internal_artifact.renderer_version,
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
            "end_semantics": (
                query_plan.time_range.get("end_semantics")
                if query_plan.time_range
                else None
            ),
            "required_result_columns": list(query_plan.required_result_columns),
            "join_program_id": spec.join_program_id,
            "plan_adherent": True,
        }
    )
    return RuntimeGoldSqlArtifact(
        sql=sql,
        sql_sha256=sql_sha,
        query_plan_sha256=plan_sha,
        result_contract_sha256=contract_sha,
        renderer_version=RUNTIME_QUERYPLAN_GOLD_RENDERER_VERSION,
        workspace_id=active_workspace.workspace_id,
        metric_ids=metric_ids,
        dimensions=tuple(query_plan.dimensions),
        time_grain=query_plan.time_grain,
        required_result_columns=tuple(query_plan.required_result_columns),
        join_program_id=spec.join_program_id,
        evidence=evidence,
    )


__all__ = [
    "RUNTIME_QUERYPLAN_GOLD_RENDERER_VERSION",
    "RuntimeGoldRenderError",
    "RuntimeGoldSqlArtifact",
    "render_runtime_queryplan_gold_sql",
]
