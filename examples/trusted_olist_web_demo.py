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
from fastapi.staticfiles import StaticFiles

from data_analysis_agent.metric_context import METRIC_EVIDENCE, SYSTEM_PROMPT
from data_analysis_agent.postgres_runner import SecurePostgresRunner
from data_analysis_agent.trusted_workflow import TrustedOlistWorkflowHandler
from data_analysis_agent.visualization import TrustedVisualizeDataTool
from vanna import Agent
from vanna.core.agent.config import AgentConfig
from vanna.core.registry import ToolRegistry
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.core.user import RequestContext
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.integrations.openai import OpenAILlmService
from vanna.servers.base import ChatHandler
from vanna.servers.fastapi.routes import register_chat_routes
from vanna.tools import RunSqlTool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOST_PAGE_PATH = REPOSITORY_ROOT / "examples" / "embedded_analyst_host.html"
WEB_COMPONENT_DIST = REPOSITORY_ROOT / "frontends" / "webcomponent" / "dist"
QUERY_RESULTS_DIRECTORY = Path(os.getenv("VANNA_QUERY_RESULTS_DIR", "/tmp/data-analysis-agent-vanna-query-results"))


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
        raise RuntimeError("SILICONFLOW_API_KEY is missing from the repository root .env file.")
    if not (WEB_COMPONENT_DIST / "vanna-components.js").is_file():
        raise RuntimeError("Build frontends/webcomponent before starting the trusted demo.")

    model_name = os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    runner = SecurePostgresRunner(model_name=model_name)
    # A process-local fallback keeps this public demo usable without adding a
    # secret to source control. Restarts invalidate old cookies by design.
    signer = DemoSessionSigner(os.getenv("DATA_ANALYSIS_DEMO_SESSION_SECRET", secrets.token_urlsafe(32)))
    role_resolver = DemoRoleResolver(signer)
    registry = ToolRegistry()
    query_file_system = LocalFileSystem(str(QUERY_RESULTS_DIRECTORY))
    registry.register_local_tool(
        RunSqlTool(
            sql_runner=runner,
            file_system=query_file_system,
            custom_tool_description="Run a policy-checked, read-only PostgreSQL analytics query.",
        ),
        access_groups=["analyst", "admin"],
    )
    registry.register_local_tool(TrustedVisualizeDataTool(query_file_system), access_groups=["analyst", "admin"])
    agent = Agent(
        llm_service=OpenAILlmService(
            api_key=api_key,
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            model=model_name,
        ),
        tool_registry=registry,
        user_resolver=role_resolver,
        agent_memory=DemoAgentMemory(),
        config=AgentConfig(
            max_tool_iterations=4,
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
    register_chat_routes(app, ChatHandler(agent), config={"cdn_url": "/static/vanna-components.js"})

    @app.get("/api/project/evidence")
    async def evidence() -> dict:
        return METRIC_EVIDENCE

    @app.get("/api/project/audits")
    async def audits(request: Request) -> list[dict]:
        user = await role_resolver.resolve_user(RequestContext(cookies=dict(request.cookies)))
        role = "admin" if "admin" in user.group_memberships else "analyst"
        return [audit_response(audit) for audit in runner.audit.list_recent(user.id, role)]

    @app.get("/api/project/session")
    async def session(request: Request) -> dict:
        user = await role_resolver.resolve_user(RequestContext(cookies=dict(request.cookies)))
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
        response = JSONResponse({
            "user_id": DEMO_IDENTITIES[selection.role],
            "role": selection.role,
            "auth_mode": "demo_signed_session",
            "is_demo": True,
        })
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
