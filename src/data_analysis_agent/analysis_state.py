"""Active analysis task, stored in the existing conversation working_memory JSONB.

WorkingMemory is the legacy view of these same fields, not a second state store.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any, Literal, Mapping
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from .result_artifact import ResultArtifact
from .working_memory import WorkingMemory
from .workspace import WorkspaceProfile


class AnalysisTaskStatus(StrEnum):
    IDLE = "idle"
    CLARIFYING = "clarifying"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


ScopeField = Literal[
    "metric_ids", "dimensions", "filters", "time_range",
    "comparison_baseline", "time_grain", "top_n",
]
_LIST_FIELDS = {"metric_ids", "dimensions", "filters"}


class StateFilter(BaseModel):
    """Structured filter crossing the model/state boundary.

    ``AnalysisState`` keeps its legacy compact ``dimension=value`` strings for
    persistence and renderer compatibility; the patch boundary uses the same
    structured shape as ``SemanticIntent.filters``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: str
    value: str


class StatePatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["set", "add", "remove", "clear", "replace_task"]
    field: ScopeField | None = None
    value: str | int | list[StateFilter] | list[str] | dict[str, str] | None = None

    @model_validator(mode="after")
    def valid_operation(self) -> "StatePatchOperation":
        if self.op == "replace_task":
            if self.field is not None or self.value is not None:
                raise ValueError("replace_task has no field or value")
        elif self.field is None:
            raise ValueError("state operation requires a field")
        elif self.op in {"add", "remove"} and self.field not in _LIST_FIELDS:
            raise ValueError("add/remove applies only to metric_ids, dimensions or filters")
        elif self.op == "clear" and self.value is not None:
            raise ValueError("clear has no value")
        return self


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operations: tuple[StatePatchOperation, ...] = ()


@dataclass(frozen=True)
class AnalysisState(WorkingMemory):
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state_version: int = 0
    workspace_id: str = ""
    dataset_version: str = ""
    metric_version: str = ""
    status: str = AnalysisTaskStatus.IDLE
    result_shape: str = "scalar"
    top_n: int | None = None
    history_start_index: int = 0
    turn_id: str | None = None
    run_id: str | None = None
    last_successful_plan: dict[str, Any] | None = None
    last_successful_state_version: int | None = None
    last_successful_run_id: str | None = None
    active_plan: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "AnalysisState":
        raw = value or {}
        legacy = WorkingMemory.from_mapping(raw)
        base = {f.name: getattr(legacy, f.name) for f in fields(WorkingMemory) if f.init}
        extra = {
            f.name: raw[f.name] for f in fields(cls)
            if f.init and f.name not in base and f.name in raw
        }
        return cls(**base, **extra)

    @classmethod
    def new(cls, workspace: WorkspaceProfile, *, history_start_index: int = 0) -> "AnalysisState":
        return cls(
            workspace_id=workspace.workspace_id,
            dataset_version=workspace.dataset_version_id,
            metric_version=workspace.metric_version,
            history_start_index=history_start_index,
        )

    def as_dict(self) -> dict[str, Any]:
        result = super().as_dict()
        result.update({
            f.name: getattr(self, f.name) for f in fields(self)
            if f.init and f.name not in result
        })
        return result

    def apply_patch(self, patch: StatePatch, *, message_count: int) -> "AnalysisState":
        """Apply field operations; Catalog/intent validation follows before SQL."""
        state = self
        for operation in patch.operations:
            if operation.op == "replace_task":
                state = AnalysisState(
                    workspace_id=self.workspace_id, dataset_version=self.dataset_version,
                    metric_version=self.metric_version, history_start_index=message_count,
                )
                continue
            name = operation.field
            assert name is not None  # StatePatchOperation validates this.
            value = operation.value
            if operation.op == "clear":
                value = () if name in _LIST_FIELDS else None
            elif name in _LIST_FIELDS:
                values = [value] if isinstance(value, str) else value
                if name == "filters" and isinstance(values, (list, tuple)):
                    values = [
                        f"{item.dimension}={item.value}" if isinstance(item, StateFilter) else item
                        for item in values
                    ]
                if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
                    raise ValueError(f"{name} requires a list of strings")
                if operation.op == "set":
                    value = tuple(dict.fromkeys(values))
                elif operation.op == "add":
                    value = tuple(dict.fromkeys((*getattr(state, name), *values)))
                else:
                    value = tuple(v for v in getattr(state, name) if v not in values)
            state = replace(state, **{name: value})
        # Editing a ranking retains its quantity. A scalar has no ranked
        # population; entity drilldowns are handled by resolve_state_intent.
        if not state.dimensions and state.dimensions != self.dimensions and not any(
            op.field == "top_n" for op in patch.operations
        ):
            state = replace(state, top_n=None)
        return replace(state, state_version=self.state_version + 1)

    def scope(self) -> dict[str, Any]:
        return {
            "metric_ids": list(self.metric_ids), "dimensions": list(self.dimensions),
            "filters": [dict(zip(("dimension", "value"), item.split("=", 1))) for item in self.filters],
            "time_range": self.time_range, "time_grain": self.time_grain,
            "comparison_baseline": self.comparison_baseline, "top_n": self.top_n,
        }

    def with_result_artifact(self, artifact: ResultArtifact) -> "AnalysisState":
        legacy = super().with_result_artifact(artifact)
        return replace(
            self, previous_result_artifact=artifact, previous_result_summary=artifact.summary,
            chart_history=legacy.chart_history, status=AnalysisTaskStatus.COMPLETED,
            last_successful_plan=self.active_plan,
            last_successful_state_version=self.state_version,
            last_successful_run_id=self.run_id,
        )

    @property
    def active_artifact_id(self) -> str | None:
        return self.previous_result_artifact.artifact_id if self.previous_result_artifact else None
