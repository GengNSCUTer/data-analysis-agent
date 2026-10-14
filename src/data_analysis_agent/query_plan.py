"""Server-owned query intent plans for deterministic Text-to-SQL grounding.

The plan is deliberately smaller than a SQL AST.  It captures the business
shape that a model must implement while leaving SQL syntax generation to
Vanna.  The SQL Policy remains the final security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Any, Literal, Mapping

from .question_router import QuestionRoute
from .semantic_catalog import (
    AttributionRequirement,
    CatalogSelection,
    MetricDefinition,
)
from .sql_policy import DEFAULT_ANALYST_RESULT_LIMIT
from .query_time import copy_time_range, extract_absolute_range, time_range_instruction
from .result_budget import ResultBudget
from .workspace import WorkspaceProfile


PlanType = Literal[
    "single_metric",
    "scalar_multi_metric_overview",
    "grouped_multi_metric",
]


_TIME_GRAINS: tuple[tuple[str, str], ...] = (
    # A calendar mention such as "2018年" is a filter, not a series. Require
    # an explicit grouping word so the result contract does not demand a
    # `time` column the user never asked to see.
    (r"按月|每(?:个)?月|月度|各月|分月", "month"),
    (r"按周|每(?:个)?周|周度|各周|分周", "week"),
    (r"按日|每天|每日|日度|各日|分日", "day"),
    (r"按季度|每(?:个)?季度|各季度|分季度|季度(?:度)?(?:汇总|统计|时间序列|序列|趋势|变化)", "quarter"),
    (r"按年|每(?:个)?年|年度|各年|分年", "year"),
)
_DIMENSION_ALIASES: tuple[tuple[str, str], ...] = (
    ("各州", "customer_state"),
    ("按州", "customer_state"),
    ("每个州", "customer_state"),
    ("哪个州", "customer_state"),
    ("分州", "customer_state"),
    ("客户州", "customer_state"),
    ("地区", "customer_state"),
    ("省份", "customer_state"),
    ("城市", "customer_city"),
    ("客户城市", "customer_city"),
    ("卖家城市", "seller_city"),
    ("商家城市", "seller_city"),
    ("品类", "product_category_name"),
    ("类目", "product_category_name"),
    ("卖家州", "seller_state"),
    ("按卖家州", "seller_state"),
    ("商家州", "seller_state"),
    ("支付方式", "payment_type"),
)
_GROUP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"哪[一种个类]?商品|什么商品|哪种货|哪类商品|哪个品类|哪种品类|"
            r"最火的货|热销商品|卖得最[好火]|火爆商品"
        ),
        "product_category_name",
    ),
    (
        re.compile(r"哪个卖家|哪些卖家|按卖家(?!州)|各卖家|卖家排名|哪个商家|按商家"),
        "seller_city",
    ),
)
_TOP_N_DIMENSIONS: dict[str, int] = {
    "customer_city": 20,
    "seller_city": 20,
}
_RANKING_CATEGORY_LIMIT = 20
_NON_DISPLAYABLE_DIMENSIONS = frozenset({"seller_id"})
_FULL_DIMENSION_CARDINALITY = {"customer_city": 4_119, "seller_city": 612}
_EXPLICIT_TOP_N = re.compile(r"(?:前|top\s*)(\d+|[零〇一二两三四五六七八九十百千万]+)", re.IGNORECASE)


def requested_top_n(question: str) -> int | None:
    match = _EXPLICIT_TOP_N.search(question)
    if match is None:
        return None
    text = match.group(1)
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1_000}
    total, section, digit = 0, 0, 0
    for character in text:
        if character == "万":
            total += (section + digit or 1) * 10_000
            section, digit = 0, 0
        elif character in units:
            section += (digit or 1) * units[character]
            digit = 0
        else:
            digit = digits[character]
    return total + section + digit

# These are server-owned display-cardinality bounds, not database statistics
# inferred at request time.  A new workspace must explicitly declare/replace
# them before enabling the corresponding grouped output.
_DISPLAY_DIMENSION_CARDINALITY: dict[str, int] = {
    "customer_state": 27,
    "seller_state": 27,
    "product_category_name": 73,
    "payment_type": 8,
}
_TIME_GRAIN_CARDINALITY: dict[str, int] = {
    "year": 3,
    "quarter": 12,
    "month": 36,
    "week": 160,
    "day": 1_100,
}
_BR_STATE_CODE = (
    "AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO"
)
_CUSTOMER_STATE_FILTER = re.compile(
    rf"(?:只看|仅看|限定|筛选)?\s*(?P<code>{_BR_STATE_CODE})\s*(?:州)?\s*(?:的)?\s*客户"
    rf"|客户(?:州|地区)?(?:是|为|在|属于|=)?\s*(?P<code2>{_BR_STATE_CODE})"
    rf"|圣保罗",
    re.IGNORECASE,
)


def customer_state_filter(question: str) -> str | None:
    """Return a Brazilian state code used as a customer filter, not a grouping."""

    match = _CUSTOMER_STATE_FILTER.search(question or "")
    if match is None:
        return None
    if match.group(0) == "圣保罗" or "圣保罗" in match.group(0):
        return "SP"
    code = match.group("code") or match.group("code2")
    return code.upper() if code else None


def ranking_category_output_bound(
    question: str, dimensions: tuple[str, ...]
) -> int | None:
    """Bound product_category_name when the question is a ranking, not a census.

    A spoken ranking such as “卖得最好的货” must not be estimated as all 73
    categories. Combined with another grouping it means the best category in
    each remaining group (1), not 27×73.
    """

    if "product_category_name" not in dimensions:
        return None
    if not _GROUP_PATTERNS[0][0].search(question or ""):
        return None
    if any(dimension != "product_category_name" for dimension in dimensions):
        return 1
    return _RANKING_CATEGORY_LIMIT


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
    top_n: tuple[tuple[str, int], ...] = ()
    result_budget: ResultBudget | None = None

    @classmethod
    def preflight(
        cls,
        metrics: tuple[MetricDefinition, ...],
        question: str,
        conversation_state: Mapping[str, Any] | None = None,
        *,
        max_result_rows: int = DEFAULT_ANALYST_RESULT_LIMIT,
        workspace: WorkspaceProfile | None = None,
        full_results: bool = False,
    ) -> QueryPlanPreflight:
        """Reject query shapes the configured execution contract cannot bound.

        This is intentionally prior to LLM/SQL generation.  The bound is the
        database execution cap, not the smaller UI preview budget. It validates
        only deterministic output capability, not business metric attribution;
        attribution remains owned by the Catalog policy checked by the Router.
        """
        if max_result_rows <= 0:
            raise ValueError("max_result_rows must be positive")
        dimensions = cls._dimensions(question, metrics, conversation_state, workspace=workspace)
        non_displayable = next(
            (dimension for dimension in dimensions if dimension in _NON_DISPLAYABLE_DIMENSIONS),
            None,
        )
        if non_displayable:
            return QueryPlanPreflight(
                False,
                "dimension_not_displayable",
                "卖家原始编号不能出现在结果中。请改为按卖家城市或卖家州查看排名最高的分组。",
            )
        time_grain = cls._time_grain(question)
        state = conversation_state or {}
        resolved_range = extract_absolute_range(question) or cls._time_range(state)
        has_explicit_range = bool(resolved_range)
        if time_grain == "day" and not has_explicit_range:
            return QueryPlanPreflight(
                False,
                "daily_series_requires_explicit_range",
                "按日统计必须先给出明确的绝对日期范围，避免结果超出当前行数预算。",
            )
        estimated_rows = 1
        configured_bindings = workspace.dimension_bindings if workspace else {}
        ranking_bound = ranking_category_output_bound(question, dimensions)
        for dimension in dimensions:
            if ranking_bound is not None and dimension == "product_category_name":
                continue
            if dimension in configured_bindings:
                estimated_rows *= configured_bindings[dimension].max_cardinality
            elif full_results and dimension in _FULL_DIMENSION_CARDINALITY:
                estimated_rows *= _FULL_DIMENSION_CARDINALITY[dimension]
            elif dimension in _TOP_N_DIMENSIONS:
                estimated_rows *= _TOP_N_DIMENSIONS[dimension]
            else:
                estimated_rows *= _DISPLAY_DIMENSION_CARDINALITY.get(
                    dimension, max_result_rows + 1
                )
        if ranking_bound is not None:
            estimated_rows *= ranking_bound
        top_n_request = requested_top_n(question)
        if full_results and top_n_request is not None and len(dimensions) == 1 and not time_grain:
            if top_n_request <= 0:
                return QueryPlanPreflight(False, "invalid_top_n", "Top-N 必须是正整数。")
            estimated_rows = min(estimated_rows, top_n_request)
        if time_grain:
            series_state = state
            if not resolved_range and workspace and workspace.fact_date_counts:
                sources = [workspace.fact_date_counts.get(metric.time_field) for metric in metrics]
                if sources and all(sources):
                    dates = [day for source in sources for day in source]
                    series_state = {"time_range": {
                        "start": min(dates),
                        "end": (date.fromisoformat(max(dates)) + timedelta(days=1)).isoformat(),
                        "end_semantics": "exclusive",
                    }}
            estimated_rows *= cls._series_bucket_bound(time_grain, question, series_state)
        if dimensions and workspace and workspace.fact_date_counts and resolved_range:
            # Each result group requires at least one fact row. Across metrics,
            # summing unfiltered fact-row bounds also bounds FULL OUTER JOIN
            # group keys. Never use a count copied from a training label.
            if resolved_range.get("end_semantics") == "exclusive":
                bounds = []
                for metric in metrics:
                    days = workspace.fact_date_counts.get(metric.time_field)
                    if days is None:
                        break
                    bounds.append(sum(count for day, count in days.items()
                                      if resolved_range["start"] <= day < resolved_range["end"]))
                if len(bounds) == len(metrics) and bounds:
                    estimated_rows = min(estimated_rows, sum(bounds))
        if estimated_rows > max_result_rows:
            labels = "、".join(dimensions) if dimensions else "当前分组"
            return QueryPlanPreflight(
                False,
                "result_row_budget_exceeded",
                f"当前分组（{labels}）可能超过可验证的结果行数预算。"
                "请去掉其中一个分组、缩小时间范围或降低时间粒度。",
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
        *,
        result_budget: ResultBudget | None = None,
    ) -> "QueryPlan":
        state = conversation_state or {}
        selection_metrics = tuple(selection.metrics)
        route_ids = tuple(route.metric_ids or ())
        selection_ids = tuple(metric.metric_id for metric in selection_metrics)
        metric_ids = tuple(sorted(route_ids or selection_ids))
        by_id = {metric.metric_id: metric for metric in selection_metrics}
        metrics = tuple(by_id[metric_id] for metric_id in metric_ids if metric_id in by_id)
        if not metrics:
            metrics = selection_metrics
        dimensions = cls._dimensions(question, metrics, state, workspace=selection.workspace)
        time_grain = cls._time_grain(question)
        time_range = extract_absolute_range(question) or cls._time_range(state)
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
        required_columns = list(dimensions)
        if time_grain:
            required_columns.append("time")
        required_columns.extend(metric_ids)

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
        state_code = customer_state_filter(question)
        if state_code:
            warnings.append(f"filter_customer_state:{state_code}")
        ranking_bound = ranking_category_output_bound(question, dimensions)
        if ranking_bound == 1:
            warnings.append("ranking_category_best_per_group")
        top_n = tuple(
            (dimension, limit)
            for dimension, limit in _TOP_N_DIMENSIONS.items()
            if dimension in dimensions and result_budget is None
            and not (selection.workspace and selection.workspace.dimension_bindings)
        )
        top_n_request = requested_top_n(question)
        if result_budget is not None and top_n_request and len(dimensions) == 1 and not time_grain:
            top_n = ((dimensions[0], top_n_request),)
        if ranking_bound == _RANKING_CATEGORY_LIMIT and (result_budget is None or top_n_request is None):
            top_n = (*top_n, ("product_category_name", _RANKING_CATEGORY_LIMIT))
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

        if result_budget is not None:
            bound = cls.preflight(
                metrics, question, state, max_result_rows=result_budget.execution_max_rows,
                workspace=selection.workspace, full_results=True,
            )
            if not bound.allowed:
                raise ValueError(bound.clarification)
            result_budget = result_budget.for_result_rows(bound.estimated_max_rows)

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
            top_n=top_n,
            result_budget=result_budget,
        )

    @staticmethod
    def _time_grain(question: str) -> str | None:
        for pattern, grain in _TIME_GRAINS:
            if re.search(pattern, question):
                return grain
        return None

    @staticmethod
    def _dimensions(
        question: str,
        metrics: tuple[MetricDefinition, ...],
        state: Mapping[str, Any] | None = None,
        *,
        workspace: WorkspaceProfile | None = None,
    ) -> tuple[str, ...]:
        """Detect grouping dimensions from query shape, not incidental words.

        ``SP州`` names a filter. It does not ask for one row per state.
        ``哪种商品`` asks for a category ranking even when the word ``品类``
        is absent. Extra dimensions may come from a Catalog-constrained
        grounding step, and only allowed dimensions are kept.
        """

        allowed = {
            dimension
            for metric in metrics
            for dimension in metric.allowed_dimensions
        }
        selected: list[str] = []
        bindings = workspace.dimension_bindings if workspace else {}
        aliases = (tuple((alias, dimension) for dimension, binding in bindings.items()
                         for alias in (*binding.aliases, dimension))
                   if bindings else _DIMENSION_ALIASES)
        # A specific "卖家州" must shadow the embedded generic "按州" or
        # "城市". Keep separately mentioned dimensions; do not invent both.
        # Shared generic aliases (e.g. 按州) prefer the current fact's allowed
        # dimension. A more specific unsupported alias is retained so Router
        # can reject it rather than silently changing the requested shape.
        matches = sorted((match.start(), -len(alias), dimension not in allowed,
                          dimension, match.end())
                         for alias, dimension in aliases
                         for match in re.finditer(re.escape(alias), question))
        occupied: list[tuple[int, int]] = []
        for start, _, _, dimension, end in matches:
            if any(start < old_end and end > old_start for old_start, old_end in occupied):
                continue
            occupied.append((start, end))
            if dimension not in selected:
                selected.append(dimension)
        if not bindings:
            for pattern, dimension in _GROUP_PATTERNS:
                if pattern.search(question) and dimension not in selected:
                    selected.append(dimension)
        raw_resolved = (state or {}).get("resolved_dimensions")
        if isinstance(raw_resolved, (list, tuple)):
            for dimension in raw_resolved:
                if (
                    isinstance(dimension, str)
                    and dimension in allowed
                    and dimension not in selected
                ):
                    selected.append(dimension)
        return tuple(selected)

    @staticmethod
    def _time_range(state: Mapping[str, Any]) -> dict[str, str] | None:
        return copy_time_range(state.get("time_range"))

    @staticmethod
    def _series_bucket_bound(
        time_grain: str, question: str, state: Mapping[str, Any]
    ) -> int:
        """Return a conservative temporal bucket count for output preflight.

        An absolute range affects only the output-shape estimate. It does not
        change the eventual SQL filter or prove a candidate SQL is correct.
        Unknown or malformed ranges retain the Catalog-owned global bound.
        """
        time_range = extract_absolute_range(question) or QueryPlan._time_range(state)
        if time_range is None:
            return _TIME_GRAIN_CARDINALITY[time_grain]
        try:
            start = date.fromisoformat(time_range["start"])
            end = date.fromisoformat(time_range["end"])
            if time_range.get("end_semantics") == "exclusive":
                end -= timedelta(days=1)
        except (KeyError, TypeError, ValueError):
            return _TIME_GRAIN_CARDINALITY[time_grain]
        if end < start:
            return _TIME_GRAIN_CARDINALITY[time_grain]

        days = (end - start).days + 1
        if time_grain == "day":
            return days
        if time_grain == "week":
            first_monday = start - timedelta(days=start.weekday())
            last_monday = end - timedelta(days=end.weekday())
            return (last_monday - first_monday).days // 7 + 1
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

    def as_dict(self, *, include_result_budget: bool = True) -> dict[str, Any]:
        result = {
            "query_plan_version": "query-plan-v2",
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
            "top_n": {dimension: limit for dimension, limit in self.top_n},
        }
        if include_result_budget and self.result_budget is not None:
            result.update(self.result_budget.as_metadata())
        return result

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
                f"- 已确认时间范围：{time_range_instruction(self.time_range)}。"
            )
        if self.comparison_baseline:
            lines.append(f"- 已确认比较基线：{self.comparison_baseline}。")
        for warning in self.warnings:
            if warning.startswith("filter_customer_state:"):
                code = warning.split(":", 1)[1]
                lines.append(
                    f"- 筛选：`dim_customers.customer_state = '{code}'`。"
                    "这是过滤条件，不要按客户州分组，最终结果列不要包含 customer_state。"
                )
        if any(item.policy_mode == "multi_membership" for item in self.attribution_requirements):
            lines.append(
                "- 多归属：同一订单或评价进入它实际碰到的每一个品类或支付方式。"
                "各组合计可以大于总体，不要为了让合计相等而只保留第一条。"
            )
        if any(item.policy_mode == "payment_share" for item in self.attribution_requirements):
            lines.append(
                "- 支付占比：在每个订单内用 payment_value / SUM(payment_value) 乘以商品金额或运费，"
                "再按 payment_type 汇总。禁止只用 payment_sequential = 1 代表整单。"
            )
        for dimension, limit in self.top_n:
            if dimension in self.dimensions:
                lines.append(
                    f"- `{dimension}` 只返回该指标最高的 {limit} 个分组，SQL 必须带这个 LIMIT。"
                    "不要返回全部城市或卖家。"
                )
        if "ranking_category_best_per_group" in self.warnings:
            lines.append(
                "- `product_category_name` 是排名维：每个其余分组只返回该指标最高的 1 个品类，"
                "不要展开全部品类。"
            )
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
