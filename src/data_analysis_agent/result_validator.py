"""Deterministic semantic checks for tabular SQL results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from typing import Any, Literal, Mapping, Sequence

import pandas as pd


ValidationState = Literal["valid", "needs_clarification", "refuse"]


@dataclass(frozen=True)
class ResultValidation:
    state: ValidationState
    reason: str
    row_count: int
    columns: tuple[str, ...]
    missing_columns: tuple[str, ...] = ()
    null_metric_columns: tuple[str, ...] = ()
    truncated: bool = False
    time_start: str | None = None
    time_end: str | None = None

    @property
    def safe_to_answer(self) -> bool:
        return self.state == "valid"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "row_count": self.row_count,
            "column_count": len(self.columns),
            "columns": list(self.columns),
            "missing_columns": list(self.missing_columns),
            "null_metric_columns": list(self.null_metric_columns),
            "truncated": self.truncated,
            "time_start": self.time_start,
            "time_end": self.time_end,
        }


class ResultValidationError(RuntimeError):
    """Safe-to-display failure raised before an unvalidated result is exposed."""

    def __init__(self, validation: ResultValidation):
        self.validation = validation
        super().__init__(validation.reason)


def build_result_summary(
    frame: pd.DataFrame,
    validation: ResultValidation,
    *,
    metric_ids: Sequence[str] = (),
    required_columns: Sequence[str] = (),
    column_aliases: Mapping[str, Sequence[str]] | None = None,
    column_labels: Mapping[str, str] | None = None,
    max_rows: int = 8,
    max_chars: int = 1_200,
) -> str:
    """Build a bounded summary from a result that already passed validation.

    The summary is intentionally derived from the DataFrame and the server
    result contract, never from assistant prose.  It is used as working
    memory for result follow-ups, so it contains only contract columns and a
    small number of JSON-safe sample rows.  It must not become a second data
    export channel.
    """
    if not validation.safe_to_answer:
        raise ValueError("only validated results may be summarized")
    if max_rows <= 0 or max_chars <= 0:
        raise ValueError("summary limits must be positive")

    aliases = column_aliases or {}
    labels = column_labels or {}
    allowed: list[str] = []
    for name in (*required_columns, *metric_ids):
        text = str(name).strip()
        if text and text not in allowed:
            allowed.append(text)
        for alias in aliases.get(text, ()):
            alias_text = str(alias).strip()
            if alias_text and alias_text not in allowed:
                allowed.append(alias_text)
    actual_columns = [str(column) for column in frame.columns]
    selected_columns = [column for column in actual_columns if column in allowed]
    # A legacy/custom runner may not provide a complete contract.  In that
    # case use the first few returned columns, still bounded, rather than
    # persisting an unbounded result preview.
    if not selected_columns:
        selected_columns = actual_columns[:8]

    rows = [
        {
            column: _summary_scalar(value)
            for column, value in row.items()
        }
        for row in frame.loc[:, selected_columns].head(max_rows).to_dict("records")
    ]
    payload: dict[str, Any] = {
        "metric_ids": [str(value) for value in metric_ids][:16],
        "columns": selected_columns[:16],
        "row_count": int(validation.row_count),
        "time_start": validation.time_start,
        "time_end": validation.time_end,
        "sample_rows": rows,
        "column_labels": {
            column: _display_label(column, labels) for column in selected_columns[:16]
        },
    }
    # Prefer complete sample rows, then progressively reduce the preview if a
    # provider returns unexpectedly verbose values.
    while True:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        summary = "已通过结果合同的可信结果摘要：" + rendered
        if len(summary) <= max_chars or not payload["sample_rows"]:
            return summary[:max_chars]
        payload["sample_rows"] = payload["sample_rows"][:-1]


def _summary_scalar(value: Any) -> Any:
    """Convert pandas/numpy scalars to small JSON-safe values."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 6)
    if isinstance(value, (int, bool, str)):
        return value if not isinstance(value, str) else value[:160]
    return str(value)[:160]


def _display_label(column: str, labels: Mapping[str, str]) -> str:
    """Resolve a Catalog label even when SQL uses a casing-only alias."""
    if column in labels:
        return str(labels[column])[:80]
    normalized = "".join(character for character in column.lower() if character.isalnum())
    for candidate, label in labels.items():
        candidate_normalized = "".join(
            character for character in str(candidate).lower() if character.isalnum()
        )
        if candidate_normalized == normalized:
            return str(label)[:80]
    return column.replace("_", " ")[:80]


