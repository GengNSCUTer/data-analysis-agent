"""Persistent request-run evidence for the trusted Agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
from typing import Any

import psycopg2
import psycopg2.extras

from vanna.core.user import User

from .budget import BudgetUsage, RequestBudget
from .postgres_runner import PostgresConnectionSettings


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    request_id: str
    conversation_id: str


class PostgresRunRecorder:
    """Create and finalize one run row through the application writer role."""

    def __init__(
        self,
        settings: PostgresConnectionSettings | None = None,
        model_name: str = "unknown",
    ):
        self.settings = settings or PostgresConnectionSettings.from_environment()
        self.model_name = model_name

    async def start(
        self,
        *,
        request_id: str,
        conversation_id: str,
        user: User,
        question: str,
        budget: RequestBudget,
        dataset_version_id: str,
        metric_version: str,
    ) -> AgentRun:
        run = AgentRun(str(uuid.uuid4()), request_id, conversation_id)
        await asyncio.to_thread(
            self._start_sync,
            run,
            user,
            question,
            budget,
            dataset_version_id,
            metric_version,
        )
        return run

    async def finish(self, run: AgentRun, usage: BudgetUsage) -> None:
        await asyncio.to_thread(self._finish_sync, run, usage)

    def _connect(self):
        return psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )

    def _start_sync(
        self,
        run: AgentRun,
        user: User,
        question: str,
        budget: RequestBudget,
        dataset_version_id: str,
        metric_version: str,
    ) -> None:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO app.agent_runs (
                            run_id, request_id, conversation_id, user_id, user_role,
                            question, model_name, dataset_version_id, metric_version,
                            max_tool_iterations, max_tool_calls, max_sql_calls,
                            max_visualization_calls, max_input_chars, max_output_tokens
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run.run_id,
                            run.request_id,
                            run.conversation_id,
                            user.id,
                            "admin" if "admin" in user.group_memberships else "analyst",
                            question,
                            self.model_name,
                            dataset_version_id,
                            metric_version,
                            budget.max_tool_iterations,
                            budget.max_tool_calls,
                            budget.max_sql_calls,
                            budget.max_visualization_calls,
                            budget.max_input_chars,
                            budget.max_output_tokens,
                        ),
                    )
        finally:
            connection.close()

    def _finish_sync(self, run: AgentRun, usage: BudgetUsage) -> None:
        values = usage.as_dict()
        catalog_trace = dict(values["catalog_trace"] or {})
        performance = values.get("performance") or {}
        if performance:
            # Keep the existing schema stable while making latency breakdowns
            # queryable with the run's redacted Catalog evidence.
            catalog_trace["performance"] = performance
        if values.get("result_summary"):
            catalog_trace["result_summary"] = values["result_summary"]
        if values.get("result_contract_satisfied"):
            catalog_trace["result_contract_satisfied"] = True
        if values.get("extra_sql_suppressed"):
            catalog_trace["extra_sql_suppressed"] = values["extra_sql_suppressed"]
        if values.get("deterministic_result_finalized"):
            catalog_trace["deterministic_result_finalized"] = True
        if values.get("deterministic_result_finalization_disabled"):
            catalog_trace["deterministic_result_finalization_disabled"] = True
        if values.get("llm_observations"):
            catalog_trace["llm_observations"] = values["llm_observations"]
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE app.agent_runs
                        SET tool_calls_used = %s, sql_calls_used = %s,
                            visualization_calls_used = %s, llm_rounds_used = %s,
                            input_chars = %s, context_chars = %s,
                            input_tokens = %s, output_tokens = %s, total_tokens = %s,
                            context_truncated = %s, termination_reason = %s,
                            error_type = %s, catalog_trace = %s,
                            repair_evidence = %s, finished_at = %s
                        WHERE run_id = %s
                        """,
                        (
                            values["tool_calls_used"],
                            values["sql_calls_used"],
                            values["visualization_calls_used"],
                            values["llm_rounds_used"],
                            values["input_chars"],
                            values["context_chars"],
                            values["input_tokens"],
                            values["output_tokens"],
                            values["total_tokens"],
                            values["context_truncated"],
                            values["termination_reason"],
                            values["error_type"],
                            psycopg2.extras.Json(catalog_trace),
                            psycopg2.extras.Json(values["repair_evidence"] or {}),
                            datetime.now(timezone.utc),
                            run.run_id,
                        ),
                    )
        finally:
            connection.close()
