"""Retrieve scoped candidates and choose one proven result; never merge artifacts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import pandas as pd

from .artifact_coverage import CoverageDecision, CoverageProver
from .artifact_history import HistoryArtifact
from .result_artifact_store import ResultArtifactIntegrityError
from .result_analysis import ResultInspectionError, analyze_result
from .semantic_intent import RequestedAnswerSpec


@dataclass
class ReusePlan:
    decision: CoverageDecision
    item: HistoryArtifact | None = None
    spec: RequestedAnswerSpec | None = None
    frame: pd.DataFrame | None = None
    attempts: tuple[dict, ...] = ()


class ArtifactReusePlanner:
    def __init__(self, history, store, catalog):
        self.history, self.store, self.catalog = history, store, catalog
        self.prover = CoverageProver(catalog)

    async def cards(self, conversation_id, user):
        return [item.card() for item in await self.history.candidates(conversation_id, user, limit=8)]

    async def authorized(self, conversation_id, user, artifact_id, *, include_previous_context=False):
        items = await self.history.candidates(conversation_id, user, artifact_id=artifact_id,
                                             include_previous_context=include_previous_context)
        if not items:
            raise FileNotFoundError("历史结果不可用或不属于当前授权范围。")
        return items[0]

    async def verified(self, conversation_id, user, artifact_id, *, include_previous_context=False):
        item = await self.authorized(conversation_id, user, artifact_id, include_previous_context=include_previous_context)
        manifest = await asyncio.to_thread(self.store.verify_manifest, artifact_id=artifact_id,
            user_id=user.id, workspace_id=self.history.workspace.workspace_id)
        artifact = item.artifact
        if (manifest["csv_sha256"], manifest["dataset_version"], manifest["metric_version"], manifest["row_count"]) != (
                artifact.csv_sha256, artifact.dataset_version, artifact.metric_version,
                artifact.result_metadata["row_count"]):
            raise ResultArtifactIntegrityError("历史索引与完整CSV不一致。")
        return item, manifest

    def requested_spec(self, intent, effective, state, source=None):
        if intent.requested_answer is not None:
            return intent.requested_answer
        if intent.request_kind == "result_followup":
            # An explicitly selected history item supplies only the scope of
            # this read; it never patches AnalysisState.
            scope = source.artifact.query_scope if source else (
                state.previous_result_artifact.query_scope if state.previous_result_artifact and
                intent.result_target == "last_successful_result" else state.scope())
            # An inspection inherits the selected result's scope, not optional
            # task fields emitted as empty arrays by the interpreter. A changed
            # target belongs in requested_answer; state_patch belongs to tasks.
            values = {k: v for k, v in scope.items() if k in RequestedAnswerSpec.model_fields}
            values["requires_full_population"] = False
            return RequestedAnswerSpec.model_validate(values)
        return RequestedAnswerSpec.model_validate(effective.scope())

    async def plan(self, conversation_id, user, intent, effective, state):
        reference = intent.history_reference
        initial = intent.requested_answer
        metrics = initial.metric_ids if initial else effective.metric_ids
        if not metrics:
            metrics = tuple(dict.fromkeys(r.metric_id for r in intent.result_inspections if r.metric_id))
        if not metrics and not reference:
            metrics = state.metric_ids
        candidates = await self.history.candidates(conversation_id, user, metric_ids=() if reference else metrics,
            artifact_id=reference.artifact_id if reference else None)
        if reference and reference.ordinal is not None:
            candidates = [item for item in candidates if item.ordinal == reference.ordinal]
        if reference and not candidates:
            return ReusePlan(CoverageDecision("CLARIFY", missing_requirements=("history_reference_unavailable",)))
        if reference and reference.mutation_intent == "derive_query":
            if len(candidates) != 1:
                return ReusePlan(CoverageDecision("CLARIFY", missing_requirements=("ambiguous_history_reference",)))
            item, _ = await self.verified(conversation_id, user, candidates[0].artifact.artifact_id)
            return ReusePlan(CoverageDecision("DERIVED_QUERY", item.artifact.artifact_id), item=item)
        attempts = []
        proven = []
        for item in candidates:
            spec = self.requested_spec(intent, effective, state, item if reference else None)
            decision = self.prover.prove(spec, item.artifact, inspections=intent.result_inspections,
                                         chart=bool(intent.chart_request))
            attempts.append(decision.as_dict())
            if decision.mode in {"DIRECT_REUSE", "LOCAL_COMPUTE"}:
                proven.append((item, spec, decision))
        # If an underspecified reference could name different source scopes,
        # semantic recency must not decide which period or population was meant.
        if reference and len(proven) > 1 and len({str(p[0].artifact.query_scope) for p in proven}) > 1:
            return ReusePlan(CoverageDecision("CLARIFY", missing_requirements=("ambiguous_history_reference",)),
                             attempts=tuple(attempts))
        proven.sort(key=lambda p: (p[2].mode != "DIRECT_REUSE",
            bool(p[2].operation_plan.get("rollup")), len(p[2].operation_plan.get("filters", {})),
            p[0].artifact.artifact_id != state.active_artifact_id, -p[0].created_at.timestamp()))
        for item, spec, decision in proven:
            try:
                await self.verified(conversation_id, user, item.artifact.artifact_id)
                artifact = item.artifact
                frame = await asyncio.to_thread(self.store.read_result_frame,
                    artifact_id=artifact.artifact_id, user_id=user.id, workspace_id=artifact.workspace_id,
                    dataset_version=artifact.dataset_version, metric_version=artifact.metric_version,
                    dimensions=artifact.query_scope.get("dimensions", ()))
                frame = await asyncio.to_thread(self.prover.compute, spec, decision, frame)
                analyses = analyze_result(frame, requests=intent.result_inspections,
                    metric_ids=spec.metric_ids, scope=spec.scope(),
                    additive_metric_ids=self.catalog.additive_metrics_for(spec.metric_ids, spec.dimensions),
                    metric_semantics=self.catalog.metric_semantics, time_column="time" if spec.time_grain else None)
                required = [a for a in analyses if a["status"] == "query_required"]
                if required:
                    requirements = {}
                    for result in required:
                        requirements.update(result.get("query_requirements", {}))
                    numerators = {r["request_index"]: {"value":r["numerator"],"row_count":r["numerator_rows"]}
                                  for r in required if r["operation"]=="share" and "numerator" in r}
                    return ReusePlan(CoverageDecision("SQL_REQUIRED", artifact.artifact_id,
                        missing_requirements=("operation_needs_additional_data",),
                        operation_plan={"query_requirements": requirements, "numerators":numerators}), item, spec, attempts=tuple(attempts))
            except (FileNotFoundError, PermissionError, ResultArtifactIntegrityError, ResultInspectionError, ValueError, KeyError):
                attempts.append({"artifact_id": item.artifact.artifact_id, "mode": "SQL_REQUIRED",
                                 "missing_requirements": ["payload_or_local_operation_unavailable"]})
                continue
            return ReusePlan(decision, item, spec, frame, tuple(attempts))
        item = candidates[0] if reference and len(candidates) == 1 else None
        spec = self.requested_spec(intent, effective, state, item)
        return ReusePlan(CoverageDecision("SQL_REQUIRED", missing_requirements=("no_proven_candidate",)),
                         item=item, spec=spec, attempts=tuple(attempts))
