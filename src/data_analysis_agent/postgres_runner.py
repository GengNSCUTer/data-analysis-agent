"""Policy-enforced PostgreSQL runner with persistent query audit records."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from time import perf_counter
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.core.tool import ToolContext

from .sql_policy import PolicyViolation, SqlPolicy


@dataclass(frozen=True)
class PostgresConnectionSettings:
    database: str = "data_analysis_agent"
    host: str = "/tmp"
    port: int = 35434
    reader_user: str = "daa_analytics_reader"
    writer_user: str = "daa_app_writer"
    statement_timeout_ms: int = 5_000
    max_rows: int = 1_000

    @classmethod
    def from_environment(cls) -> "PostgresConnectionSettings":
        return cls(
            database=os.getenv("DATA_ANALYSIS_POSTGRES_DB", cls.database),
            host=os.getenv("DATA_ANALYSIS_POSTGRES_HOST", cls.host),
            port=int(os.getenv("DATA_ANALYSIS_POSTGRES_PORT", str(cls.port))),
            reader_user=os.getenv("DATA_ANALYSIS_POSTGRES_READER_USER", cls.reader_user),
            writer_user=os.getenv("DATA_ANALYSIS_POSTGRES_WRITER_USER", cls.writer_user),
            statement_timeout_ms=int(
                os.getenv("DATA_ANALYSIS_STATEMENT_TIMEOUT_MS", str(cls.statement_timeout_ms))
            ),
            max_rows=int(os.getenv("DATA_ANALYSIS_MAX_ROWS", str(cls.max_rows))),
        )


class PostgresQueryAudit:
    """Persist policy and execution outcomes through the app-only database role."""

    def __init__(self, settings: PostgresConnectionSettings, model_name: str):
        self.settings = settings
        self.model_name = model_name

    def record(
        self,
        context: ToolContext,
        role: str,
        original_sql: str,
        *,
        status: str,
        final_sql: str | None = None,
        reason: str | None = None,
        elapsed_ms: int | None = None,
        row_count: int | None = None,
        error_message: str | None = None,
    ) -> None:
        connection = psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO app.query_audits (
                        run_id, request_id, conversation_id, user_id, user_role, question,
                        original_sql, final_sql, policy_status, policy_reason, model_name,
                        dataset_version_id, metric_version, elapsed_ms, row_count, error_message
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        context.metadata.get("run_id"),
                        context.request_id,
                        context.conversation_id,
                        context.user.id,
                        role,
                        context.metadata.get("question"),
                        original_sql,
                        final_sql,
                        status,
                        reason,
                        self.model_name,
                        "olist-kaggle-v2-2026-08-03",
                        "0.1-draft",
                        elapsed_ms,
                        row_count,
                        error_message,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def list_recent(self, user_id: str, role: str, limit: int = 20) -> list[dict[str, Any]]:
        connection = psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                if role == "admin":
                    cursor.execute(
                        "SELECT * FROM app.query_audits ORDER BY audit_id DESC LIMIT %s", (limit,)
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM app.query_audits WHERE user_id = %s ORDER BY audit_id DESC LIMIT %s",
                        (user_id, limit),
                    )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()


class SecurePostgresRunner(SqlRunner):
    """Run only policy-approved SQL through the dedicated readonly database role."""

    def __init__(
        self,
        settings: PostgresConnectionSettings | None = None,
        policy: SqlPolicy | None = None,
        model_name: str = "deepseek-ai/DeepSeek-V4-Flash",
    ):
        self.settings = settings or PostgresConnectionSettings.from_environment()
        self.policy = policy or SqlPolicy()
        self.audit = PostgresQueryAudit(self.settings, model_name)

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        return await asyncio.to_thread(self._run_sql_sync, args, context)

    def _role_for_context(self, context: ToolContext) -> str:
        return "admin" if "admin" in context.user.group_memberships else "analyst"

    def _run_sql_sync(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        role = self._role_for_context(context)
        started_at = perf_counter()
        try:
            decision = self.policy.evaluate(args.sql, role=role)
        except PolicyViolation as exc:
            self.audit.record(
                context, role, args.sql, status="rejected", reason=str(exc),
                elapsed_ms=int((perf_counter() - started_at) * 1000), error_message=str(exc),
            )
            raise

        try:
            connection = psycopg2.connect(
                host=self.settings.host,
                port=self.settings.port,
                database=self.settings.database,
                user=self.settings.reader_user,
            )
            try:
                connection.set_session(readonly=True, autocommit=False)
                with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute("SET LOCAL statement_timeout = %s", (self.settings.statement_timeout_ms,))
                    cursor.execute(decision.final_sql)
                    rows: list[dict[str, Any]] = [dict(row) for row in cursor.fetchmany(self.settings.max_rows)]
            finally:
                connection.close()
        except Exception as exc:
            self.audit.record(
                context, role, args.sql, status="execution_error", final_sql=decision.final_sql,
                reason="PostgreSQL execution failed", elapsed_ms=int((perf_counter() - started_at) * 1000),
                error_message=str(exc),
            )
            raise

        self.audit.record(
            context, role, args.sql, status="allowed", final_sql=decision.final_sql,
            reason=decision.reason, elapsed_ms=int((perf_counter() - started_at) * 1000),
            row_count=len(rows),
        )
        return pd.DataFrame(rows)
