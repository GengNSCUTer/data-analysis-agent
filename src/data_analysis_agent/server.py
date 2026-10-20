"""Installed FastAPI product entry point; serves its bundled Web Component."""

from __future__ import annotations

import os
import asyncio
import secrets
import logging
from pathlib import Path
import uvicorn
from dotenv import load_dotenv
from dataclasses import replace

load_dotenv(Path(os.getenv("DATA_ANALYSIS_ENV_FILE", ".env")))
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from data_analysis_agent.demo_session import SESSION_COOKIE_NAME
from data_analysis_agent.export_audit import ExportAudit
from data_analysis_agent.local_auth import (
    LOCAL_SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS as LOCAL_SESSION_MAX_AGE_SECONDS,
    AccountStore,
    ProductIdentityResolver,
    SignedToken,
    allowed_embed_origins,
    ensure_bootstrap_account,
    issue_embed_token,
    issue_session,
    normalize_origin,
    password_matches,
)
from data_analysis_agent.copilot_contract import (
    BootstrapContractError,
    CopilotBootstrapRequest,
    WorkspaceAccessRegistry,
    WorkspaceGrant,
    build_copilot_bootstrap,
)
from data_analysis_agent.budget import (
    BudgetedToolRegistry,
    BudgetSafetyMiddleware,
    RequestBudget,
)
from data_analysis_agent.semantic_intent import SemanticIntentInterpreter
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.context_builder import ContextBudgetFilter
from data_analysis_agent.analysis_context import AnalysisContextBuilder
from data_analysis_agent.history_summary import build_llm_summary_provider
from data_analysis_agent.analysis_lifecycle import (
    CLEAR_ANALYSIS_NOTICE,
    VERSION_MISMATCH_NOTICE,
    analysis_scope_is_current,
    clear_active_analysis_state,
    current_result_artifact,
)
from data_analysis_agent.working_memory import WorkingMemory, replayable_chart_refs
from data_analysis_agent.conversation_store import (
    ConversationStateConflict,
    InvalidConversationId,
    PostgresConversationStore,
)
from data_analysis_agent.run_recorder import PostgresRunRecorder
from fastapi.staticfiles import StaticFiles

