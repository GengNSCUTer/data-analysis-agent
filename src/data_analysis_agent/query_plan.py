"""Server-owned query intent plans for deterministic Text-to-SQL grounding.

The plan is deliberately smaller than a SQL AST.  It captures the business
shape that a model must implement while leaving SQL syntax generation to
Vanna.  The SQL Policy remains the final security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any, Literal, Mapping

from .question_router import QuestionRoute
from .semantic_catalog import (
    AttributionRequirement,
    CatalogSelection,
    MetricDefinition,
)
from .sql_policy import DEFAULT_ANALYST_RESULT_LIMIT


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

# These are server-owned display-cardinality bounds, not database statistics
# inferred at request time.  A new workspace must explicitly declare/replace
# them before enabling the corresponding grouped output.
_DISPLAY_DIMENSION_CARDINALITY: dict[str, int] = {
    "customer_state": 27,
    "product_category_name": 73,
}
_TIME_GRAIN_CARDINALITY: dict[str, int] = {
    "year": 3,
    "quarter": 12,
    "month": 36,
    "week": 160,
    "day": 1_100,
}
_NON_DISPLAYABLE_DIMENSIONS = frozenset({"seller_id", "customer_city", "payment_type"})
_EXPLICIT_DATE_RANGE = re.compile(
    r"(?P<start>20\d{2}-\d{2}-\d{2})\s*(?:至|到|到|-|~|～)\s*"
    r"(?P<end>20\d{2}-\d{2}-\d{2})"
)
_EXPLICIT_YEAR = re.compile(r"(?<!\d)20\d{2}(?!\d)")


@dataclass(frozen=True)
class QueryPlanPreflight:
    """Deterministic capability check before candidate SQL generation."""

    allowed: bool
    reason_code: str | None = None
    clarification: str | None = None
    estimated_max_rows: int | None = None


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
    attribution_requirements: tuple[AttributionRequirement, ...]
    execution_strategy: str
    warnings: tuple[str, ...] = ()

    @classmethod
    def preflight(
        cls,
        metrics: tuple[MetricDefinition, ...],
        question: str,
        conversation_state: Mapping[str, Any] | None = None,
        *,
        max_result_rows: int = DEFAULT_ANALYST_RESULT_LIMIT,
    ) -> QueryPlanPreflight:
        """Reject query shapes the current result contract cannot display.

        This is intentionally prior to LLM/SQL generation.  It validates only
        deterministic output capability, not business metric attribution;
        attribution remains owned by the Catalog policy checked by the Router.
        """
        if max_result_rows <= 0:
            raise ValueError("max_result_rows must be positive")
        dimensions = cls._dimensions(question, metrics)
        non_displayable = next(
            (dimension for dimension in dimensions if dimension in _NON_DISPLAYABLE_DIMENSIONS),
            None,
        )
        if non_displayable:
            display_name = {
                "seller_id": "卖家",
                "customer_city": "客户城市",
                "payment_type": "支付方式",
            }[non_displayable]
            return QueryPlanPreflight(
                False,
                "dimension_not_displayable",
                f"当前结果合同尚未支持按{display_name}稳定展示。请改按客户州、商品品类，或缩小为已支持的汇总范围。",
            )
        time_grain = cls._time_grain(question)
        state = conversation_state or {}
        has_explicit_range = bool(
            cls._time_range(state)
            or _EXPLICIT_DATE_RANGE.search(question)
            or _EXPLICIT_YEAR.search(question)
        )
        if time_grain == "day" and not has_explicit_range:
            return QueryPlanPreflight(
                False,
                "daily_series_requires_explicit_range",
                "按日统计必须先给出明确的绝对日期范围，避免结果超出当前行数预算。",
            )
        estimated_rows = 1
        for dimension in dimensions:
            estimated_rows *= _DISPLAY_DIMENSION_CARDINALITY.get(dimension, max_result_rows + 1)
        if time_grain:
            estimated_rows *= cls._series_bucket_bound(time_grain, question, state)
        if estimated_rows > max_result_rows:
            return QueryPlanPreflight(
                False,
                "result_row_budget_exceeded",
                "当前分组形状可能超过可验证的结果行数预算。请缩小时间范围、降低时间粒度或减少分组维度。",
                estimated_rows,
            )
        return QueryPlanPreflight(True, estimated_max_rows=estimated_rows)

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
        attribution_requirements = tuple(
            AttributionRequirement(
                metric_id=metric.metric_id,
                metric_grain=metric.grain,
                dimension=dimension,
                policy_mode=policy.mode,
                attribution_rule_id=policy.attribution_rule_id,
            )
            for metric in metrics
            for dimension in dimensions
            if (policy := metric.dimension_policies.get(dimension)) is not None
        )

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
            attribution_requirements=attribution_requirements,
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
    def _series_bucket_bound(
        time_grain: str, question: str, state: Mapping[str, Any]
    ) -> int:
        """Return a conservative temporal bucket count for output preflight.

        An absolute range affects only the output-shape estimate. It does not
        change the eventual SQL filter or prove a candidate SQL is correct.
        Unknown or malformed ranges retain the Catalog-owned global bound.
        """
        time_range = QueryPlan._time_range(state)
        if time_range is None:
            match = _EXPLICIT_DATE_RANGE.search(question)
            if match:
                time_range = match.groupdict()
            else:
                year = _EXPLICIT_YEAR.search(question)
                if year:
                    time_range = {
                        "start": f"{year.group(0)}-01-01",
                        "end": f"{year.group(0)}-12-31",
                    }
        if time_range is None:
            return _TIME_GRAIN_CARDINALITY[time_grain]
        try:
            start = date.fromisoformat(time_range["start"])
            end = date.fromisoformat(time_range["end"])
        except (KeyError, TypeError, ValueError):
            return _TIME_GRAIN_CARDINALITY[time_grain]
        if end < start:
            return _TIME_GRAIN_CARDINALITY[time_grain]

        days = (end - start).days + 1
        if time_grain == "day":
            return days
        if time_grain == "week":
            return math.ceil(days / 7)
        if time_grain == "month":
            return (end.year - start.year) * 12 + end.month - start.month + 1
        if time_grain == "quarter":
            start_quarter = (start.month - 1) // 3
            end_quarter = (end.month - 1) // 3
            return (end.year - start.year) * 4 + end_quarter - start_quarter + 1
        if time_grain == "year":
            return end.year - start.year + 1
        return _TIME_GRAIN_CARDINALITY[time_grain]

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
            "attribution_requirements": [
                requirement.as_dict()
                for requirement in self.attribution_requirements
            ],
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
        for requirement in self.attribution_requirements:
            if requirement.policy_mode == "safe_direct":
                lines.append(
                    f"- `{requirement.metric_id}` 按 `{requirement.dimension}` 使用安全直连维度；"
                    f"指标事实粒度保持为 `{requirement.metric_grain}`。"
                )
            elif requirement.policy_mode == "server_owned_rule":
                lines.append(
                    f"- `{requirement.metric_id}` 按 `{requirement.dimension}` 的归因规则为"
                    f"服务器拥有的 `{requirement.attribution_rule_id}`；不得由模型自行推断、"
                    "替换或实现该规则。"
                )
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
