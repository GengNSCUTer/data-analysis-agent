"""Project-owned chat boundary for budgets and persistent Agent runs."""

from __future__ import annotations

import uuid
from time import perf_counter

from vanna.components import RichTextComponent, SimpleTextComponent, StatusCardComponent, UiComponent
from vanna.core.llm import LlmMessage, LlmRequest
from vanna.core.storage import Conversation, Message
from vanna.servers.base import ChatHandler
from vanna.servers.base.models import ChatRequest, ChatStreamChunk

from .budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from .metric_context import OLIST_WORKSPACE, PROMPT_VERSION
from .question_router import QuestionRouter
from .query_plan import QueryPlan
from .run_recorder import PostgresRunRecorder
from .semantic_catalog import ResultContract
from .working_memory import WorkingMemory
from .workspace import WorkspaceProfile


TOOL_FREE_SYSTEM_PROMPT = """
你是一个通用业务解释助手，当前请求没有数据库工具。

你可以解释通用业务概念、分析方法、指标的一般含义和经营建议，但不得声称自己查询了当前
工作区数据，也不得编造当前数据中的数字、排名、趋势或结论。如果用户明确要求当前数据事实，
请说明需要进入受控数据查询流程，并请用户提供指标、时间范围或维度。回答应简洁、中文、可执行；
如果问题属于当前工作区的指标定义，应优先依赖服务器提供的 Semantic Catalog，而不是猜测口径。
""".strip()


