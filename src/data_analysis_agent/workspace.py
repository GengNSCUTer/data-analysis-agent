"""Dataset/workspace configuration shared by the trusted query pipeline.

The runtime components in this package are intentionally dataset-agnostic.  A
``WorkspaceProfile`` supplies the objects and versions for one deployment
while the current Olist catalog remains an adapter in ``metric_context`` and
``data/catalog``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]+$")


@dataclass(frozen=True)
class WorkspaceProfile:
    """Server-owned identity and access contract for one analysis workspace."""

    workspace_id: str
    dataset_id: str
    dataset_version: str
    metric_version: str
    catalog_version: str
    policy_version: str
    sql_dialect: str = "postgres"
    analytics_schema: str = "analytics"
    reader_role: str = "daa_analytics_reader"
    writer_role: str = "daa_app_writer"
    allowed_columns: Mapping[str, frozenset[str]] = MappingProxyType({})
    analyst_tables: frozenset[str] = frozenset()
    sensitive_projection_columns: frozenset[str] = frozenset()
    catalog_path: Path | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_id",
            "dataset_id",
            "dataset_version",
            "metric_version",
            "catalog_version",
            "policy_version",
            "sql_dialect",
            "analytics_schema",
            "reader_role",
            "writer_role",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

        normalized_tables = {
            str(table).lower(): frozenset(str(column).lower() for column in columns)
            for table, columns in dict(self.allowed_columns).items()
        }
        if not normalized_tables:
            raise ValueError("allowed_columns must contain at least one table")
        if any(not _IDENTIFIER.fullmatch(table) for table in normalized_tables):
            raise ValueError("allowed_columns contains an unsafe table identifier")
        if any(
            not columns or any(not _IDENTIFIER.fullmatch(column) for column in columns)
            for columns in normalized_tables.values()
        ):
            raise ValueError("allowed_columns contains an unsafe column identifier")

        analyst_tables = frozenset(str(table).lower() for table in self.analyst_tables)
        if not analyst_tables <= normalized_tables.keys():
            raise ValueError("analyst_tables must be a subset of allowed_columns")
        sensitive = frozenset(
            str(column).lower() for column in self.sensitive_projection_columns
        )
        known_columns = frozenset().union(*normalized_tables.values())
        if not sensitive <= known_columns:
            raise ValueError(
                "sensitive_projection_columns must be present in allowed_columns"
            )

        object.__setattr__(self, "allowed_columns", MappingProxyType(normalized_tables))
        object.__setattr__(self, "analyst_tables", analyst_tables)
        object.__setattr__(self, "sensitive_projection_columns", sensitive)
        if self.catalog_path is not None:
            object.__setattr__(self, "catalog_path", Path(self.catalog_path))

    @property
    def admin_tables(self) -> frozenset[str]:
        """Tables visible to the workspace's admin role."""

        return frozenset(self.allowed_columns)

    @property
    def dataset_version_id(self) -> str:
        """Alias used by the PostgreSQL audit schema."""

        return self.dataset_version

    def columns_for(self, table: str) -> frozenset[str]:
        """Return the server-owned column allowlist for a physical table."""

        try:
            return self.allowed_columns[table.lower()]
        except KeyError as exc:
            raise KeyError(f"unknown workspace table: {table}") from exc
