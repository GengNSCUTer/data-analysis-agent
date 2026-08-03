"""Run the trusted Olist PostgreSQL Vanna prototype on 127.0.0.1:32010."""

from __future__ import annotations

import os
import re
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from data_analysis_agent.metric_context import METRIC_EVIDENCE, SYSTEM_PROMPT
from data_analysis_agent.postgres_runner import SecurePostgresRunner
from vanna import Agent
from vanna.core.agent.config import AgentConfig
from vanna.core.registry import ToolRegistry
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.core.user import RequestContext, User, UserResolver
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
SAFE_USER_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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


class DemoRoleResolver(UserResolver):
    """Demo-only resolver; a production deployment must replace this with real auth."""

    async def resolve_user(self, request_context: RequestContext) -> User:
        role = request_context.get_header("X-Demo-Role", "analyst").lower()
        if role not in {"analyst", "admin"}:
            role = "analyst"
        user_id = request_context.get_header("X-Demo-User", "demo-analyst")
        if not SAFE_USER_ID.fullmatch(user_id):
            user_id = "demo-analyst"
        return User(id=user_id, username=user_id, group_memberships=[role])


def create_app() -> FastAPI:
    load_dotenv(REPOSITORY_ROOT / ".env")
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is missing from the repository root .env file.")
    if not (WEB_COMPONENT_DIST / "vanna-components.js").is_file():
        raise RuntimeError("Build frontends/webcomponent before starting the trusted demo.")

    model_name = os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    runner = SecurePostgresRunner(model_name=model_name)
    registry = ToolRegistry()
    registry.register_local_tool(
        RunSqlTool(
            sql_runner=runner,
            file_system=LocalFileSystem(str(QUERY_RESULTS_DIRECTORY)),
            custom_tool_description="Run a policy-checked, read-only PostgreSQL analytics query.",
        ),
        access_groups=["analyst", "admin"],
    )
    agent = Agent(
        llm_service=OpenAILlmService(
            api_key=api_key,
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            model=model_name,
        ),
        tool_registry=registry,
        user_resolver=DemoRoleResolver(),
        agent_memory=DemoAgentMemory(),
        config=AgentConfig(max_tool_iterations=4, temperature=0.0),
        system_prompt_builder=DefaultSystemPromptBuilder(base_prompt=SYSTEM_PROMPT),
    )
    app = FastAPI(title="Trusted Olist Data Analysis Agent")
    app.mount("/static", StaticFiles(directory=WEB_COMPONENT_DIST), name="static")
    register_chat_routes(app, ChatHandler(agent), config={"cdn_url": "/static/vanna-components.js"})

    @app.get("/api/project/evidence")
    async def evidence() -> dict:
        return METRIC_EVIDENCE

    @app.get("/api/project/audits")
    async def audits(request: Request) -> list[dict]:
        user = await DemoRoleResolver().resolve_user(
            RequestContext(headers=dict(request.headers), query_params=dict(request.query_params))
        )
        role = "admin" if "admin" in user.group_memberships else "analyst"
        return [audit_response(audit) for audit in runner.audit.list_recent(user.id, role)]

    @app.get("/api/project/session")
    async def session(request: Request) -> dict:
        user = await DemoRoleResolver().resolve_user(
            RequestContext(headers=dict(request.headers), query_params=dict(request.query_params))
        )
        role = "admin" if "admin" in user.group_memberships else "analyst"
        return {"user_id": user.id, "role": role, "auth_mode": "demo_header"}

    @app.get("/embedded-demo", include_in_schema=False)
    async def embedded_demo() -> FileResponse:
        return FileResponse(HOST_PAGE_PATH, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=32010)
