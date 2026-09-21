"""Bounded, server-owned references to trusted query results.

This is intentionally *not* a result cache.  It preserves only the already
validated result summary and the contract/version identity needed to restore a
conversation after a client reload. Raw SQL and full result rows remain
repository-external; conversation memory stores only opaque references and
integrity metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping


@dataclass(frozen=True)
class ResultArtifact:
    """One replayable, trusted-result summary for the active conversation."""

    artifact_id: str
    summary: str
    metric_ids: tuple[str, ...]
    result_columns: tuple[str, ...]
    dataset_version: str
    metric_version: str
    chart_state: str
    version: str = "trusted-result-artifact-v1"
    workspace_id: str = "unknown"
    csv_ref: str | None = None
    csv_sha256: str | None = None
    csv_bytes: int | None = None
    plotly_ref: str | None = None
    plotly_sha256: str | None = None
    plotly_bytes: int | None = None

    @classmethod
    def from_trusted_usage(
        cls,
        *,
        request_id: str,
        summary: str,
        catalog_trace: Mapping[str, Any] | None,
        chart_rendered: bool,
    ) -> "ResultArtifact":
        """Build an artifact only from server-validated run evidence."""
        trace = catalog_trace if isinstance(catalog_trace, Mapping) else {}
        contract = trace.get("result_contract")
        contract = contract if isinstance(contract, Mapping) else {}
        metric_ids = _strings(contract.get("metric_ids") or trace.get("selected_metrics"))
        result_columns = _strings(contract.get("required_result_columns"))
        dataset_version = _bounded_text(
            contract.get("dataset_version") or trace.get("dataset_version"), 128
        ) or "unknown"
        metric_version = _bounded_text(
            contract.get("metric_version") or trace.get("metric_version"), 128
        ) or "unknown"
        external = trace.get("result_artifact")
        external = external if isinstance(external, Mapping) else {}
        return cls(
            artifact_id="rta_" + sha256(request_id.encode("utf-8")).hexdigest()[:20],
            summary=_bounded_text(summary, 1_200) or "",
            metric_ids=metric_ids,
            result_columns=result_columns,
            dataset_version=dataset_version,
            metric_version=metric_version,
            chart_state="rendered" if chart_rendered else "not_rendered",
            workspace_id=_bounded_text(external.get("workspace_id"), 128) or "unknown",
            csv_ref=_bounded_text(external.get("csv_ref"), 128),
            csv_sha256=_bounded_text(external.get("csv_sha256"), 64),
            csv_bytes=_non_negative_int(external.get("csv_bytes")),
            plotly_ref=_bounded_text(external.get("plotly_ref"), 128),
            plotly_sha256=_bounded_text(external.get("plotly_sha256"), 64),
            plotly_bytes=_non_negative_int(external.get("plotly_bytes")),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ResultArtifact | None":
        if not isinstance(value, Mapping):
            return None
        artifact_id = _bounded_text(value.get("artifact_id"), 64)
        summary = _bounded_text(value.get("summary"), 1_200)
        if not artifact_id or not summary:
            return None
        chart_state = _bounded_text(value.get("chart_state"), 32) or "not_rendered"
        if chart_state not in {"rendered", "not_rendered"}:
            return None
        return cls(
            artifact_id=artifact_id,
            summary=summary,
            metric_ids=_strings(value.get("metric_ids")),
            result_columns=_strings(value.get("result_columns")),
            dataset_version=_bounded_text(value.get("dataset_version"), 128) or "unknown",
            metric_version=_bounded_text(value.get("metric_version"), 128) or "unknown",
            chart_state=chart_state,
            version=_bounded_text(value.get("version"), 64) or "trusted-result-artifact-v1",
            workspace_id=_bounded_text(value.get("workspace_id"), 128) or "unknown",
            csv_ref=_bounded_text(value.get("csv_ref"), 128),
            csv_sha256=_bounded_text(value.get("csv_sha256"), 64),
            csv_bytes=_non_negative_int(value.get("csv_bytes")),
            plotly_ref=_bounded_text(value.get("plotly_ref"), 128),
            plotly_sha256=_bounded_text(value.get("plotly_sha256"), 64),
            plotly_bytes=_non_negative_int(value.get("plotly_bytes")),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "summary": self.summary,
            "metric_ids": list(self.metric_ids),
            "result_columns": list(self.result_columns),
            "dataset_version": self.dataset_version,
            "metric_version": self.metric_version,
            "chart_state": self.chart_state,
            "version": self.version,
            "workspace_id": self.workspace_id,
            "csv_ref": self.csv_ref,
            "csv_sha256": self.csv_sha256,
            "csv_bytes": self.csv_bytes,
            "plotly_ref": self.plotly_ref,
            "plotly_sha256": self.plotly_sha256,
            "plotly_bytes": self.plotly_bytes,
        }

    def replay_text(self) -> str:
        """Render an explicit replay notice; this never triggers a new query."""
        metrics = "、".join(self.metric_ids) or "受控指标"
        chart_note = "；历史图表未缓存" if self.chart_state == "rendered" else ""
        return (
            "已恢复上一轮已验证的数据结果摘要（不会重新执行 SQL）。\n"
            f"指标：{metrics}；数据版本：{self.dataset_version}；"
            f"指标版本：{self.metric_version}{chart_note}\n\n"
            f"{self.summary}"
        )


def _strings(value: Any, limit: int = 16) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            item for item in (_bounded_text(value, 128) for value in value) if item
        )
    )[:limit]


def _bounded_text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _non_negative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
