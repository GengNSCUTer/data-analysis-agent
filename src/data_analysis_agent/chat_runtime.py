"""Project-owned chat boundary for budgets and persistent Agent runs."""

from __future__ import annotations

import uuid
import inspect
import re
from time import perf_counter

from vanna.components import RichTextComponent, SimpleTextComponent, StatusCardComponent, UiComponent
from vanna.core.llm import LlmMessage, LlmRequest, LlmResponse
from vanna.core.storage import Conversation, Message
from vanna.servers.base import ChatHandler
from vanna.servers.base.models import ChatRequest, ChatStreamChunk

from .analysis_lifecycle import prepare_turn_memory
from .catalog_grounding import ConstrainedCatalogGrounder, grounded_retrieval_question
from .candidate_sql_generator import build_runtime_candidate_request
from .question_router import (
    _is_clarification_continuation,
    expand_retrieval_question,
    is_result_refinement,
)
from .budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from .chart_contract import ChartContract, ChartContractError
from .conversation_store import ConversationStateConflict
from .metric_context import OLIST_WORKSPACE, PROMPT_VERSION
from .question_router import QuestionRouter
from .query_plan import QueryPlan
from .result_artifact import ResultArtifact
from .result_artifact_store import ResultArtifactIntegrityError, ResultArtifactStore
from .run_recorder import PostgresRunRecorder
from .semantic_catalog import ResultContract
from .working_memory import WorkingMemory
from .workspace import WorkspaceProfile
from .visualization import build_server_chart_component


def _safe_artifact_manifest(manifest: dict[str, object]) -> dict[str, object]:
    allowed = {
        "version", "artifact_id", "workspace_id", "dataset_version",
        "metric_version", "row_count", "columns", "csv_sha256", "csv_bytes",
        "csv_ref", "plotly_sha256", "plotly_bytes", "plotly_ref",
    }
    return {key: manifest[key] for key in allowed if key in manifest}


_FOLLOWUP_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def followup_adds_numbers(answer: str, summary: str) -> bool:
    """True when a result explanation contains a number absent from the summary."""

    allowed = set(_FOLLOWUP_NUMBER.findall(summary))
    return any(number not in allowed for number in _FOLLOWUP_NUMBER.findall(answer))


def constrain_followup_answer(answer: str, summary: str) -> str:
    if not followup_adds_numbers(answer, summary):
        return answer
    return "这次只复述已验证摘要，不补充摘要以外的数字。\n" + summary.strip()


TOOL_FREE_SYSTEM_PROMPT = """
你是一个通用业务解释助手，当前请求没有数据库工具。

你可以解释通用业务概念、分析方法、指标的一般含义和经营建议，但不得声称自己查询了当前
工作区数据，也不得编造当前数据中的数字、排名、趋势或结论。如果用户明确要求当前数据事实，
请说明需要进入受控数据查询流程，并请用户提供指标、时间范围或维度。回答应简洁、中文、可执行；
如果问题属于当前工作区的指标定义，应优先依赖服务器提供的 Semantic Catalog，而不是猜测口径。
""".strip()

