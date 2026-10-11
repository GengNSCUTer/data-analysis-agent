"""Read-only single-turn replay before converting frozen SFT inputs.

This diagnostic calls the existing runtime rules without an LLM grounder,
history, database or inference. QuerySpec is read only after the request plan
has been built, to compare provenance with the independently derived input.
It can render the shared v2 component request when a workspace is supplied,
but it is not a release admission gate or the full conversational runtime.
"""

from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

from vanna.core.user import User

from .candidate_sql_generator import (
    CandidateSqlContext,
    build_runtime_candidate_request,
    render_candidate_sql_prompt,
    render_runtime_candidate_prompt,
)
from .query_plan import QueryPlan
from .question_router import QuestionRouter
from .semantic_catalog import CatalogRetriever, ResultContract
from .sql_policy import DEFAULT_ANALYST_EXECUTION_LIMIT
from .working_memory import WorkingMemory

if TYPE_CHECKING:
    from .workspace import WorkspaceProfile

AUDIT_VERSION = "single-turn-runtime-alignment-audit-v2"


def replay_single_turn(
    question: str, retriever: CatalogRetriever, *, workspace: WorkspaceProfile | None = None
) -> dict[str, Any]:
    """Derive context from the question alone, without injecting source answers."""
    user = User(id="alignment-audit", group_memberships=["analyst"])
    memory = WorkingMemory()
    selection = retriever.retrieve(question, user)
    # The online runtime separates its 1,000-row execution cap from the
    # 200-row UI preview. Replaying with Router's legacy preview default
    # would incorrectly reject valid workspace groups before generation.
    router = QuestionRouter(retriever, max_result_rows=DEFAULT_ANALYST_EXECUTION_LIMIT)
    route = router.classify(question, user=user, selection=selection)
    if route.should_generate_sql:
        route = router.admit_sql_request(question, user=user, selection=selection)
    output: dict[str, Any] = {
        "route_state": route.state,
        "route_reason": route.reason,
        "selected_metrics": list(selection.trace.selected_metrics),
        "selected_tables": list(selection.trace.selected_tables),
        "selected_columns": dict(selection.trace.selected_columns),
        "selected_joins": list(selection.trace.selected_joins),
        "plan": None,
        "candidate_prompt": None,
    }
    if not route.should_generate_sql:
        return output
    memory = memory.apply(question, route)
    plan = QueryPlan.from_selection(selection, question, route, memory.as_dict())
    contract = ResultContract.from_selection(
        selection,
        question,
        memory.time_range,
        catalog=retriever.catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    output["plan"] = plan.as_dict()
    output["result_contract"] = contract.as_tool_metadata()
    output["candidate_prompt"] = render_candidate_sql_prompt(
        CandidateSqlContext(
            question=question,
            catalog_prompt=selection.prompt,
            query_plan_prompt=plan.prompt_context(),
            required_result_columns=contract.required_result_columns,
        )
    )
    if workspace is not None:
        request = build_runtime_candidate_request(
            question=question,
            selection=selection,
            query_plan=plan,
            result_contract=contract,
            workspace=workspace,
            route=route,
        )
        output["candidate_prompt"] = render_runtime_candidate_prompt(request)
        output["candidate_request"] = request.safe_evidence()
    return output


def compare_source_semantics(
    replay: Mapping[str, Any], spec: Mapping[str, Any]
) -> tuple[str, ...]:
    """Report structural disagreement, not a proof of complete SQL semantics."""
    reasons: list[str] = []
    plan = replay.get("plan")
    if not isinstance(plan, Mapping):
        return ("route_not_admitted",)
    if set(plan["metric_ids"]) != set(spec["metric_ids"]):
        reasons.append("metric_mismatch")
    expected_dimensions = [spec["dimension"]] if spec.get("dimension") else []
    if list(plan["dimensions"]) != expected_dimensions:
        reasons.append("dimension_mismatch")
    source_time = spec["time"]
    if plan.get("time_grain") != source_time.get("grain"):
        reasons.append("time_grain_mismatch")
    expected_range = (
        {"start": source_time["start"], "end": source_time["end_exclusive"], "end_semantics": "exclusive"}
        if source_time.get("start")
        else None
    )
    if plan.get("time_range") != expected_range:
        reasons.append("time_range_mismatch")
    if plan.get("top_n") and plan["top_n"] != spec.get("top_n"):
        reasons.append("ranking_not_represented_in_source_spec")
    actual_columns = list(plan["required_result_columns"])
    expected_columns = list(spec["required_result_columns"])
    if set(actual_columns) != set(expected_columns):
        reasons.append("result_columns_mismatch")
    elif actual_columns != expected_columns:
        reasons.append("result_column_order_mismatch")
    return tuple(reasons)
