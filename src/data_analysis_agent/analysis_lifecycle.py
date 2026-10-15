"""Session analysis-state lifecycle for one trusted workspace.

R1 chat history stays in ``Conversation.messages``. This module only decides
whether the active R2 analysis state and R3 result reference may be reused.
"""

from __future__ import annotations

from typing import Mapping
from dataclasses import replace

from vanna.core.storage import Conversation

from .result_artifact import ResultArtifact
from .working_memory import WorkingMemory
from .workspace import WorkspaceProfile
from .analysis_state import AnalysisState


VERSION_MISMATCH_NOTICE = (
    "当前工作区的数据或指标版本已变化，上一轮分析状态和结果引用已失效。"
    "请重新说明指标、时间和维度；聊天记录仍保留。"
)
CLEAR_ANALYSIS_NOTICE = (
    "已清空本会话的分析上下文。聊天记录仍保留，下一轮不会沿用上一轮指标、时间、维度或结果。"
)


def analysis_scope_is_current(
    metadata: Mapping[str, object] | None, workspace: WorkspaceProfile
) -> bool:
    """Return whether pinned workspace and versions still match the server.

    A conversation that has never been pinned is current. Any stored pin that
    differs from the server workspace is stale and must not feed SQL.
    """

    metadata = metadata or {}
    workspace_id = metadata.get("workspace_id")
    dataset_version = metadata.get("dataset_version_id")
    metric_version = metadata.get("metric_version")
    unpinned = workspace_id in (None, "") and dataset_version in (None, "") and metric_version in (None, "")
    if unpinned:
        return True
    return (
        workspace_id == workspace.workspace_id
        and dataset_version == workspace.dataset_version_id
        and metric_version == workspace.metric_version
    )


def prepare_turn_memory(
    conversation: Conversation, workspace: WorkspaceProfile
) -> tuple[WorkingMemory, str | None]:
    """Return memory safe for this turn, clearing it when versions drifted."""

    if analysis_scope_is_current(conversation.metadata, workspace):
        return (
            WorkingMemory.from_mapping(conversation.metadata.get("working_memory")),
            None,
        )
    _rebind_scope(conversation, workspace)
    conversation.metadata["working_memory"] = WorkingMemory().as_dict()
    conversation.metadata.pop("history_summary", None)
    return WorkingMemory(), VERSION_MISMATCH_NOTICE


def prepare_analysis_state(
    conversation: Conversation, workspace: WorkspaceProfile,
) -> tuple[AnalysisState, str | None]:
    if not analysis_scope_is_current(conversation.metadata, workspace):
        _rebind_scope(conversation, workspace)
        state = AnalysisState.new(workspace, history_start_index=conversation.metadata.get("message_count", len(conversation.messages)))
        conversation.metadata["working_memory"] = state.as_dict()
        conversation.metadata.pop("history_summary", None)
        return state, VERSION_MISMATCH_NOTICE
    state = AnalysisState.from_mapping(conversation.metadata.get("working_memory"))
    return replace(state, workspace_id=workspace.workspace_id, dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version), None


def clear_active_analysis_state(
    conversation: Conversation, workspace: WorkspaceProfile
) -> None:
    """Drop R2 and the active R3 reference while keeping R1 and R4."""

    summary = conversation.metadata.get("history_summary")
    raw_memory = conversation.metadata.get("working_memory")
    if summary is None and isinstance(raw_memory, Mapping):
        summary = raw_memory.get("__history_summary")
    conversation.metadata["working_memory"] = AnalysisState.new(
        workspace, history_start_index=conversation.metadata.get("message_count", len(conversation.messages)),
    ).as_dict()
    if summary:
        conversation.metadata["history_summary"] = summary
    else:
        conversation.metadata.pop("history_summary", None)
    _rebind_scope(conversation, workspace)


def current_result_artifact(
    conversation: Conversation, workspace: WorkspaceProfile
) -> ResultArtifact | None:
    """Expose the active artifact only when its workspace versions still match."""

    if not analysis_scope_is_current(conversation.metadata, workspace):
        return None
    memory = WorkingMemory.from_mapping(conversation.metadata.get("working_memory"))
    return memory.previous_result_artifact


def _rebind_scope(conversation: Conversation, workspace: WorkspaceProfile) -> None:
    conversation.metadata["workspace_id"] = workspace.workspace_id
    conversation.metadata["dataset_version_id"] = workspace.dataset_version_id
    conversation.metadata["metric_version"] = workspace.metric_version
