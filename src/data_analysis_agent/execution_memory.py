"""A read-only execution evidence view assembled from existing audit rows.

ExecutionMemory deliberately does not create another persistence layer.  One
SQL attempt is identified by the existing ``query_audits.audit_id`` and is
joined to the existing ``agent_runs`` row.  Raw SQL and full result rows never
leave the audit store through this view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping


@dataclass(frozen=True)
class ExecutionMemory:
    """Safe, replay-oriented projection of one audited SQL attempt."""

    execution_id: str
    run_id: str | None
    conversation_id: str
    task_id: str | None
    turn_id: str | None
    state_version: int | None
    parent_state_version: int | None
    query_plan: dict[str, Any]
    query_scope: dict[str, Any]
    dataset_version: str
    metric_version: str
    model_name: str
    policy_status: str
    execution_status: str
    validation_status: str
    artifact_id: str | None
    row_count: int | None
    elapsed_ms: int | None
    failure_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
    query_plan_version: str | None
    execution_strategy: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ExecutionMemory":
        trace = row.get("catalog_trace")
        trace = trace if isinstance(trace, Mapping) else {}
        state = trace.get("analysis_state")
        state = state if isinstance(state, Mapping) else {}
        plan = trace.get("query_plan")
        plan = plan if isinstance(plan, Mapping) else {}
        attempt = (trace.get("execution_attempts") or {}).get(str(row["audit_id"]), {})
        policy_status = str(row.get("policy_status") or "unknown")
        validation_status = str(attempt.get("validation_status") or "not_validated")
        if policy_status == "rejected":
            validation_status = "not_validated"
        run_status = {"allowed": "completed", "rejected": "rejected", "execution_error": "failed"}.get(policy_status, "unknown")
        # Detailed driver/policy errors remain in the protected audit, as they
        # may contain SQL, literals or object names. This API returns a category.
        failure_reason = None if policy_status == "allowed" else (
            "result_validation_failed" if str(row.get("policy_reason", "")).startswith("result_validation:")
            else "policy_rejected" if policy_status == "rejected" else "execution_failed"
        )
        finished_at = row.get("created_at")
        elapsed_ms = _non_negative_int(row.get("elapsed_ms"))
        return cls(
            execution_id=f"audit-{row['audit_id']}",
            run_id=_text(row.get("run_id")),
            conversation_id=str(row.get("conversation_id") or ""),
            task_id=_text(state.get("task_id")),
            turn_id=_text(state.get("turn_id")),
            state_version=_non_negative_int(state.get("state_version")),
            parent_state_version=_non_negative_int(state.get("parent_state_version")),
            query_plan=dict(plan),
            query_scope={key: state[key] for key in (
                "workspace_id", "dataset_version", "metric_version", "metric_ids", "dimensions",
                "filters", "time_range", "time_grain", "comparison_baseline", "top_n", "result_shape",
            ) if key in state},
            dataset_version=str(row.get("dataset_version_id") or "unknown"),
            metric_version=str(row.get("metric_version") or "unknown"),
            model_name=str(row.get("model_name") or "unknown"),
            policy_status=policy_status,
            execution_status=run_status,
            validation_status=validation_status,
            artifact_id=_text(attempt.get("artifact_id")) if validation_status == "valid" and policy_status == "allowed" else None,
            row_count=_non_negative_int(row.get("row_count")),
            elapsed_ms=_non_negative_int(row.get("elapsed_ms")),
            failure_reason=failure_reason[:256] if failure_reason else None,
            started_at=finished_at - timedelta(milliseconds=elapsed_ms) if finished_at and elapsed_ms is not None else None,
            finished_at=finished_at,
            query_plan_version=_text(plan.get("query_plan_version")),
            execution_strategy=_text(plan.get("execution_strategy")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "state_version": self.state_version,
            "parent_state_version": self.parent_state_version,
            "query_plan": self.query_plan,
            "query_scope": self.query_scope,
            "dataset_version": self.dataset_version,
            "metric_version": self.metric_version,
            "model_name": self.model_name,
            "policy_status": self.policy_status,
            "execution_status": self.execution_status,
            "validation_status": self.validation_status,
            "artifact_id": self.artifact_id,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
            "failure_reason": self.failure_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "query_plan_version": self.query_plan_version,
            "execution_strategy": self.execution_strategy,
        }


def reuse_decision(
    *,
    artifact_scope: Mapping[str, Any],
    requested_scope: Mapping[str, Any],
    artifact_id: str | None,
    validation_status: str,
    presentation_only: bool = False,
) -> str:
    """Classify whether an existing validated artifact can answer a request.

    ``display_only`` means pagination/chart presentation can reuse the same
    artifact. ``validated_result`` means the requested analytical scope is
    unchanged. Any scope/version difference requires a new governed query.
    """

    if not artifact_id or validation_status != "valid":
        return "query_required"
    keys = ("workspace_id", "dataset_version", "metric_version", "metric_ids", "dimensions", "filters", "time_range", "time_grain", "comparison_baseline", "top_n", "result_shape")
    if any(key not in artifact_scope or key not in requested_scope for key in keys):
        return "query_required"
    if any(artifact_scope[key] != requested_scope[key] for key in keys):
        return "query_required"
    if any(artifact_scope[key] in (None, "", "unknown") for key in ("workspace_id", "dataset_version", "metric_version")):
        return "query_required"
    return "display_only" if presentation_only else "validated_result"


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _non_negative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