class BudgetedChatHandler(ChatHandler):
    """Attach one isolated budget tracker and run record to each chat request."""

    def __init__(
        self,
        agent,
        budget: RequestBudget,
        run_recorder: PostgresRunRecorder,
        question_router: QuestionRouter | None = None,
        workspace: WorkspaceProfile | None = None,
    ):
        super().__init__(agent)
        self.budget = budget
        self.run_recorder = run_recorder
        self.question_router = question_router
        self.workspace = workspace or OLIST_WORKSPACE

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
                        "workspace_id": self.workspace.workspace_id,
                        "dataset_version_id": self.workspace.dataset_version_id,
                        "metric_version": self.workspace.metric_version,
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
                dataset_version_id=self.workspace.dataset_version_id,
                metric_version=self.workspace.metric_version,
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
                route_started_at = perf_counter()
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
                query_plan = None
                if route.should_generate_sql:
                    query_plan = QueryPlan.from_selection(
                        selection,
                        request.message,
                        route,
                        updated_memory.as_dict(),
                    )
                    usage.set_query_plan(
                        query_plan.as_dict(), query_plan.prompt_context()
                    )
                result_contract = ResultContract.from_selection(
                    selection,
                    request.message,
                    updated_memory.time_range,
                    catalog=self.question_router.retriever.catalog,
                    required_result_columns=(
                        query_plan.required_result_columns if query_plan else None
                    ),
                )
                # These fields are server-derived and overwrite any client
                # metadata before the Agent constructs ToolContext.
                request.request_context.metadata.update(
                    result_contract.as_tool_metadata()
                )
                catalog_context = selection.prompt
                if query_plan is not None:
                    catalog_context += query_plan.prompt_context()
                request.request_context.metadata["catalog_context"] = catalog_context[: self.budget.max_context_chars]
                request.request_context.metadata["prompt_version"] = PROMPT_VERSION
                catalog_evidence = {
                    **selection.trace.as_dict(),
                    "prompt_version": PROMPT_VERSION,
                    "result_contract": result_contract.as_evidence(),
                }
                if query_plan is not None:
                    request.request_context.metadata["query_plan"] = query_plan.as_dict()
                    catalog_evidence["query_plan"] = query_plan.as_dict()
                usage.record_catalog(catalog_evidence)
                usage.set_catalog_context(
                    retrieval_question, updated_memory.as_dict()
                )
                request.request_context.metadata["route_intent"] = route.intent
                request.request_context.metadata["route_evidence_mode"] = route.evidence_mode
                request.request_context.metadata["route_confidence"] = route.confidence
                conversation.metadata["working_memory"] = updated_memory.as_dict()
                conversation.metadata["workspace_id"] = self.workspace.workspace_id
                conversation.metadata["dataset_version_id"] = self.workspace.dataset_version_id
                conversation.metadata["metric_version"] = self.workspace.metric_version
                await self.agent.conversation_store.update_conversation(conversation)
                usage.record_timing(
                    "route_catalog",
                    int((perf_counter() - route_started_at) * 1000),
                )

                if not route.should_generate_sql:
                    detail = route.direct_answer or route.clarification or "当前请求无法在受控数据范围内回答。"
                    if route.should_use_tool_free_llm:
                        detail = await self._tool_free_answer(
                            request,
                            route,
                            updated_memory,
                            user,
                        )
                    conversation.add_message(Message(role="user", content=request.message))
                    conversation.add_message(
                        Message(
                            role="assistant",
                            content=detail,
                            metadata={"question_route": route.as_dict()},
                        )
                    )
                    await self.agent.conversation_store.update_conversation(conversation)
                    if route.state == "catalog_answered":
                        usage.terminate("catalog_answered")
                        yield self._text_chunk(
                            conversation_id, request_id, detail
                        )
                        return
                    if route.state == "help" or route.should_use_tool_free_llm:
                        usage.terminate("completed")
                        yield self._text_chunk(
                            conversation_id, request_id, detail
                        )
                        return
                    if route.needs_clarification:
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
            if usage.result_summary:
                try:
                    await self._persist_result_summary(
                        conversation_id,
                        user,
                        usage.result_summary,
                    )
                except Exception:
                    # A result-memory write must never turn an already
                    # validated answer into an SSE failure.  The run still
                    # records the error type for diagnosis.
                    usage.error_type = "result_summary_persistence"
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

    @staticmethod
    def _text_chunk(
        conversation_id: str, request_id: str, content: str
    ) -> ChatStreamChunk:
        """Build a Markdown-capable deterministic response without an LLM call."""
        component = UiComponent(
            rich_component=RichTextComponent(content=content, markdown=True),
            simple_component=SimpleTextComponent(text=content),
        )
        return ChatStreamChunk.from_component(component, conversation_id, request_id)

    async def _tool_free_answer(
        self,
        request: ChatRequest,
        route,
        memory: WorkingMemory,
        user,
    ) -> str:
        """Answer a non-data request without exposing the SQL tool registry.

        The same configured model service may be reused, but the request is
        sent directly with ``tools=None`` and a system prompt that forbids
        claims about current database facts.  This keeps the data Agent as the
        only component allowed to call ``run_sql``.
        """
        prompt = request.message.strip()
        if route.intent == "result_followup" and memory.previous_result_summary:
            prompt = (
                "可信的上一轮结果摘要如下（只能解释其中已有信息，不得补造新数字）：\n"
                f"{memory.previous_result_summary[:1000]}\n\n"
                f"当前追问：{prompt}"
            )
        llm_request = LlmRequest(
            messages=[LlmMessage(role="user", content=prompt)],
            user=user,
            tools=None,
            stream=False,
            temperature=0.0,
            max_tokens=min(self.budget.max_output_tokens, 800),
            system_prompt=TOOL_FREE_SYSTEM_PROMPT,
            metadata={
                "purpose": "tool_free_response",
                "intent": route.intent,
                "conversation_id": request.conversation_id,
                "request_id": request.request_id,
            },
        )
        sender = getattr(self.agent, "_send_llm_request", None)
        if not callable(sender):
            raise RuntimeError("agent does not expose the project LLM request boundary")
        response = await sender(llm_request)
        content = (response.content or "").strip()
        if not content:
            return "当前无法生成通用解释；如果你要查询实际数据，请补充指标和时间范围。"
        return content

    async def _persist_result_summary(self, conversation_id: str, user, summary: str) -> None:
        """Persist only the trusted, bounded result summary into working memory."""
        conversation = await self.agent.conversation_store.get_conversation(
            conversation_id, user
        )
        if conversation is None:
            return
        memory = WorkingMemory.from_mapping(conversation.metadata.get("working_memory"))
        conversation.metadata["working_memory"] = memory.with_result_summary(summary).as_dict()
        await self.agent.conversation_store.update_conversation(conversation)
