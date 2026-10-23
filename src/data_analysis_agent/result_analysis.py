"""Deterministic analysis of a validated returned population; no SQL or LLM calls."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import math
from typing import Any, Literal, Mapping, Sequence

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class ResultInspectionError(ValueError):
    """A request refers to an unavailable result column or invalid selection."""


class ResultInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["overview", "rows", "sum", "compare", "trend", "period_compare",
                       "rank", "share", "distribution", "threshold"] = "overview"
    metric_id: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    row_numbers: tuple[int, ...] = ()
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100_000)
    group_by: tuple[str, ...] = ()
    trend_mode: Literal["overview", "strict_increase", "nondecreasing", "cumulative", "rolling"] = "overview"
    window: int = Field(default=3, ge=1, le=3660)
    baseline: Literal["previous_period", "previous_year", "first_period"] = "previous_period"
    periods: tuple[str, ...] = ()
    within_period: tuple[str, str] | None = None
    direction: Literal["asc", "desc"] = "desc"
    include_ties: bool = False
    rank_by: Literal["value", "change", "relative_change"] = "value"
    denominator: Literal["returned_result", "business_total"] = "returned_result"
    share_basis: Literal["value", "change"] = "value"
    numerator_top_n: int | None = Field(default=None, ge=1, le=100_000)
    comparison: Literal["gt", "ge", "lt", "le", "eq"] = "lt"
    threshold_value: FiniteFloat | None = None
    threshold_unit: Literal["native", "percent"] = "native"

    @model_validator(mode="after")
    def validate_request(self):
        if any(n < 1 for n in self.row_numbers):
            raise ValueError("row numbers start at one")
        if self.operation not in {"overview", "rows"} and not self.metric_id:
            raise ValueError("a calculation requires metric_id")
        if self.operation == "compare" and len(self.row_numbers) != 2:
            raise ValueError("compare requires exactly two source row numbers")
        if self.operation == "threshold" and self.threshold_value is None:
            raise ValueError("threshold requires threshold_value")
        if self.periods and len(self.periods) != 2:
            raise ValueError("periods must contain current and baseline")
        if self.within_period and date.fromisoformat(self.within_period[0]) >= date.fromisoformat(self.within_period[1]):
            raise ValueError("within_period must be a nonempty half-open date interval")
        return self


def scalar(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def column(name: str, columns: Sequence[str]) -> str:
    normalize = lambda value: "".join(c for c in value.casefold() if c.isalnum())
    matches = [c for c in columns if normalize(c) == normalize(name)]
    if len(matches) != 1:
        raise ResultInspectionError(f"结果中没有唯一可用的列：{name}")
    return matches[0]


def select_rows(frame: pd.DataFrame, request: ResultInspection, metric_ids: Sequence[str]) -> pd.DataFrame:
    selected = frame
    metrics = {column(m, frame.columns) for m in metric_ids}
    for name, value in request.filters.items():
        key = column(name, frame.columns)
        if key in metrics:
            raise ResultInspectionError("结果筛选只能选择已返回的维度或时间列。")
        selected = selected.loc[selected[key].map(lambda v: str(scalar(v))) == value]
    if request.row_numbers:
        if max(request.row_numbers) > len(frame):
            raise ResultInspectionError("所请求的行超出这份完整结果，不能从样本猜测。")
        selected = selected.loc[[n - 1 for n in dict.fromkeys(request.row_numbers) if n - 1 in selected.index]]
    return selected


def _sum(values) -> int | float | None:
    valid = [Decimal(str(v)) for v in values if pd.notna(v) and math.isfinite(float(v))]
    return scalar(sum(valid, Decimal(0))) if valid else None


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [{str(k): scalar(v) for k, v in row.items()} for row in frame.to_dict("records")]


def _delta(current, baseline, *, rate: bool = False) -> dict[str, Any]:
    a, b = Decimal(str(current)), Decimal(str(baseline))
    return {"current": scalar(a), "baseline": scalar(b), "difference": scalar(a - b),
            "relative_change_percent": scalar((a - b) / b * 100) if b > 0 else None,
            "percentage_points": scalar((a - b) * 100) if rate else None,
            "baseline_status": "positive" if b > 0 else "zero" if b == 0 else "negative"}


_FREQUENCIES = {"day": "D", "week": "W-SUN", "month": "M", "quarter": "Q-DEC", "year": "Y-DEC"}


def acquisition_scope(requests: Sequence[ResultInspection], scope: Mapping[str, Any]):
    """Acquire comparison periods before a first SQL, keeping the requested operands explicit."""
    updates, normalized = {}, []
    bounds, grain = scope.get("time_range") or {}, scope.get("time_grain")
    for request in requests:
        if request.operation == "share" and request.denominator == "business_total" and scope.get("top_n"):
            top = scope["top_n"]
            n = top if isinstance(top, int) else next(iter(top.values()))
            request = request.model_copy(update={"numerator_top_n": request.numerator_top_n or n})
            updates["top_n"] = None
        compare = (request.operation == "period_compare" or request.operation == "rank" and request.rank_by != "value"
                   or request.operation == "share" and request.share_basis == "change")
        if compare and grain in _FREQUENCIES and bounds.get("end") and request.baseline != "first_period":
            current = _period(request.periods[0], grain) if request.periods else _period(pd.Timestamp(bounds["end"]) - pd.Timedelta(nanoseconds=1), grain)
            if pd.Timestamp(bounds["end"]) < (current + 1).start_time:
                normalized.append(request)
                continue
            if request.periods:
                baseline = _period(request.periods[1], grain)
            elif request.baseline == "previous_year":
                if grain == "week":
                    stamp = current.start_time.isocalendar()
                    try:
                        baseline = _period(date.fromisocalendar(stamp.year - 1, stamp.week, 1), grain)
                    except ValueError:
                        normalized.append(request)
                        continue
                else:
                    baseline = _period(current.start_time - pd.DateOffset(years=1), grain)
            else:
                baseline = current - 1
            request = request.model_copy(update={"periods": (current.start_time.date().isoformat(), baseline.start_time.date().isoformat())})
            existing = updates.get("time_range", bounds)
            lo = min(pd.Timestamp(existing.get("start") or current.start_time), baseline.start_time, current.start_time)
            hi = max(pd.Timestamp(existing["end"]), (current + 1).start_time, (baseline + 1).start_time)
            updates["time_range"] = {"start": lo.date().isoformat(), "end": hi.date().isoformat(), "end_semantics": "exclusive"}
        normalized.append(request)
    if updates.get("time_range") and bounds.get("start") and bounds.get("end"):
        normalized = [r.model_copy(update={"within_period": (bounds["start"], bounds["end"])})
                      if r.operation not in {"period_compare"} and not (r.operation=="rank" and r.rank_by!="value")
                      and not (r.operation=="share" and r.share_basis=="change") and r.within_period is None else r for r in normalized]
    return updates, tuple(normalized)


def _period(value, grain):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)  # Preserve the server's business bucket, not a new timezone.
    return stamp.to_period(_FREQUENCIES[grain])


def _groups(frame, dimensions):
    if not dimensions:
        return [((), frame)]
    return frame.groupby(list(dimensions), dropna=False, sort=False)


def _group_key(key, dimensions):
    keys = key if isinstance(key, tuple) else (key,)
    return {name: scalar(value) for name, value in zip(dimensions, keys)}


def _series_context(frame, metric, request, scope, time_column, additive):
    grain = scope.get("time_grain")
    if not time_column or grain not in _FREQUENCIES:
        return None, "需要按时间分组的完整结果。", {"time_grain": "month"}
    time = column(time_column, frame.columns)
    dimensions = tuple(column(d, frame.columns) for d in (request.group_by or scope.get("dimensions") or ()))
    work = frame.copy()
    try:
        work["__period"] = [_period(v, grain) if pd.notna(v) else pd.NaT for v in work[time]]
    except (ValueError, TypeError):
        return None, "时间列无法按已确认的粒度解释。", {}
    bounds = scope.get("time_range") or {}
    start = _period(bounds["start"], grain) if bounds.get("start") else None
    end = _period(pd.Timestamp(bounds["end"]) - pd.Timedelta(nanoseconds=1), grain) if bounds.get("end") else None
    return (work, dimensions, start, end), None, {}


def _trend(frame, metric, request, scope, time_column, additive):
    prepared, reason, needed = _series_context(frame, metric, request, scope, time_column, additive)
    if prepared is None:
        return {"status": "query_required", "reason": reason, "query_requirements": needed}
    work, dimensions, start, end = prepared
    entries = []
    for key, group in _groups(work, dimensions):
        group = group.sort_values("__period")
        periods = group["__period"]
        values = pd.to_numeric(group[metric], errors="coerce").replace([float("inf"), -float("inf")], float("nan"))
        valid = periods.notna() & values.notna()
        duplicates = bool(periods.dropna().duplicated().any())
        lo = start if start is not None else periods.min()
        hi = end if end is not None else periods.max()
        expected = pd.period_range(lo, hi, freq=_FREQUENCIES[scope["time_grain"]]) if pd.notna(lo) and pd.notna(hi) else []
        observed = set(periods.dropna())
        missing = [str(p) for p in expected if p not in observed]
        bounds = scope.get("time_range") or {}
        partial = bool(bounds.get("start") and pd.Timestamp(bounds["start"]) != lo.start_time)
        partial |= bool(bounds.get("end") and pd.Timestamp(bounds["end"]) != (hi + 1).start_time)
        limited = bool(scope.get("top_n"))
        complete = len(expected) > 0 and not missing and bool(valid.all()) and not duplicates and not partial and not limited
        differences = []
        up = down = flat = run_up = run_down = longest_up = longest_down = 0
        if not duplicates:
            previous = None
            for period, value in zip(periods, values):
                if pd.isna(period) or pd.isna(value):
                    previous = None
                    run_up = run_down = 0
                    continue
                if previous and period.ordinal == previous[0].ordinal + 1:
                    change = _delta(value, previous[1])
                    difference = change["difference"]
                    up += difference > 0
                    down += difference < 0
                    flat += difference == 0
                    run_up = run_up + 1 if difference > 0 else 0
                    run_down = run_down + 1 if difference < 0 else 0
                    longest_up, longest_down = max(longest_up, run_up), max(longest_down, run_down)
                    differences.append({"period": str(period), "previous_period": str(previous[0]), **change})
                else:
                    run_up = run_down = 0
                previous = (period, value)
        contradiction = down > 0 or (request.trend_mode != "nondecreasing" and flat > 0)
        judgment = "false" if contradiction else "true" if complete and len(differences) > 0 else "insufficient"
        endpoints = None
        if valid.any():
            clean = group.loc[valid]
            endpoints = {"first_period": str(clean["__period"].iloc[0]), "last_period": str(clean["__period"].iloc[-1]),
                         **_delta(clean[metric].iloc[-1], clean[metric].iloc[0])}
        entry = {"group": _group_key(key, dimensions), "judgment": judgment,
                 "period_count": len(group), "expected_period_count": len(expected),
                 "complete": complete, "missing_periods": missing, "null_count": int((~valid).sum()),
                 "duplicate_periods": duplicates, "partial_periods": partial, "top_n_limited": limited,
                 "up_count": up, "down_count": down, "flat_count": flat,
                 "longest_up_transitions": longest_up, "longest_down_transitions": longest_down,
                 "endpoints": endpoints,
                 "first_counterexample": next((d for d in differences if d["difference"] < 0 or
                                                (request.trend_mode != "nondecreasing" and d["difference"] == 0)), None),
                 "largest_drop": min((d for d in differences if d["difference"] < 0), key=lambda d: d["difference"], default=None)}
        if request.trend_mode in {"cumulative", "rolling"}:
            if request.trend_mode == "cumulative" and not additive:
                entry["calculation_notice"] = "该指标不能跨期间累加，需要按原口径查询。"
            elif complete:
                sequence = [Decimal(str(v)) for v in group[metric]]
                derived = []
                for i, period in enumerate(periods):
                    sample = sequence[:i + 1] if request.trend_mode == "cumulative" else sequence[max(0, i - request.window + 1):i + 1]
                    value = sum(sample, Decimal(0))
                    if request.trend_mode == "rolling":
                        value = value / len(sample) if len(sample) == request.window else None
                    derived.append({"period": str(period), "value": scalar(value)})
                entry["derived_rows"] = derived[request.offset:request.offset + request.limit]
                entry["derived_scope"] = "可加量累计" if request.trend_mode == "cumulative" else "时间桶数值的序列均值，不是重新加权的总体指标"
        entries.append(entry)
    return {"status": "ready", "mode": request.trend_mode, "group_count": len(entries),
            "groups": entries[request.offset:request.offset + request.limit],
            "all_groups_included": request.offset == 0 and len(entries) <= request.limit}


def _period_compare(frame, metric, request, scope, time_column, additive, rate):
    prepared, reason, needed = _series_context(frame, metric, request, scope, time_column, additive)
    if prepared is None:
        return {"status": "query_required", "reason": reason, "query_requirements": needed}
    work, dimensions, _, _ = prepared
    if work.empty:
        return {"status": "ready", "comparisons": [], "notice": "结果为空，没有可比较期间。"}
    grain = scope["time_grain"]
    current = _period(request.periods[0], grain) if request.periods else work["__period"].max()
    if request.periods:
        baseline = _period(request.periods[1], grain)
    elif request.baseline == "first_period":
        baseline = work["__period"].min()
    elif request.baseline == "previous_year":
        # ISO weeks align by weekday/week number; unsupported week53 never silently shifts.
        if grain == "week":
            stamp = current.start_time
            try:
                baseline = _period(date.fromisocalendar(stamp.isocalendar().year - 1, stamp.isocalendar().week, 1), grain)
            except ValueError:
                return {"status": "unsupported", "reason": "上一ISO年不存在对应周，不能直接同比。"}
        else:
            baseline = _period(current.start_time - pd.DateOffset(years=1), grain)
    else:
        baseline = current - 1
    if pd.isna(current) or pd.isna(baseline) or current == baseline:
        return {"status": "ready", "comparisons": [], "notice": "没有两个不同的有效期间。"}
    bounds = scope.get("time_range") or {}
    lo, hi = min(current, baseline).start_time, (max(current, baseline) + 1).start_time
    needed = {"time_range": {"start": lo.date().isoformat(), "end": hi.date().isoformat(), "end_semantics": "exclusive"}, "time_grain": grain}
    for period in (current, baseline):
        if (bounds.get("start") and period.start_time < pd.Timestamp(bounds["start"]) < (period + 1).start_time or
            bounds.get("end") and period.start_time < pd.Timestamp(bounds["end"]) < (period + 1).start_time):
            return {"status": "unsupported", "reason": "当前比较包含部分期间，不能与完整期间直接比较；请指定完整期间或等长截至日口径。"}
    if scope.get("top_n"):
        return {"status": "query_required", "reason": "前N项不能覆盖全部实体的本期和基期。", "query_requirements": {**needed, "top_n": None}}
    # A previous SQL's filters remain unchanged when acquiring the baseline.
    if bounds.get("start") and pd.Timestamp(bounds["start"]) > lo or bounds.get("end") and pd.Timestamp(bounds["end"]) < hi:
        return {"status": "query_required", "reason": "缺少相同完整期间的比较基期。", "query_requirements": needed}
    comparisons = []
    for key, group in _groups(work, dimensions):
        pairs = []
        for period in (current, baseline):
            vals = pd.to_numeric(group.loc[group["__period"] == period, metric], errors="coerce").replace([float("inf"), -float("inf")], float("nan"))
            if len(vals) != 1 or vals.isna().any():
                return {"status": "query_required", "reason": "部分分组缺少唯一有效的本期或基期。", "query_requirements": needed}
            pairs.append(group.loc[group["__period"] == period, metric].iloc[0])
        comparisons.append({"group": _group_key(key, dimensions), "current_period": str(current),
                            "baseline_period": str(baseline), **_delta(*pairs, rate=rate)})
    return {"status": "ready", "comparisons": comparisons, "comparison_count": len(comparisons)}


def analyze_result(frame: pd.DataFrame, *, requests: Sequence[ResultInspection], metric_ids: Sequence[str],
                   scope: Mapping[str, Any] | None = None, additive_metric_ids: Sequence[str] = (),
                   metric_semantics: Mapping[str, Mapping[str, str]] | None = None,
                   time_column: str | None = None, numerator_overrides: Mapping[int, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Requests share one complete, allowlisted frame. No hidden re-query occurs here."""
    source_scope, semantics = dict(scope or {}), metric_semantics or {}
    answers = []
    seen = []
    for request_index, request in enumerate(requests):
        if request in seen:
            continue
        seen.append(request)
        population, scope = frame, source_scope
        if request.within_period:
            if not time_column:
                raise ResultInspectionError("期间内计算需要已返回的时间列。")
            times = frame[column(time_column, frame.columns)].map(lambda v: pd.Timestamp(v).tz_localize(None))
            start, end = (pd.Timestamp(v) for v in request.within_period)
            population = frame.loc[(times >= start) & (times < end)].reset_index(drop=True)
            scope = {**source_scope, "time_range":{"start":request.within_period[0],"end":request.within_period[1]}}
        metric = column(request.metric_id, frame.columns) if request.metric_id else None
        if request.metric_id and request.metric_id not in metric_ids:
            raise ResultInspectionError("只能对本次已返回的指标计算。")
        selected = select_rows(population, request, metric_ids)
        values = pd.to_numeric(selected[metric], errors="coerce").replace([float("inf"), -float("inf")], float("nan")) if metric else pd.Series(dtype=float)
        spec = semantics.get(request.metric_id, {})
        additive = request.metric_id in additive_metric_ids
        base = {"operation": request.operation, "metric_id": request.metric_id, "request_index":request_index,
                "within_period":list(request.within_period) if request.within_period else None,
                "unit": spec.get("unit", "原指标单位"), "status": "ready",
                "selected_count": len(selected), "null_count": int(values.isna().sum()),
                "scope": "returned_top_n" if scope.get("top_n") else "returned_result"}
        result = {}
        if request.operation == "rows":
            result = {"rows": _records(selected.iloc[request.offset:request.offset + request.limit])}
        elif request.operation == "sum":
            if not additive:
                result = {"status": "query_required", "reason": "该指标不可跨组求和；需按原口径重新查询。", "query_requirements": {"dimensions": [], "time_grain": None, "top_n": None}}
            else:
                result = {"value": _sum(selected.loc[values.notna(), metric])}
        elif request.operation == "compare":
            if len(values) != 2 or values.isna().any():
                result = {"status": "unsupported", "reason": "比较需要两个非空数值。"}
            else:
                result = {**_delta(*selected[metric], rate=spec.get("kind") == "rate"), "row_numbers": list(request.row_numbers),
                          "operands": _records(selected)}
        elif request.operation == "trend":
            result = _trend(selected, metric, request, scope, time_column, additive)
        elif request.operation == "period_compare":
            result = _period_compare(selected, metric, request, scope, time_column, additive, spec.get("kind") == "rate")
        elif request.operation == "share":
            if not additive:
                result = {"status": "unsupported", "reason": "非可加或重叠群组不能用组值相加计算占比。"}
            elif request.denominator == "business_total" and scope.get("top_n"):
                numerator = selected.sort_values(metric, ascending=False, kind="stable").head(request.numerator_top_n) if request.numerator_top_n else selected
                result = {"status": "query_required", "reason": "前N项没有同口径全体分母。", "query_requirements": {"top_n": None},
                          "numerator":_sum(numerator[metric]), "numerator_rows":len(numerator)}
            elif request.share_basis == "change":
                compared = _period_compare(population, metric, request, scope, time_column, additive, False)
                if compared["status"] != "ready" or not compared.get("comparisons"):
                    result = compared
                elif request.row_numbers:
                    result = {"status":"unsupported", "reason":"增量贡献按实体及期间计算，请用分组值选择实体；时序原始行号不能充当实体编号。"}
                else:
                    changes = pd.DataFrame([{**r["group"], metric:r["difference"]} for r in compared["comparisons"]])
                    numerator = select_rows(changes, request, (request.metric_id,))
                    if request.numerator_top_n:
                        numerator = numerator.sort_values(metric, ascending=False, kind="stable").head(request.numerator_top_n)
                    a, b = _sum(numerator[metric]), _sum(changes[metric])
                    result = {"numerator":a, "denominator":b, "denominator_scope":request.denominator,
                              "share_basis":"change", "share_percent":float(Decimal(str(a))/Decimal(str(b))*100) if a is not None and b is not None and b>0 else None,
                              "contributions":[{**{k:v for k,v in r.items() if k != metric}, "difference":r[metric],
                                  "contribution_percent":float(Decimal(str(r[metric]))/Decimal(str(b))*100) if b is not None and b>0 else None}
                                  for r in _records(numerator.iloc[request.offset:request.offset+request.limit])],
                              "denominator_status":"positive" if b is not None and b>0 else "undefined"}
            else:
                numerator = selected
                if request.numerator_top_n:
                    numerator = selected.sort_values(metric, ascending=False, kind="stable").head(request.numerator_top_n)
                original = (numerator_overrides or {}).get(request_index)
                a, b = original["value"] if original else _sum(numerator[metric]), _sum(population[metric])
                result = {"numerator": a, "denominator": b, "denominator_scope": request.denominator,
                          "numerator_rows": original["row_count"] if original else len(numerator), "share_percent": float(Decimal(str(a)) / Decimal(str(b)) * 100) if a is not None and b is not None and b > 0 else None,
                          "denominator_status": "positive" if b is not None and b > 0 else "undefined"}
        elif request.operation == "rank":
            rank_frame = selected.copy()
            sort_key = metric
            if request.rank_by != "value":
                entity_request = request.model_copy(update={"group_by": tuple(scope.get("dimensions") or ())})
                compared = _period_compare(selected, metric, entity_request, scope, time_column, additive, False)
                if compared["status"] != "ready" or not compared.get("comparisons"):
                    answers.append({**base, **compared})
                    continue
                sort_key = "difference" if request.rank_by == "change" else "relative_change_percent"
                rank_frame = pd.DataFrame([{**c["group"], sort_key: c[sort_key]} for c in compared["comparisons"]])
            partitions = [column(d, rank_frame.columns) for d in request.group_by]
            rows = []
            ties = sorted(c for c in rank_frame.columns if c not in {sort_key, *partitions})
            for _, group in _groups(rank_frame, partitions):
                ordered = group.dropna(subset=[sort_key]).sort_values([sort_key, *ties], ascending=[request.direction == "asc", *([True] * len(ties))], kind="stable")
                ordered = ordered.copy()
                ordered["rank"] = ordered[sort_key].rank(method="dense", ascending=request.direction == "asc").astype(int) if request.include_ties else range(1, len(ordered) + 1)
                rows.extend(_records(ordered.loc[ordered["rank"] <= request.limit]))
            result = {"rows": rows, "rank_by": request.rank_by, "include_ties": request.include_ties, "ranked_count": len(rows),
                      "unrankable_count": int(rank_frame[sort_key].isna().sum())}
        elif request.operation == "distribution":
            valid = values.dropna()
            result = {"population": "返回群组的指标值，不是原始订单分布", "count": len(valid),
                      "minimum": scalar(valid.min()) if len(valid) else None,
                      "maximum": scalar(valid.max()) if len(valid) else None,
                      "median": scalar(valid.median()) if len(valid) else None,
                      "p25": scalar(valid.quantile(.25)) if len(valid) else None,
                      "p75": scalar(valid.quantile(.75)) if len(valid) else None}
        elif request.operation == "threshold":
            threshold = request.threshold_value
            if request.threshold_unit == "percent":
                if spec.get("kind") != "rate":
                    result = {"status": "unsupported", "reason": "当前指标不是比例，不能把阈值当作百分比。"}
                threshold /= 100
            if not result:
                mask = getattr(values, request.comparison)(threshold) & values.notna()
                matched = selected.loc[mask]
                result = {"threshold": threshold, "comparison": request.comparison, "match_count": len(matched),
                          "population": "返回群组数，不是原始订单数", "rows": _records(matched.iloc[request.offset:request.offset + request.limit])}
        answers.append({**base, **result})
    return answers
