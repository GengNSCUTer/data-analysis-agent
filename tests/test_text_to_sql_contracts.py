from __future__ import annotations

from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.metric_context import SYSTEM_PROMPT
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.working_memory import WorkingMemory
from vanna.core.user import User


def test_catalog_trace_and_budget_evidence_are_redacted_contracts() -> None:
    question = "统计 GMV；不要把原文写入运行证据"
    user = User(id="contract-analyst", group_memberships=["analyst"])
    selection = CatalogRetriever(CatalogLoader().load()).retrieve(question, user)
    usage = BudgetUsage(RequestBudget())
    usage.record_catalog(selection.trace.as_dict())
    usage.set_catalog_context(question, WorkingMemory().as_dict())

    assert question not in repr(selection.trace.as_dict())
    assert question not in repr(usage.as_dict())
    assert usage.as_dict()["catalog_trace"]["catalog_version"] == "olist-catalog-v1"


def test_router_states_are_stable_and_explicit() -> None:
    router = QuestionRouter(CatalogRetriever(CatalogLoader().load()))
    user = User(id="contract-analyst", group_memberships=["analyst"])
    states = {
        router.classify("统计 GMV", user=user).state,
        router.classify("本月 GMV", user=user).state,
        router.classify("删除订单", user=user).state,
        router.classify("运行 Python 预测", user=user).state,
    }

    assert states <= {
        "answerable",
        "missing_time",
        "missing_metric",
        "missing_comparison",
        "unauthorized",
        "unsupported",
    }


def test_system_prompt_exposes_the_version_contract() -> None:
    for value in (
        "prompt_version=trusted-olist-prompt-v2",
        "catalog_version=olist-catalog-v1",
        "dataset_version=olist-kaggle-v2-2026-08-03",
        "metric_version=0.1-draft",
        "policy_version=sql-policy-v1",
    ):
        assert value in SYSTEM_PROMPT
