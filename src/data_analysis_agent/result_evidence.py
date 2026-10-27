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
from .result_presentation import display_value, display_evidence_row


RESULT_EVIDENCE_PREFIX = "已通过结果合同的可信结果摘要："

def result_evidence_token_limit() -> int:
    """Persistence ceiling, not the per-call allocation (which uses remaining space)."""
    available = int(os.getenv("DATA_ANALYSIS_MAX_PROMPT_TOKENS", "64000")) - int(os.getenv("DATA_ANALYSIS_MAX_OUTPUT_TOKENS", "4000"))
    configured = os.getenv("DATA_ANALYSIS_MAX_RESULT_EVIDENCE_TOKENS")
    return min(available, int(configured)) if configured else available


def render_analysis_facts(evidence: Mapping[str, Any]) -> list[str]:
    """Authoritative statements about requested operations, not an LLM paraphrase."""
    lines = []
    labels = evidence.get("column_labels", {})
    for result in evidence.get("analyses", []):
        op, metric = result["operation"], result.get("metric_id")
        label = labels.get(metric, metric)
        if result["status"] != "ready":
            lines.append(f"{label or '本次分析'}：{result['reason']}")
            continue
        unit = result.get("unit", "原指标单位")
        if result.get("within_period"):
            lines.append(f"本项计算范围：{result['within_period'][0]}（含）至{result['within_period'][1]}（不含）。")
        if op == "rows":
            lines.extend(display_evidence_row(row, evidence) for row in result["rows"])
        elif op == "sum":
            lines.append(f"选定结果的{label}合计：{result['value']}（{unit}）。")
        elif op == "compare":
            lines.append(f"{label}：第一项{result['current']}，第二项{result['baseline']}，差额{result['difference']}（{unit}）。")
            lines.extend(_comparison_text(label, result))
        elif op == "trend":
            for group in result["groups"]:
                name = "，".join(f"{labels.get(k,k)}={v}" for k,v in group["group"].items()) or "当前范围"
                judgment = {"true": "成立", "false": "不成立", "insufficient": "证据不足"}[group["judgment"]]
                kind = "从未下降" if result["mode"] == "nondecreasing" else "逐期严格增长"
                lines.append(f"{name}的{label}：上涨{group['up_count']}次、下降{group['down_count']}次、持平{group['flat_count']}次；{kind}判断：{judgment}。")
                counter = group.get("first_counterexample")
                if counter:
                    lines.append(f"反例：{counter['period']}为{counter['current']}，前期{counter['previous_period']}为{counter['baseline']}（{unit}）。")
                end = group.get("endpoints")
                if end:
                    lines.append(f"{end['last_period']}较{end['first_period']}差额{end['difference']}（{unit}）；首末变化不代表中间持续增长。")
                lines.append(f"最长连续上涨{group['longest_up_transitions']}次、连续下降{group['longest_down_transitions']}次。")
                if group.get("largest_drop"):
                    drop = group["largest_drop"]
                    lines.append(f"最大下跌发生在{drop['period']}，较{drop['previous_period']}变化{drop['difference']}（{unit}）。")
                if not group["complete"]:
                    lines.append(f"覆盖不完整：缺少期间{group['missing_periods']}；无效值{group['null_count']}；重复期间={group['duplicate_periods']}；部分期间={group['partial_periods']}；前N项限制={group['top_n_limited']}。未将缺失补0。")
                if group.get("derived_rows"):
                    lines.append(group["derived_scope"] + "：")
                    lines.extend(f"{row['period']}：{row['value'] if row['value'] is not None else '窗口期间不足'}（{unit}）" for row in group["derived_rows"])
                if group.get("calculation_notice"):
                    lines.append(group["calculation_notice"])
            if not result["all_groups_included"]:
                lines.append(f"共{result['group_count']}组，仅列本次选定分组。")
        elif op == "period_compare":
            for comparison in result.get("comparisons", []):
                name = "，".join(f"{labels.get(k,k)}={v}" for k,v in comparison["group"].items()) or "当前范围"
                lines.append(f"{name}的{label}：{comparison['current_period']}为{comparison['current']}，基期{comparison['baseline_period']}为{comparison['baseline']}，差额{comparison['difference']}（{unit}）。")
                lines.extend(_comparison_text(label, comparison))
            if result.get("notice"):
                lines.append(result["notice"])
        elif op == "share":
            percent = result.get("share_percent")
            lines.append(f"{label}分子{result['numerator']}，分母{result['denominator']}（{unit}），占比{percent:.2f}%。" if percent is not None else f"{label}的分母非正或为空，不能计算占比。")
            lines.append("分母为同口径全体。" if result["denominator_scope"] == "business_total" else "分母为本次查询范围内的完整结果合计，沿用原时间与筛选。")
            if result.get("share_basis") == "change":
                lines.append("上述分子和分母均为相同本期与基期之间的增量；以下为数值贡献，不推断业务原因：")
                for row in result["contributions"]:
                    name = "，".join(f"{labels.get(k,k)}={v}" for k,v in row.items() if k not in {"difference","contribution_percent"}) or "当前范围"
                    percent = row["contribution_percent"]
                    lines.append(f"{name}：增量{row['difference']}（{unit}），贡献率{percent:.2f}%。" if percent is not None else f"{name}：增量{row['difference']}（{unit}），总体净增量非正，贡献率未定义。")
        elif op == "distribution":
            lines.append(f"{label}：有效群组{result['count']}，空值{result['null_count']}，中位数{result['median']}，25%分位数{result['p25']}，75%分位数{result['p75']}（{unit}）。{result['population']}。")
        elif op in {"rank", "threshold"}:
            if op == "threshold":
                lines.append(f"{label}满足阈值的群组数：{result['match_count']}。{result['population']}。")
            else:
                ranking = {"value":"指标值", "change":"增量", "relative_change":"增长率"}[result["rank_by"]]
                lines.append(f"{label}排名（按{ranking}）：")
                if result.get("unrankable_count"):
                    lines.append(f"{result['unrankable_count']}组缺少可用数值或正基数，未参与排名。")
            for row in result["rows"]:
                names = {"rank":"名次", "difference":"增量", "relative_change_percent":"增长率（%）", **labels}
                lines.append("，".join(f"{names.get(k,k)}：{v}" for k,v in row.items()))
        if result.get("detail_items_omitted"):
            lines.append(f"受本次上下文预算限制，另有{result['detail_items_omitted']}项计算明细未展开；计算仍覆盖完整查询结果，可继续按分组或行定位查看。")
    return lines


def _comparison_text(label, result):
    lines = []
    if result.get("percentage_points") is not None:
        lines.append(f"{label}变化{result['percentage_points']:.2f}个百分点。")
    relative = result.get("relative_change_percent")
    lines.append(f"相对基期变化{relative:.2f}%。" if relative is not None else "基数为零或负数，不按正基数惯例计算增长率。")
    return lines


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
                "minimum_display": display_value(metric_id, numeric.min()),
                "maximum_display": display_value(metric_id, numeric.max()),
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
    formatted = [{c: display_value(metric_columns[c], row[c]) for c in metric_columns} for row in rows]
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
