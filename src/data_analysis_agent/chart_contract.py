"""Server-owned contracts for charts built from trusted SQL result artifacts.

The semantic interpreter proposes the requested chart type. This module checks
that proposal against the server-owned QueryPlan and ResultContract. The server
then renders only the validated result artifact, without a second model tool
call or display-time aggregation. Frozen legacy callers retain lexical requests.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping

import pandas as pd

from .query_plan import QueryPlan
from .result_artifact import ResultArtifact
from .semantic_catalog import ResultContract


CHART_CONTRACT_VERSION = "chart-contract-v2"
ChartStatus = Literal[
    "not_requested",
    "valid",
    "clarification_required",
    "unsupported",
]
ChartType = Literal["bar", "horizontal_bar", "line", "area", "donut"]

_CHART_TYPES = frozenset({"bar", "horizontal_bar", "line", "area", "donut"})

_VISUALIZATION_REQUEST = re.compile(
    r"图表|图形|可视化|柱状图|条形图|横向条形图|横向柱状图|水平条形图|"
    r"折线图|曲线图|面积图|饼图|环形图|甜甜圈图|占比构成|散点图|热力图|地图|画图|绘图"
)
_BAR_REQUEST = re.compile(r"(?<!横向)(?<!水平)(?<!竖向)(?<!纵向)柱状图|竖向柱状图|纵向柱状图")
_HORIZONTAL_BAR_REQUEST = re.compile(r"横向条形图|横向柱状图|水平条形图|条形图")
_LINE_REQUEST = re.compile(r"折线图|曲线图")
_AREA_REQUEST = re.compile(r"面积图|面积趋势图")
_DONUT_REQUEST = re.compile(r"饼图|环形图|甜甜圈图|占比构成")
_DONUT_COMPATIBLE_METRICS = frozenset(
    {
        "gmv",
        "paid_order_count",
        "item_count",
        "freight_amount",
        "canceled_order_count",
        "delivered_order_count",
        "unavailable_order_count",
        "review_count",
    }
)
_UNSUPPORTED_REQUESTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"散点图"), "散点图"),
    (re.compile(r"热力图"), "热力图"),
    (re.compile(r"地图|地理图|地图可视化"), "地图"),
    (re.compile(r"雷达图"), "雷达图"),
    (re.compile(r"直方图|分布图"), "直方图"),
    (re.compile(r"漏斗图"), "漏斗图"),
    (re.compile(r"瀑布图"), "瀑布图"),
    (re.compile(r"堆叠柱状图|堆叠条形图"), "堆叠图"),
)


class ChartContractError(ValueError):
    """A result artifact cannot be safely rendered under its chart contract."""


@dataclass(frozen=True)
class ChartContract:
    """Bounded chart semantics derived before the SQL Agent is invoked."""

    status: ChartStatus
    requested: bool
    chart_type: ChartType | None
    explicit_type: bool
    x_column: str | None
    y_columns: tuple[str, ...]
    series_column: str | None
    allowed_result_columns: tuple[str, ...]
    title: str | None
    clarification: str | None
    max_rows: int = 200
    max_x_values: int = 120
    max_series: int = 8
    version: str = CHART_CONTRACT_VERSION

    @property
    def safe_to_visualize(self) -> bool:
        return self.status == "valid"

    @classmethod
    def from_query_plan(
        cls,
        question: str,
        query_plan: QueryPlan,
        result_contract: ResultContract,
        *,
        semantic_intent: Mapping[str, Any] | None = None,
    ) -> "ChartContract":
        """Derive chart structure without trusting model tool arguments."""
        return cls._from_shape(
            question, query_plan.dimensions, query_plan.time_grain, query_plan.metric_ids,
            query_plan.required_result_columns, result_contract, semantic_intent=semantic_intent,
        )

    @classmethod
    def from_result_artifact(cls, artifact: ResultArtifact, result_contract: ResultContract, *, chart_request: str) -> "ChartContract":
        """A verified CSV needs chart-shape checks, not a new SQL admission."""
        return cls._from_shape(
            "", tuple(artifact.query_scope["dimensions"]), artifact.query_scope["time_grain"],
            artifact.metric_ids, artifact.result_columns, result_contract,
            semantic_intent={"chart_request": chart_request},
        )

    @classmethod
    def _from_shape(cls, question, dimensions, time_grain, metric_ids, result_columns,
                    result_contract, *, semantic_intent=None) -> "ChartContract":

        semantic_type = None
        if semantic_intent is not None:
            raw_type = semantic_intent.get("chart_request")
            names = {
                "line chart": "line", "折线图": "line", "曲线图": "line",
                "bar chart": "bar", "柱状图": "bar",
                "horizontal bar chart": "horizontal_bar", "横向条形图": "horizontal_bar",
                "条形图": "horizontal_bar", "area chart": "area", "面积图": "area",
                "donut chart": "donut", "环形图": "donut", "饼图": "donut",
                "图表": "auto", "可视化": "auto",
            }
            raw_type = raw_type.strip().casefold() if isinstance(raw_type, str) else None
            semantic_type = names.get(raw_type, raw_type)
        requested = (
            semantic_type is not None if semantic_intent is not None
            else bool(_VISUALIZATION_REQUEST.search(question))
        )
        allowed = tuple(dict.fromkeys(result_columns))
        if not requested:
            return cls(
                status="not_requested",
                requested=False,
                chart_type=None,
                explicit_type=False,
                x_column=None,
                y_columns=(),
                series_column=None,
                allowed_result_columns=allowed,
                title=None,
                clarification=None,
            )

        unsupported = next(
            (name for pattern, name in _UNSUPPORTED_REQUESTS if pattern.search(question)),
            None,
        ) if semantic_intent is None else (
            semantic_type if semantic_type not in {*_CHART_TYPES, "auto"} else None
        )
        if unsupported:
            return cls._blocked(
                "unsupported",
                allowed,
                f"当前支持柱状图、横向条形图、折线图、面积图和环形图；不能把你请求的{unsupported}静默替换为其他图表。",
            )

        explicit_types = tuple(
            chart_type
            for chart_type, pattern in (
                ("bar", _BAR_REQUEST),
                ("horizontal_bar", _HORIZONTAL_BAR_REQUEST),
                ("line", _LINE_REQUEST),
                ("area", _AREA_REQUEST),
                ("donut", _DONUT_REQUEST),
            )
            if pattern.search(question)
        ) if semantic_intent is None else (
            (semantic_type,) if semantic_type in _CHART_TYPES else ()
        )
        if len(explicit_types) > 1:
            return cls._blocked(
                "clarification_required",
                allowed,
                "请只指定一种图表类型：柱状图、横向条形图、折线图、面积图或环形图。",
            )
        requested_type = explicit_types[0] if explicit_types else None

        if len(dimensions) > 2 or (time_grain and len(dimensions) > 1):
            return cls._blocked(
                "clarification_required",
                allowed,
                "当前一次图表最多支持一个时间轴和一个业务分组维度；请缩小分组范围后重试。",
            )

        if requested_type == "line" and not time_grain:
            return cls._blocked(
                "clarification_required",
                allowed,
                "折线图需要明确的时间粒度；请补充按年、季度、月、周或日汇总。",
            )
        if requested_type == "area" and (
            not time_grain
            or dimensions
            or len(metric_ids) != 1
        ):
            return cls._blocked(
                "clarification_required",
                allowed,
                "面积图仅支持一个指标、无业务分组的时间序列；请补充时间粒度并缩小为单指标趋势。",
            )
        if requested_type == "horizontal_bar" and time_grain:
            return cls._blocked(
                "clarification_required",
                allowed,
                "横向条形图需要一个业务分组横轴，不能用于时间序列；请改用折线图、面积图或柱状图。",
            )
        if requested_type == "donut":
            if time_grain or len(dimensions) != 1 or len(metric_ids) != 1:
                return cls._blocked(
                    "clarification_required",
                    allowed,
                    "环形图仅支持一个可汇总业务维度和一个指标，不能混用时间轴、多指标或多分组。",
                )
            if metric_ids[0] not in _DONUT_COMPATIBLE_METRICS:
                return cls._blocked(
                    "unsupported",
                    allowed,
                    "当前环形图只支持可相加的金额或数量指标，不能把比率、均值或时长解释成构成占比。",
                )
        if time_grain:
            chart_type: ChartType = "line"
            x_column = "time"
            series = dimensions[0] if dimensions else None
        elif dimensions:
            chart_type = (
                "horizontal_bar"
                if dimensions[0] == "product_category_name"
                else "bar"
            )
            x_column = dimensions[0]
            series = dimensions[1] if len(dimensions) == 2 else None
        else:
            return cls._blocked(
                "clarification_required",
                allowed,
                "图表需要横轴。请补充时间粒度或一个可汇总的业务维度。",
            )

        if requested_type is not None:
            chart_type = requested_type  # type: ignore[assignment]

        required_fields = (x_column, *metric_ids, *( (series,) if series else () ))
        if not set(required_fields) <= set(allowed):
            return cls._blocked(
                "clarification_required",
                allowed,
                "当前查询计划没有冻结图表所需的结果字段；请先补充可汇总维度或时间范围。",
            )

        return cls(
            status="valid",
            requested=True,
            chart_type=chart_type,
            explicit_type=requested_type is not None,
            x_column=x_column,
            y_columns=tuple(metric_ids),
            series_column=series,
            allowed_result_columns=allowed,
            title=cls._title(chart_type, x_column, metric_ids, result_contract),
            clarification=None,
            max_x_values=8 if chart_type == "donut" else 120,
        )

    @classmethod
    def from_tool_metadata(cls, metadata: Mapping[str, Any]) -> "ChartContract | None":
        """Restore only the bounded contract form carried in ``ToolContext``."""

        raw = metadata.get("chart_contract")
        if not isinstance(raw, Mapping):
            return None
        try:
            status = str(raw["status"])
            if status not in {
                "not_requested",
                "valid",
                "clarification_required",
                "unsupported",
            }:
                return None
            chart_type = raw.get("chart_type")
            if chart_type is not None and chart_type not in _CHART_TYPES:
                return None
            return cls(
                status=status,  # type: ignore[arg-type]
                requested=bool(raw.get("requested")),
                chart_type=chart_type,  # type: ignore[arg-type]
                explicit_type=bool(raw.get("explicit_type")),
                x_column=cls._optional_string(raw.get("x_column")),
                y_columns=cls._strings(raw.get("y_columns")),
                series_column=cls._optional_string(raw.get("series_column")),
                allowed_result_columns=cls._strings(raw.get("allowed_result_columns")),
                title=cls._optional_string(raw.get("title")),
                clarification=cls._optional_string(raw.get("clarification")),
                max_rows=cls._positive_int(raw.get("max_rows"), 200),
                max_x_values=cls._positive_int(raw.get("max_x_values"), 120),
                max_series=cls._positive_int(raw.get("max_series"), 8),
                version=cls._optional_string(raw.get("version")) or CHART_CONTRACT_VERSION,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def as_tool_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested": self.requested,
            "chart_type": self.chart_type,
            "explicit_type": self.explicit_type,
            "x_column": self.x_column,
            "y_columns": list(self.y_columns),
            "series_column": self.series_column,
            "allowed_result_columns": list(self.allowed_result_columns),
            "title": self.title,
            "clarification": self.clarification,
            "max_rows": self.max_rows,
            "max_x_values": self.max_x_values,
            "max_series": self.max_series,
            "version": self.version,
        }

    def as_evidence(self) -> dict[str, Any]:
        return self.as_tool_metadata()

    def prompt_context(self) -> str:
        if self.status == "not_requested":
            return "\n### 服务器图表合同\n- 本轮用户未请求图表；不得调用 `visualize_data`。"
        if not self.safe_to_visualize:
            return (
                "\n### 服务器图表合同\n"
                f"- 本轮图表状态：`{self.status}`；在澄清前不得调用 `visualize_data`。\n"
                f"- 需要反馈：{self.clarification or '图表请求不在当前受控范围内。'}"
            )
        return "\n".join(
            [
                "\n### 服务器图表合同",
                "- 用户已明确请求图表。本轮 `run_sql` 结果通过验证后，服务器会直接渲染图表；不得调用或讨论 `visualize_data`。",
                f"- 图表类型固定为 `{self.chart_type}`；标题由服务器固定为“{self.title}”。",
                f"- 横轴固定为 `{self.x_column}`；指标列固定为 {', '.join(f'`{item}`' for item in self.y_columns)}。",
                f"- 系列列：`{self.series_column}`。" if self.series_column else "- 本轮无系列拆分。",
                "- 不得修改字段、图表类型、标题或在图表层重新聚合、求和、计数、去重或补值。",
            ]
        )

    def validate_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Return a canonical, bounded frame or reject before rendering.

        The returned rows preserve SQL result values.  It can only sort a time
        axis; it never groups, sums, counts, imputes, or deduplicates rows.
        """

        if not self.safe_to_visualize or not self.chart_type or not self.x_column:
            raise ChartContractError(self.clarification or "当前请求没有可执行的服务器图表合同。")
        if frame.empty:
            raise ChartContractError("查询结果为空，不能生成图表。")
        if len(frame) > self.max_rows:
            raise ChartContractError(f"结果超过图表合同上限 {self.max_rows} 行，请先缩小聚合范围。")
        columns = tuple(str(column) for column in frame.columns)
        if len(columns) != len(set(columns)):
            raise ChartContractError("结果包含重复列名，不能安全生成图表。")
        unknown = set(columns) - set(self.allowed_result_columns)
        if unknown:
            raise ChartContractError("结果包含未在服务器图表合同中声明的字段，不能生成图表。")

        required = (self.x_column, *self.y_columns, *( (self.series_column,) if self.series_column else () ))
        missing = [column for column in required if column not in columns]
        if missing:
            raise ChartContractError(f"结果缺少图表合同字段：{', '.join(missing)}。")
        chart_frame = frame.loc[:, list(required)].copy()
        if chart_frame.isnull().any().any():
            raise ChartContractError("图表字段包含空值；系统不会在展示层补值或删除数据。")
        for column in self.y_columns:
            try:
                chart_frame[column] = pd.to_numeric(chart_frame[column], errors="raise")
            except (TypeError, ValueError) as exc:
                raise ChartContractError(f"指标列 `{column}` 不是完整数值列，不能安全绘图。") from exc

        if self.chart_type == "donut" and (chart_frame.loc[:, self.y_columns] < 0).any().any():
            raise ChartContractError("环形图不能展示负值；请改用柱状图查看该指标。")

        if self.x_column == "time":
            try:
                parsed = pd.to_datetime(chart_frame[self.x_column], errors="raise")
            except (TypeError, ValueError) as exc:
                raise ChartContractError("时间轴无法严格解析，不能安全生成折线图。") from exc
            if parsed.isnull().any():
                raise ChartContractError("时间轴包含空值，不能安全生成折线图。")
            chart_frame = chart_frame.assign(__chart_sort_time=parsed).sort_values(
                "__chart_sort_time", kind="stable"
            ).drop(columns="__chart_sort_time")

        if chart_frame[self.x_column].nunique(dropna=False) > self.max_x_values:
            raise ChartContractError(f"横轴类别超过图表合同上限 {self.max_x_values} 个，请缩小范围。")
        if self.series_column:
            if chart_frame[self.series_column].nunique(dropna=False) > self.max_series:
                raise ChartContractError(f"系列数量超过图表合同上限 {self.max_series} 个，请缩小范围。")
            keys = [self.x_column, self.series_column]
        else:
            keys = [self.x_column]
        if chart_frame.duplicated(keys).any():
            raise ChartContractError("同一横轴/系列存在多行结果；系统不会在图表层隐式聚合。")
        return chart_frame

    @classmethod
    def _blocked(
        cls,
        status: Literal["clarification_required", "unsupported"],
        allowed: tuple[str, ...],
        clarification: str,
    ) -> "ChartContract":
        return cls(
            status=status,
            requested=True,
            chart_type=None,
            explicit_type=False,
            x_column=None,
            y_columns=(),
            series_column=None,
            allowed_result_columns=allowed,
            title=None,
            clarification=clarification,
        )

    @staticmethod
    def _title(
        chart_type: ChartType,
        x_column: str,
        metric_ids: tuple[str, ...],
        result_contract: ResultContract,
    ) -> str:
        labels = result_contract.result_column_labels
        metric_labels = "、".join(labels.get(metric_id, metric_id) for metric_id in metric_ids)
        axis = labels.get(x_column, "时间" if x_column == "time" else x_column)
        suffix = "构成" if chart_type == "donut" else "趋势" if chart_type in {"line", "area"} else "对比"
        return f"按{axis}{suffix}：{metric_labels}"

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("must be a sequence")
        values = tuple(str(item).strip() for item in value)
        if not values or any(not item for item in values):
            raise ValueError("must contain non-empty strings")
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        if value is None:
            return default
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("must be positive")
        return parsed
