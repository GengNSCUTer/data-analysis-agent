"""Deterministic intent and answerability routing before Text-to-SQL.

The router deliberately separates two decisions that used to be conflated:

* ``state`` describes the request lifecycle (answerable, clarification,
  refusal, or a deterministic response); and
* ``intent``/``evidence_mode`` describe which source of evidence is allowed.

An occurrence of a metric name is therefore not enough to enter the SQL
agent.  A user can mention GMV while asking for a definition or general
business advice, neither of which should receive a database tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from vanna.core.user import User

from .semantic_catalog import CatalogSelection, CatalogRetriever


QuestionState = Literal[
    "answerable",
    "catalog_answered",
    "help",
    "general_business",
    "general_knowledge",
    "result_followup",
    "clarification_required",
    "missing_time",
    "missing_metric",
    "missing_comparison",
    "unauthorized",
    "unsupported",
]

IntentKind = Literal[
    "data_query",
    "data_analysis",
    "data_analysis",
    "catalog_definition",
    "general_business",
    "general_knowledge",
    "help",
    "mixed_request",
    "result_followup",
    "clarification_required",
    "unsupported",
    "unsafe_or_unauthorized",
]

EvidenceMode = Literal[
    "database_result",
    "catalog",
    "general_knowledge",
    "mixed",
    "previous_result",
    "clarification",
    "none",
]

_RELATIVE_TIME = re.compile(r"本月|本周|本季度|今年|最近|近[一二三四五六七八九十0-9]+天|上月|上个月|去年|过去")
_COMPARISON = re.compile(r"相比|对比|比较|变化|增长|下降|环比|同比|趋势|最好|最高|最低|异常")
_UNAUTHORIZED = re.compile(
    r"写入|删除|修改|更新|插入|建表|导出原始|密码|密钥|token|api.?key|information_schema|pg_catalog|app\.|系统表|任意文件",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(r"预测|机器学习训练|自动下单|发起退款|执行脚本|运行 python|运行代码", re.IGNORECASE)
_DEFINITION_REQUEST = re.compile(r"是什么|指什么|含义|定义|统计口径|口径是什么|口径|怎么算|如何计算")
_DATA_INTENT = re.compile(
    r"概览|多少|数值|统计|查询|分析|趋势|排名|前[一二三四五六七八九十0-9]+|总|平均|比例|数量|金额|按|对比|比较|变化|增长|下降|最好|最高|最低"
)
_HELP_REQUEST = re.compile(
    r"你能做什么|能做什么|怎么用|如何使用|帮助|使用说明|功能介绍|支持哪些|有哪些功能|^/?help$",
    re.IGNORECASE,
)
_GENERAL_BUSINESS_REQUEST = re.compile(
    r"通常|一般|为什么会|原因|如何提升|怎么提升|最佳实践|建议|策略|经验|怎么理解|业务上|行业|背离|改善",
)
_EXPLICIT_DATA_REQUEST = re.compile(
    r"概览|多少|数值|统计|查询|查一下|排名|前[一二三四五六七八九十0-9]+|总|平均|比例|数量|金额|按.{0,10}(州|地区|品类|城市|卖家|月|周|日|年)|同比|环比",
)
_CAUSE_REQUEST = re.compile(r"归因|原因|影响因素|驱动因素|为什么.*(?:下降|增长|变化|异常)")
_EXPLICIT_DATA_CONTEXT = re.compile(
    r"当前数据|这份数据|本数据|数据库中|查一下|查当前|实际数据|数据里|数据中|基于当前|结合当前|从结果看|本次结果|本轮结果",
)
_RESULT_FOLLOWUP = re.compile(
    r"这个结果|该结果|刚才|上面的?(数字|结果|数据)|上一轮|前面查询|为什么这么高|为什么这么低|是否合理|是否正常|结果如何",
)
_DRILLDOWN_REQUEST = re.compile(r"按|拆分|展开|下钻|明细|分解|排名|趋势|对比|比较")


@dataclass(frozen=True)
class QuestionRoute:
    state: QuestionState
    missing: tuple[str, ...]
    metric_ids: tuple[str, ...]
    clarification: str | None
    reason: str
    direct_answer: str | None = None
    intent: IntentKind | None = None
    requires_database: bool | None = None
    evidence_mode: EvidenceMode | None = None
    confidence: float = 1.0
    reason_code: str | None = None

    def __post_init__(self) -> None:
        """Fill new structured fields without breaking old route callers."""
        if self.intent is None:
            intent = {
                "answerable": "data_query",
                "catalog_answered": "catalog_definition",
                "help": "help",
                "general_business": "general_business",
                "general_knowledge": "general_knowledge",
                "result_followup": "result_followup",
                "clarification_required": "clarification_required",
                "missing_time": "clarification_required",
                "missing_metric": "clarification_required",
                "missing_comparison": "clarification_required",
                "unauthorized": "unsafe_or_unauthorized",
                "unsupported": "unsupported",
            }.get(self.state, "clarification_required")
            object.__setattr__(self, "intent", intent)
        if self.requires_database is None:
            object.__setattr__(self, "requires_database", self.state == "answerable")
        if self.evidence_mode is None:
            mode = {
                "data_query": "database_result",
                "data_analysis": "database_result",
                "catalog_definition": "catalog",
                "general_business": "general_knowledge",
                "general_knowledge": "general_knowledge",
                "help": "none",
                "mixed_request": "mixed",
                "result_followup": "previous_result",
                "clarification_required": "clarification",
                "unsupported": "none",
                "unsafe_or_unauthorized": "none",
            }.get(self.intent, "none")
            object.__setattr__(self, "evidence_mode", mode)
        if self.reason_code is None:
            object.__setattr__(self, "reason_code", self.reason)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("route confidence must be between 0 and 1")

    @property
    def should_generate_sql(self) -> bool:
        return bool(self.requires_database)

    @property
    def should_use_tool_free_llm(self) -> bool:
        """Whether the request may use an LLM without database tools."""
        return self.intent in {
            "general_business",
            "general_knowledge",
            "result_followup",
        }

    @property
    def needs_clarification(self) -> bool:
        return self.state in {
            "clarification_required",
            "missing_time",
            "missing_metric",
            "missing_comparison",
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "missing": list(self.missing),
            "metric_ids": list(self.metric_ids),
            "clarification": self.clarification,
            "reason": self.reason,
            "direct_answer": self.direct_answer,
            "should_generate_sql": self.should_generate_sql,
            "intent": self.intent,
            "requires_database": self.requires_database,
            "evidence_mode": self.evidence_mode,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }


class QuestionRouter:
    """Classify requests using server-owned rules and Catalog evidence.

    This is intentionally a deterministic first gate.  A future structured
    LLM classifier may be used only for the ambiguous branch; it must never
    receive database tools or replace SQL Policy/RBAC checks.
    """

    def __init__(self, retriever: CatalogRetriever):
        self.retriever = retriever

    def classify(
        self,
        question: str,
        *,
        user: User | None = None,
        selection: CatalogSelection | None = None,
        conversation_state: dict[str, object] | None = None,
    ) -> QuestionRoute:
        if not isinstance(question, str) or not question.strip():
            return QuestionRoute("unsupported", ("question",), (), "请先输入一个具体的分析问题。", "empty_question")
        if _UNAUTHORIZED.search(question):
            return QuestionRoute("unauthorized", (), (), "当前只支持受控的只读业务分析，不能执行写库、系统表或敏感数据操作。", "unsafe_or_unauthorized_request")
        if _UNSUPPORTED.search(question):
            return QuestionRoute("unsupported", (), (), "当前版本只支持 PostgreSQL 只读经营分析；请改为查询已有业务指标。", "unsupported_capability")
        selection = selection or self.retriever.retrieve(question, user)
        state = conversation_state or {}

        if _HELP_REQUEST.search(question.strip()):
            return QuestionRoute(
                "help",
                (),
                (),
                None,
                "help_request",
                self._format_capabilities(),
                intent="help",
                requires_database=False,
                evidence_mode="none",
            )

        metric_ids = tuple(selection.trace.selected_metrics)
        if not metric_ids:
            remembered = state.get("metric_ids")
            if isinstance(remembered, (list, tuple)):
                metric_ids = tuple(str(value) for value in remembered)

        # A follow-up about a prior result should not be mistaken for a new
        # metric request.  If no trusted result summary exists, ask for the
        # missing context instead of inventing one.
        if _RESULT_FOLLOWUP.search(question):
            previous_result = state.get("previous_result_summary")
            if isinstance(previous_result, str) and previous_result.strip():
                if _DRILLDOWN_REQUEST.search(question) and _DATA_INTENT.search(question):
                    return QuestionRoute(
                        "answerable",
                        (),
                        metric_ids,
                        None,
                        "result_followup_requires_new_scope",
                        intent="data_analysis",
                        requires_database=True,
                        evidence_mode="database_result",
                    )
                return QuestionRoute(
                    "result_followup",
                    (),
                    metric_ids,
                    None,
                    "followup_on_trusted_result",
                    intent="result_followup",
                    requires_database=False,
                    evidence_mode="previous_result",
                )
            return QuestionRoute(
                "clarification_required",
                ("previous_result",),
                metric_ids,
                "请说明你要追问的是哪一轮结果，或重新描述指标、时间范围和筛选条件。",
                "followup_without_trusted_result",
                intent="clarification_required",
                requires_database=False,
                evidence_mode="clarification",
            )

        definition_match = _DEFINITION_REQUEST.search(question)
        question_without_definition = _DEFINITION_REQUEST.sub("", question)
        if definition_match and not _DATA_INTENT.search(question_without_definition):
            if not metric_ids:
                return QuestionRoute(
                    "general_knowledge",
                    (),
                    (),
                    None,
                    "generic_definition_without_catalog_metric",
                    intent="general_knowledge",
                    requires_database=False,
                    evidence_mode="general_knowledge",
                )
            metrics = selection.metrics
            if not metrics:
                metrics = tuple(
                    self.retriever.catalog.metrics_by_id[metric_id]
                    for metric_id in metric_ids
                    if metric_id in self.retriever.catalog.metrics_by_id
                )
            return QuestionRoute(
                "catalog_answered",
                (),
                metric_ids,
                None,
                "catalog_metric_definition",
                self._format_metric_definitions(metrics),
                intent="catalog_definition",
                requires_database=False,
                evidence_mode="catalog",
            )

        # A metric mention in a generic advice/education question is not an
        # instruction to inspect the current dataset.  Explicit grounding
        # phrases such as “结合当前数据” take precedence and continue to the
        # data path below.
        if (
            _GENERAL_BUSINESS_REQUEST.search(question)
            and not _EXPLICIT_DATA_CONTEXT.search(question)
            and not _EXPLICIT_DATA_REQUEST.search(question_without_definition)
        ):
            return QuestionRoute(
                "general_business",
                (),
                metric_ids,
                None,
                "generic_business_question",
                intent="general_business",
                requires_database=False,
                evidence_mode="general_knowledge",
            )

        if not metric_ids:
            if _GENERAL_BUSINESS_REQUEST.search(question) and not _EXPLICIT_DATA_CONTEXT.search(question):
                return QuestionRoute(
                    "general_business",
                    (),
                    (),
                    None,
                    "generic_business_question_without_metric",
                    intent="general_business",
                    requires_database=False,
                    evidence_mode="general_knowledge",
                )
            return QuestionRoute(
                "missing_metric",
                ("metric",),
                (),
                "你希望比较或统计哪个指标？例如 GMV、有效订单数、平均履约天数或好评率。",
                "no_metric_match",
            )

        # Dimension attribution is a Catalog-owned business rule.  Run it
        # only after help, definition, generic-advice, and follow-up branches
        # have opted out of database access.  A data question may omit words
        # such as "统计" (for example, "不同支付方式的 GMV"), so the presence
        # of a known metric plus a requested allowed dimension is sufficient.
        # This stops SQL generation before the model can invent a
        # deduplication rule or choose an arbitrary fact-row representative.
        requested_dimensions = self.retriever.requested_dimensions(
            question, selection.metrics
        )
        selected_metrics = {metric.metric_id: metric for metric in selection.metrics}
        for dimension, _table_id in requested_dimensions:
            for metric_id in metric_ids:
                metric = selected_metrics.get(metric_id)
                if metric is None:
                    metric = self.retriever.catalog.metrics_by_id.get(metric_id)
                policy = metric.dimension_policies.get(dimension) if metric else None
                if policy is None or policy.mode == "safe_direct":
                    continue
                if policy.mode == "requires_attribution":
                    metric_name = metric.name if metric is not None else metric_id
                    return QuestionRoute(
                        "clarification_required",
                        (f"dimension_policy:{dimension}",),
                        metric_ids,
                        f"当前工作区尚未冻结“{metric_name}”按“{dimension}”汇总的归属口径："
                        f"{policy.description} 请改用无此归因歧义的维度，"
                        "或先由管理员在 Semantic Catalog 中配置归属/分摊规则。",
                        "dimension_attribution_requires_clarification",
                        intent="clarification_required",
                        requires_database=False,
                        evidence_mode="clarification",
                    )
                if not self.retriever.catalog.has_available_attribution_rule(
                    policy.attribution_rule_id
                ):
                    metric_name = metric.name if metric is not None else metric_id
                    return QuestionRoute(
                        "clarification_required",
                        (f"attribution_rule:{policy.attribution_rule_id}",),
                        metric_ids,
                        f"当前工作区声明“{metric_name}”按“{dimension}”需要服务器归因规则，"
                        f"但规则 `{policy.attribution_rule_id}` 尚未有可执行的服务器实现。"
                        "请改用无此归因歧义的维度，或由管理员完成规则实现并启用后再查询。",
                        "dimension_attribution_rule_unavailable",
                        intent="clarification_required",
                        requires_database=False,
                        evidence_mode="clarification",
                    )

        intent: IntentKind = "data_query"
        evidence_mode: EvidenceMode = "database_result"
        if definition_match and _DATA_INTENT.search(question_without_definition):
            intent = "mixed_request"
            evidence_mode = "mixed"
        elif _CAUSE_REQUEST.search(question):
            intent = "data_analysis"

        if _RELATIVE_TIME.search(question) and not state.get("time_range"):
            return QuestionRoute(
                "missing_time",
                ("time_range",),
                metric_ids,
                "请补充具体统计时间范围，例如 2017-01-01 至 2017-12-31；当前不会把“本月/最近”臆定为日历时间。",
                "relative_time_without_explicit_range",
                intent=intent,
                requires_database=False,
                evidence_mode="clarification",
            )
        if _COMPARISON.search(question) and not (
            state.get("comparison") or state.get("comparison_baseline")
        ):
            explicit_baseline = bool(re.search(r"上月|上个月|去年|同比|环比|与.+相比", question))
            if not explicit_baseline and re.search(r"相比|对比|比较|变化|增长|下降|趋势|最好|最高|最低|异常", question):
                return QuestionRoute(
                    "missing_comparison",
                    ("comparison_baseline",),
                    metric_ids,
                    "请说明比较基线或判断标准，例如同比、环比、与哪个地区/品类比较。",
                    "comparison_without_baseline",
                    intent=intent,
                    requires_database=False,
                    evidence_mode="clarification",
                )
        return QuestionRoute(
            "answerable",
            (),
            metric_ids,
            None,
            "matched_catalog_and_no_blocking_ambiguity",
            intent=intent,
            requires_database=True,
            evidence_mode=evidence_mode,
        )

    @staticmethod
    def _format_capabilities() -> str:
        return "\n".join(
            [
                "## 当前可用能力",
                "",
                "- 查询受控的只读 PostgreSQL 分析数据：GMV、有效订单数、履约时长、好评率等指标。",
                "- 按时间、地区、品类等允许维度进行汇总、排名、趋势和对比。",
                "- 解释指标定义、统计口径、时间字段、默认过滤和数据版本。",
                "- 对 SQL 做只读权限、AST 策略、结果合同和审计校验。",
                "",
                "当前不支持写库、任意 Python 执行、自动下单、预测模型训练和访问系统表。",
                "如需查询实际数值，请直接给出指标、时间范围和维度；如果只想了解业务概念，不会默认查询数据库。",
            ]
        )

    @staticmethod
    def _format_metric_definitions(metrics) -> str:
        lines = [
            "## 指标定义与统计口径",
            "",
            "以下内容直接来自当前工作区的 Semantic Catalog，不会触发数据库查询。",
            "",
            "| 指标 | 定义 | 时间字段 | 默认过滤 |",
            "| --- | --- | --- | --- |",
        ]
        for metric in metrics:
            filters = "；".join(metric.default_filters) or "无额外过滤"
            lines.append(
                f"| {metric.name}（`{metric.metric_id}`） | {metric.description} | "
                f"`{metric.time_field}` | {filters} |"
            )
        lines.extend(
            [
                "",
                "如需查看实际数值，请继续提出带有统计范围、维度或对比条件的问题。",
            ]
        )
        return "\n".join(lines)
