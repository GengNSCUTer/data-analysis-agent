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
import re
from typing import Any, Literal, Mapping, Sequence

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .token_budget import DEFAULT_TOKEN_COUNTER, ContextCapacityError, TokenCounter
from .result_presentation import display_value


RESULT_EVIDENCE_PREFIX = "已通过结果合同的可信结果摘要："
DEFAULT_RESULT_EVIDENCE_TOKENS = 4_000

_NUMBER = re.compile(r"(?<![a-zA-Z0-9_.])-?\d+(?:,\d{3})*(?:\.\d+)?%?")
_ORDINAL = re.compile(r"(?:第|排名第|前|top\s*)\s*$", re.IGNORECASE)
_UNIT = re.compile(r"^\s*(?:名|位|个|项|条|行|组|页|的|是|为)")
_COUNT = re.compile(r"(?:共|总共|返回|展示|显示|列出|保留)\s*$")


def _business_numbers(text: str) -> set[tuple[Decimal, bool]]:
    values = set()
    for match in _NUMBER.finditer(text or ""):
        before = text[max(0, match.start() - 12):match.start()]
        after = text[match.end():match.end() + 8]
        line_before = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
        if _ORDINAL.search(before) and (re.search(r"top\s*$", before, re.IGNORECASE) or _UNIT.search(after)):
            continue
        if _COUNT.search(before) and _UNIT.search(after):
            continue
        if not line_before.strip() and re.match(r"\s*[.)）、:：-]", after):
            continue
        number = match.group(0).replace(",", "")
        values.add((Decimal(number.rstrip("%")), number.endswith("%")))
    return values


def followup_adds_numbers(answer: str, summary: str) -> bool:
    return not _business_numbers(answer).issubset(_business_numbers(summary))


def constrain_followup_answer(answer: str, summary: str) -> str:
    if not followup_adds_numbers(answer, summary):
        return answer
    if summary.startswith(RESULT_EVIDENCE_PREFIX):
        # The follow-up prompt may append verified scope after the evidence.
        evidence, _ = json.JSONDecoder().raw_decode(summary.removeprefix(RESULT_EVIDENCE_PREFIX))
        if "coverage" in evidence:
            return "解释中出现了证据外数字，以下直接展示已验证内容。\n" + render_result_facts(evidence)
    return "这次只复述已验证摘要，不补充摘要以外的数字。\n" + summary.strip()


def render_result_facts(evidence: Mapping[str, Any]) -> str:
    """Readable exact facts if an explanation cannot be used, without raw JSON."""
    labels = evidence.get("column_labels", {})
    lines = [f"完整结果共 {evidence['source_row_count']} 行；本次提供 {evidence['included_row_count']} 行。"]
    for number, row in zip(evidence["row_numbers"], evidence["sample_rows"]):
        lines.append(f"第 {number} 行：" + "，".join(f"{labels.get(name, name)}：{display_value(name, value)}" for name, value in row.items()))
    for metric_id, stat in evidence.get("statistics", {}).items():
        label = labels.get(metric_id, metric_id)
        lines.append(f"{label}在这份结果中的最小值：{stat['minimum_display']}；最大值：{stat['maximum_display']}。")
        if "sum_of_returned_rows" in stat:
            lines.append(f"这份结果的{label}合计：{stat['sum_of_returned_rows']}。")
    calculation = evidence.get("calculation")
    if calculation:
        if calculation["operation"] == "sum":
            lines.append(f"选定结果合计：{calculation['value']}。")
        else:
            lines.append(f"第一行减第二行的差额：{calculation['difference_first_minus_second']}。")
    if evidence.get("top_n") is not None:
        lines.append("本次仅覆盖查询返回的前N项，不代表全部业务总额。")
    return "\n".join(lines)


def result_evidence_token_limit() -> int:
    return int(os.getenv("DATA_ANALYSIS_MAX_RESULT_EVIDENCE_TOKENS", DEFAULT_RESULT_EVIDENCE_TOKENS))


