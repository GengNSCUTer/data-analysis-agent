"""Catalog-bound equality filters shared by semantic admission and QueryPlan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .semantic_catalog import CatalogSelection


@dataclass(frozen=True)
class QueryFilter:
    dimension: str
    value: str
    sources: tuple[tuple[str, str], ...]
    value_sources: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = {"dimension": self.dimension, "value": self.value,
                  "sources": [{"table": table, "column": column} for table, column in self.sources]}
        if self.value_sources:
            result["value_sources"] = [{"table": table, "column": column} for table, column in self.value_sources]
        return result


def bind_query_filters(
    selection: CatalogSelection, filters: Sequence[Mapping[str, str]],
) -> tuple[QueryFilter, ...]:
    """Bind visible logical dimensions to columns actually supplied to the model."""
    result = []
    bindings = selection.workspace.dimension_bindings if selection.workspace else {}
    selected_columns = dict(selection.trace.selected_columns)
    for item in filters:
        dimension, value = item["dimension"], item["value"]
        if any(dimension not in metric.allowed_dimensions for metric in selection.metrics):
            raise ValueError(f"指标不支持筛选维度：{dimension}")
        binding = bindings.get(dimension)
        sources = tuple(
            (table.physical_name, column.name)
            for table in selection.tables for column in table.columns
            if not column.sensitive and column.name in selected_columns.get(table.table_id, ())
            and ((table.table_id, column.name) in binding.sources if binding else
                 column.name == dimension or dimension in column.aliases)
        )
        if not sources:
            raise ValueError(f"当前 Catalog 未提供筛选维度的可用字段：{dimension}")
        result.append(QueryFilter(dimension, value, sources))
    return tuple(result)
