"""Run Vanna's built-in web UI against a small SQLite demo database.

This is intentionally a thin launcher around Vanna's public extension points.
It does not modify Vanna core code and it does not implement the project's
future SQL safety policy. The goal is to observe the original Vanna experience
before deciding where product-specific changes belong.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from vanna import Agent
from vanna.core.agent.config import AgentConfig
from vanna.core.registry import ToolRegistry
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.core.user import RequestContext, User, UserResolver
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.integrations.openai import OpenAILlmService
from vanna.integrations.sqlite import SqliteRunner
from vanna.servers.base import ChatHandler
from vanna.servers.fastapi.routes import register_chat_routes
from vanna.tools import RunSqlTool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = REPOSITORY_ROOT / "examples" / "data" / "vanna_demo.sqlite"
HOST_PAGE_PATH = REPOSITORY_ROOT / "examples" / "embedded_analyst_host.html"
WEB_COMPONENT_DIST = REPOSITORY_ROOT / "frontends" / "webcomponent" / "dist"
QUERY_RESULTS_DIRECTORY = (
    Path(
        os.getenv(
            "VANNA_QUERY_RESULTS_DIR", "/tmp/data-analysis-agent-vanna-query-results"
        )
    ).expanduser()
)

SYSTEM_PROMPT = """
你是一个中文业务数据分析助手。回答涉及数据的问题时，必须先调用 run_sql 工具，不能编造数值。

当前是 Vanna 原生能力验证，使用 SQLite 数据库。仅有 sales_daily 表：
- business_date：日期，格式 YYYY-MM-DD；
- region：区域，华东或华南；
- category：品类，家居或数码；
- order_count：支付订单数；
- gmv：成交额；
- avg_delivery_days：平均履约天数；
- positive_review_rate：好评率，0 到 1 的小数。

SQL 使用 SQLite 语法。查询完成后，用中文简要解释结果，并明确这是合成演示数据。
""".strip()


class DemoUserResolver(UserResolver):
    """Resolve every prototype request to a single local analyst user."""

    async def resolve_user(self, request_context: RequestContext) -> User:
        del request_context
        return User(
            id="demo-analyst",
            username="Demo Analyst",
            email="demo@example.local",
            group_memberships=["analyst"],
        )


def create_demo_database() -> None:
    """Create deterministic, local-only fixture data on every first run."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_daily (
                business_date TEXT NOT NULL,
                region TEXT NOT NULL,
                category TEXT NOT NULL,
                order_count INTEGER NOT NULL,
                gmv REAL NOT NULL,
                avg_delivery_days REAL NOT NULL,
                positive_review_rate REAL NOT NULL,
                PRIMARY KEY (business_date, region, category)
            )
            """
        )
        existing_count = connection.execute("SELECT COUNT(*) FROM sales_daily").fetchone()[0]
        if existing_count == 0:
            connection.executemany(
                "INSERT INTO sales_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("2026-07-01", "华东", "家居", 128, 24680.0, 2.1, 0.95),
                    ("2026-07-01", "华东", "数码", 96, 31800.0, 2.8, 0.91),
                    ("2026-07-01", "华南", "家居", 102, 19800.0, 1.9, 0.96),
                    ("2026-07-01", "华南", "数码", 88, 27900.0, 3.2, 0.87),
                    ("2026-07-02", "华东", "家居", 134, 26150.0, 2.2, 0.94),
                    ("2026-07-02", "华东", "数码", 91, 30500.0, 2.7, 0.92),
                    ("2026-07-02", "华南", "家居", 110, 21700.0, 2.0, 0.95),
                    ("2026-07-02", "华南", "数码", 84, 25100.0, 3.4, 0.85),
                ],
            )
        connection.commit()
    finally:
        connection.close()


def create_app() -> FastAPI:
    load_dotenv(REPOSITORY_ROOT / ".env")

    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is missing from the repository root .env file.")
    if not (WEB_COMPONENT_DIST / "vanna-components.js").is_file():
        raise RuntimeError(
            "Vanna Web Component bundle is missing. Run `npm install --package-lock=false "
            "&& npm run build` in frontends/webcomponent before starting the demo."
        )

    create_demo_database()
    llm = OpenAILlmService(
        api_key=api_key,
        base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        model=os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
    )
    registry = ToolRegistry()
    registry.register_local_tool(
        RunSqlTool(
            sql_runner=SqliteRunner(str(DATABASE_PATH)),
            file_system=LocalFileSystem(str(QUERY_RESULTS_DIRECTORY)),
        ),
        access_groups=["analyst"],
    )
    agent = Agent(
        llm_service=llm,
        tool_registry=registry,
        user_resolver=DemoUserResolver(),
        agent_memory=DemoAgentMemory(),
        config=AgentConfig(max_tool_iterations=4, temperature=0.0),
        system_prompt_builder=DefaultSystemPromptBuilder(base_prompt=SYSTEM_PROMPT),
    )

    app = FastAPI(title="Vanna Original Web Demo")
    app.mount("/static", StaticFiles(directory=WEB_COMPONENT_DIST), name="static")
    register_chat_routes(
        app,
        ChatHandler(agent),
        config={"cdn_url": "/static/vanna-components.js"},
    )

    @app.get("/embedded-demo", include_in_schema=False)
    async def embedded_demo() -> FileResponse:
        """Serve the framework-free business-page embedding reference."""
        return FileResponse(HOST_PAGE_PATH, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Avoid a noisy browser 404 during component smoke tests."""
        return Response(status_code=204)

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=32009)
