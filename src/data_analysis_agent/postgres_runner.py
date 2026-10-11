"""Policy-enforced PostgreSQL runner with persistent query audit records."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import os
from time import perf_counter
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras
from sqlglot import exp, parse_one

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.core.tool import ToolContext

from .sql_policy import DEFAULT_ANALYST_EXECUTION_LIMIT, PolicyViolation, SqlPolicy
from .result_validator import (
    ResultValidationError,
    ResultValidator,
    build_result_summary,
)
from .sql_repair import SafeSqlExecutionError, sanitize_sql_error
from .metric_context import DATASET_VERSION, METRIC_VERSION, OLIST_WORKSPACE
from .workspace import WorkspaceProfile


def execution_probe(final_sql: str, row_limit: int, *, policy_limit_applied: bool) -> tuple[str, int]:
    """Read one extra row only when policy itself capped the statement.

    The extra row is not part of the SQL stored in the audit. Callers must
    drop it before validation, summaries, charts, or the model prompt.
    """

    if not policy_limit_applied:
        return final_sql, row_limit
    statement = parse_one(final_sql, read="postgres")
    statement.set("limit", exp.Limit(expression=exp.Literal.number(row_limit + 1)))
    return statement.sql(dialect="postgres"), row_limit + 1


def cap_probed_rows(
    rows: list[dict[str, Any]],
    row_limit: int,
    *,
    policy_limit_applied: bool,
) -> tuple[list[dict[str, Any]], bool]:
    if policy_limit_applied and len(rows) > row_limit:
        return rows[:row_limit], True
    return rows, False


@dataclass(frozen=True)
class PostgresConnectionSettings:
    database: str = "data_analysis_agent"
    host: str = "/tmp"
    port: int = 35434
    reader_user: str = "daa_analytics_reader"
    writer_user: str = "daa_app_writer"
    statement_timeout_ms: int = 5_000
    max_rows: int = DEFAULT_ANALYST_EXECUTION_LIMIT

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
        repair_evidence: dict[str, Any] | None = None,
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
                        dataset_version_id, metric_version, elapsed_ms, row_count,
                        error_message, repair_evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        context.metadata.get("dataset_version_id", DATASET_VERSION),
                        context.metadata.get("metric_version", METRIC_VERSION),
                        elapsed_ms,
                        row_count,
                        error_message,
                        psycopg2.extras.Json(repair_evidence)
                        if repair_evidence is not None
                        else None,
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
        result_validator: ResultValidator | None = None,
        workspace: WorkspaceProfile | None = None,
    ):
        self.workspace = workspace or OLIST_WORKSPACE
        configured_settings = settings or PostgresConnectionSettings.from_environment()
        if workspace is not None:
            configured_settings = replace(
                configured_settings,
                reader_user=workspace.reader_role,
                writer_user=workspace.writer_role,
            )
        self.settings = configured_settings
        # A directly constructed online runner follows the database execution
        # cap (normally 1,000 rows). Protected/offline contracts inject their
        # own explicit 200-row policy and are therefore unaffected.
        self.policy = policy or SqlPolicy(
            analyst_limit=self.settings.max_rows,
            admin_limit=self.settings.max_rows,
            workspace=self.workspace,
        )
        self.audit = PostgresQueryAudit(self.settings, model_name)
        # Result validation is part of the trusted execution boundary. A
        # direct runner construction must not accidentally bypass it just
        # because the composition root omitted an explicit dependency.
        self.result_validator = result_validator or ResultValidator(self.settings.max_rows)

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
            self._record_timing(context, "sql_policy", started_at)
            self.audit.record(
                context, role, args.sql, status="rejected", reason=str(exc),
                elapsed_ms=int((perf_counter() - started_at) * 1000),
                error_message=str(exc),
                repair_evidence=context.metadata.get("repair_evidence"),
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
                    probe_sql, probe_count = execution_probe(
                        decision.final_sql,
                        decision.row_limit,
                        policy_limit_applied=decision.policy_limit_applied,
                    )
                    cursor.execute(probe_sql)
                    fetched = [dict(row) for row in cursor.fetchmany(probe_count)]
                    rows, execution_truncated = cap_probed_rows(
                        fetched,
                        decision.row_limit,
                        policy_limit_applied=decision.policy_limit_applied,
                    )
            finally:
                connection.close()
        except Exception as exc:
            self._record_timing(context, "postgres_sql", started_at)
            safe_error = sanitize_sql_error(exc)
            context.metadata["safe_sql_error"] = safe_error
            context.metadata["sql_error"] = safe_error.as_dict()
            self.audit.record(
                context, role, args.sql, status="execution_error", final_sql=decision.final_sql,
                reason="PostgreSQL execution failed", elapsed_ms=int((perf_counter() - started_at) * 1000),
                error_message=safe_error.public_reason,
                repair_evidence=context.metadata.get("repair_evidence"),
            )
            raise SafeSqlExecutionError(safe_error) from exc

        if self.result_validator is not None:
            time_bucket_grain = context.metadata.get("result_time_grain")
            query_plan = context.metadata.get("query_plan")
            if time_bucket_grain is None and isinstance(query_plan, dict):
                time_bucket_grain = query_plan.get("time_grain")
            validation = self.result_validator.validate(
                pd.DataFrame(rows),
                required_columns=context.metadata.get("required_result_columns", ()),
                required_column_aliases=context.metadata.get(
                    "required_result_column_aliases", {}
                ),
                metric_columns=context.metadata.get("metric_result_columns", ()),
                time_column=context.metadata.get("result_time_column"),
                time_column_aliases=context.metadata.get("result_time_column_aliases", ()),
                time_bucket_grain=time_bucket_grain,
                requested_start=context.metadata.get("requested_start"),
                requested_end=context.metadata.get("requested_end"),
                requested_end_exclusive=context.metadata.get("requested_end_exclusive", False) is True,
                # Truncation is true only when the policy cap probe saw one
                # extra row. That row is already discarded. An explicit
                # smaller LIMIT is not evidence of a policy truncation.
                limit_applied=False,
                execution_truncated=execution_truncated,
                join_multiplicity=context.metadata.get("join_multiplicity"),
                exact_columns=bool(context.metadata.get("exact_result_columns", False)),
                metric_value_constraints=context.metadata.get(
                    "metric_value_constraints", {}
                ),
            )
            context.metadata["result_validation"] = validation.as_dict()
            context.metadata["display_result_row_budget"] = self.result_validator.display_row_budget
            context.metadata["execution_row_limit"] = self.result_validator.max_rows
            if not validation.safe_to_display:
                self._record_timing(context, "postgres_sql", started_at)
                self.audit.record(
                    context,
                    role,
                    args.sql,
                    status="execution_error",
                    final_sql=decision.final_sql,
                    reason=f"result_validation:{validation.reason}",
                    elapsed_ms=int((perf_counter() - started_at) * 1000),
                    row_count=len(rows),
                    repair_evidence=context.metadata.get("repair_evidence"),
                )
                raise ResultValidationError(validation)
            validated_frame = pd.DataFrame(rows)
            if validation.dropped_columns:
                validated_frame = validated_frame.drop(
                    columns=list(validation.dropped_columns), errors="ignore"
                )
            if not validation.safe_to_answer:
                context.metadata["result_display_notice"] = validation.reason
                self.audit.record(
                    context,
                    role,
                    args.sql,
                    status="allowed",
                    final_sql=decision.final_sql,
                    reason=f"displayed:{validation.reason}",
                    elapsed_ms=int((perf_counter() - started_at) * 1000),
                    row_count=len(validated_frame),
                    repair_evidence=context.metadata.get("repair_evidence"),
                )
                self._record_timing(context, "postgres_sql", started_at)
                return validated_frame
            result_summary = build_result_summary(
                validated_frame,
                validation,
                metric_ids=context.metadata.get("metric_result_columns", ()),
                required_columns=context.metadata.get("required_result_columns", ()),
                column_aliases=context.metadata.get(
                    "required_result_column_aliases", {}
                ),
                column_labels=context.metadata.get("result_column_labels", {}),
                max_rows=8,
                max_chars=1_200,
            )
            context.metadata["validated_result_summary"] = result_summary
            usage = context.metadata.get("budget_usage")
            if usage is not None and hasattr(usage, "set_result_summary"):
                usage.set_result_summary(result_summary)
            if usage is not None and hasattr(usage, "mark_result_contract_satisfied"):
                usage.mark_result_contract_satisfied()
            chart_metadata = context.metadata.get("chart_contract")
            if (
                usage is not None
                and isinstance(chart_metadata, dict)
                and chart_metadata.get("status") == "valid"
                and hasattr(usage, "set_validated_chart_frame")
            ):
                usage.set_validated_chart_frame(validated_frame)
            context.metadata["result_contract_satisfied"] = True

        self.audit.record(
            context, role, args.sql, status="allowed", final_sql=decision.final_sql,
            reason=decision.reason, elapsed_ms=int((perf_counter() - started_at) * 1000),
            row_count=len(rows),
            repair_evidence=context.metadata.get("repair_evidence"),
        )
        self._record_timing(context, "postgres_sql", started_at)
        return pd.DataFrame(rows)

    @staticmethod
    def _record_timing(context: ToolContext, phase: str, started_at: float) -> None:
        usage = context.metadata.get("budget_usage")
        if usage is not None and hasattr(usage, "record_timing"):
            usage.record_timing(phase, int((perf_counter() - started_at) * 1000))