class ResultValidator:
    """Reject only conditions that can be determined without an LLM."""

    def __init__(self, max_rows: int = 1_000):
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        self.max_rows = max_rows

    def validate(
        self,
        frame: pd.DataFrame,
        *,
        required_columns: Sequence[str] = (),
        required_column_aliases: Mapping[str, Sequence[str]] | None = None,
        metric_columns: Sequence[str] = (),
        time_column: str | None = None,
        time_column_aliases: Sequence[str] = (),
        requested_start: str | date | datetime | None = None,
        requested_end: str | date | datetime | None = None,
        limit_applied: bool = False,
        join_multiplicity: Mapping[str, int] | None = None,
        exact_columns: bool = False,
        metric_value_constraints: Mapping[str, Mapping[str, float | bool]] | None = None,
    ) -> ResultValidation:
        if not isinstance(frame, pd.DataFrame):
            return ResultValidation("refuse", "结果不是受支持的表格类型。", 0, ())
        columns = tuple(str(column) for column in frame.columns)
        aliases = required_column_aliases or {}
        missing = tuple(
            column
            for column in required_columns
            if not any(
                candidate in frame.columns
                for candidate in dict.fromkeys((column, *aliases.get(column, ())))
            )
        )
        if missing:
            return ResultValidation(
                "refuse",
                "结果缺少请求所需的指标或维度列。",
                len(frame),
                columns,
                missing_columns=missing,
            )
        if exact_columns:
            allowed_actual_columns: set[str] = set()
            for column in required_columns:
                allowed_actual_columns.update(
                    candidate
                    for candidate in dict.fromkeys((column, *aliases.get(column, ())))
                    if candidate in frame.columns
                )
            unexpected = tuple(column for column in columns if column not in allowed_actual_columns)
            if unexpected:
                return ResultValidation(
                    "refuse",
                    "结果包含未在服务器合同中声明的列。",
                    len(frame),
                    columns,
                )
        if join_multiplicity and any(value > 1 for value in join_multiplicity.values()):
            return ResultValidation(
                "refuse",
                "关联粒度可能造成重复放大，未输出确定性数字。",
                len(frame),
                columns,
            )
        if frame.empty:
            return ResultValidation(
                "needs_clarification",
                "查询没有返回数据，请确认时间范围、筛选条件或指标口径。",
                0,
                columns,
            )
        null_metrics = tuple(
            column
            for column in metric_columns
            if column in frame.columns and frame[column].isna().all()
        )
        if null_metrics:
            return ResultValidation(
                "refuse",
                "结果中的指标值全部为空，未输出确定性数字。",
                len(frame),
                columns,
                null_metric_columns=null_metrics,
            )
        for column in metric_columns:
            if column not in frame.columns:
                continue
            numeric = pd.to_numeric(frame[column], errors="coerce")
            if numeric.notna().any() and not numeric.dropna().map(math.isfinite).all():
                return ResultValidation(
                    "refuse",
                    "结果包含无法解释的数值，未输出确定性数字。",
                    len(frame),
                    columns,
                )
            constraints = (metric_value_constraints or {}).get(column, {})
            if numeric.notna().any():
                values = numeric.dropna()
                minimum = constraints.get("minimum")
                maximum = constraints.get("maximum")
                integer_like = constraints.get("integer_like", False)
                if (
                    (minimum is not None and (values < float(minimum)).any())
                    or (maximum is not None and (values > float(maximum)).any())
                    or (
                        integer_like
                        and not values.map(lambda value: float(value).is_integer()).all()
                    )
                ):
                    return ResultValidation(
                        "refuse",
                        "结果中的指标值不满足服务器定义的合理范围。",
                        len(frame),
                        columns,
                    )
        if len(frame) >= self.max_rows or limit_applied:
            return ResultValidation(
                "needs_clarification",
                "结果可能受到行数上限截断，请缩小范围或分批查询。",
                len(frame),
                columns,
                truncated=True,
            )
        if time_column and (requested_start is not None or requested_end is not None):
            candidate_time_columns = tuple(
                dict.fromkeys((time_column, *time_column_aliases))
            )
            resolved_time_column = next(
                (column for column in candidate_time_columns if column in frame.columns),
                None,
            )
            if resolved_time_column is None:
                return ResultValidation(
                    "needs_clarification",
                    "结果没有可核对的时间列，请补充明确的时间范围。",
                    len(frame),
                    columns,
                )
            parsed = pd.to_datetime(frame[resolved_time_column], errors="coerce").dropna()
            if parsed.empty:
                return ResultValidation(
                    "needs_clarification",
                    "结果没有可核对的时间覆盖，暂不输出确定性数字。",
                    len(frame),
                    columns,
                )
            start = pd.Timestamp(requested_start) if requested_start is not None else None
            end = pd.Timestamp(requested_end) if requested_end is not None else None
            if start is not None and parsed.min() < start or end is not None and parsed.max() > end:
                return ResultValidation(
                    "refuse",
                    "结果时间范围超出请求范围，未输出确定性数字。",
                    len(frame),
                    columns,
                    time_start=parsed.min().isoformat(),
                    time_end=parsed.max().isoformat(),
                )
            return ResultValidation(
                "valid",
                "结果通过指标、时间和粒度检查。",
                len(frame),
                columns,
                time_start=parsed.min().isoformat(),
                time_end=parsed.max().isoformat(),
            )
        return ResultValidation("valid", "结果通过确定性检查。", len(frame), columns)
