"""Shared deterministic execution contracts for TheLook v3 release gates.

TheLook v3 intentionally keeps the v2 QuerySpec and renderer frozen.  This
module derives the result-side contract from that offline plan and pinned
Catalog, so admission does not need to invent a question or use retrieval.
"""

from __future__ import annotations

from types import MappingProxyType

import pandas as pd
from sqlglot import exp, parse_one

from .result_validator import ResultValidation, ResultValidator
from .semantic_catalog import Catalog, ResultContract
from .thelook_v2_context import THELOOK_V2_WORKSPACE
from .thelook_v2_queryspec import TheLookV2QuerySpec, validate_thelook_v2_query_spec


def build_thelook_v3_result_contract(
    spec: TheLookV2QuerySpec, catalog: Catalog
) -> ResultContract:
    """Build a server-owned result contract from a validated offline plan.

    Production requests derive this object from retrieval and a user question.
    A protected offline Gold release has neither: its already validated
    QuerySpec is the semantic request.  This function therefore has no
    question argument and preserves only Catalog/QuerySpec-owned facts.
    """

    validated = validate_thelook_v2_query_spec(spec, catalog)
    metrics = tuple(catalog.metrics_by_id[metric_id] for metric_id in validated.metric_ids)
    labels = {metric.metric_id: metric.name for metric in metrics}
    if validated.dimension:
        labels[validated.dimension] = validated.dimension.replace("_", " ")
    if validated.result_shape == "time_series":
        labels["time"] = "时间"
    return ResultContract(
        catalog_version=catalog.catalog_version,
        dataset_version=catalog.dataset_version,
        metric_version=catalog.metric_version,
        policy_version=catalog.policy_version,
        metric_ids=validated.metric_ids,
        required_result_columns=validated.required_result_columns,
        metric_result_columns=validated.metric_ids,
        result_time_column="time" if validated.result_shape == "time_series" else None,
        result_time_column_aliases=("time",) if validated.result_shape == "time_series" else (),
        requested_start=validated.time.start,
        requested_end=validated.time.end_exclusive,
        selected_join_ids=(),
        result_column_labels=MappingProxyType(labels),
        metric_fact_grains=MappingProxyType(
            {metric.metric_id: metric.grain for metric in metrics}
        ),
        exact_result_columns=True,
        metric_value_constraints=MappingProxyType(
            {
                metric.metric_id: metric.result_value_constraints
                for metric in metrics
            }
        ),
    )


def validate_thelook_result(
    frame: pd.DataFrame,
    contract: ResultContract,
    *,
    time_bucket_grain: str | None,
    max_rows: int,
    display_row_budget: int = 200,
) -> ResultValidation:
    """Validate a TheLook result against a caller-owned execution contract.

    The protected v3 release intentionally passes ``max_rows=200``.  Online
    and SFT admission callers use the runtime 1,000-row execution cap while
    retaining an independent 200-row display preview budget.
    """

    return ResultValidator(max_rows=max_rows, display_row_budget=display_row_budget).validate(
        frame,
        required_columns=contract.required_result_columns,
        metric_columns=contract.metric_result_columns,
        time_column=contract.result_time_column,
        time_column_aliases=contract.result_time_column_aliases,
        time_bucket_grain=time_bucket_grain,
        requested_start=contract.requested_start,
        requested_end=contract.requested_end,
        exact_columns=contract.exact_result_columns,
        metric_value_constraints=contract.metric_value_constraints,
    )


def validate_thelook_v3_result(
    frame: pd.DataFrame,
    contract: ResultContract,
    *,
    time_bucket_grain: str | None,
) -> ResultValidation:
    """Validate a protected v3 result under its frozen 200-row contract."""

    return validate_thelook_result(
        frame,
        contract,
        time_bucket_grain=time_bucket_grain,
        max_rows=200,
        display_row_budget=200,
    )


def assert_thelook_v3_gold_uses_analytics_views_only(sql: str) -> None:
    """Independently ensure canonical Gold SQL cannot read raw TheLook tables."""

    try:
        statement = parse_one(sql, read="postgres")
    except Exception as exc:  # sqlglot provides several parse exception types.
        raise ValueError("Gold SQL is not valid PostgreSQL") from exc
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    accessed = 0
    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if not table.db and name in cte_names:
            continue
        accessed += 1
        schema = table.db.lower() if table.db else ""
        if table.catalog or schema != THELOOK_V2_WORKSPACE.analytics_schema:
            raise ValueError("Gold SQL must access only the analytics schema")
        if name not in THELOOK_V2_WORKSPACE.analyst_tables:
            raise ValueError(f"Gold SQL accesses a non-analyst table: {name}")
    if not accessed:
        raise ValueError("Gold SQL must access at least one analytics view")
