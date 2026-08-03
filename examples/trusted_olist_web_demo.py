"""Run the trusted Olist PostgreSQL Vanna prototype on 127.0.0.1:32010."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from data_analysis_agent.demo_session import (
    DEMO_IDENTITIES,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    DemoRoleResolver,
    DemoSessionSigner,
)
from data_analysis_agent.budget import (
    BudgetedToolRegistry,
    BudgetSafetyMiddleware,
    RequestBudget,
)
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.context_builder import ContextBudgetFilter
from data_analysis_agent.conversation_store import (
    InvalidConversationId,
    PostgresConversationStore,
)
from data_analysis_agent.run_recorder import PostgresRunRecorder
from fastapi.staticfiles import StaticFiles

from data_analysis_agent.metric_context import (
    DATASET_VERSION,
    METRIC_EVIDENCE,
    METRIC_VERSION,
    SYSTEM_PROMPT,
)
from data_analysis_agent.postgres_runner import (
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.trusted_workflow import TrustedOlistWorkflowHandler
from data_analysis_agent.visualization import TrustedVisualizeDataTool
from vanna import Agent
from vanna.core.agent.config import AgentConfig
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.core.user import RequestContext
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.integrations.openai import OpenAILlmService
from vanna.servers.fastapi.routes import register_chat_routes
from vanna.tools import RunSqlTool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOST_PAGE_PATH = REPOSITORY_ROOT / "examples" / "embedded_analyst_host.html"
WEB_COMPONENT_DIST = REPOSITORY_ROOT / "frontends" / "webcomponent" / "dist"
QUERY_RESULTS_DIRECTORY = Path(
    os.getenv("VANNA_QUERY_RESULTS_DIR", "/tmp/data-analysis-agent-vanna-query-results")
)


class DemoSessionRequest(BaseModel):
    role: Literal["analyst", "admin"]


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
        "created_at": audit["created_at"],
    }


def create_app() -> FastAPI:
    load_dotenv(REPOSITORY_ROOT / ".env")
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "SILICONFLOW_API_KEY is missing from the repository root .env file."
        )
    if not (WEB_COMPONENT_DIST / "vanna-components.js").is_file():
        raise RuntimeError(
            "Build frontends/webcomponent before starting the trusted demo."
        )

    model_name = os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    settings = PostgresConnectionSettings.from_environment()
    runner = SecurePostgresRunner(settings=settings, model_name=model_name)
    budget = RequestBudget.from_environment()
    conversation_store = PostgresConversationStore(settings)
    run_recorder = PostgresRunRecorder(settings, model_name=model_name)
    # A process-local fallback keeps this public demo usable without adding a
    # secret to source control. Restarts invalidate old cookies by design.
    signer = DemoSessionSigner(
        os.getenv("DATA_ANALYSIS_DEMO_SESSION_SECRET", secrets.token_urlsafe(32))
    )
    role_resolver = DemoRoleResolver(signer)
    registry = BudgetedToolRegistry()
    query_file_system = LocalFileSystem(str(QUERY_RESULTS_DIRECTORY))
    registry.register_local_tool(
        RunSqlTool(
            sql_runner=runner,
            file_system=query_file_system,
            custom_tool_description="Run a policy-checked, read-only PostgreSQL analytics query.",
        ),
        access_groups=["analyst", "admin"],
    )
    registry.register_local_tool(
        TrustedVisualizeDataTool(query_file_system), access_groups=["analyst", "admin"]
    )
    agent = Agent(
        llm_service=OpenAILlmService(
            api_key=api_key,
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            model=model_name,
        ),
        tool_registry=registry,
        user_resolver=role_resolver,
        agent_memory=DemoAgentMemory(),
        conversation_store=conversation_store,
        conversation_filters=[
            ContextBudgetFilter(
                max_chars=budget.max_context_chars,
                max_messages=budget.max_context_messages,
            )
        ],
        llm_middlewares=[BudgetSafetyMiddleware()],
        config=AgentConfig(
            max_tool_iterations=budget.max_tool_iterations,
            max_tokens=budget.max_output_tokens,
            temperature=0.0,
            input_placeholder="输入经营分析问题",
            idle_status_message="已就绪",
            idle_status_detail="选择示例问题或直接输入",
        ),
        system_prompt_builder=DefaultSystemPromptBuilder(base_prompt=SYSTEM_PROMPT),
        workflow_handler=TrustedOlistWorkflowHandler(),
    )
    app = FastAPI(title="Trusted Olist Data Analysis Agent")
    app.mount("/static", StaticFiles(directory=WEB_COMPONENT_DIST), name="static")
    register_chat_routes(
        app,
        BudgetedChatHandler(agent, budget, run_recorder),
        config={"cdn_url": "/static/vanna-components.js"},
    )

    @app.get("/api/project/evidence")
    async def evidence() -> dict:
        return METRIC_EVIDENCE

    @app.get("/api/project/audits")
    async def audits(request: Request) -> list[dict]:
        user = await role_resolver.resolve_user(
            RequestContext(cookies=dict(request.cookies))
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
            RequestContext(cookies=dict(request.cookies))
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
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in items
        ]

    @app.get("/api/project/conversations/{conversation_id}")
    async def conversation_detail(
        conversation_id: str, request: Request
    ) -> JSONResponse:
        user = await role_resolver.resolve_user(
            RequestContext(cookies=dict(request.cookies))
        )
        try:
            conversation = await conversation_store.get_conversation(
                conversation_id, user
            )
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        return JSONResponse(
            {
                "conversation_id": conversation.id,
                "title": conversation.metadata.get("title") or "未命名分析会话",
                "dataset_version": conversation.metadata.get(
                    "dataset_version_id", DATASET_VERSION
                ),
                "metric_version": conversation.metadata.get(
                    "metric_version", METRIC_VERSION
                ),
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
            }
        )

    @app.delete("/api/project/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str, request: Request
    ) -> JSONResponse:
        user = await role_resolver.resolve_user(
            RequestContext(cookies=dict(request.cookies))
        )
        try:
            deleted = await conversation_store.delete_conversation(
                conversation_id, user
            )
        except InvalidConversationId as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if not deleted:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        return JSONResponse({"conversation_id": conversation_id, "deleted": True})

    @app.get("/api/project/session")
    async def session(request: Request) -> dict:
        user = await role_resolver.resolve_user(
            RequestContext(cookies=dict(request.cookies))
        )
        role = "admin" if "admin" in user.group_memberships else "analyst"
        return {
            "user_id": user.id,
            "role": role,
            "auth_mode": "demo_signed_session",
            "is_demo": True,
            "session_max_age_seconds": SESSION_MAX_AGE_SECONDS,
        }

    @app.post("/api/project/demo-session")
    async def set_demo_session(selection: DemoSessionRequest) -> JSONResponse:
        """Choose a preconfigured demo role; this endpoint is not real login."""
        session = signer.dumps(selection.role)
        response = JSONResponse(
            {
                "user_id": DEMO_IDENTITIES[selection.role],
                "role": selection.role,
                "auth_mode": "demo_signed_session",
                "is_demo": True,
            }
        )
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
        return response

    @app.get("/embedded-demo", include_in_schema=False)
    async def embedded_demo() -> FileResponse:
        return FileResponse(HOST_PAGE_PATH, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=32010)
