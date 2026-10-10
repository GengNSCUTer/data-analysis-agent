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

from .semantic_catalog import (
    CatalogSelection,
    CatalogRetriever,
    _COLLOQUIAL_METRIC_HINTS,
)
from .sql_policy import DEFAULT_ANALYST_RESULT_LIMIT


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
_COMPARISON = re.compile(r"相比|对比|比较|变化|增长|下降|环比|同比|趋势|异常")
_EXPLICIT_PERIOD = re.compile(r"(?<!\d)20\d{2}(?:-\d{2}(?:-\d{2})?|年(?:\d{1,2}月)?)?(?!\d)")
_UNAUTHORIZED = re.compile(
    r"写入|删除|修改|更新|插入|建表|导出原始|密码|密钥|token|api.?key|information_schema|pg_catalog|app\.|系统表|任意文件",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(r"预测|机器学习训练|自动下单|发起退款|执行脚本|运行 python|运行代码", re.IGNORECASE)
_DEFINITION_REQUEST = re.compile(
    r"是什么|指什么|什么意思|含义|定义|统计口径|口径是什么|口径|怎么算|如何计算|解释一下|解释下|解释"
)
# Read-query cues on the residual sentence after Catalog aliases are stripped.
# Do not treat 平均/金额/数量 inside a metric alias as permission to open SQL.
_DATA_INTENT = re.compile(
    r"概览|总览|多少|数值|统计|查询|查一下|计算|分析|趋势|排名|"
    r"前[一二三四五六七八九十0-9]+|同比|环比|"
    r"各|按|不同|每个|各个|"
    r"对比|比较|变化|增长|下降|最好|最高|最低|哪个|哪些|哪[一种个类]|"
    r"比例|数量|金额"
)
# Advice stays advice unless the residual still asks to fetch numbers.
_ADVICE_BLOCKING_READ = re.compile(
    r"概览|总览|多少|数值|统计|查询|查一下|计算|排名|"
    r"前[一二三四五六七八九十0-9]+|同比|环比|"
    r"按.{0,10}(?:州|地区|品类|城市|卖家|月|周|日|年)"
)
_WORLD_REWRITE_FRAME = re.compile(
    r"(?:把|将).{0,48}?(?:改成|改为|变成|变为|更新为|设为|设置为|改写成)"
)
_WORLD_REWRITE_SAFE_TAIL = re.compile(
    r"^(?:按|拆分|展开|下钻|分解|再查|查询|统计|分析)"
)
_HELP_REQUEST = re.compile(
    r"你能做什么|能做什么|怎么用|如何使用|帮助|使用说明|功能介绍|支持哪些|有哪些功能|^/?help$",
    re.IGNORECASE,
)
_GENERAL_BUSINESS_REQUEST = re.compile(
    r"通常|一般|为什么会|原因|如何提升|怎么提升|最佳实践|建议|策略|经验|怎么理解|业务上|行业|背离|改善",
)
_CAUSE_REQUEST = re.compile(r"归因|原因|影响因素|驱动因素|为什么.*(?:下降|增长|变化|异常)")
_EXPLICIT_DATA_CONTEXT = re.compile(
    r"当前数据|这份数据|本数据|数据库中|查一下|查当前|实际数据|数据里|数据中|基于当前|结合当前|从结果看|本次结果|本轮结果",
)
_RESULT_FOLLOWUP = re.compile(
    r"这个结果|该结果|刚才|上面的?(数字|结果|数据)|上一轮|前面查询|为什么这么高|为什么这么低|是否合理|是否正常|结果如何",
)
# Explaining a previously returned ranking is not equivalent to asking the
# database for a new ranking.  Keep interpretation ahead of the new-query
# branch so “刚才排名第一的州，结果是否合理？” remains tool-free.
_RESULT_INTERPRETATION_REQUEST = re.compile(
    r"是否合理|是否正常|为什么(?:这么)?(?:高|低)|怎么理解|意味着什么|如何解释|合理吗|正常吗"
)
_EXPLICIT_RESULT_REQUERY_REQUEST = re.compile(
    r"(?:重新|再|继续).{0,12}(?:查|查询|统计|分析)|"
    r"(?:按.{0,12}(?:拆分|展开|下钻|明细|分解))|"
    r"(?:把|将).{0,12}(?:拆分|展开|下钻|分解)"
)
_AMOUNT_REQUEST = re.compile(r"金额|多少钱")
_BARE_ORDER_REQUEST = re.compile(r"订单")
_ORDER_METRIC_REQUEST = re.compile(r"订单数|订单量")
_DIMENSION_LABELS = {
    "customer_state": "客户州",
    "customer_city": "客户城市",
    "seller_state": "卖家州",
    "seller_city": "卖家城市",
    "product_category_name": "商品品类",
    "payment_type": "支付方式",
}


_RESULT_REFINEMENT = re.compile(
    r"^\s*(?:那|那么|再|改|换|只看|仅看|按|分|拆|前[一二三四五六七八九十0-9]+|取前|排序|倒序|降序|升序)"
    r"|拆分|展开|下钻|只看|只要|改看|改成|换成|改为|换个"
)
_OVERVIEW_REQUEST = re.compile(
    r"概览|总览|大盘|核心指标|整体(?:情况|表现|数据|经营|怎么样|如何)|经营(?:情况|状况|概况|表现)"
)
# The same four metrics as the starter “指标概览” action.
OVERVIEW_METRIC_IDS = (
    "gmv",
    "paid_order_count",
    "average_delivery_days",
    "positive_review_rate",
)


_METRIC_SWAP = re.compile(r"改看|换成|改成|改为|换个")
_SPEECH_ACT_CLARIFICATION = (
    "已对上指标，但还看不出是要查数、看口径还是听建议。"
    "请改成例如：统计 2017 年…、…是什么、或如何提升…。"
)


def residual_speech_text(question: str, forms: tuple[str, ...] | list[str]) -> str:
    """Strip Catalog surface forms so speech-act cues are not taken from aliases."""

    residual = question or ""
    unique = sorted(
        {form.strip() for form in forms if isinstance(form, str) and len(form.strip()) >= 2},
        key=len,
        reverse=True,
    )
    for form in unique:
        if form.isascii():
            residual = re.sub(re.escape(form), " ", residual, flags=re.IGNORECASE)
        else:
            residual = residual.replace(form, " ")
    return re.sub(r"\s+", " ", residual).strip()


def _is_world_rewrite(text: str) -> bool:
    """Syntactic rewrite-the-world frame, not a verb deny-list."""

    if not text:
        return False
    for match in _WORLD_REWRITE_FRAME.finditer(text):
        tail = text[match.end() :].lstrip()
        if not _WORLD_REWRITE_SAFE_TAIL.search(tail):
            return True
    return False


def _is_read_query(text: str) -> bool:
    return bool(
        text
        and (
            _DATA_INTENT.search(text)
            or _RELATIVE_TIME.search(text)
            or _EXPLICIT_PERIOD.search(text)
        )
    )


def _has_colloquial_read(question: str) -> bool:
    return any(pattern.search(question or "") for pattern, _ in _COLLOQUIAL_METRIC_HINTS)


def is_result_refinement(
    question: str, state: dict | None, *, has_metric_match: bool = False
) -> bool:
    """A short edit of the last trusted result, such as “按州拆分” or “只看前五”.

    Only a turn that follows a server-validated result qualifies, so a fresh
    conversation still has to name its metric. A turn that names its own
    metric is a new question unless it explicitly swaps the metric
    (“改看有效订单数”).
    """

    state = state or {}
    previous = state.get("previous_result_summary")
    remembered = state.get("metric_ids")
    question = question or ""
    return bool(
        isinstance(previous, str)
        and previous.strip()
        and isinstance(remembered, (list, tuple))
        and remembered
        and _RESULT_REFINEMENT.search(question)
        and (not has_metric_match or _METRIC_SWAP.search(question))
    )


def is_overview_request(question: str) -> bool:
    return bool(_OVERVIEW_REQUEST.search(question or ""))


def expand_retrieval_question(
    question: str,
    state: dict | None,
    *,
    has_metric_match: bool,
) -> str:
    """Add the metric and dimension ids a turn implies but does not spell out.

    Retrieval scores metric ids and column names as aliases, so appending them
    selects the same Catalog slice the router, QueryPlan and ResultContract
    will use.
    """

    state = state or {}
    if is_result_refinement(question, state, has_metric_match=has_metric_match):
        remembered = [] if has_metric_match else [
            str(item) for item in state.get("metric_ids") or ()
        ]
        remembered += [str(item) for item in state.get("dimensions") or ()]
        return f"{question}\n{' '.join(remembered)}" if remembered else question
    if has_metric_match:
        return question
    if is_overview_request(question):
        return f"{question}\n{' '.join(OVERVIEW_METRIC_IDS)}"
    return question


def _is_clarification_continuation(state: dict) -> bool:
    """True only while the user is answering an unfinished clarification."""

    pending_question = state.get("pending_question")
    if isinstance(pending_question, str) and pending_question.strip():
        return True
    pending_missing = state.get("pending_missing")
    return isinstance(pending_missing, (list, tuple)) and any(
        str(item).strip() for item in pending_missing
    )


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
    """Decide whether a turn may generate SQL.

    ``classify`` retrieves Catalog mentions, then judges a closed speech-act on
    the residual sentence. Only an explicit read-query opens SQL. Definitions,
    advice, help, follow-ups, and rewrite-the-world frames stay tool-free.
    Metric identity, grouping, time, attribution, and row shape are checked
    later by ``admit_sql_request``. That second step still cannot write SQL or
    bypass SqlPolicy.
    """

    def __init__(
        self,
        retriever: CatalogRetriever,
        *,
        max_result_rows: int = DEFAULT_ANALYST_RESULT_LIMIT,
    ):
        if max_result_rows <= 0:
            raise ValueError("max_result_rows must be positive")
        self.retriever = retriever
        # This is the pre-SQL execution bound. The frontend preview budget is
        # deliberately kept in ResultValidator/RunSqlTool and is not used to
        # reject a safe grouped query.
        self.max_result_rows = max_result_rows

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
        # A previous turn's metric is only reused for an unfinished
        # clarification or an explicit drill-down of a trusted result.
        if not metric_ids:
            remembered = state.get("metric_ids")
            reuse_remembered = _is_clarification_continuation(state) or (
                _RESULT_FOLLOWUP.search(question)
                and _EXPLICIT_RESULT_REQUERY_REQUEST.search(question)
            )
            if reuse_remembered and isinstance(remembered, (list, tuple)):
                metric_ids = tuple(str(value) for value in remembered)

        # A follow-up about a prior result should not be mistaken for a new
        # metric request.  If no trusted result summary exists, ask for the
        # missing context instead of inventing one.
        if _RESULT_FOLLOWUP.search(question):
            previous_result = state.get("previous_result_summary")
            if isinstance(previous_result, str) and previous_result.strip():
                if _RESULT_INTERPRETATION_REQUEST.search(question):
                    reason = (
                        "followup_interprets_trusted_result"
                        if re.search(r"排名", question)
                        else "followup_on_trusted_result"
                    )
                    return QuestionRoute(
                        "result_followup",
                        (),
                        metric_ids,
                        None,
                        reason,
                        intent="result_followup",
                        requires_database=False,
                        evidence_mode="previous_result",
                    )
                if _EXPLICIT_RESULT_REQUERY_REQUEST.search(question):
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

        residual = self._speech_residual(question, metric_ids, selection)
        if _is_world_rewrite(question) or _is_world_rewrite(residual):
            return QuestionRoute(
                "unauthorized",
                (),
                (),
                "当前只支持受控的只读业务分析，不能执行写库、系统表或敏感数据操作。",
                "unsafe_or_unauthorized_request",
            )

        definition_match = _DEFINITION_REQUEST.search(residual)
        question_without_definition = _DEFINITION_REQUEST.sub("", residual)
        read_without_definition = _is_read_query(question_without_definition)
        if definition_match and not read_without_definition:
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
        # data path below. Judge advice on the residual so alias words such as
        # 平均/金额 do not look like a data request.
        if (
            _GENERAL_BUSINESS_REQUEST.search(residual)
            and not _EXPLICIT_DATA_CONTEXT.search(question)
            and not _ADVICE_BLOCKING_READ.search(question_without_definition)
            and not _RELATIVE_TIME.search(question_without_definition)
            and not _EXPLICIT_PERIOD.search(question_without_definition)
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

        if not (
            _is_read_query(residual)
            or _has_colloquial_read(question)
            or _EXPLICIT_DATA_CONTEXT.search(question)
        ):
            if metric_ids:
                return QuestionRoute(
                    "clarification_required",
                    ("speech_act",),
                    metric_ids,
                    _SPEECH_ACT_CLARIFICATION,
                    "catalog_mention_without_speech_act",
                    intent="clarification_required",
                    requires_database=False,
                    evidence_mode="clarification",
                )
            return QuestionRoute(
                "missing_metric",
                ("metric",),
                (),
                self._missing_metric_clarification(question, user),
                "no_metric_match",
                intent="clarification_required",
                requires_database=False,
                evidence_mode="clarification",
            )

        intent: IntentKind = "data_query"
        evidence_mode: EvidenceMode = "database_result"
        if definition_match and read_without_definition:
            intent = "mixed_request"
            evidence_mode = "mixed"
        elif _CAUSE_REQUEST.search(question):
            intent = "data_analysis"
        return QuestionRoute(
            "answerable",
            (),
            metric_ids,
            None,
            "sql_requested",
            intent=intent,
            requires_database=True,
            evidence_mode=evidence_mode,
        )

    def admit_sql_request(
        self,
        question: str,
        *,
        user: User | None = None,
        selection: CatalogSelection | None = None,
        conversation_state: dict[str, object] | None = None,
    ) -> QuestionRoute:
        """Check metric, grouping, time, and result shape before SQL generation."""

        selection = selection or self.retriever.retrieve(question, user)
        state = conversation_state or {}
        metric_ids = tuple(selection.trace.selected_metrics)
        if not metric_ids:
            remembered = state.get("metric_ids")
            reuse_remembered = _is_clarification_continuation(state) or (
                _RESULT_FOLLOWUP.search(question)
                and _EXPLICIT_RESULT_REQUERY_REQUEST.search(question)
            )
            if reuse_remembered and isinstance(remembered, (list, tuple)):
                metric_ids = tuple(str(value) for value in remembered)
        residual = self._speech_residual(question, metric_ids, selection)
        definition_match = _DEFINITION_REQUEST.search(residual)
        question_without_definition = _DEFINITION_REQUEST.sub("", residual)

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
                self._missing_metric_clarification(question, user),
                "no_metric_match",
            )

        matched_metric_ids = self.retriever.matched_metric_ids(question, user)
        if len(matched_metric_ids) > self.retriever.max_metrics:
            return QuestionRoute(
                "clarification_required",
                ("metric_count",),
                matched_metric_ids,
                f"当前一次受控查询最多支持 {self.retriever.max_metrics} 个指标；"
                "请拆分为更小的指标组合，系统不会静默遗漏你提出的指标。",
                "metric_count_exceeds_prompt_contract",
                intent="clarification_required",
                requires_database=False,
                evidence_mode="clarification",
            )

        # Dimension attribution is a Catalog-owned business rule.  Run it
        # only after help, definition, generic-advice, and follow-up branches
        # have opted out of database access.  A data question may omit words
        # such as "统计" (for example, "不同支付方式的 GMV"), so the presence
        # of a known metric plus a requested allowed dimension is sufficient.
        # This stops SQL generation before the model can invent a
        # deduplication rule or choose an arbitrary fact-row representative.
        # Import locally to avoid the QueryPlan -> QuestionRoute module cycle.
        from .query_plan import QueryPlan

        metric_objects = tuple(
            self.retriever.catalog.metrics_by_id[metric_id]
            for metric_id in metric_ids
            if metric_id in self.retriever.catalog.metrics_by_id
        )
        selected_metrics = {metric.metric_id: metric for metric in selection.metrics}
        # Grouping comes from query shape, not from every mention of “州/商品”.
        # A filter such as “SP州” must not become a state breakdown.
        for dimension in QueryPlan._dimensions(question, metric_objects, state):
            for metric_id in metric_ids:
                metric = selected_metrics.get(metric_id)
                if metric is None:
                    metric = self.retriever.catalog.metrics_by_id.get(metric_id)
                policy = metric.dimension_policies.get(dimension) if metric else None
                if policy is None or policy.mode in {
                    "safe_direct",
                    "multi_membership",
                    "payment_share",
                }:
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

        preflight = QueryPlan.preflight(
            tuple(
                self.retriever.catalog.metrics_by_id[metric_id]
                for metric_id in metric_ids
                if metric_id in self.retriever.catalog.metrics_by_id
            ),
            question,
            state,
            max_result_rows=self.max_result_rows,
        )
        if not preflight.allowed:
            return QuestionRoute(
                "clarification_required",
                (preflight.reason_code or "query_shape",),
                metric_ids,
                preflight.clarification,
                preflight.reason_code or "query_shape_not_supported",
                intent="clarification_required",
                requires_database=False,
                evidence_mode="clarification",
            )

        intent: IntentKind = "data_query"
        evidence_mode: EvidenceMode = "database_result"
        reason = "matched_catalog_and_no_blocking_ambiguity"
        if definition_match and _is_read_query(question_without_definition):
            intent = "mixed_request"
            evidence_mode = "mixed"
        elif _RESULT_FOLLOWUP.search(question) and _EXPLICIT_RESULT_REQUERY_REQUEST.search(
            question
        ):
            intent = "data_analysis"
            reason = "result_followup_requires_new_scope"
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
            explicit_baseline = bool(
                re.search(r"上月|上个月|去年|同比|环比|与.+相比", question)
                or len(set(_EXPLICIT_PERIOD.findall(question))) >= 2
            )
            if not explicit_baseline and re.search(
                r"相比|对比|比较|变化|增长|下降|趋势|异常", question
            ):
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
            reason,
            intent=intent,
            requires_database=True,
            evidence_mode=evidence_mode,
        )

    def _speech_residual(
        self,
        question: str,
        metric_ids: tuple[str, ...],
        selection: CatalogSelection,
    ) -> str:
        """Drop selected metric names/aliases/colloquial hints from the question."""

        forms: list[str] = []
        catalog = self.retriever.catalog
        selected = set(metric_ids) | set(selection.trace.selected_metrics)
        for metric_id in selected:
            metric = catalog.metrics_by_id.get(metric_id)
            if metric is None:
                continue
            forms.append(metric.name)
            forms.append(metric.metric_id)
            forms.extend(metric.aliases)
        for pattern, metric_id in _COLLOQUIAL_METRIC_HINTS:
            if metric_id not in selected:
                continue
            match = pattern.search(question)
            if match is not None:
                forms.append(match.group(0))
        return residual_speech_text(question, forms)

    def _missing_metric_clarification(
        self, question: str, user: User | None
    ) -> str:
        """Reuse what retrieval already understood instead of a generic prompt."""

        dimensions = self.retriever.hinted_dimensions(question, user)
        if "payment_type" in dimensions and _AMOUNT_REQUEST.search(question):
            return (
                "已对上支付方式。本工作区里“金额”不是单一指标："
                "商品成交额是 GMV，运费是运费金额；支付金额不能直接替代成交额。"
                "请点名其中一个，不要让系统猜口径。"
            )
        if dimensions:
            role = "admin" if user is not None and "admin" in user.group_memberships else "analyst"
            compatible: list[str] = []
            for metric in self.retriever.catalog.metrics:
                if role not in metric.role_visibility:
                    continue
                if all(dimension in metric.allowed_dimensions for dimension in dimensions):
                    compatible.append(metric.name)
            names = "、".join(compatible[:4]) or "GMV、有效订单数、平均履约天数或好评率"
            labels = "、".join(
                _DIMENSION_LABELS.get(dimension, dimension) for dimension in dimensions
            )
            return f"已理解要按{labels}看，但还没对上指标。这个维度上可以查：{names}。"
        if _BARE_ORDER_REQUEST.search(question) and not _ORDER_METRIC_REQUEST.search(
            question
        ):
            return (
                "“订单”还没对上具体指标。常见的是有效订单数、取消订单数或已送达订单数。"
                "请点名其中一个，并说明要按州、品类还是合计。"
            )
        return "你希望比较或统计哪个指标？例如 GMV、有效订单数、平均履约天数或好评率。"

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
