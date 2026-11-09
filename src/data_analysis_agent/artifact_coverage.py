"""Single-artifact coverage proof and deterministic local execution.

Similarity may retrieve a candidate; only supported algebra over a complete,
authorized snapshot may answer. No SQL text or model-generated code executes here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from .result_analysis import ResultInspectionError
from .semantic_intent import RequestedAnswerSpec

ReuseMode = Literal["DIRECT_REUSE", "LOCAL_COMPUTE", "DERIVED_QUERY", "SQL_REQUIRED", "CLARIFY"]


@dataclass(frozen=True)
class CoverageDecision:
    mode: ReuseMode
    artifact_id: str | None = None
    verified_predicates: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    operation_plan: dict = field(default_factory=dict)

    def as_dict(self):
        return {"mode": self.mode, "artifact_id": self.artifact_id,
                "verified_predicates": list(self.verified_predicates),
                "missing_requirements": list(self.missing_requirements),
                "operation_plan": self.operation_plan,
                "confidence": "proven" if self.mode in {"DIRECT_REUSE", "LOCAL_COMPUTE"} else "unproven"}


def _filters(items):
    return {f["dimension"]: f["value"] for f in items or ()}


def _top(value):
    if isinstance(value, dict):
        return next(iter(value.values()), None)
    return value or None


def _aligned(value, grain):
    stamp = pd.Timestamp(value)
    if grain == "day":
        return stamp == stamp.normalize()
    if grain == "week":
        return stamp.weekday() == 0 and stamp == stamp.normalize()
    if grain == "month":
        return stamp.day == 1
    if grain == "quarter":
        return stamp.day == 1 and stamp.month in {1, 4, 7, 10}
    if grain == "year":
        return stamp.month == stamp.day == 1
    return False


class CoverageProver:
    def __init__(self, catalog):
        self.catalog = catalog

    def prove(self, spec: RequestedAnswerSpec, artifact, *, inspections=(), chart=False, snapshot_read=False):
        source, metadata = artifact.query_scope, artifact.result_metadata
        predicates = ["authorized_candidate", "current_versions", "unexpired_snapshot"]
        def missing(reason):
            return CoverageDecision("SQL_REQUIRED", artifact.artifact_id, tuple(predicates), (reason,))
        if spec.refresh:
            return missing("fresh_data_requested")
        if metadata.get("validation_status") != "valid" or metadata.get("result_complete") is not True:
            return missing("result_completeness_unproven")
        if not spec.metric_ids or not set(spec.metric_ids) <= set(artifact.metric_ids):
            return missing("missing_metrics")
        if source.get("dimensions") and set(spec.metric_ids) != set(artifact.metric_ids):
            return missing("group_population_depends_on_other_metrics")
        if not snapshot_read and any(self.catalog.metrics_by_id[m].temporal_mode == "current_snapshot" for m in spec.metric_ids):
            return missing("current_snapshot_requires_refresh")
        predicates.extend(("metric_definitions_match", "validated_complete_csv"))
        sf, tf = _filters(source.get("filters")), _filters(spec.scope()["filters"])
        if len(sf) != len(source.get("filters") or ()) or len(tf) != len(spec.filters):
            return missing("duplicate_filter_dimension")
        if any(tf.get(name) != value for name, value in sf.items()):
            return missing("source_population_is_narrower")
        extra_filters = {name: value for name, value in tf.items() if sf.get(name) != value}
        sd, td = tuple(source.get("dimensions") or ()), tuple(spec.dimensions)
        if not set(td) <= set(sd):
            return missing("finer_grain_required")
        if not set(extra_filters) <= set(sd):
            return missing("filter_column_not_returned")
        if source.get("comparison_baseline") != spec.comparison_baseline:
            return missing("comparison_scope_changed")
        source_top, target_top = _top(source.get("top_n")), spec.top_n
        if metadata.get("population_coverage") != "complete" and not source_top:
            return missing("population_scope_unproven")
        if source_top and not metadata.get("ordering"):
            return missing("ranking_order_unproven")
        if source_top and spec.requires_full_population and target_top is None:
            return missing("top_n_does_not_cover_population")
        if source_top and target_top and target_top > source_top:
            return missing("top_n_too_small")
        if source_top and (extra_filters or set(td) != set(sd) or source.get("time_grain") != spec.time_grain):
            return missing("top_n_population_cannot_be_transformed")
        # Inspection filters/periods also restrict coverage, even when a model
        # leaves them out of the top-level requested scope.
        for request in inspections:
            if not set(request.filters) <= (set(sd) | {"time"}):
                return missing("inspection_filter_missing")
            if request.within_period and (not spec.time_range or tuple(request.within_period) !=
                    (spec.time_range.start, spec.time_range.end)):
                return missing("inspection_period_requires_separate_scope")
            if source_top and (request.denominator == "business_total" or request.numerator_top_n and request.numerator_top_n > source_top):
                return missing("top_n_missing_business_denominator")
            if request.operation == "sum" and request.metric_id not in self.catalog.additive_metrics_for(spec.metric_ids, sd):
                return missing("non_additive_metric")
        sg, tg = source.get("time_grain"), spec.time_grain
        sb, tb = source.get("time_range"), spec.scope()["time_range"]
        time_filter = None
        if sb != tb:
            if not tb or (sb and not (sb["start"] <= tb["start"] < tb["end"] <= sb["end"])):
                return missing("source_time_does_not_cover_request")
            if not sg or "time" not in artifact.result_columns:
                return missing("no_filterable_time_column")
            if not _aligned(tb["start"], sg) or not _aligned(tb["end"], sg):
                return missing("partial_bucket_cannot_be_split")
            time_filter = [tb["start"], tb["end"]]
        if tg and not sg:
            return missing("finer_time_grain_required")
        if tg != sg and tg and not (sg == "day" and tg in {"week", "month", "quarter", "year"}
                or sg == "month" and tg in {"quarter", "year"} or sg == "quarter" and tg == "year"):
            return missing("incompatible_time_buckets")
        rollup = set(td) != set(sd) or tg != sg
        if rollup and not set(spec.metric_ids) <= set(self.catalog.additive_metrics_for(spec.metric_ids, sd)):
            return missing("non_additive_rollup")
        if target_top:
            direction = next((r.direction for r in inspections if r.operation == "rank"), "desc")
            primary = (metadata.get("ordering") or [{}])[0]
            if primary.get("column") != spec.metric_ids[0] or primary.get("direction") != direction:
                return missing("ranking_direction_unproven")
        predicates.extend(("filter_population_covers_target", "time_boundaries_cover_target", "grain_is_sufficient"))
        plan = {"filters": extra_filters, "time_range": time_filter, "rollup": rollup,
                "dimensions": list(td), "time_grain": tg, "metric_ids": list(spec.metric_ids),
                "top_n": target_top if not source_top or target_top and target_top < source_top else None,
                "ordering": metadata.get("ordering", []), "source_time_grain": sg,
                "source_dimensions": list(sd)}
        changed = bool(extra_filters or time_filter or rollup or plan["top_n"] or inspections)
        return CoverageDecision("LOCAL_COMPUTE" if changed else "DIRECT_REUSE", artifact.artifact_id,
                                tuple(predicates), operation_plan=plan)

    def compute(self, spec, decision, frame):
        """Execute only the proven projection/filter/roll-up, never arbitrary formulas."""
        plan = decision.operation_plan
        selected = frame.copy()
        source_keys = [*plan["source_dimensions"], *(("time",) if plan["source_time_grain"] else ())]
        if source_keys and selected.duplicated(source_keys, keep=False).any() or not source_keys and len(selected) != 1:
            raise ResultInspectionError("完整CSV不满足声明的唯一聚合粒度。")
        for name, value in plan["filters"].items():
            selected = selected.loc[selected[name].map(lambda v, expected=value: pd.notna(v) and str(v) == expected)]
        if plan["time_range"]:
            times = pd.to_datetime(selected["time"], utc=True)
            start, end = (pd.Timestamp(d, tz="UTC") for d in plan["time_range"])
            selected = selected.loc[(times >= start) & (times < end)]
        # An absent group is not evidence for a zero aggregate.
        if selected.empty:
            raise ResultInspectionError("目标实体/期间未出现在可信结果中，不能把缺失当作零。")
        keys = list(plan["dimensions"])
        if plan["time_grain"]:
            keys.append("time")
        if plan["rollup"]:
            source_grain = getattr(spec, "time_grain", None)
            if "time" in selected and decision.operation_plan.get("source_time_grain"):
                source_grain = decision.operation_plan["source_time_grain"]
                bounds = spec.scope()["time_range"]
                if bounds:
                    freq = {"day":"D", "week":"W-SUN", "month":"M", "quarter":"Q", "year":"Y"}[source_grain]
                    expected = set(pd.period_range(pd.Timestamp(bounds["start"]),
                        pd.Timestamp(bounds["end"]) - pd.Timedelta(nanoseconds=1), freq=freq))
                    groups = selected.groupby(keys[:-1] if "time" in keys else keys, dropna=False) if len(keys) > (1 if "time" in keys else 0) else [((), selected)]
                    for _, group in groups:
                        observed = set(pd.to_datetime(group["time"], utc=True).dt.tz_localize(None).dt.to_period(freq))
                        if not expected <= observed:
                            raise ResultInspectionError("旧结果缺少完整时间桶，不能将缺失桶视为零后汇总。")
            if plan["time_grain"]:
                freq = {"day":"D", "week":"W-SUN", "month":"M", "quarter":"Q", "year":"Y"}[plan["time_grain"]]
                selected["time"] = pd.to_datetime(selected["time"], utc=True).dt.tz_localize(None).dt.to_period(freq).dt.start_time
            if keys:
                selected = selected.groupby(keys, dropna=False, sort=False)[list(spec.metric_ids)].sum(min_count=1).reset_index()
            else:
                selected = pd.DataFrame([{m: selected[m].sum(min_count=1) for m in spec.metric_ids}])
        selected = selected.loc[:, [*keys, *spec.metric_ids]]
        if plan["top_n"]:
            ordering = plan["ordering"][0]
            if ordering["column"] not in spec.metric_ids:
                raise ResultInspectionError("源结果没有可信指标排序。")
            if plan["time_grain"] or len(plan["dimensions"]) > 1:
                raise ResultInspectionError("当前本地Top-N仅支持单分组完整结果。")
            selected = selected.sort_values(ordering["column"], ascending=ordering["direction"] == "asc").head(plan["top_n"])
        return selected.reset_index(drop=True)