RESULT_FOLLOWUP_SYSTEM_PROMPT = """
你只能解释上一轮已验证结果摘要。只能使用摘要里已经出现的数字和事实。
不要计算、估计或补充任何新数字，也不要把摘要没有写明的原因说成确定事实。
若摘要不足以回答，直接说明依据不足。回答应简洁、中文。
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
        result_artifact_store: ResultArtifactStore | None = None,
        catalog_grounder: ConstrainedCatalogGrounder | None = None,
    ):
        super().__init__(agent)
        self.budget = budget
        self.run_recorder = run_recorder
        self.question_router = question_router
        self.workspace = workspace or OLIST_WORKSPACE
        self.result_artifact_store = result_artifact_store
        self.catalog_grounder = catalog_grounder

    async def _get_conversation(self, conversation_id: str, user):
        """Load a conversation with workspace scope while preserving test-store compatibility."""
        getter = self.agent.conversation_store.get_conversation
        parameters = inspect.signature(getter).parameters
        if "workspace_id" in parameters or len(parameters) >= 3:
            return await getter(conversation_id, user, self.workspace.workspace_id)
        # Small in-memory stores used by focused runtime tests implement the
        # pre-M2 two-argument protocol. They have no cross-workspace data and
        # remain valid fixtures without weakening the Postgres contract.
        return await getter(conversation_id, user)

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
        chart_contract: ChartContract | None = None
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
            conversation = await self._get_conversation(conversation_id, user)
            if not conversation:
                conversation = Conversation(
                    id=conversation_id,
                    user=user,
                    metadata={
                        "workspace_id": self.workspace.workspace_id,
                        "state_version": 0,
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

            memory, invalidation_notice = prepare_turn_memory(
                conversation, self.workspace
            )
            if invalidation_notice:
                usage.suppress_semantic_history_summary = True
                await self.agent.conversation_store.update_conversation(conversation)
                yield self._budget_chunk(
                    conversation_id,
                    request_id,
                    "分析上下文已失效",
                    invalidation_notice,
                )

            # Route only user-authored analysis turns. Workflow commands such as
            # /help must remain owned by Vanna's workflow handler.
            if self.question_router and request.message.strip().lower() not in {
                "/help",
                "help",
                "/h",
            }:
                route_started_at = perf_counter()
                route_state = memory.as_dict()
                # A new question that already names a metric must not be
                # searched together with the previous clarification text.
                # Otherwise "本月 GMV" stays attached to a later order-count
                # question and the result contract demands both columns.
                retrieval_question = request.message.strip()
                selection = self.question_router.retriever.retrieve(
                    retrieval_question, user
                )
                if (
                    not selection.trace.selected_metrics
                    and memory.pending_question
                ):
                    retrieval_question = memory.retrieval_context(request.message)
                    if retrieval_question != request.message.strip():
                        selection = self.question_router.retriever.retrieve(
                            retrieval_question, user
                        )
                has_metric_match = bool(selection.trace.selected_metrics)
                expanded_question = expand_retrieval_question(
                    retrieval_question,
                    route_state,
                    has_metric_match=has_metric_match,
                )
                if expanded_question != retrieval_question:
                    retrieval_question = expanded_question
                    selection = self.question_router.retriever.retrieve(
                        retrieval_question, user
                    )
                if (
                    is_result_refinement(
                        request.message,
                        route_state,
                        has_metric_match=has_metric_match,
                    )
                    and memory.dimensions
                    and not QueryPlan._dimensions(request.message, selection.metrics)
                ):
                    route_state = {
                        **route_state,
                        "resolved_dimensions": list(memory.dimensions),
                    }
                usage.set_catalog_context(retrieval_question, memory.as_dict())
                usage.record_catalog(selection.trace.as_dict())
                route = self.question_router.classify(
                    request.message,
                    user=user,
                    selection=selection,
                    conversation_state=route_state,
                )
                resolved_dimensions: tuple[str, ...] = ()
                if route.should_generate_sql:
                    if (
                        not selection.trace.selected_metrics
                        and self.catalog_grounder is not None
                        and not _is_clarification_continuation(route_state)
                    ):
                        grounded = await self._ground_unmatched_question(
                            request.message, user
                        )
                        if grounded is not None:
                            selection = self.question_router.retriever.retrieve(
                                grounded_retrieval_question(
                                    request.message,
                                    grounded,
                                    self.question_router.retriever.catalog,
                                ),
                                user,
                            )
                            if any(
                                metric_id in selection.trace.selected_metrics
                                for metric_id in grounded.metric_ids
                            ):
                                resolved_dimensions = grounded.dimensions
                                route_state = {
                                    **route_state,
                                    "resolved_dimensions": list(resolved_dimensions),
                                }
                                usage.record_catalog(
                                    {"catalog_grounding": grounded.as_dict()}
                                )
                    route = self.question_router.admit_sql_request(
                        request.message,
                        user=user,
                        selection=selection,
                        conversation_state=route_state,
                    )
                updated_memory = memory.apply(request.message, route)
                query_plan = None
                if route.should_generate_sql:
                    plan_state = updated_memory.as_dict()
                    if resolved_dimensions:
                        plan_state["resolved_dimensions"] = list(resolved_dimensions)
                    query_plan = QueryPlan.from_selection(
                        selection,
                        request.message,
                        route,
                        plan_state,
                    )
                    # QueryPlan, not assistant prose or client metadata, owns
                    # the resolved grouping for this turn.  Persist it only as
                    # observable conversation state; future SQL generation
                    # does not silently inherit it.
                    updated_memory = updated_memory.with_query_plan_dimensions(
                        request.message, query_plan.dimensions
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
                    requested_dimensions=(
                        query_plan.dimensions if query_plan else ()
                    ),
                )
                chart_contract = (
                    ChartContract.from_query_plan(
                        request.message, query_plan, result_contract
                    )
                    if query_plan is not None
                    else None
                )
                # These fields are server-derived and overwrite any client
                # metadata before the Agent constructs ToolContext.
                request.request_context.metadata.update(
                    result_contract.as_tool_metadata()
                )
                request.request_context.metadata["workspace_id"] = self.workspace.workspace_id
                catalog_context = selection.prompt
                if query_plan is not None:
                    catalog_context += query_plan.prompt_context()
                if chart_contract is not None:
                    request.request_context.metadata["chart_contract"] = (
                        chart_contract.as_tool_metadata()
                    )
                    catalog_context += chart_contract.prompt_context()
                request.request_context.metadata["catalog_context"] = catalog_context[: self.budget.max_context_chars]
                request.request_context.metadata["prompt_version"] = PROMPT_VERSION
                catalog_evidence = {
                    **selection.trace.as_dict(),
                    "prompt_version": PROMPT_VERSION,
                    "result_contract": result_contract.as_evidence(),
                    "route_intent": route.intent,
                    "route_evidence_mode": route.evidence_mode,
                    "route_state": route.state,
                    "route_requires_database": route.requires_database,
                    "route_reason_code": route.reason_code,
                    "working_memory": {
                        "metric_ids": list(updated_memory.metric_ids),
                        "time_range": dict(updated_memory.time_range)
                        if updated_memory.time_range
                        else None,
                        "dimensions": list(updated_memory.dimensions),
                        "pending_missing": list(updated_memory.pending_missing),
                        "previous_result_summary_present": bool(
                            updated_memory.previous_result_summary
                        ),
                    },
                }
                if query_plan is not None:
                    request.request_context.metadata["query_plan"] = query_plan.as_dict()
                    catalog_evidence["query_plan"] = query_plan.as_dict()
                    candidate_request = build_runtime_candidate_request(
                        question=request.message,
                        selection=selection,
                        query_plan=query_plan,
                        result_contract=result_contract,
                        workspace=self.workspace,
                        route=route,
                    )
                    # A shared SQL-component fingerprint, not a replacement
                    # for the default conversational provider or its tools.
                    request.request_context.metadata["candidate_request"] = (
                        candidate_request.safe_evidence()
                    )
                    catalog_evidence["candidate_request"] = candidate_request.safe_evidence()
                if chart_contract is not None:
                    catalog_evidence["chart_contract"] = chart_contract.as_evidence()
                usage.record_catalog(catalog_evidence)
                usage.set_catalog_context(
                    retrieval_question,
                    updated_memory.as_dict(),
                    catalog_prompt=selection.prompt,
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

                if chart_contract is not None and not chart_contract.safe_to_visualize and chart_contract.requested:
                    detail = chart_contract.clarification or "当前图表请求无法在受控范围内执行。"
                    conversation.add_message(Message(role="user", content=request.message))
                    conversation.add_message(
                        Message(
                            role="assistant",
                            content=detail,
                            metadata={"chart_contract": chart_contract.as_evidence()},
                        )
                    )
                    await self.agent.conversation_store.update_conversation(conversation)
                    if chart_contract.status == "clarification_required":
                        usage.terminate("clarification_required")
                        title = "需要补充图表信息"
                    else:
                        usage.terminate("unsupported_request")
                        title = "图表请求未执行"
                    yield self._budget_chunk(conversation_id, request_id, title, detail)
                    return

                if not route.should_generate_sql:
                    detail = route.direct_answer or route.clarification or "当前请求无法在受控数据范围内回答。"
                    streamed_tool_free = False
                    if route.should_use_tool_free_llm:
                        detail = ""
                        async for piece in self._tool_free_answer(
                            request,
                            route,
                            updated_memory,
                            user,
                        ):
                            if isinstance(piece, str):
                                detail = piece
                            else:
                                streamed_tool_free = True
                                yield piece
                        if not detail:
                            detail = "当前无法生成通用解释；如果你要查询实际数据，请补充指标和时间范围。"
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
                        if not streamed_tool_free:
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
                if (
                    chart_contract is not None
                    and chart_contract.safe_to_visualize
                    and usage.result_contract_satisfied
                    and not usage.deterministic_chart_rendered
                ):
                    frame = usage.take_validated_chart_frame()
                    if frame is not None:
                        try:
                            component = build_server_chart_component(chart_contract, frame)
                        except (ChartContractError, ValueError) as exc:
                            # A valid pre-query contract can still reject the
                            # actual result shape (for example >200 chart rows).
                            # Do not ask the model to repair or improvise a chart.
                            yield self._budget_chunk(
                                conversation_id,
                                request_id,
                                "图表未生成",
                                str(exc),
                            )
                            usage.record_catalog(
                                {"deterministic_chart": {"status": "rejected"}}
                            )
                        else:
                            self._persist_chart_artifact(
                                user=user,
                                request_id=request_id,
                                chart_component=component,
                                usage=usage,
                            )
                            usage.mark_deterministic_chart_rendered()
                            usage.record_catalog(
                                {
                                    "deterministic_chart": {
                                        "status": "rendered",
                                        "chart_contract_version": chart_contract.version,
                                    }
                                }
                            )
                            yield ChatStreamChunk.from_component(
                                component, conversation_id, request_id
                            )
            try:
                await self._persist_history_summary(conversation_id, user, usage)
            except Exception:
                usage.error_type = "history_summary_persistence"
            if usage.result_summary:
                try:
                    await self._persist_result_artifact(
                        conversation_id,
                        user,
                        request_id,
                        usage,
                    )
                except Exception:
                    # A result-memory write must never turn an already
                    # validated answer into an SSE failure.  The run still
                    # records the error type for diagnosis.
                    usage.error_type = "result_summary_persistence"
            if usage.result_contract_satisfied:
                # The model's natural-language answer and its tool call are
                # separate SSE components.  Give the user a small, explicit
                # provenance marker only after the server has validated the
                # result artifact; never attach this marker to a candidate
                # that merely executed or to a clarification/refusal.
                yield self._trusted_result_evidence_chunk(
                    conversation_id, request_id, usage
                )
            if (
                usage.termination_reason == "running"
                and usage.llm_rounds_used >= self.budget.max_tool_iterations
                and usage.last_response_had_tool_calls
            ):
                usage.terminate("tool_budget_exhausted")
            usage.finish()
        except ConversationStateConflict:
            usage.terminate("state_conflict")
            yield self._budget_chunk(
                conversation_id,
                request_id,
                "会话状态已更新",
                "当前会话已有新消息，请刷新后重试；本次请求未覆盖已有分析状态。",
            )
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

    @staticmethod
    def _trusted_result_evidence_chunk(
        conversation_id: str, request_id: str, usage: BudgetUsage
    ) -> ChatStreamChunk:
        """Render a compact, server-derived answer-provenance card.

        This intentionally exposes only Catalog/ResultContract identity—not
        SQL text, result rows, sample values, user text, or model reasoning.
        It helps users distinguish an answer based on a trusted result from
        tool-free explanation without turning the card into another export
        channel.
        """
        trace = usage.catalog_trace or {}
        contract = trace.get("result_contract")
        if not isinstance(contract, dict):
            contract = {}
        metric_ids = contract.get("metric_ids") or trace.get("selected_metrics") or ()
        columns = contract.get("required_result_columns") or ()
        metric_text = "、".join(str(item) for item in metric_ids[:8]) or "受控指标"
        column_text = "、".join(str(item) for item in columns[:8]) or "受控结果字段"
        dataset_version = str(contract.get("dataset_version") or trace.get("dataset_version") or "未知版本")
        metric_version = str(contract.get("metric_version") or trace.get("metric_version") or "未知版本")
        content = (
            "本回答引用了本轮已通过服务器结果合同的查询结果。"
            f"指标：{metric_text}；结果字段：{column_text}；"
            f"数据版本：{dataset_version}；指标版本：{metric_version}。"
        )
        plan = trace.get("query_plan") if isinstance(trace.get("query_plan"), dict) else {}
        requirements = plan.get("attribution_requirements") or []
        if any(
            isinstance(item, dict) and item.get("policy_mode") == "multi_membership"
            for item in requirements
        ):
            content += "按品类或支付方式拆开时，同一订单可以进入多个分组，各组合计可以大于总体。"
        if any(str(item).startswith("filter_customer_state:") for item in plan.get("warnings") or []):
            content += "客户州条件是筛选，不是按州分组。"
        component = UiComponent(
            rich_component=StatusCardComponent(
                title="本次回答的数据依据",
                status="success",
                description=content,
                icon="✓",
            ),
            simple_component=SimpleTextComponent(text=content),
        )
        return ChatStreamChunk.from_component(component, conversation_id, request_id)

    async def _tool_free_answer(
        self,
        request: ChatRequest,
        route,
        memory: WorkingMemory,
        user,
    ):
        """Answer a non-data request without exposing the SQL tool registry.

        Text deltas are yielded as chat chunks. The last item is the final
        string stored on the conversation. The request uses ``tools=None`` and
        a system prompt that forbids claims about current database facts.
        """
        prompt = request.message.strip()
        summary = memory.previous_result_summary or ""
        hold_followup = route.intent == "result_followup" and bool(summary.strip())
        system_prompt = (
            RESULT_FOLLOWUP_SYSTEM_PROMPT if hold_followup else TOOL_FREE_SYSTEM_PROMPT
        )
        if hold_followup:
            prompt = (
                "可信的上一轮结果摘要如下。只能使用摘要里已经出现的数字；"
                "不要计算、估计或补充任何新数字：\n"
                f"{summary[:1000]}\n\n"
                f"当前追问：{prompt}"
            )
        llm_request = LlmRequest(
            messages=[LlmMessage(role="user", content=prompt)],
            user=user,
            tools=None,
            stream=True,
            temperature=0.0,
            max_tokens=min(self.budget.max_output_tokens, 800),
            system_prompt=system_prompt,
            metadata={
                "purpose": "tool_free_response",
                "intent": route.intent,
                "conversation_id": request.conversation_id,
                "request_id": request.request_id,
            },
        )
        streamer = getattr(self.agent, "_handle_streaming_response", None)
        streaming = bool(getattr(getattr(self.agent, "config", None), "stream_responses", False))
        if streaming and callable(streamer):
            content = ""
            async for piece in streamer(llm_request):
                if isinstance(piece, LlmResponse):
                    content = (piece.content or "").strip()
                elif not hold_followup:
                    yield ChatStreamChunk.from_component(
                        piece, request.conversation_id or "", request.request_id or ""
                    )
            if hold_followup:
                content = constrain_followup_answer(content, summary)
            yield content
            return
        sender = getattr(self.agent, "_send_llm_request", None)
        if not callable(sender):
            raise RuntimeError("agent does not expose the project LLM request boundary")
        response = await sender(llm_request)
        content = (response.content or "").strip()
        if hold_followup:
            content = constrain_followup_answer(content, summary)
        yield content

    async def _ground_unmatched_question(self, question: str, user):
        """Align an unmatched data question to Catalog ids, or keep clarifying."""

        if self.catalog_grounder is None:
            return None
        try:
            return await self.catalog_grounder.ground(question, user)
        except Exception:
            return None

    async def _persist_result_artifact(
        self, conversation_id: str, user, request_id: str, usage: BudgetUsage
    ) -> None:
        """Persist a small trusted-result replay artifact into working memory."""
        conversation = await self._get_conversation(conversation_id, user)
        if conversation is None:
            return
        memory = WorkingMemory.from_mapping(conversation.metadata.get("working_memory"))
        artifact = ResultArtifact.from_trusted_usage(
            request_id=request_id,
            summary=usage.result_summary or "",
            catalog_trace=usage.catalog_trace,
            chart_rendered=usage.deterministic_chart_rendered,
        )
        conversation.metadata["working_memory"] = memory.with_result_artifact(
            artifact
        ).as_dict()
        await self.agent.conversation_store.update_conversation(conversation)

    def _persist_chart_artifact(
        self, *, user, request_id: str, chart_component: UiComponent, usage: BudgetUsage
    ) -> None:
        if self.result_artifact_store is None:
            return
        trace = usage.catalog_trace or {}
        external = trace.get("result_artifact")
        if not isinstance(external, dict):
            return
        rich = chart_component.rich_component
        figure = getattr(rich, "data", None)
        if not isinstance(figure, dict):
            return
        try:
            manifest = self.result_artifact_store.save_plotly_json(
                artifact_id=str(external.get("artifact_id")),
                user_id=user.id,
                workspace_id=self.workspace.workspace_id,
                figure=figure,
            )
        except (FileNotFoundError, PermissionError, ResultArtifactIntegrityError, ValueError):
            return
        usage.record_catalog({"result_artifact": _safe_artifact_manifest(manifest)})

    async def _persist_history_summary(
        self, conversation_id: str, user, usage: BudgetUsage
    ) -> None:
        """Persist only the bounded semantic summary in the owner-scoped session."""
        if not usage.history_summary or not usage.history_summary_text:
            return
        conversation = await self._get_conversation(conversation_id, user)
        if conversation is None:
            return
        conversation.metadata["history_summary"] = {
            **usage.history_summary,
            "text": usage.history_summary_text,
        }
        await self.agent.conversation_store.update_conversation(conversation)