from data_analysis_agent.runtime_workspace import load_runtime_workspace, build_runtime_system_prompt
from data_analysis_agent.llm_observability import ObservedLlmService
from data_analysis_agent.postgres_runner import (
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.result_artifact_store import (
    ResultArtifactExpired,
    ResultArtifactIntegrityError,
    ResultArtifactStore,
)
from data_analysis_agent.runtime_control import RuntimeLimits, RuntimeControl, ManagedChatHandler, cleanup_query_files
from data_analysis_agent.semantic_catalog import (
    CatalogContextEnhancer,
    CatalogLoader,
    CatalogRetriever,
)
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.runtime_diagnostics import RuntimeDiagnostics
from data_analysis_agent.user_memory import UserMemoryStore, ALLOWED_PREFERENCES
from data_analysis_agent.trusted_workflow import AnalyticsWorkflowHandler
from vanna import Agent
from vanna.core.agent.config import AgentConfig
from vanna.core.enhancer import DefaultLlmContextEnhancer
from vanna.core.llm import LlmMessage, LlmRequest
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.core.user import RequestContext
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.integrations.openai import OpenAILlmService
from vanna.servers.fastapi.routes import register_chat_routes
from data_analysis_agent.trusted_sql_tool import (
    LlmRepairCandidateProvider,
    TrustedRunSqlTool,
)
from data_analysis_agent.runtime_paths import RESOURCES, state_directory, session_secret


EMBEDDABLE_COPILOT_PATH = RESOURCES / "embeddable_copilot_shell.html"
WEB_COMPONENT_DIST = RESOURCES / "static"
QUERY_RESULTS_DIRECTORY = Path(
    os.getenv("VANNA_QUERY_RESULTS_DIR", str(state_directory() / "query-results"))
)
RESULT_ARTIFACT_DIRECTORY = Path(
    os.getenv(
        "DATA_ANALYSIS_RESULT_ARTIFACT_ROOT",
        str(state_directory() / "result-artifacts"),
    )
)
DEMO_PORT = int(os.getenv("DATA_ANALYSIS_DEMO_PORT", "32010"))


class PreferenceRequest(BaseModel):
    memory_kind: str
    value: str


class SaveAnalysisRequest(BaseModel):
    conversation_id: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    request_id: str


class EmbedTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str
    surface_id: str = "embedded-copilot"


def _cookie_secure(request: Request) -> bool:
    configured = os.getenv("DATA_ANALYSIS_COOKIE_SECURE", "").lower()
    if configured in {"1", "true", "yes"}:
        return True
    if configured in {"0", "false", "no"}:
        return False
    return request.url.scheme == "https"


def audit_response(audit: dict) -> dict:
    """Expose only analyst-safe audit evidence to the embedded host page."""
    return {
        "request_id": audit["request_id"],
        "question": audit["question"] or "未记录问题（旧审计记录）",
        "role": audit["user_role"],
        "status": audit["policy_status"],
        "reason": audit["policy_reason"],
        "final_sql": audit["final_sql"],
        "dataset_version": audit["dataset_version_id"],
        "metric_version": audit["metric_version"],
        "elapsed_ms": audit["elapsed_ms"],
        "row_count": audit["row_count"],
        "repair_evidence": audit.get("repair_evidence"),
        "created_at": audit["created_at"],
    }


def create_app(*, llm_provider=None) -> FastAPI:
    os.environ.setdefault("PGCONNECT_TIMEOUT", "5")
    os.environ.setdefault("PGOPTIONS", "-c statement_timeout=10000 -c lock_timeout=5000")
    limits = RuntimeLimits.from_environment()
    control = RuntimeControl(limits)
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key and llm_provider is None:
        raise RuntimeError(
            "SILICONFLOW_API_KEY is required; set it in the environment or DATA_ANALYSIS_ENV_FILE."
        )
    if not (WEB_COMPONENT_DIST / "vanna-components.js").is_file():
        raise RuntimeError(
            "Build frontends/webcomponent before starting the trusted demo."
        )

    model_name = os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V3.2")
    runtime_workspace = load_runtime_workspace()
    workspace = runtime_workspace.profile
    settings = replace(PostgresConnectionSettings.from_environment(),
                       reader_user=workspace.reader_role, writer_user=workspace.writer_role)
    runner = SecurePostgresRunner(
        settings=settings,
        model_name=model_name,
        result_validator=ResultValidator(settings.max_rows),
        workspace=workspace,
    )
    budget = RequestBudget.from_environment()
    conversation_store = PostgresConversationStore(
        settings, default_workspace_id=workspace.workspace_id,
        default_dataset_version=workspace.dataset_version, default_metric_version=workspace.metric_version,
    )
    run_recorder = PostgresRunRecorder(settings, model_name=model_name)
    agent_memory = DemoAgentMemory()
    # A filtered category analysis needs orders, items, customers, products and
    # category translation together; four tables cannot express that scope.
    catalog_retriever = CatalogRetriever(CatalogLoader(workspace).load(), max_tables=6)
    catalog_enhancer = CatalogContextEnhancer(
        catalog_retriever,
        base_enhancer=DefaultLlmContextEnhancer(agent_memory),
    )
    question_router = QuestionRouter(
        catalog_retriever,
        max_result_rows=settings.max_aggregate_rows,
        full_results=True,
    )
    token_secret = session_secret()
    session_signer = SignedToken(token_secret, max_age_seconds=LOCAL_SESSION_MAX_AGE_SECONDS)
    embed_signer = SignedToken(token_secret, max_age_seconds=5 * 60)
    embed_origins = allowed_embed_origins()
    account_store = AccountStore(settings)
    role_resolver = ProductIdentityResolver(
        session_signer=session_signer,
        embed_signer=embed_signer,
        allowed_origins=embed_origins,
        account_store=account_store,
        workspace_id=workspace.workspace_id,
    )
    export_audit = ExportAudit(settings)
    ensure_bootstrap_account(account_store)
    copilot_access = WorkspaceAccessRegistry(
        {
            workspace.workspace_id: WorkspaceGrant(
                workspace=workspace,
                allowed_subjects=frozenset(),
                allowed_roles=frozenset({"analyst", "admin"}),
                allow_local_accounts=True,
            )
        }
    )
    registry = BudgetedToolRegistry()
    query_file_system = LocalFileSystem(str(QUERY_RESULTS_DIRECTORY))
    result_artifact_store = ResultArtifactStore(RESULT_ARTIFACT_DIRECTORY, retention_seconds=limits.artifact_retention_seconds)
    provider_llm_service = llm_provider or OpenAILlmService(
        api_key=api_key,
        base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        model=model_name,
        connect_timeout_seconds=budget.llm_connect_timeout_seconds,
    )
    llm_service = ObservedLlmService(
        provider_llm_service,
        connect_timeout_seconds=budget.llm_connect_timeout_seconds,
        first_byte_timeout_seconds=budget.llm_first_byte_timeout_seconds,
        stream_idle_timeout_seconds=budget.llm_stream_idle_timeout_seconds,
        capacity=control.models,
    )
    registry.register_local_tool(
        TrustedRunSqlTool(
            sql_runner=runner,
            repair_provider=LlmRepairCandidateProvider(llm_service, max_tokens=budget.max_output_tokens),
            file_system=query_file_system,
            result_artifact_store=result_artifact_store,
            custom_tool_description="Run a policy-checked, read-only PostgreSQL analytics query.",
        ),
        access_groups=["analyst", "admin"],
    )
    agent = Agent(
        llm_service=llm_service,
        tool_registry=registry,
        user_resolver=role_resolver,
        agent_memory=agent_memory,
        llm_context_enhancer=catalog_enhancer,
        conversation_store=conversation_store,
        conversation_filters=[
            ContextBudgetFilter(
                max_chars=budget.max_context_chars,
                max_messages=budget.max_context_messages,
                max_tokens=budget.max_history_tokens,
                max_prompt_tokens=budget.max_prompt_tokens,
            )
        ],
        llm_middlewares=[BudgetSafetyMiddleware()],
        config=AgentConfig(
            max_tool_iterations=budget.max_tool_iterations,
            max_tokens=budget.max_output_tokens,
            temperature=0.0,
            stream_responses=True,
            input_placeholder="输入经营分析问题",
            idle_status_message="已就绪",
            idle_status_detail="选择示例问题或直接输入",
        ),
        system_prompt_builder=DefaultSystemPromptBuilder(base_prompt=build_runtime_system_prompt(runtime_workspace)),
        workflow_handler=AnalyticsWorkflowHandler(runtime_workspace),
    )
    from contextlib import asynccontextmanager

    context_builder = AnalysisContextBuilder(build_llm_summary_provider(
        llm_service, model_name=model_name, budget=budget,
    ), budget=budget)

    @asynccontextmanager
    async def lifespan(app):
        async def cleanup():
            while True:
                try:
                    result = await asyncio.to_thread(result_artifact_store.cleanup_expired)
                    result['query_cache_files'] = await asyncio.to_thread(cleanup_query_files, QUERY_RESULTS_DIRECTORY, limits.artifact_retention_seconds)
                    logging.getLogger(__name__).info("artifact_cleanup %s", result)
                except OSError:
                    logging.getLogger(__name__).exception("artifact_cleanup_failed")
                await asyncio.sleep(limits.artifact_cleanup_seconds)
        cleanup_task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            await control.close()
            await context_builder.close()
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
            await provider_llm_service.aclose()

    app = FastAPI(title=f"{runtime_workspace.display_name} | Data Analysis Agent", lifespan=lifespan)
    app.state.runtime_control = control
    if embed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=sorted(embed_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Embed-Parent-Origin"],
            allow_credentials=False,
        )

    @app.middleware("http")
    async def require_authenticated_api(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith("/api/") or path in {
            "/api/auth/login",
            "/api/auth/session",
        }:
            return await call_next(request)
        user = await role_resolver.resolve_user(
            RequestContext(cookies=dict(request.cookies), headers=dict(request.headers))
        )
        if user.metadata.get("auth_mode") == "anonymous":
            return JSONResponse({"detail": "login required"}, status_code=401)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=WEB_COMPONENT_DIST), name="static")

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}
    async def complete_semantic_intent(prompt: str, user) -> str:
        response = await agent._send_llm_request(
            LlmRequest(
                messages=[LlmMessage(role="user", content=prompt)],
                user=user,
                tools=None,
                stream=False,
                temperature=0.0,
                max_tokens=800,
                system_prompt="你是数据分析语义理解器。只输出符合要求的 JSON，不生成 SQL，不执行工具。",
                metadata={"purpose": "semantic_intent"},
            )
        )
        return (response.content or "").strip()

    register_chat_routes(
        app,
        ManagedChatHandler(BudgetedChatHandler(
            agent,
            budget,
            run_recorder,
            question_router,
            workspace=workspace,
            result_artifact_store=result_artifact_store,
            result_budget=ResultBudget.aggregate(
                execution_max_rows=settings.max_aggregate_rows,
                statement_timeout_ms=settings.statement_timeout_ms,
                result_max_bytes=settings.max_result_bytes,
            ),
            semantic_interpreter=SemanticIntentInterpreter(
                question_router.retriever.catalog,
                complete_semantic_intent,
            ),
            context_builder=context_builder,
            filter_source_resolver=runner.resolve_filter_sources,
            require_semantic_interpreter=True,
        ), control, workspace.workspace_id),
        config={"cdn_url": "/static/vanna-components.js", "sse_only": True},
    )

    @app.post("/api/vanna/v2/cancel")
    async def cancel_analysis(body: CancelRequest, request: Request):
        user = await role_resolver.resolve_user(_request_user_context(request))
        cancelled = await control.cancel(user_id=user.id, workspace_id=workspace.workspace_id,
                                         conversation_id=body.conversation_id, request_id=body.request_id)
        return JSONResponse({"cancelled": cancelled})

    @app.get("/api/project/evidence")
    async def evidence() -> dict:
        return {
            "dataset_version": workspace.dataset_version,
            "metric_version": workspace.metric_version,
            "metrics": [{"metric_id": metric.metric_id, "time_field": metric.time_field,
                         "source_tables": list(metric.source_tables)}
                        for metric in catalog_retriever.catalog.metrics],
        }

    @app.get("/api/project/runtime-contract")
    async def runtime_contract() -> dict:
        """Expose only versioned, server-owned capability facts for diagnostics."""
        return {
            "workspace_id": workspace.workspace_id,
            "dataset_version": workspace.dataset_version,
            "metric_version": workspace.metric_version,
            "catalog_version": catalog_retriever.catalog.catalog_version,
            "policy_version": catalog_retriever.catalog.policy_version,
            "metric_ids": [metric.metric_id for metric in catalog_retriever.catalog.metrics],
            "chart_contract": {
                "supported_types": [
                    "bar",
                    "horizontal_bar",
                    "line",
                    "area",
                    "donut",
                ],
                "maximum_result_rows": 200,
                "maximum_x_values": 120,
                "maximum_series": 8,
                "donut_maximum_categories": 8,
            },
        }

    @app.get("/api/project/audits")
    async def audits(request: Request) -> list[dict]:
        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        role = "admin" if "admin" in user.group_memberships else "analyst"
        return [
            audit_response(audit) for audit in runner.audit.list_recent(user.id, role)
        ]

    @app.get("/api/project/conversations")
    async def conversations(
        request: Request, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        if limit <= 0 or offset < 0:
            return JSONResponse({"detail": "invalid pagination"}, status_code=400)
        items = await conversation_store.list_conversations(
            user, limit=min(limit, 50), offset=offset
        )
        return [
            {
                "conversation_id": item.id,
                "title": item.metadata.get("title") or "未命名分析会话",
                "message_count": item.metadata.get("message_count", len(item.messages)),
                "dataset_version": item.metadata.get("dataset_version_id"),
                "metric_version": item.metadata.get("metric_version"),
                "workspace_id": item.metadata.get("workspace_id"),
                "state_version": item.metadata.get("state_version"),
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in items
        ]

    async def _conversation_charts(user_id: str, conversation) -> list[dict[str, object]]:
        """Replay every saved chart for this user and workspace after hash checks."""
        memory = WorkingMemory.from_mapping(conversation.metadata.get("working_memory"))
        artifact = memory.previous_result_artifact
        recorded = await run_recorder.list_rendered_charts(
            conversation_id=conversation.id, user_id=user_id
        )
        ordered: list[tuple[object, str | None]] = list(recorded)
        seen = {ref.artifact_id for ref, _question in ordered}
        for ref in replayable_chart_refs(memory, artifact):
            if ref.artifact_id not in seen:
                ordered.append((ref, None))
                seen.add(ref.artifact_id)
        charts: list[dict[str, object]] = []
        for ref, question in ordered[:8]:
            workspace_id = ref.workspace_id
            if workspace_id == "unknown":
                workspace_id = workspace.workspace_id
            elif workspace_id != workspace.workspace_id:
                continue
            try:
                figure = result_artifact_store.load_verified_plotly(
                    artifact_id=ref.artifact_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
            except (
                FileNotFoundError,
                PermissionError,
                ResultArtifactIntegrityError,
                ValueError,
                OSError,
            ):
                continue
            if question:
                figure["question"] = question
            charts.append(figure)
        return charts

    @app.get("/api/project/conversations/{conversation_id}")
    async def conversation_detail(
        conversation_id: str, request: Request
    ) -> JSONResponse:
        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        try:
            conversation = await conversation_store.get_conversation(
                conversation_id, user, recent_turns=None
            )
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        artifact = current_result_artifact(conversation, workspace)
        scope_current = analysis_scope_is_current(
            conversation.metadata, workspace
        )
        return JSONResponse(
            {
                "conversation_id": conversation.id,
                "title": conversation.metadata.get("title") or "未命名分析会话",
                "dataset_version": conversation.metadata.get(
                    "dataset_version_id", workspace.dataset_version
                ),
                "metric_version": conversation.metadata.get(
                    "metric_version", workspace.metric_version
                ),
                "workspace_id": conversation.metadata.get(
                    "workspace_id", workspace.workspace_id
                ),
                "state_version": conversation.metadata.get("state_version", 0),
                "analysis_scope_current": scope_current,
                "analysis_scope_notice": None if scope_current else VERSION_MISMATCH_NOTICE,
                "messages": [
                    {
                        "role": message.role,
                        "content": "工具结果已记录"
                        if message.role == "tool"
                        else message.content,
                        "timestamp": message.timestamp.isoformat(),
                    }
                    for message in conversation.messages
                    if message.role != "system"
                ],
                # This is a bounded replay card, not a SQL/result-table
                # export. The client may render it after a reload without
                # triggering the model or database.
                "trusted_result_artifact": artifact.as_dict() if artifact else None,
                "analysis_state": conversation.metadata.get("working_memory") if scope_current else None,
                "charts": await _conversation_charts(user.id, conversation),
            }
        )

    @app.get("/api/project/conversations/{conversation_id}/messages")
    async def conversation_message_page(conversation_id: str, request: Request, before: int | None = None, limit: int = 50):
        user = await role_resolver.resolve_user(_request_user_context(request))
        try:
            messages = await conversation_store.get_history_page(conversation_id, user, before=before, limit=limit)
        except (InvalidConversationId, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return {"messages": [
            {"role": m.role, "content": "工具结果已记录" if m.role == "tool" else m.content,
             "message_id": m.metadata["message_id"], "message_index": m.metadata["message_index"],
             "timestamp": m.timestamp.isoformat()} for m in messages if m.role != "system"
        ], "next_cursor": messages[0].metadata["message_index"] if messages else None}

    @app.get("/api/project/conversations/{conversation_id}/execution-memory")
    async def conversation_execution_memory(
        conversation_id: str, request: Request, limit: int = Query(default=100, ge=1, le=100)
    ) -> JSONResponse:
        """Return safe execution evidence without raw SQL or result rows."""
        user = await role_resolver.resolve_user(_request_user_context(request))
        try:
            conversation = await conversation_store.get_conversation(conversation_id, user)
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        executions = await run_recorder.list_execution_memory(
            conversation_id=conversation_id, user_id=user.id, limit=limit
        )
        return JSONResponse({"conversation_id": conversation_id, "executions": [item.as_dict() for item in executions]})

    @app.get("/api/project/conversations/{conversation_id}/artifacts/{artifact_id}/manifest")
    async def result_artifact_manifest(
        conversation_id: str, artifact_id: str, request: Request
    ) -> JSONResponse:
        """Return only the scoped manifest; payload files stay external."""
        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        conversation = await conversation_store.get_conversation(conversation_id, user)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        artifact = current_result_artifact(conversation, workspace)
        if artifact is None or artifact.artifact_id != artifact_id:
            return JSONResponse({"detail": "artifact not found"}, status_code=404)
        workspace_id = conversation.metadata.get(
            "workspace_id", workspace.workspace_id
        )
        if artifact.workspace_id != workspace_id:
            return JSONResponse({"detail": "artifact not found"}, status_code=404)
        try:
            manifest = result_artifact_store.verify_manifest(
                artifact_id=artifact_id,
                user_id=user.id,
                workspace_id=workspace_id,
            )
        except ResultArtifactExpired:
            return JSONResponse({"detail": "保存的结果已到期，请重新运行分析。"}, status_code=410)
        except (FileNotFoundError, PermissionError, ResultArtifactIntegrityError, ValueError):
            return JSONResponse({"detail": "artifact not found"}, status_code=404)
        return JSONResponse(manifest)

    @app.get("/api/project/conversations/{conversation_id}/artifacts/{artifact_id}/rows")
    async def result_artifact_rows(
        conversation_id: str, artifact_id: str, request: Request,
        cursor: int = Query(default=0, ge=0), page_size: int = Query(default=200, ge=1, le=500),
    ):
        user = await role_resolver.resolve_user(_request_user_context(request))
        try:
            conversation = await conversation_store.get_conversation(conversation_id, user)
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        artifact = current_result_artifact(conversation, workspace) if conversation else None
        if artifact is None or artifact.artifact_id != artifact_id:
            return JSONResponse({"detail": "artifact not found"}, status_code=404)
        try:
            page = await asyncio.to_thread(
                result_artifact_store.read_csv_page,
                artifact_id=artifact_id, user_id=user.id,
                workspace_id=workspace.workspace_id,
                cursor=cursor, page_size=page_size,
            )
        except ResultArtifactExpired:
            return JSONResponse({"detail": "保存的结果已到期，请重新运行分析。"}, status_code=410)
        except FileNotFoundError:
            return JSONResponse({"detail": "artifact not found"}, status_code=404)
        except ResultArtifactIntegrityError:
            return JSONResponse({"detail": "artifact integrity check failed"}, status_code=409)
        return JSONResponse(page)

    user_memory = UserMemoryStore(settings)
    runtime_diagnostics = RuntimeDiagnostics(settings)

    @app.get("/api/project/preferences")
    async def list_preferences(request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        return JSONResponse(user_memory.list_preferences(user.id, workspace.workspace_id))

    @app.put("/api/project/preferences")
    async def put_preference(body: PreferenceRequest, request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        try:
            user_memory.put_preference(
                user.id,
                workspace.workspace_id,
                body.memory_kind,
                body.value,
            )
        except ValueError:
            return JSONResponse({"detail": "unsupported preference"}, status_code=400)
        return JSONResponse({"memory_kind": body.memory_kind, "value": body.value})

    @app.delete("/api/project/preferences/{memory_kind}")
    async def delete_preference(memory_kind: str, request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        if memory_kind not in ALLOWED_PREFERENCES:
            return JSONResponse({"detail": "unsupported preference"}, status_code=400)
        deleted = user_memory.delete_preference(
            user.id, workspace.workspace_id, memory_kind
        )
        if not deleted:
            return JSONResponse({"detail": "preference not found"}, status_code=404)
        return JSONResponse({"deleted": True})

    @app.get("/api/project/saved-analyses")
    async def list_saved_analyses(request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        return JSONResponse(user_memory.list_analyses(user.id, workspace.workspace_id))

    @app.post("/api/project/saved-analyses")
    async def save_analysis(body: SaveAnalysisRequest, request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        conversation = await conversation_store.get_conversation(body.conversation_id, user)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        artifact = current_result_artifact(conversation, workspace)
        if artifact is None:
            return JSONResponse({"detail": "no validated result to save"}, status_code=400)
        saved_id = user_memory.save_analysis(
            subject_id=user.id,
            workspace_id=workspace.workspace_id,
            dataset_version=artifact.dataset_version,
            metric_version=artifact.metric_version,
            artifact_id=artifact.artifact_id,
            summary=artifact.summary,
        )
        return JSONResponse(
            {
                "saved_id": saved_id,
                "dataset_version": artifact.dataset_version,
                "metric_version": artifact.metric_version,
                "note": "保存的分析只用于回看，不会成为下一问的指标或筛选事实。",
            }
        )

    @app.delete("/api/project/saved-analyses/{saved_id}")
    async def delete_saved_analysis(saved_id: str, request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        deleted = user_memory.delete_analysis(
            user.id, workspace.workspace_id, saved_id
        )
        if not deleted:
            return JSONResponse({"detail": "saved analysis not found"}, status_code=404)
        return JSONResponse({"deleted": True})

    @app.get("/api/project/diagnostics")
    async def diagnostics(request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        include_workspace = "admin" in user.group_memberships
        result = await asyncio.to_thread(runtime_diagnostics.summary, subject_id=user.id, include_workspace=include_workspace)
        if include_workspace:
            result['capacity'] = control.snapshot()
        return JSONResponse(result)

    @app.post("/api/project/conversations/{conversation_id}/replace-last-question")
    async def replace_last_question(
        conversation_id: str, request: Request
    ) -> JSONResponse:
        """Drop the latest user turn so the edited question can be asked again."""

        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        if (user.id, workspace.workspace_id, conversation_id) in control.inflight:
            return JSONResponse({'detail': '本会话正在处理，请先停止当前请求再编辑。'}, status_code=409)
        try:
            removed_index = await conversation_store.truncate_from_last_user_message(
                conversation_id, user
            )
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except ConversationStateConflict:
            return JSONResponse(
                {"detail": "当前会话已有新消息，请刷新后重试。"},
                status_code=409,
            )
        if removed_index is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        return JSONResponse(
            {
                "conversation_id": conversation_id,
                "removed_from_index": removed_index,
            }
        )

    @app.post("/api/project/conversations/{conversation_id}/analysis-context")
    async def clear_analysis_context(
        conversation_id: str, request: Request
    ) -> JSONResponse:
        """Clear active analysis state without deleting chat history or audits."""

        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        if (user.id, workspace.workspace_id, conversation_id) in control.inflight:
            return JSONResponse({'detail': '本会话正在处理，请先停止当前请求再清空。'}, status_code=409)
        try:
            conversation = await conversation_store.get_conversation(
                conversation_id, user
            )
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        message_count = len(conversation.messages)
        clear_active_analysis_state(conversation, workspace)
        try:
            await conversation_store.update_conversation(conversation)
        except ConversationStateConflict:
            return JSONResponse(
                {
                    "detail": (
                        "当前会话已有新消息，请刷新后重试；本次请求未覆盖已有分析状态。"
                    )
                },
                status_code=409,
            )
        return JSONResponse(
            {
                "conversation_id": conversation.id,
                "cleared": True,
                "message_count": message_count,
                "state_version": conversation.metadata.get("state_version"),
                "analysis_scope_current": True,
                "notice": CLEAR_ANALYSIS_NOTICE,
            }
        )

    @app.delete("/api/project/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str, request: Request
    ) -> JSONResponse:
        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        if (user.id, workspace.workspace_id, conversation_id) in control.inflight:
            return JSONResponse({'detail': '本会话正在处理，请先停止当前请求再删除。'}, status_code=409)
        try:
            deleted = await conversation_store.delete_conversation(
                conversation_id, user
            )
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if not deleted:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        return JSONResponse({"conversation_id": conversation_id, "deleted": True})

    def _request_user_context(request: Request) -> RequestContext:
        return RequestContext(cookies=dict(request.cookies), headers=dict(request.headers))

    @app.post("/api/auth/login")
    async def login(body: LoginRequest, request: Request) -> JSONResponse:
        retry = control.rates.retry_after(('login', request.client.host if request.client else 'unknown'), limits.login_per_minute)
        if retry:
            return JSONResponse({"detail": "登录尝试过多，请稍后重试。"}, status_code=429, headers={"Retry-After": str(retry)})
        account = await asyncio.to_thread(account_store.find_by_username, body.username.strip().lower())
        if not await asyncio.to_thread(password_matches, account, body.password) or account is None:
            return JSONResponse({"detail": "用户名或密码不正确"}, status_code=401)
        response = JSONResponse(
            {
                "authenticated": True,
                "username": account.username,
                "role": account.role,
                "auth_mode": "local_session",
            }
        )
        response.set_cookie(
            key=LOCAL_SESSION_COOKIE,
            value=issue_session(session_signer, account),
            max_age=LOCAL_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        if user.metadata.get('auth_mode') != 'local_session':
            return JSONResponse({'detail': 'logout requires local login'}, status_code=403)
        await asyncio.to_thread(account_store.revoke_sessions, user.id)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(LOCAL_SESSION_COOKIE, path="/")
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    @app.get("/api/auth/session")
    async def auth_session(request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        if user.metadata.get("auth_mode") == "anonymous":
            return JSONResponse({"authenticated": False})
        role = "admin" if "admin" in user.group_memberships else "analyst"
        return JSONResponse(
            {
                "authenticated": True,
                "username": user.username,
                "role": role,
                "auth_mode": user.metadata.get("auth_mode"),
                "embed_origin": user.metadata.get("embed_origin"),
            }
        )

    @app.post("/api/copilot/embed-tokens")
    async def issue_embed(body: EmbedTokenRequest, request: Request) -> JSONResponse:
        user = await role_resolver.resolve_user(_request_user_context(request))
        if user.metadata.get("auth_mode") != "local_session":
            return JSONResponse({"detail": "embed token requires a local login"}, status_code=403)
        try:
            origin = normalize_origin(body.origin)
        except ValueError:
            return JSONResponse({"detail": "origin is invalid"}, status_code=400)
        if origin not in embed_origins:
            return JSONResponse({"detail": "origin is not allowlisted"}, status_code=403)
        if not body.surface_id or len(body.surface_id) > 64:
            return JSONResponse({"detail": "surface_id is invalid"}, status_code=400)
        role = "admin" if "admin" in user.group_memberships else "analyst"
        token = issue_embed_token(
            embed_signer,
            account_subject=user.id,
            username=str(user.username or user.id),
            role=role,
            origin=origin,
            workspace_id=workspace.workspace_id,
            session_version=user.metadata['session_version'],
        )
        return JSONResponse(
            {
                "token": token,
                "expires_in": 300,
                "origin": origin,
                "workspace_id": workspace.workspace_id,
            }
        )

    @app.get("/api/project/conversations/{conversation_id}/result.csv")
    async def download_result_csv(conversation_id: str, request: Request):
        user = await role_resolver.resolve_user(_request_user_context(request))
        try:
            conversation = await conversation_store.get_conversation(conversation_id, user)
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        artifact = (
            current_result_artifact(conversation, workspace)
            if conversation is not None
            else None
        )
        if artifact is None or not artifact.artifact_id:
            return JSONResponse({"detail": "no validated result to download"}, status_code=404)
        try:
            manifest = result_artifact_store.verify_manifest(
                artifact_id=artifact.artifact_id,
                user_id=user.id,
                workspace_id=workspace.workspace_id,
            )
            csv_file = result_artifact_store.open_verified_csv(
                artifact_id=artifact.artifact_id,
                user_id=user.id,
                workspace_id=workspace.workspace_id,
            )
        except ResultArtifactExpired:
            return JSONResponse({"detail": "保存的结果已到期，请重新运行分析。"}, status_code=410)
        except FileNotFoundError:
            return JSONResponse({"detail": "validated result file is missing"}, status_code=404)
        except ResultArtifactIntegrityError:
            return JSONResponse({"detail": "validated result file failed integrity check"}, status_code=409)
        role = "admin" if "admin" in user.group_memberships else "analyst"
        try:
            await asyncio.to_thread(export_audit.record,
                artifact_id=artifact.artifact_id, user_id=user.id, user_role=role,
                workspace_id=workspace.workspace_id, row_count=int(manifest["row_count"]),
                csv_sha256=str(manifest["csv_sha256"]))
        except BaseException:
            csv_file.close()
            raise
        async def stream_csv():
            try:
                while chunk := await asyncio.to_thread(csv_file.read, 64 * 1024):
                    yield chunk
            finally:
                csv_file.close()
        return StreamingResponse(
            stream_csv(),
            media_type="text/csv",
            headers={"X-Row-Count": str(manifest["row_count"]),
                     "Content-Disposition": f'attachment; filename="{artifact.artifact_id}.csv"'},
        )

    @app.get("/api/project/session")
    async def session(request: Request) -> dict:
        user = await role_resolver.resolve_user(_request_user_context(request))
        role = "admin" if "admin" in user.group_memberships else "analyst"
        auth_mode = str(user.metadata.get("auth_mode") or "anonymous")
        return {
            "user_id": user.id,
            "username": user.username,
            "role": role,
            "auth_mode": auth_mode,
            "is_demo": False,
            "session_max_age_seconds": LOCAL_SESSION_MAX_AGE_SECONDS,
        }

    @app.post("/api/copilot/bootstrap")
    async def copilot_bootstrap(
        selection: CopilotBootstrapRequest, request: Request
    ) -> JSONResponse:
        """Return server-derived host/context scope for an embedded Copilot."""

        user = await role_resolver.resolve_user(
            _request_user_context(request)
        )
        try:
            bootstrap = build_copilot_bootstrap(
                user=user,
                request=selection,
                access_registry=copilot_access,
            )
        except BootstrapContractError:
            return JSONResponse({"detail": "workspace access denied"}, status_code=403)
        return JSONResponse(bootstrap.model_dump(mode="json"))

    @app.get("/embedded-demo", include_in_schema=False)
    async def embedded_demo() -> RedirectResponse:
        """Old host URL. The product shell is the only page."""
        return RedirectResponse("/embedded-copilot", status_code=307)

    @app.get("/embedded-copilot", include_in_schema=False)
    async def embedded_copilot() -> FileResponse:
        """Serve the single product shell. Admin controls stay behind the page toggle."""
        return FileResponse(EMBEDDABLE_COPILOT_PATH, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    uvicorn.run(
        create_app(),
        host=os.getenv("DATA_ANALYSIS_DEMO_HOST", "127.0.0.1"),
        port=DEMO_PORT,
        timeout_graceful_shutdown=10,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
