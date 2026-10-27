"""Bounded evidence and deterministic operations over an already validated result.

These operations read the returned population, not the underlying business facts.
Only Catalog-approved additive metrics can be summed across result groups.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import math
import os
from typing import Any, Mapping, Sequence

import pandas as pd
from .result_analysis import ResultInspection, ResultInspectionError, analyze_result

from .token_budget import DEFAULT_TOKEN_COUNTER, ContextCapacityError, TokenCounter
from .result_presentation import display_metric_value


RESULT_EVIDENCE_PREFIX = "已通过结果合同的可信结果摘要："

def result_evidence_token_limit() -> int:
    """Persistence ceiling, not the per-call allocation (which uses remaining space)."""
    available = int(os.getenv("DATA_ANALYSIS_MAX_PROMPT_TOKENS", "64000")) - int(os.getenv("DATA_ANALYSIS_MAX_OUTPUT_TOKENS", "4000"))
    configured = os.getenv("DATA_ANALYSIS_MAX_RESULT_EVIDENCE_TOKENS")
    return min(available, int(configured)) if configured else available




def _scalar(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _normalized(value: str) -> str:
    return "".join(c for c in value.casefold() if c.isalnum())


def _column(name: str, columns: Sequence[str]) -> str:
    matches = [column for column in columns if _normalized(column) == _normalized(name)]
    if len(matches) != 1:
        raise ResultInspectionError(f"结果中没有唯一可用的列：{name}")
    return matches[0]


def _render(payload: dict[str, Any]) -> str:
    return RESULT_EVIDENCE_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def bound_result_evidence(value: str | None, max_tokens: int | None = None) -> str | None:
    """Use the same token bound on generation, persistence and restoration.

    Oversized structured evidence must be regenerated, never cut mid-JSON.
    Plain legacy summaries are retained when they fit the current budget.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if DEFAULT_TOKEN_COUNTER.count_text(text) > (max_tokens or result_evidence_token_limit()):
        if text.startswith(RESULT_EVIDENCE_PREFIX):
            return resize_result_evidence(text, max_tokens or result_evidence_token_limit())
        raise ContextCapacityError("结果证据超过上下文预算，请从完整工件重新构建。")
    return text


