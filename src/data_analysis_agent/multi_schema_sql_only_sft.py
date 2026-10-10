"""Deterministic contracts for the multi-schema SQL-only SFT release.

The module is deliberately offline-only.  It turns a pinned Catalog and a
validated QuerySpec into the *server-owned* parts of the common prompt.  It
does not route natural language, render Gold SQL, access PostgreSQL, or load a
model.  Those operations belong to earlier admission stages and later trainer
stages respectively.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from .semantic_catalog import Catalog


MULTI_SCHEMA_SQL_ONLY_PROMPT_VERSION = "multi-schema-sql-only-v1"


class MultiSchemaSftError(ValueError):
    """Raised when an offline multi-schema example lacks pinned semantics."""


# QuerySpec dimension IDs are semantic, while TheLook's physical Catalog uses
# concise source column names.  Keep this translation here, in the prompt
# projection layer; it does not grant access to a column absent from Catalog.
_DIMENSION_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "customer_state": ("state", "customer_state"),
    "customer_city": ("city", "customer_city"),
    "customer_traffic_source": ("traffic_source", "customer_traffic_source"),
    "product_category": ("category", "product_category"),
    "product_brand": ("brand", "product_brand"),
    "product_department": ("department", "product_department"),
    "distribution_center": ("distribution_center_id", "distribution_center"),
    "event_type": ("event_type",),
    "event_browser": ("browser", "event_browser"),
    "event_traffic_source": ("traffic_source", "event_traffic_source"),
    "user_country": ("country", "user_country"),
    "user_traffic_source": ("traffic_source", "user_traffic_source"),
    "user_state": ("state", "user_state"),
    "product_category_name": ("product_category_name",),
}


def _table_ids_for_spec(catalog: Catalog, spec: Mapping[str, Any]) -> set[str]:
    metric_ids = spec.get("metric_ids")
    dimension = spec.get("dimension")
    if not isinstance(metric_ids, list) or not all(isinstance(item, str) for item in metric_ids):
        raise MultiSchemaSftError("QuerySpec metric_ids must be a string list")
    if dimension is not None and not isinstance(dimension, str):
        raise MultiSchemaSftError("QuerySpec dimension must be a string or null")
    selected: set[str] = set()
    for metric_id in metric_ids:
        try:
            metric = catalog.metrics_by_id[metric_id]
        except KeyError as exc:
            raise MultiSchemaSftError(f"unknown Catalog metric: {metric_id}") from exc
        selected.update(metric.source_tables)
    if dimension:
        aliases = _DIMENSION_COLUMN_ALIASES.get(dimension, (dimension,))
        candidates = [
            table.table_id
            for table in catalog.tables
            if any(column.name in aliases for column in table.columns)
        ]
        if not candidates:
            raise MultiSchemaSftError(f"Catalog has no table for dimension: {dimension}")
        # A Catalog may expose the same column name in several facts.  Choose
        # the closest join-connected table, rather than including every such
        # table and needlessly bloating the prompt with another fact domain.
        graph: dict[str, list[str]] = {}
        for join in catalog.joins:
            graph.setdefault(join.from_table, []).append(join.to_table)
            graph.setdefault(join.to_table, []).append(join.from_table)
        frontier = list(selected)
        parent = dict.fromkeys(selected)
        target: str | None = next((item for item in candidates if item in selected), None)
        while frontier and target is None:
            current = frontier.pop(0)
            for adjacent in graph.get(current, []):
                if adjacent in parent:
                    continue
                parent[adjacent] = current
                if adjacent in candidates:
                    target = adjacent
                    break
                frontier.append(adjacent)
        if target is None:
            raise MultiSchemaSftError(f"dimension {dimension} is not join-connected to metric facts")
        while target is not None:
            selected.add(target)
            target = parent[target]
    if not selected:
        raise MultiSchemaSftError("QuerySpec selected no Catalog tables")
    return selected


def render_catalog_payload(catalog: Catalog, query_spec: Mapping[str, Any]) -> dict[str, Any]:
    """Produce a compact, deterministic, role-scoped Catalog projection.

    The renderer exposes only tables reachable from the QuerySpec's frozen
    metric/dimension semantics.  Sensitive columns remain marked as join-only;
    the prompt is context, not an authorization expansion.
    """

    table_ids = _table_ids_for_spec(catalog, query_spec)
    tables = []
    for table in sorted(catalog.tables, key=lambda item: item.table_id):
        if table.table_id not in table_ids:
            continue
        tables.append(
            {
                "table_id": table.table_id,
                "physical_name": f"analytics.{table.physical_name}",
                "grain": table.grain,
                "description": table.description,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "description": column.description,
                        "sensitive_join_only": column.sensitive,
                    }
                    for column in table.columns
                ],
            }
        )
    metric_ids = query_spec["metric_ids"]
    metrics = []
    for metric_id in metric_ids:
        metric = catalog.metrics_by_id[metric_id]
        metrics.append(
            {
                "metric_id": metric.metric_id,
                "name": metric.name,
                "description": metric.description,
                "grain": metric.grain,
                "time_field": metric.time_field,
                "source_tables": list(metric.source_tables),
                "default_filters": list(metric.default_filters),
                "allowed_dimensions": list(metric.allowed_dimensions),
            }
        )
    joins = [
        {
            "join_id": join.join_id,
            "from_table": join.from_table,
            "to_table": join.to_table,
            "cardinality": join.cardinality,
            "on": join.on,
        }
        for join in sorted(catalog.joins, key=lambda item: item.join_id)
        if join.from_table in table_ids and join.to_table in table_ids
    ]
    return {
        "catalog_version": catalog.catalog_version,
        "dataset_version": catalog.dataset_version,
        "metric_version": catalog.metric_version,
        "policy_version": catalog.policy_version,
        "dialect": "PostgreSQL",
        "tables": tables,
        "metrics": metrics,
        "joins": joins,
    }


def build_result_contract_payload(catalog: Catalog, query_spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build the result-side contract solely from frozen Catalog/QuerySpec facts."""

    metric_ids = query_spec.get("metric_ids")
    required = query_spec.get("required_result_columns")
    time = query_spec.get("time")
    shape = query_spec.get("result_shape")
    if (
        not isinstance(metric_ids, list)
        or not all(isinstance(item, str) for item in metric_ids)
        or not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or not isinstance(time, Mapping)
        or not isinstance(shape, str)
    ):
        raise MultiSchemaSftError("QuerySpec lacks result-contract fields")
    metrics = [catalog.metrics_by_id[metric_id] for metric_id in metric_ids]
    return {
        "catalog_version": catalog.catalog_version,
        "dataset_version": catalog.dataset_version,
        "metric_version": catalog.metric_version,
        "policy_version": catalog.policy_version,
        "metric_ids": metric_ids,
        "required_result_columns": required,
        "metric_result_columns": metric_ids,
        "result_time_column": "time" if shape == "time_series" else None,
        "requested_start": time.get("start"),
        "requested_end": time.get("end_exclusive"),
        "exact_result_columns": True,
        "metric_fact_grains": {metric.metric_id: metric.grain for metric in metrics},
        "metric_value_constraints": {
            metric.metric_id: dict(metric.result_value_constraints) for metric in metrics
        },
    }


def render_multi_schema_prompt(
    *,
    catalog_payload: Mapping[str, Any],
    query_spec: Mapping[str, Any],
    result_contract: Mapping[str, Any],
    question: str,
) -> str:
    """Render the exact shared outer SQL-only prompt without SQL target text."""

    if not isinstance(question, str) or not question.strip():
        raise MultiSchemaSftError("question must be non-empty")
    return "\n".join(
        (
            "### Task",
            "Generate exactly one read-only SQL query for the supplied Chinese business question.",
            "### SQL dialect",
            "PostgreSQL",
            "### Candidate contract",
            "Return SQL only; one SELECT or WITH ... SELECT statement; use only the server-provided workspace Catalog.",
            "### Server-provided Semantic Catalog",
            json.dumps(catalog_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "### Server-provided QuerySpec",
            json.dumps(query_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "### Server-provided Result Contract",
            json.dumps(result_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "### Question",
            question.strip(),
            "### SQL",
        )
    )


def training_text(prompt: str, canonical_sql: str) -> str:
    if not prompt.endswith("### SQL") or not canonical_sql.strip():
        raise MultiSchemaSftError("invalid prompt or canonical SQL")
    return f"{prompt}\n{canonical_sql.strip()}"
