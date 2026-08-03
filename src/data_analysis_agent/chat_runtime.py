"""Project-owned chat boundary for budgets and persistent Agent runs."""

from __future__ import annotations

import uuid

from vanna.components import SimpleTextComponent, StatusCardComponent, UiComponent
from vanna.core.storage import Conversation
from vanna.servers.base import ChatHandler
from vanna.servers.base.models import ChatRequest, ChatStreamChunk

from .budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from .metric_context import DATASET_VERSION, METRIC_VERSION
from .run_recorder import PostgresRunRecorder


class BudgetedChatHandler(ChatHandler):
    """Attach one isolated budget tracker and run record to each chat request."""

    def __init__(self, agent, budget: RequestBudget, run_recorder: PostgresRunRecorder):
        super().__init__(agent)
        self.budget = budget
        self.run_recorder = run_recorder

    async def handle_stream(self, request: ChatRequest):
        conversation_id = request.conversation_id or f"conv_{uuid.uuid4().hex[:8]}"
        request_id = request.request_id or str(uuid.uuid4())
        request.conversation_id = conversation_id
        request.request_id = request_id
        request.request_context.metadata = {
            **request.request_context.metadata,
            "budget_usage": BudgetUsage(self.budget),
        }
        usage = request.request_context.metadata["budget_usage"]
        usage.set_input(request.message)
        tracker_token = CURRENT_BUDGET.set(usage)
        run = None
        try:
            user = await self.agent.user_resolver.resolve_user(request.request_context)
            # Agent Run has a foreign key to the conversation. Create an empty
            # owner-scoped row before recording the run; Agent appends messages
            # through ConversationStore after this boundary is established.
            if not await self.agent.conversation_store.get_conversation(
                conversation_id, user
            ):
                await self.agent.conversation_store.update_conversation(
                    Conversation(
                        id=conversation_id,
                        user=user,
                        metadata={
                            "dataset_version_id": DATASET_VERSION,
                            "metric_version": METRIC_VERSION,
                        },
                    )
                )
            run = await self.run_recorder.start(
                request_id=request_id,
                conversation_id=conversation_id,
                user=user,
                question=request.message,
                budget=self.budget,
                dataset_version_id=DATASET_VERSION,
                metric_version=METRIC_VERSION,
            )
            request.request_context.metadata["run_id"] = run.run_id

            if usage.termination_reason == "input_too_long":
                yield self._budget_chunk(
                    conversation_id,
                    request_id,
                    "输入问题过长",
                    "当前请求超过输入长度预算，请拆分问题后重试。",
                )
                return

            async for chunk in super().handle_stream(request):
                yield chunk
            if (
                usage.termination_reason == "running"
                and usage.llm_rounds_used >= self.budget.max_tool_iterations
                and usage.last_response_had_tool_calls
            ):
                usage.terminate("tool_budget_exhausted")
            usage.finish()
        except Exception as exc:
            usage.terminate("execution_error", type(exc).__name__)
            raise
        finally:
            try:
                if run is not None:
                    await self.run_recorder.finish(run, usage)
            finally:
                CURRENT_BUDGET.reset(tracker_token)

    @staticmethod
    def _budget_chunk(
        conversation_id: str, request_id: str, title: str, detail: str
    ) -> ChatStreamChunk:
        component = UiComponent(
            rich_component=StatusCardComponent(
                title=title,
                status="warning",
                description=detail,
                icon="⚠️",
            ),
            simple_component=SimpleTextComponent(text=detail),
        )
        return ChatStreamChunk.from_component(component, conversation_id, request_id)
