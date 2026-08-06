"""Deterministic answerability routing before Text-to-SQL generation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from vanna.core.user import User

from .semantic_catalog import CatalogSelection, CatalogRetriever


QuestionState = Literal[
    "answerable",
    "catalog_answered",
    "missing_time",
    "missing_metric",
    "missing_comparison",
    "unauthorized",
    "unsupported",
]

_RELATIVE_TIME = re.compile(r"本月|本周|本季度|今年|最近|近[一二三四五六七八九十0-9]+天|上月|上个月|去年|过去")
_COMPARISON = re.compile(r"相比|对比|比较|变化|增长|下降|环比|同比|趋势|最好|最高|最低|异常")
_UNAUTHORIZED = re.compile(
    r"写入|删除|修改|更新|插入|建表|导出原始|密码|密钥|token|api.?key|information_schema|pg_catalog|app\.|系统表|任意文件",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(r"预测|机器学习训练|自动下单|发起退款|执行脚本|运行 python|运行代码", re.IGNORECASE)
_DEFINITION_REQUEST = re.compile(r"是什么|指什么|含义|定义|统计口径|口径是什么|怎么算|如何计算")
_DATA_INTENT = re.compile(
    r"概览|多少|数值|统计|查询|分析|趋势|排名|前[一二三四五六七八九十0-9]+|总|平均|比例|数量|金额|按|对比|比较|变化|增长|下降|最好|最高|最低"
)


@dataclass(frozen=True)
class QuestionRoute:
    state: QuestionState
    missing: tuple[str, ...]
    metric_ids: tuple[str, ...]
    clarification: str | None
    reason: str
    direct_answer: str | None = None

    @property
    def should_generate_sql(self) -> bool:
        return self.state == "answerable"

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "missing": list(self.missing),
            "metric_ids": list(self.metric_ids),
            "clarification": self.clarification,
            "reason": self.reason,
            "direct_answer": self.direct_answer,
            "should_generate_sql": self.should_generate_sql,
        }


class QuestionRouter:
    """Classify only what can be decided without an additional LLM call."""

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
        metric_ids = tuple(selection.trace.selected_metrics)
        if not metric_ids:
            remembered = state.get("metric_ids")
            if isinstance(remembered, (list, tuple)):
                metric_ids = tuple(str(value) for value in remembered)
        if not metric_ids:
            return QuestionRoute(
                "missing_metric",
                ("metric",),
                (),
                "你希望比较或统计哪个指标？例如 GMV、有效订单数、平均履约天数或好评率。",
                "no_metric_match",
            )
        definition_match = _DEFINITION_REQUEST.search(question)
        question_without_definition = _DEFINITION_REQUEST.sub("", question)
        if definition_match and not _DATA_INTENT.search(question_without_definition):
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
            )
        if _RELATIVE_TIME.search(question) and not state.get("time_range"):
            return QuestionRoute(
                "missing_time",
                ("time_range",),
                metric_ids,
                "请补充具体统计时间范围，例如 2017-01-01 至 2017-12-31；当前不会把“本月/最近”臆定为日历时间。",
                "relative_time_without_explicit_range",
            )
        if _COMPARISON.search(question) and not state.get("comparison"):
            explicit_baseline = bool(re.search(r"上月|上个月|去年|同比|环比|与.+相比", question))
            if not explicit_baseline and re.search(r"相比|对比|比较|变化|增长|下降|趋势|最好|最高|最低|异常", question):
                return QuestionRoute(
                    "missing_comparison",
                    ("comparison_baseline",),
                    metric_ids,
                    "请说明比较基线或判断标准，例如同比、环比、与哪个地区/品类比较。",
                    "comparison_without_baseline",
                )
        return QuestionRoute("answerable", (), metric_ids, None, "matched_catalog_and_no_blocking_ambiguity")

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
