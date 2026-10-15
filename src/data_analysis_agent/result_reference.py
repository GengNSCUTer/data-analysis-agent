"""Resolve bounded natural-language references against trusted result artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .analysis_state import AnalysisState
from .result_artifact import ResultArtifact
from .semantic_intent import ReferenceRequest


class ResultReferenceError(ValueError):
    """A reference cannot be converted into a server-owned filter."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedReference:
    """A verified entity binding. It contains no SQL expression."""

    source_artifact_id: str
    dimension: str
    value: str
    rank: int
    match_mode: str
    ordering_column: str
    ordering_direction: str
    ordering_value: Any

    def as_dict(self) -> dict[str, object]:
        return {
            "source_artifact_id": self.source_artifact_id,
            "dimension": self.dimension,
            "value": self.value,
            "rank": self.rank,
            "match_mode": self.match_mode,
            "ordering_column": self.ordering_column,
            "ordering_direction": self.ordering_direction,
            "ordering_value": self.ordering_value,
        }


def resolve_ranked_entity(
    request: ReferenceRequest,
    artifact: ResultArtifact | None,
    *,
    workspace_id: str,
    dataset_version: str,
    metric_version: str,
) -> ResolvedReference:
    """Resolve one model reference using only server-owned artifact metadata."""

    if artifact is None:
        raise ResultReferenceError("reference_artifact_missing", "上一轮没有可引用的已验证结果，请先完成一次查询。")
    if artifact.workspace_id != workspace_id:
        raise ResultReferenceError("reference_workspace_mismatch", "上一轮结果不属于当前工作区，不能继续引用。")
    if artifact.dataset_version != dataset_version or artifact.metric_version != metric_version:
        raise ResultReferenceError("reference_version_stale", "上一轮结果对应的数据或指标版本已失效，请重新查询。")
    if artifact.result_metadata.get("validation_status") != "valid":
        raise ResultReferenceError("reference_result_unvalidated", "上一轮结果尚未通过结果校验，不能作为新的筛选条件。")
    if request.expected_source_artifact_id and request.expected_source_artifact_id != artifact.artifact_id:
        raise ResultReferenceError("reference_artifact_mismatch", "引用的结果不是当前会话中的已验证结果。")

    dimensions = artifact.query_scope.get("dimensions")
    if not isinstance(dimensions, (list, tuple)) or request.dimension not in dimensions:
        raise ResultReferenceError("reference_dimension_missing", "上一轮结果没有这个分组维度，无法解析引用。")
    columns = set(artifact.result_columns)
    if request.dimension not in columns:
        raise ResultReferenceError("reference_dimension_not_returned", "上一轮结果没有返回可引用的分组列。")

    ordering = artifact.result_metadata.get("ordering")
    if not isinstance(ordering, list) or not ordering:
        raise ResultReferenceError("reference_ordering_missing", "上一轮结果没有可验证的排序依据。")
    primary = ordering[0]
    if not isinstance(primary, Mapping) or not isinstance(primary.get("column"), str):
        raise ResultReferenceError("reference_ordering_invalid", "上一轮结果的排序依据不可验证。")
    ordering_column = primary["column"]
    direction = primary.get("direction")
    if direction not in {"asc", "desc"}:
        raise ResultReferenceError("reference_ordering_invalid", "上一轮结果的排序方向不可验证。")

    ranked = artifact.result_metadata.get("ranked_entities")
    if not isinstance(ranked, list) or len(ranked) < request.rank:
        raise ResultReferenceError("reference_rank_missing", "上一轮结果没有包含所请求的排名行。")
    entry = ranked[request.rank - 1]
    if not isinstance(entry, Mapping) or entry.get("rank") != request.rank:
        raise ResultReferenceError("reference_rank_invalid", "上一轮结果的排名序号不可验证。")
    value = entry.get("value")
    order_value = entry.get("order_value")
    if not isinstance(value, str) or not value.strip() or order_value is None:
        raise ResultReferenceError("reference_entity_missing", "上一轮结果没有可引用的实体值。")

    if request.match_mode == "unique_highest":
        if request.rank != 1 or direction != "desc":
            raise ResultReferenceError("reference_not_unique_highest", "当前结果不能证明所指实体是唯一最高值。")
        if len(ranked) > 1:
            second = ranked[1]
            if not isinstance(second, Mapping) or second.get("order_value") == order_value:
                raise ResultReferenceError("reference_tie", "第一名存在并列，无法唯一确定‘最高的’实体。")

    return ResolvedReference(
        source_artifact_id=artifact.artifact_id,
        dimension=request.dimension,
        value=value.strip(),
        rank=request.rank,
        match_mode=request.match_mode,
        ordering_column=ordering_column,
        ordering_direction=direction,
        ordering_value=order_value,
    )


def apply_reference_filter(state: AnalysisState, reference: ResolvedReference) -> AnalysisState:
    """Apply a resolved value as a server-owned state filter."""

    prefix = f"{reference.dimension}="
    filters = tuple(item for item in state.filters if not item.startswith(prefix))
    return replace(state, filters=(*filters, f"{reference.dimension}={reference.value}"))
