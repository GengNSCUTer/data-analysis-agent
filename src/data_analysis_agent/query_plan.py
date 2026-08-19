"""Server-owned query intent plans for deterministic Text-to-SQL grounding.

The plan is deliberately smaller than a SQL AST.  It captures the business
shape that a model must implement while leaving SQL syntax generation to
Vanna.  The SQL Policy remains the final security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping

from .question_router import QuestionRoute
from .semantic_catalog import CatalogSelection, MetricDefinition


PlanType = Literal[
    "single_metric",
    "scalar_multi_metric_overview",
    "grouped_multi_metric",
]


_TIME_GRAINS: tuple[tuple[str, str], ...] = (
    (r"按?月|每月|月度", "month"),
    (r"按?周|每周|周度", "week"),
    (r"按?日|每天|每日|日度", "day"),
    (r"按?季度|季度", "quarter"),
    (r"按?年|年度|每年", "year"),
    (r"趋势|时间序列", "day"),
)
_DIMENSION_ALIASES: tuple[tuple[str, str], ...] = (
    ("州", "customer_state"),
    ("地区", "customer_state"),
    ("省份", "customer_state"),
    ("城市", "customer_city"),
    ("品类", "product_category_name"),
    ("类目", "product_category_name"),
    ("卖家", "seller_id"),
    ("商家", "seller_id"),
    ("支付方式", "payment_type"),
)


@dataclass(frozen=True)
class MetricPlan:
    """The metric-specific facts a SQL candidate must preserve."""

    metric_id: str
    grain: str
    time_field: str
    source_tables: tuple[str, ...]
    default_filters: tuple[str, ...]
    allowed_dimensions: tuple[str, ...]

    @classmethod
    def from_metric(cls, metric: MetricDefinition) -> "MetricPlan":
        return cls(
            metric_id=metric.metric_id,
            grain=metric.grain,
            time_field=metric.time_field,
            source_tables=metric.source_tables,
            default_filters=metric.default_filters,
            allowed_dimensions=metric.allowed_dimensions,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "grain": self.grain,
            "time_field": self.time_field,
            "source_tables": list(self.source_tables),
            "default_filters": list(self.default_filters),
            "allowed_dimensions": list(self.allowed_dimensions),
        }


@dataclass(frozen=True)
class QueryPlan:
    """A bounded, auditable description of the requested query shape."""

    plan_type: PlanType
    intent: str
    metric_ids: tuple[str, ...]
    dimensions: tuple[str, ...]
    time_grain: str | None
    time_range: dict[str, str] | None
    comparison_baseline: str | None
    required_result_columns: tuple[str, ...]
    metric_plans: tuple[MetricPlan, ...]
    execution_strategy: str
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_selection(
        cls,
        selection: CatalogSelection,
        question: str,
        route: QuestionRoute,
        conversation_state: Mapping[str, Any] | None = None,
    ) -> "QueryPlan":
        state = conversation_state or {}
        metrics = tuple(selection.metrics)
        metric_ids = tuple(metric.metric_id for metric in metrics)
        dimensions = cls._dimensions(question, metrics)
        time_grain = cls._time_grain(question)
        time_range = cls._time_range(state)
        comparison = cls._comparison(state)

        if len(metric_ids) > 1 and not dimensions and time_grain is None:
            plan_type: PlanType = "scalar_multi_metric_overview"
            execution_strategy = "one_cte_per_metric_then_cross_join"
        elif len(metric_ids) > 1:
            plan_type = "grouped_multi_metric"
            execution_strategy = "one_cte_per_metric_then_join_on_group_keys"
        else:
            plan_type = "single_metric"
            execution_strategy = "single_metric_aggregation"

        # Dimensions are part of the result contract as well as prompt
        # context.  Without them a model could return only metric columns and
        # still look valid to the result gate for a grouped query.
        required_columns = list(dimensions) + list(metric_ids)
        if time_grain:
            required_columns.append("time")

        time_fields = tuple(
            dict.fromkeys(metric.time_field for metric in metrics)
        )
        warnings: list[str] = []
        if time_grain and len(time_fields) > 1:
            warnings.append("selected_metrics_use_different_time_fields")
        unsupported_dimensions = sorted(
            {
                dimension
                for dimension in dimensions
                if metrics and not all(dimension in metric.allowed_dimensions for metric in metrics)
            }
        )
        if unsupported_dimensions:
            warnings.append("dimension_not_supported_by_all_metrics")

        return cls(
            plan_type=plan_type,
            intent=route.intent or "data_query",
            metric_ids=metric_ids,
            dimensions=dimensions,
            time_grain=time_grain,
            time_range=time_range,
            comparison_baseline=comparison,
            required_result_columns=tuple(dict.fromkeys(required_columns)),
            metric_plans=tuple(MetricPlan.from_metric(metric) for metric in metrics),
            execution_strategy=execution_strategy,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _time_grain(question: str) -> str | None:
        for pattern, grain in _TIME_GRAINS:
            if re.search(pattern, question):
                return grain
        return None

    @staticmethod
    def _dimensions(
        question: str, metrics: tuple[MetricDefinition, ...]
    ) -> tuple[str, ...]:
        if not metrics:
            return ()
        selected: list[str] = []
        for alias, dimension in _DIMENSION_ALIASES:
            if alias in question and dimension not in selected:
                selected.append(dimension)
        return tuple(selected)

    @staticmethod
    def _time_range(state: Mapping[str, Any]) -> dict[str, str] | None:
        value = state.get("time_range")
        if not isinstance(value, Mapping):
            return None
        start, end = value.get("start"), value.get("end")
        if isinstance(start, str) and isinstance(end, str) and start and end:
            return {"start": start[:32], "end": end[:32]}
        return None

    @staticmethod
    def _comparison(state: Mapping[str, Any]) -> str | None:
        value = state.get("comparison_baseline", state.get("comparison"))
        return value.strip()[:120] if isinstance(value, str) and value.strip() else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_type": self.plan_type,
            "intent": self.intent,
            "metric_ids": list(self.metric_ids),
            "dimensions": list(self.dimensions),
            "time_grain": self.time_grain,
            "time_range": dict(self.time_range) if self.time_range else None,
            "comparison_baseline": self.comparison_baseline,
            "required_result_columns": list(self.required_result_columns),
            "metric_plans": [metric.as_dict() for metric in self.metric_plans],
            "execution_strategy": self.execution_strategy,
            "warnings": list(self.warnings),
        }

    def prompt_context(self) -> str:
        """Render a bounded instruction block for the SQL-generating Agent."""
        lines = [
            "\n### 服务器生成的查询计划",
            f"- plan_type：`{self.plan_type}`；intent：`{self.intent}`。",
            (
                "- 顶层最终 SELECT 的结果列白名单："
                f"{', '.join(f'`{column}`' for column in self.required_result_columns) or '无'}。"
            ),
            (
                "- 顶层最终 SELECT 必须且只能返回上述白名单列；不得为解释、关联或调试"
                "额外投影任何明细列、标识列或未声明字段。"
            ),
            (
                "- 敏感标识列仅可在内部 CTE 或受控聚合子查询中用于 JOIN ON、WHERE、"
                "COUNT/COUNT DISTINCT 或其他受控聚合；不得原样出现在顶层最终 SELECT、"
                "最终结果别名、GROUP BY 或 ORDER BY 中。内部 CTE 如需按关联键保持事实粒度，"
                "可以保留该键，但外层最终结果必须丢弃它。"
            ),
            f"- 维度：{', '.join(self.dimensions) if self.dimensions else '无'}；时间粒度：{self.time_grain or '标量汇总'}。",
            f"- 执行策略：`{self.execution_strategy}`。",
        ]
        if self.time_range:
            lines.append(
                f"- 已确认时间范围：{self.time_range['start']} 至 {self.time_range['end']}。"
            )
        if self.comparison_baseline:
            lines.append(f"- 已确认比较基线：{self.comparison_baseline}。")
        if self.warnings:
            lines.append(f"- 需要注意：{'；'.join(self.warnings)}。")
        if self.plan_type == "scalar_multi_metric_overview":
            lines.extend(
                [
                    "- 多指标标量概览必须只生成一条 SQL；每个指标先在自己的事实粒度独立聚合，再用 CROSS JOIN 合并单行结果。",
                    "- 禁止先把订单、商品、支付、评价明细直接 Join 后再聚合；禁止用一个指标的时间字段替代另一个指标的时间字段。",
                ]
            )
        elif self.plan_type == "grouped_multi_metric":
            lines.append(
                "- 多指标分组查询必须先在各自事实粒度聚合，再使用受 Catalog 允许的统一维度/时间键 Join。"
            )
        lines.append("- 指标 SQL 别名必须严格使用 Catalog metric_id；不要添加 Catalog 未声明的结果列。")
        return "\n".join(lines)
