"""Deterministic semantic checks for tabular SQL results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from typing import Any, Literal, Mapping, Sequence

import pandas as pd


ValidationState = Literal["valid", "degraded", "needs_clarification", "refuse"]


@dataclass(frozen=True)
class ResultValidation:
    state: ValidationState
    reason: str
    row_count: int
    columns: tuple[str, ...]
    missing_columns: tuple[str, ...] = ()
    null_metric_columns: tuple[str, ...] = ()
    dropped_columns: tuple[str, ...] = ()
    truncated: bool = False
    display_truncated: bool = False
    time_start: str | None = None
    time_end: str | None = None

    @property
    def safe_to_answer(self) -> bool:
        """Whether this result may be stamped as a trusted contract result."""
        return self.state == "valid"

    @property
    def safe_to_display(self) -> bool:
        """Whether the table may be shown, including imperfect but readable results."""
        return self.state in {"valid", "degraded"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "row_count": self.row_count,
            "column_count": len(self.columns),
            "columns": list(self.columns),
            "missing_columns": list(self.missing_columns),
            "null_metric_columns": list(self.null_metric_columns),
            "dropped_columns": list(self.dropped_columns),
            "truncated": self.truncated,
            "execution_truncated": self.truncated,
            "display_truncated": self.display_truncated,
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

    def __init__(self, max_rows: int = 1_000, display_row_budget: int | None = None):
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if display_row_budget is None:
            # Small test/evaluation caps should not make construction fail;
            # the preview cannot exceed the configured execution cap.
            display_row_budget = min(200, max_rows)
        if display_row_budget <= 0:
            raise ValueError("display_row_budget must be positive")
        if display_row_budget > max_rows:
            raise ValueError("display_row_budget cannot exceed max_rows")
        self.max_rows = max_rows
        self.display_row_budget = display_row_budget

    def validate(
        self,
        frame: pd.DataFrame,
        *,
        required_columns: Sequence[str] = (),
        required_column_aliases: Mapping[str, Sequence[str]] | None = None,
        metric_columns: Sequence[str] = (),
        time_column: str | None = None,
        time_column_aliases: Sequence[str] = (),
        time_bucket_grain: str | None = None,
        requested_start: str | date | datetime | None = None,
        requested_end: str | date | datetime | None = None,
        requested_end_exclusive: bool = False,
        limit_applied: bool = False,
        execution_truncated: bool | None = None,
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
        metric_names = {str(column) for column in metric_columns}
        missing_metrics = tuple(column for column in missing if column in metric_names)
        missing_shape = tuple(column for column in missing if column not in metric_names)
        if missing_metrics:
            return ResultValidation(
                "refuse",
                "结果缺少请求所需的指标列，未输出确定性数字。",
                len(frame),
                columns,
                missing_columns=missing,
            )
        unexpected: tuple[str, ...] = ()
        if exact_columns:
            allowed_actual_columns: set[str] = set()
            for column in required_columns:
                allowed_actual_columns.update(
                    candidate
                    for candidate in dict.fromkeys((column, *aliases.get(column, ())))
                    if candidate in frame.columns
                )
            unexpected = tuple(
                column for column in columns if column not in allowed_actual_columns
            )
        if join_multiplicity and any(value > 1 for value in join_multiplicity.values()):
            return ResultValidation(
                "refuse",
                "关联粒度可能造成重复放大，未输出确定性数字。",
                len(frame),
                columns,
            )
        shape_notes: list[str] = []
        if unexpected:
            shape_notes.append(
                "结果包含未在合同中声明的列，展示时已去掉："
                + "、".join(unexpected)
                + "。"
            )
        if missing_shape:
            shape_notes.append(
                "结果缺少计划中的列，已显示现有列：" + "、".join(missing_shape) + "。"
            )
        if frame.empty:
            return ResultValidation(
                "degraded",
                "查询没有返回数据。请核对时间范围、筛选条件或指标口径。"
                + ("".join(shape_notes)),
                0,
                columns,
                missing_columns=missing_shape,
                dropped_columns=unexpected,
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
        reached_cap = (
            (len(frame) >= self.max_rows or limit_applied)
            if execution_truncated is None
            else execution_truncated
        )
        if reached_cap:
            if execution_truncated is True:
                shape_notes.append(
                    f"结果超过执行上限 {self.max_rows} 行，已只返回前 {self.max_rows} 行。"
                    "多读的一行只用于判断是否截断，未展示给用户或模型。"
                )
            else:
                # Callers that did not run the +1 sentinel keep the conservative
                # wording; they must not claim a probe row was read and discarded.
                shape_notes.append(
                    f"结果达到执行上限 {self.max_rows} 行，可能不完整；请缩小查询范围后重试。"
                )
        time_start = None
        time_end = None
        if time_column and (requested_start is not None or requested_end is not None):
            candidate_time_columns = tuple(
                dict.fromkeys((time_column, *time_column_aliases))
            )
            resolved_time_column = next(
                (column for column in candidate_time_columns if column in frame.columns),
                None,
            )
            if resolved_time_column is None:
                shape_notes.append("结果没有可核对的时间列，表中的数字仍会显示。")
            else:
                parsed = pd.to_datetime(frame[resolved_time_column], errors="coerce").dropna()
                if parsed.empty:
                    shape_notes.append("结果没有可核对的时间覆盖，表中的数字仍会显示。")
                else:
                    start = pd.Timestamp(requested_start) if requested_start is not None else None
                    end = pd.Timestamp(requested_end) if requested_end is not None else None
                    # PostgreSQL timestamptz buckets are timezone-aware while QuerySpec
                    # dates are intentionally timezone-neutral. Compare both on the
                    # same naive timeline rather than raising on mixed timestamp types.
                    if parsed.dt.tz is not None:
                        parsed = parsed.dt.tz_localize(None)
                    if start is not None and start.tzinfo is not None:
                        start = start.tz_localize(None)
                    if end is not None and end.tzinfo is not None:
                        end = end.tz_localize(None)
                    if start is not None and time_bucket_grain is not None:
                        start = _bucket_start(start, time_bucket_grain)
                    if (
                        start is not None and parsed.min() < start
                        or end is not None and (
                            parsed.max() >= end if requested_end_exclusive else parsed.max() > end
                        )
                    ):
                        shape_notes.append(
                            "结果时间覆盖与请求范围不完全一致："
                            f"{parsed.min().isoformat()} 至 {parsed.max().isoformat()}。"
                        )
                    time_start = parsed.min().isoformat()
                    time_end = parsed.max().isoformat()
        if shape_notes:
            return ResultValidation(
                "degraded",
                "".join(shape_notes),
                len(frame),
                columns,
                missing_columns=missing_shape,
                dropped_columns=unexpected,
                truncated=reached_cap,
                display_truncated=len(frame) > self.display_row_budget,
                time_start=time_start,
                time_end=time_end,
            )
        return ResultValidation(
            "valid",
            "结果通过确定性检查。"
            + (
                f" 查询完整执行，共返回 {len(frame)} 行；界面仅预览前 {self.display_row_budget} 行。"
                if len(frame) > self.display_row_budget
                else ""
            ),
            len(frame),
            columns,
            display_truncated=len(frame) > self.display_row_budget,
            time_start=time_start,
            time_end=time_end,
        )


def _bucket_start(value: pd.Timestamp, grain: str) -> pd.Timestamp:
    """Map a requested instant to the label used by PostgreSQL date_trunc.

    A grouped time-series result legitimately labels a partial month, quarter,
    year, or Monday-starting week with the beginning of that bucket.  This
    helper is intentionally available only when the server owns the grain;
    arbitrary model-provided labels keep the strict time-bound check.
    """
    if grain == "day":
        return value.normalize()
    if grain == "week":
        return value.to_period("W-SUN").start_time
    if grain == "month":
        return value.to_period("M").start_time
    if grain == "quarter":
        return value.to_period("Q").start_time
    if grain == "year":
        return value.to_period("Y").start_time
    raise ValueError(f"unsupported server-owned time bucket grain: {grain}")