class ResultInspection(BaseModel):
    """A model may select a bounded operation; it cannot supply code or a path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["overview", "rows", "sum", "compare"] = "overview"
    metric_id: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    row_numbers: tuple[int, ...] = Field(default=(), max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def valid_operation(self):
        if any(number < 1 for number in self.row_numbers):
            raise ValueError("row numbers start at one")
        if self.operation in {"sum", "compare"} and not self.metric_id:
            raise ValueError("a calculation requires metric_id")
        if self.operation == "compare" and len(self.row_numbers) != 2:
            raise ValueError("compare requires exactly two source row numbers")
        return self


class ResultInspectionError(ValueError):
    """The saved result cannot support the proposed calculation."""


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
        raise ContextCapacityError("结果证据超过上下文预算，请从完整工件重新构建。")
    return text


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
    inspection: ResultInspection | None = None,
    max_tokens: int | None = None,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> str:
    """Include complete small results, otherwise full-result facts and selected rows."""
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
    request = inspection or ResultInspection()
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
    metric = _column(request.metric_id, columns) if request.metric_id else None
    if metric is not None and metric not in metric_columns:
        raise ResultInspectionError("只能对本次已返回的指标计算。")
    additive = {_normalized(name) for name in additive_metric_ids}
    calculation: dict[str, Any] | None = None
    if request.operation == "sum":
        if _normalized(metric_columns[metric]) not in additive:
            raise ResultInspectionError("该指标不可跨组求和；比率、均值或去重计数需按原口径重新查询。")
        values = pd.to_numeric(selected[metric], errors="coerce").dropna()
        calculation = {"operation": "sum", "metric_id": metric_columns[metric],
                       "value": _sum(values), "scope": "selected_returned_rows"}
    elif request.operation == "compare":
        if len(selected) != 2:
            raise ResultInspectionError("比较所需的两行不在选定结果内。")
        values = pd.to_numeric(selected[metric], errors="coerce")
        if values.isna().any():
            raise ResultInspectionError("比较所需数值为空，不能计算。")
        left, right = (Decimal(str(value)) for value in values)
        calculation = {"operation": "compare", "metric_id": metric_columns[metric],
                       "row_numbers": list(request.row_numbers),
                       "difference_first_minus_second": _scalar(left - right),
                       "relative_change_percent": _scalar((left - right) / right * 100) if right else None,
                       "zero_denominator": not bool(right)}
        calculation["relative_change_display"] = f"{(left - right) / right * 100:.2f}%" if right else None
    population_count = len(selected)
    statistics = {}
    for column, metric_id in metric_columns.items():
        numeric = pd.to_numeric(selected[column], errors="coerce").dropna()
        if numeric.empty:
            continue
        stat = {"non_null_count": len(numeric), "minimum": _scalar(numeric.min()),
                "maximum": _scalar(numeric.max()),
                "minimum_display": display_value(metric_id, numeric.min()),
                "maximum_display": display_value(metric_id, numeric.max()),
                "minimum_row": _row(selected, numeric.idxmin()),
                "maximum_row": _row(selected, numeric.idxmax()),
                "maximum_tie_count": int((numeric == numeric.max()).sum()),
                "sum_allowed": _normalized(metric_id) in additive}
        if stat["sum_allowed"]:
            stat["sum_of_returned_rows"] = _sum(numeric)
        statistics[metric_id] = stat
    payload = {
        "metric_ids": list(metric_ids), "columns": columns,
        "column_labels": {name: _label(name, column_labels or {}) for name in columns},
        "source_row_count": source_count, "row_count": population_count,
        "scope": "returned_top_n" if top_n is not None else "returned_result",
        "top_n": top_n, "selection": request.model_dump(mode="json"),
        "statistics": statistics, "calculation": calculation,
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
    if counter.count_text(_render(payload)) > limit:
        raise ContextCapacityError("指标统计证据超过预算，请减少本次解释的指标或结果列。")
    if request.operation == "rows":
        selected = selected.iloc[request.offset:request.offset + request.limit]
    # Extremes and endpoints already describe the full population above. Row
    # samples preserve source order; they never masquerade as the whole result.
    for index in selected.index:
        row = _row(selected, index)
        payload["sample_rows"].append(row)
        payload["formatted_metrics"].append({column: display_value(metric_columns[column], row[column])
                                             for column in metric_columns})
        payload["row_numbers"].append(original_positions[index])
        payload["included_row_count"] += 1
        payload["coverage"] = "complete" if payload["included_row_count"] == population_count else "selected_rows"
        if counter.count_text(_render(payload)) > limit:
            payload["sample_rows"].pop()
            payload["formatted_metrics"].pop()
            payload["row_numbers"].pop()
            payload["included_row_count"] -= 1
            payload["coverage"] = "selected_rows"
            break
    return _render(payload)


def _row(frame: pd.DataFrame, index: int) -> dict[str, Any]:
    return {name: _scalar(value) for name, value in frame.loc[index].items()}


def _sum(values: pd.Series) -> int | float | None:
    if values.empty:
        return None
    return _scalar(sum((Decimal(str(value)) for value in values), Decimal(0)))


def _label(name: str, labels: Mapping[str, str]) -> str:
    return next((label for column, label in labels.items() if _normalized(name) == _normalized(column)), name)
