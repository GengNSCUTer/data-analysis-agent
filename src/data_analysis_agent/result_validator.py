"""Deterministic semantic checks for tabular SQL results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
        metric_columns: Sequence[str] = (),
        time_column: str | None = None,
        requested_start: str | date | datetime | None = None,
        requested_end: str | date | datetime | None = None,
        limit_applied: bool = False,
        join_multiplicity: Mapping[str, int] | None = None,
    ) -> ResultValidation:
        if not isinstance(frame, pd.DataFrame):
            return ResultValidation("refuse", "结果不是受支持的表格类型。", 0, ())
        columns = tuple(str(column) for column in frame.columns)
        missing = tuple(column for column in required_columns if column not in frame.columns)
        if missing:
            return ResultValidation(
                "refuse",
                "结果缺少请求所需的指标或维度列。",
                len(frame),
                columns,
                missing_columns=missing,
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
        if len(frame) >= self.max_rows or limit_applied:
            return ResultValidation(
                "needs_clarification",
                "结果可能受到行数上限截断，请缩小范围或分批查询。",
                len(frame),
                columns,
                truncated=True,
            )
        if time_column and (requested_start is not None or requested_end is not None):
            if time_column not in frame.columns:
                return ResultValidation(
                    "needs_clarification",
                    "结果没有可核对的时间列，请补充明确的时间范围。",
                    len(frame),
                    columns,
                )
            parsed = pd.to_datetime(frame[time_column], errors="coerce").dropna()
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
