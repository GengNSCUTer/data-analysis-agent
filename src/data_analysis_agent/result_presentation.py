"""Server-owned presentation transforms for already validated query results.

These helpers are intentionally display-only.  SQL validation, result
contracts, CSV artifacts and chart inputs keep their original typed values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


RATE_METRIC_IDS = frozenset(
    {
        "positive_review_rate",
        "on_time_delivery_rate",
        "cancellation_rate",
    }
)


@dataclass(frozen=True)
class ResultPresentation:
    """Bounded, server-owned metadata for presenting a validated result.

    ``rows`` are deliberately not part of this contract.  The caller passes
    only the bounded preview to the UI, while this DTO preserves the facts
    needed to distinguish that preview from the complete validated result.
    Export remains disabled until an authenticated, audited download contract
    is available.
    """

    total_row_count: int
    preview_row_count: int
    display_truncated: bool
    columns: tuple[str, ...]
    column_labels: Mapping[str, str]
    column_units: Mapping[str, str]
    preview_export_allowed: bool
    full_export_allowed: bool
    workspace_id: str
    dataset_version: str
    metric_version: str
    audit_ref: str
    artifact_id: str | None = None

    def __post_init__(self) -> None:
        if self.total_row_count < 0:
            raise ValueError("total_row_count must be non-negative")
        if self.preview_row_count < 0 or self.preview_row_count > self.total_row_count:
            raise ValueError("preview_row_count must be within total_row_count")
        if self.display_truncated != (self.preview_row_count < self.total_row_count):
            raise ValueError("display_truncated must match preview/total row counts")
        if not self.workspace_id or not self.dataset_version or not self.metric_version:
            raise ValueError("workspace and version references are required")
        if not self.audit_ref:
            raise ValueError("audit_ref is required")
        if self.full_export_allowed and not self.preview_export_allowed:
            raise ValueError("full export cannot be enabled without preview export")

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_row_count": self.total_row_count,
            "preview_row_count": self.preview_row_count,
            "display_truncated": self.display_truncated,
            "columns": list(self.columns),
            "column_labels": dict(self.column_labels),
            "column_units": dict(self.column_units),
            "preview_export_allowed": self.preview_export_allowed,
            "full_export_allowed": self.full_export_allowed,
            "workspace_id": self.workspace_id,
            "dataset_version": self.dataset_version,
            "metric_version": self.metric_version,
            "audit_ref": self.audit_ref,
            "artifact_id": self.artifact_id,
        }


def build_result_presentation(
    *,
    total_row_count: int,
    preview_row_count: int,
    columns: Sequence[str],
    labels: Mapping[str, str] | None,
    units: Mapping[str, str] | None,
    workspace_id: str,
    dataset_version: str,
    metric_version: str,
    audit_ref: str,
    artifact_id: str | None = None,
) -> ResultPresentation:
    """Create the fail-closed presenter contract from server-owned metadata."""
    normalized_columns = tuple(str(column)[:128] for column in columns)
    supplied_labels = labels if isinstance(labels, Mapping) else {}
    supplied_units = units if isinstance(units, Mapping) else {}
    column_labels = {
        column: display_label(column, supplied_labels) for column in normalized_columns
    }
    column_units = {
        column: str(supplied_units[column])[:40]
        for column in normalized_columns
        if column in supplied_units and str(supplied_units[column]).strip()
    }
    return ResultPresentation(
        total_row_count=max(0, int(total_row_count)),
        preview_row_count=max(0, int(preview_row_count)),
        display_truncated=preview_row_count < total_row_count,
        columns=normalized_columns,
        column_labels=column_labels,
        column_units=column_units,
        # No generic UI export can claim to be a complete, audited download.
        preview_export_allowed=False,
        full_export_allowed=False,
        workspace_id=str(workspace_id)[:128] or "unknown",
        dataset_version=str(dataset_version)[:128] or "unknown",
        metric_version=str(metric_version)[:128] or "unknown",
        audit_ref=str(audit_ref)[:128] or "unknown",
        artifact_id=str(artifact_id)[:128] if artifact_id else None,
    )


def display_label(column: str, labels: Mapping[str, str]) -> str:
    """Resolve one bounded server-owned result column label."""
    if column in labels:
        return str(labels[column])[:80]
    normalized = _normalized(column)
    for candidate, label in labels.items():
        if _normalized(str(candidate)) == normalized:
            return str(label)[:80]
    return column.replace("_", " ")[:80]


def display_value(column: str, value: Any) -> str:
    """Format a scalar for UI without changing the trusted result value."""
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)[:120]
    if _normalized(column) in {_normalized(metric) for metric in RATE_METRIC_IDS} and 0 <= numeric <= 1:
        return f"{numeric * 100:.2f}%"
    if _normalized(column) == "averagedeliverydays":
        return f"{numeric:.2f} 天"
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}"


def display_records(
    records: Sequence[Mapping[str, Any]], *, columns: Sequence[str], labels: Mapping[str, str]
) -> tuple[list[dict[str, str]], list[str]]:
    """Return UI records with presentation labels and safely formatted values."""
    display_columns = [display_label(str(column), labels) for column in columns]
    rendered = [
        {
            display_label(str(column), labels): display_value(str(column), row.get(column))
            for column in columns
        }
        for row in records
    ]
    return rendered, display_columns


def _normalized(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())
