"""Project-owned chat boundary for budgets and persistent Agent runs."""

from __future__ import annotations

import uuid

from vanna.components import SimpleTextComponent, StatusCardComponent, UiComponent
from vanna.core.storage import Conversation, Message
from vanna.servers.base import ChatHandler
from vanna.servers.base.models import ChatRequest, ChatStreamChunk

from .budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from .metric_context import DATASET_VERSION, METRIC_VERSION, PROMPT_VERSION
from .question_router import QuestionRouter
from .run_recorder import PostgresRunRecorder
from .semantic_catalog import ResultContract
from .working_memory import WorkingMemory


class BudgetedChatHandler(ChatHandler):
    """Attach one isolated budget tracker and run record to each chat request."""

    def __init__(
        self,
        agent,
        budget: RequestBudget,
        run_recorder: PostgresRunRecorder,
        question_router: QuestionRouter | None = None,
    ):
        super().__init__(agent)
        self.budget = budget
        self.run_recorder = run_recorder
        self.question_router = question_router

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
            is_starter_request = (
                not request.message.strip()
                or request.request_context.metadata.get("starter_ui_request", False)
            )
            if is_starter_request:
                # Starter UI has no user-authored turn and must not create an
                # empty conversation or an auditable Agent Run.
                async for chunk in super().handle_stream(request):
                    yield chunk
                return
            # Agent Run has a foreign key to the conversation. Create an empty
            # owner-scoped row before recording the run; Agent appends messages
            # through ConversationStore after this boundary is established.
            conversation = await self.agent.conversation_store.get_conversation(
                conversation_id, user
            )
            if not conversation:
                conversation = Conversation(
                    id=conversation_id,
                    user=user,
                    metadata={
                        "dataset_version_id": DATASET_VERSION,
                        "metric_version": METRIC_VERSION,
                        "working_memory": {},
                    },
                )
                await self.agent.conversation_store.update_conversation(
                    conversation
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

            # Route only user-authored analysis turns. Workflow commands such as
            # /help must remain owned by Vanna's workflow handler.
            if self.question_router and request.message.strip().lower() not in {
                "/help",
                "help",
                "/h",
            }:
                memory = WorkingMemory.from_mapping(
                    conversation.metadata.get("working_memory")
                )
                retrieval_question = memory.retrieval_context(request.message)
                usage.set_catalog_context(retrieval_question, memory.as_dict())
                selection = self.question_router.retriever.retrieve(
                    retrieval_question, user
                )
                usage.record_catalog(selection.trace.as_dict())
                route = self.question_router.classify(
                    request.message,
                    user=user,
                    selection=selection,
                    conversation_state=memory.as_dict(),
                )
                updated_memory = memory.apply(request.message, route)
                result_contract = ResultContract.from_selection(
                    selection,
                    request.message,
                    updated_memory.time_range,
                    catalog=self.question_router.retriever.catalog,
                )
                # These fields are server-derived and overwrite any client
                # metadata before the Agent constructs ToolContext.
                request.request_context.metadata.update(
                    result_contract.as_tool_metadata()
                )
                request.request_context.metadata["prompt_version"] = PROMPT_VERSION
                usage.record_catalog(
                    {
                        **selection.trace.as_dict(),
                        "prompt_version": PROMPT_VERSION,
                        "result_contract": result_contract.as_evidence(),
                    }
                )
                usage.set_catalog_context(
                    retrieval_question, updated_memory.as_dict()
                )
                conversation.metadata["working_memory"] = updated_memory.as_dict()
                conversation.metadata["dataset_version_id"] = DATASET_VERSION
                conversation.metadata["metric_version"] = METRIC_VERSION
                await self.agent.conversation_store.update_conversation(conversation)

                if not route.should_generate_sql:
                    detail = route.clarification or "当前请求无法在受控数据范围内回答。"
                    conversation.add_message(Message(role="user", content=request.message))
                    conversation.add_message(
                        Message(
                            role="assistant",
                            content=detail,
                            metadata={"question_route": route.as_dict()},
                        )
                    )
                    await self.agent.conversation_store.update_conversation(conversation)
                    if route.state in {
                        "missing_time",
                        "missing_metric",
                        "missing_comparison",
                    }:
                        usage.terminate("clarification_required")
                        title = "需要补充信息"
                    else:
                        usage.terminate("unsupported_request")
                        title = "请求未执行"
                    yield self._budget_chunk(
                        conversation_id, request_id, title, detail
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
