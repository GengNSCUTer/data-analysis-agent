"""Constrained Catalog grounding for questions that miss alias lookup."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from vanna.core.user import RequestContext, User
from vanna.servers.base.models import ChatRequest

from data_analysis_agent.budget import RequestBudget
from data_analysis_agent.catalog_grounding import (
    CatalogGrounding,
    ConstrainedCatalogGrounder,
    parse_catalog_grounding,
)
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.run_recorder import AgentRun
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever


def _catalog():
    return CatalogLoader().load()


def test_parser_keeps_only_catalog_ids_and_drops_unknown_fields() -> None:
    grounding = parse_catalog_grounding(
        '说明一下 {"metric_ids":["item_count","not_a_metric"],"dimensions":["product_category_name","seller_id"]}',
        _catalog(),
    )

    assert grounding is not None
    assert grounding.metric_ids == ("item_count",)
    assert grounding.dimensions == ("product_category_name",)


def test_parser_rejects_empty_or_non_json_output() -> None:
    catalog = _catalog()
    assert parse_catalog_grounding("无法判断", catalog) is None
    assert parse_catalog_grounding('{"metric_ids":[]}', catalog) is None


@pytest.mark.asyncio
async def test_unmatched_paraphrase_uses_grounding_then_enters_sql_path() -> None:
    user = User(id="grounding-analyst", group_memberships=["analyst"])
    router = QuestionRouter(CatalogRetriever(CatalogLoader().load()))

    class Resolver:
        async def resolve_user(self, context):
            return user

    class Store:
        def __init__(self):
            self.conversation = None

        async def get_conversation(self, conversation_id, resolved_user):
            return self.conversation

        async def update_conversation(self, conversation):
            self.conversation = conversation

    @dataclass
    class Recorder:
        usage = None

        async def start(self, **kwargs):
            return AgentRun("run-grounding", kwargs["request_id"], kwargs["conversation_id"])

        async def finish(self, run, usage):
            self.usage = usage

    class Agent:
        def __init__(self):
            self.user_resolver = Resolver()
            self.conversation_store = Store()
            self.called = False

        async def send_message(self, request_context, message, conversation_id=None):
            self.called = True
            if False:
                yield  # pragma: no cover

    async def complete(prompt: str, resolved_user: User) -> str:
        assert "卖得最火" in prompt
        assert resolved_user.id == user.id
        return '{"metric_ids":["item_count"],"dimensions":["product_category_name"]}'

    agent = Agent()
    handler = BudgetedChatHandler(
        agent,
        RequestBudget(),
        Recorder(),
        question_router=router,
        catalog_grounder=ConstrainedCatalogGrounder(router.retriever.catalog, complete),
    )
    request = ChatRequest(
        message="卖得最火的货",
        conversation_id="conversation-grounding",
        request_context=RequestContext(),
    )

    _chunks = [chunk async for chunk in handler.handle_stream(request)]

    assert agent.called is True
    stored = agent.conversation_store.conversation.metadata["working_memory"]
    assert stored["metric_ids"] == ["item_count"]
    assert "gmv" not in stored["metric_ids"]


def test_grounding_dimensions_are_request_local() -> None:
    grounding = CatalogGrounding(("item_count",), ("product_category_name",))
    assert grounding.as_dict()["dimensions"] == ["product_category_name"]