def _fit_calculations(payload: dict, limit: int, counter: TokenCounter) -> None:
    """Trim presentation detail, never the population used for calculations."""
    while counter.count_text(_render(payload)) > limit:
        choices = []
        for result in payload["analyses"]:
            for key in ("rows", "comparisons", "groups", "contributions"):
                if result.get(key):
                    choices.append((result, key, result[key]))
            for group in result.get("groups", []):
                if group.get("derived_rows"):
                    choices.append((result, "derived_rows", group["derived_rows"]))
        if not choices:
            raise ContextCapacityError("计算结论超过本轮剩余上下文，请缩小要解释的指标或分组范围。")
        result, key, details = max(choices, key=lambda entry: len(json.dumps(entry[2], ensure_ascii=False)))
        cut = max(1, len(details) // 2)
        del details[-cut:]
        result["detail_items_omitted"] = result.get("detail_items_omitted", 0) + cut
        if key == "groups":
            result["all_groups_included"] = False


def resize_result_evidence(summary: str, max_tokens: int, counter: TokenCounter = DEFAULT_TOKEN_COUNTER) -> str:
    """Fit an existing evidence object to the actual final model request space."""
    payload = json.loads(summary.removeprefix(RESULT_EVIDENCE_PREFIX))
    payload["evidence_token_limit"] = max_tokens
    rows, formatted, numbers = (payload[k] for k in ("sample_rows", "formatted_metrics", "row_numbers"))
    payload.update(sample_rows=[], formatted_metrics=[], row_numbers=[], included_row_count=0,
                   coverage="complete" if payload["row_count"] == 0 else "selected_rows")
    _fit_calculations(payload, max_tokens, counter)
    low, high = 0, len(rows)
    def pack(count):
        payload.update(sample_rows=rows[:count], formatted_metrics=formatted[:count], row_numbers=numbers[:count],
                       included_row_count=count, coverage="complete" if count == payload["row_count"] else "selected_rows")
    while low < high:
        middle = (low + high + 1) // 2
        pack(middle)
        if counter.count_text(_render(payload)) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    pack(low)
    return _render(payload)


def build_result_evidence(
    frame: pd.DataFrame,
    *,
    required_columns: Sequence[str],
    metric_ids: Sequence[str],
    column_aliases: Mapping[str, Sequence[str]] | None = None,
    column_labels: Mapping[str, str] | None = None,
    additive_metric_ids: Sequence[str] = (),
    time_column: str | None = None,
    top_n: int | Mapping[str, int] | None = None,
    inspections: Sequence[ResultInspection] = (),
    query_scope: Mapping[str, Any] | None = None,
    metric_semantics: Mapping[str, Mapping[str, str]] | None = None,
    numerator_overrides: Mapping[int, Mapping[str, Any]] | None = None,
    max_tokens: int | None = None,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> str:
    """Include complete small results, otherwise full-result facts and selected rows."""
    top_n = top_n or None
    limit = max_tokens or result_evidence_token_limit()
    allowed = set(required_columns) | set(metric_ids)
    for name, aliases in (column_aliases or {}).items():
        if name in allowed:
            allowed.update(aliases)
    # Column names may have been folded by PostgreSQL; use only contract columns.
    normalized_allowed = {_normalized(name) for name in allowed}
    columns = [str(name) for name in frame.columns if _normalized(str(name)) in normalized_allowed]
    if not columns:
        raise ResultInspectionError("没有可用于解释的已验证结果列。")
    selected = frame.loc[:, columns].reset_index(drop=True)
    source_count = len(selected)
    original_positions = list(range(1, source_count + 1))
    requests = tuple(inspections)
    request = next((r for r in requests if r.operation == "rows"), ResultInspection())
    metric_columns = {_column(name, columns): name for name in metric_ids}
    for name, value in request.filters.items():
        column = _column(name, columns)
        if column in metric_columns:
            raise ResultInspectionError("结果筛选只能选择已返回的维度或时间列。")
        selected = selected.loc[selected[column].map(lambda item: str(_scalar(item))) == value]
    if request.row_numbers:
        if max(request.row_numbers) > source_count:
            raise ResultInspectionError("所请求的行超出这份完整结果，不能从样本猜测。")
        selected = selected.loc[list(dict.fromkeys(number - 1 for number in request.row_numbers if number - 1 in selected.index))]
    additive = {_normalized(name) for name in additive_metric_ids}
    scope = {**(query_scope or {}), "top_n": top_n}
    canonical_time = None
    if time_column:
        time_names = {_normalized(n) for n in (time_column, *(column_aliases or {}).get(time_column, ()))}
        canonical_time = next((n for n in columns if _normalized(n) in time_names), None)
    # A normal trend query also receives computed facts, even without an explicit operation.
    if not requests and canonical_time and scope.get("time_grain"):
        requests = tuple(ResultInspection(operation="trend", metric_id=m) for m in metric_ids)
    analyses = analyze_result(frame.loc[:, columns].reset_index(drop=True), requests=requests,
                             metric_ids=metric_ids, scope=scope, additive_metric_ids=additive_metric_ids,
                             metric_semantics=metric_semantics, time_column=canonical_time, numerator_overrides=numerator_overrides)
    population_count = len(selected)
    statistics = {}
    for column, metric_id in metric_columns.items():
        numeric = pd.to_numeric(selected[column], errors="coerce").replace([float("inf"), -float("inf")], float("nan")).dropna()
        if numeric.empty:
            continue
        stat = {"non_null_count": len(numeric), "minimum": _scalar(numeric.min()),
                "maximum": _scalar(numeric.max()),
                "minimum_display": display_metric_value(metric_id, numeric.min(), metric_semantics or {}),
                "maximum_display": display_metric_value(metric_id, numeric.max(), metric_semantics or {}),
                "minimum_row": _row(selected, numeric.idxmin()),
                "maximum_row": _row(selected, numeric.idxmax()),
                "maximum_tie_count": int((numeric == numeric.max()).sum()),
                "minimum_tie_count": int((numeric == numeric.min()).sum()),
                "sum_allowed": _normalized(metric_id) in additive}
        if stat["sum_allowed"]:
            stat["sum_of_returned_rows"] = _sum(selected.loc[numeric.index, column])
        statistics[metric_id] = stat
    payload = {
        "metric_ids": list(metric_ids), "columns": columns,
        "metric_columns": metric_columns,
        "metric_semantics": {metric_id: dict(spec) for metric_id, spec in (metric_semantics or {}).items()
                             if metric_id in metric_ids},
        "column_labels": {name: _label(name, column_labels or {}) for name in columns},
        "source_row_count": source_count, "row_count": population_count,
        "scope": "returned_top_n" if top_n is not None else "returned_result",
        "top_n": top_n, "selection": request.model_dump(mode="json"),
        "statistics": statistics, "analyses": analyses,
        "evidence_token_limit": limit,
        "coverage": "complete" if population_count == 0 else "selected_rows", "included_row_count": 0,
        "row_numbers": [], "sample_rows": [], "formatted_metrics": [],
    }
    if time_column:
        time_names = {_normalized(name) for name in (time_column, *(column_aliases or {}).get(time_column, ()))}
        time_matches = [name for name in columns if _normalized(name) in time_names]
        if time_matches and not selected.empty:
            chronological = selected.sort_values(time_matches[0])
            payload["time_endpoints"] = {"first": _row(chronological, chronological.index[0]),
                                         "last": _row(chronological, chronological.index[-1])}
    _fit_calculations(payload, limit, counter)
    if request.operation == "rows":
        selected = selected.iloc[request.offset:request.offset + request.limit]
    # Extremes and endpoints already describe the full population above. Row
    # samples preserve source order; they never masquerade as the whole result.
    rows = [_row(selected, index) for index in selected.index]
    positions = [original_positions[index] for index in selected.index]
    formatted = [{c: display_metric_value(metric_columns[c], row[c], metric_semantics or {}) for c in metric_columns} for row in rows]
    def pack(count):
        payload.update(sample_rows=rows[:count], formatted_metrics=formatted[:count], row_numbers=positions[:count],
                       included_row_count=count, coverage="complete" if count == population_count else "selected_rows")
    # Binary search avoids re-tokenizing an ever-growing prefix once per source row.
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high + 1) // 2
        pack(middle)
        if counter.count_text(_render(payload)) <= limit:
            low = middle
        else:
            high = middle - 1
    pack(low)
    return _render(payload)


def _row(frame: pd.DataFrame, index: int) -> dict[str, Any]:
    return {name: _scalar(value) for name, value in frame.loc[index].items()}


def _sum(values: pd.Series) -> int | float | None:
    if values.empty:
        return None
    return _scalar(sum((Decimal(str(value)) for value in values), Decimal(0)))


def _label(name: str, labels: Mapping[str, str]) -> str:
    return next((label for column, label in labels.items() if _normalized(name) == _normalized(column)), name)
