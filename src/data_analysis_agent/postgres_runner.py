"""Policy-enforced PostgreSQL runner with persistent query audit records."""

from __future__ import annotations

import asyncio
import threading
import anyio
from dataclasses import dataclass, replace
import os
import json
import uuid
from time import perf_counter
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2 import sql as pg_sql
from sqlglot import exp, parse_one

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.core.tool import ToolContext

from .sql_policy import DEFAULT_ANALYST_EXECUTION_LIMIT, PolicyViolation, SqlPolicy
from .result_validator import (
    ResultValidationError,
    ResultValidation,
    ResultValidator,
    build_result_summary,
)
from .sql_repair import SafeSqlExecutionError, sanitize_sql_error
from .metric_context import DATASET_VERSION, METRIC_VERSION, OLIST_WORKSPACE
from .workspace import WorkspaceProfile
from .query_filters import QueryFilter
from .result_budget import ResultBudget, ResultBudgetError, validate_runtime_result_budget
from .result_evidence import RESULT_EVIDENCE_PREFIX, build_result_evidence
from .join_evidence import build_join_probes


class _QueryCancellation:
    """Forward asyncio cancellation to the connection owned by the SQL thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.connection = None
        self.cancelled = False

    def attach(self, connection):
        with self.lock:
            self.connection = connection
            if self.cancelled:
                raise RuntimeError("query cancelled before execution")

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.connection is not None and not self.connection.closed:
                self.connection.cancel()

    def close(self, connection):
        with self.lock:
            self.connection = None
            connection.close()


async def _cancellable_query(function, *args):
    cancellation = _QueryCancellation()
    worker = asyncio.create_task(asyncio.to_thread(function, *args, cancellation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling to_thread alone leaves PostgreSQL running. Wait for its
        # rollback/close before releasing the request capacity slot.
        with anyio.CancelScope(shield=True):
            await asyncio.to_thread(cancellation.cancel)
            await asyncio.gather(worker, return_exceptions=True)
        raise


async def complete_database_io(function, *args):
    """Let a short metadata transaction finish before reporting cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await asyncio.gather(worker, return_exceptions=True)
        raise


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


def read_bounded_rows(cursor, *, probe_count: int, max_bytes: int, deadline: float):
    """Fetch batches from a server cursor; bound retained payload and total time."""
    rows: list[dict[str, Any]] = []
    size = 2  # JSON array brackets; bytes are a transport budget, not RSS.
    while len(rows) < probe_count:
        batch = cursor.fetchmany(min(1_000, probe_count - len(rows)))
        if not batch:
            break
        for source in batch:
            row = dict(source)
            size += len(json.dumps(row, default=str, ensure_ascii=False).encode("utf-8")) + 1
            if size > max_bytes or perf_counter() > deadline:
                reason = "结果超过字节预算，请缩小分组或时间范围。" if size > max_bytes else "结果读取超过时间预算，请缩小查询范围。"
                raise ResultValidationError(ResultValidation("needs_clarification", reason, len(rows), tuple(row)))
            rows.append(row)
    return rows, size


@dataclass(frozen=True)
class PostgresConnectionSettings:
    database: str = "data_analysis_agent"
    host: str = "/tmp"
    port: int = 35434
    reader_user: str = "daa_analytics_reader"
    writer_user: str = "daa_app_writer"
    statement_timeout_ms: int = 5_000
    max_rows: int = DEFAULT_ANALYST_EXECUTION_LIMIT
    max_aggregate_rows: int = 100_000
    max_result_bytes: int = 32 * 1024 * 1024

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
            max_aggregate_rows=int(os.getenv("DATA_ANALYSIS_MAX_AGGREGATE_ROWS", str(cls.max_aggregate_rows))),
            max_result_bytes=int(os.getenv("DATA_ANALYSIS_MAX_RESULT_BYTES", str(cls.max_result_bytes))),
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
                    RETURNING audit_id
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
                audit_id = cursor.fetchone()[0]
            connection.commit()
            context.metadata["query_audit_id"] = audit_id
            validation = context.metadata.get("result_validation") or {}
            validation_state = validation.get("state", "not_validated")
            context.metadata["audited_sql"] = final_sql if status == "allowed" else None
            usage = context.metadata.get("budget_usage")
            if usage is not None:
                attempts = dict((usage.catalog_trace or {}).get("execution_attempts") or {})
                attempts[str(audit_id)] = {"validation_status": validation_state}
                usage.record_catalog({"execution_attempts": attempts})
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
        model_name: str = "deepseek-ai/DeepSeek-V3.2",
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
        return await _cancellable_query(self._run_sql_sync, args, context)

    async def resolve_filter_sources(self, filters: tuple[QueryFilter, ...]) -> tuple[QueryFilter, ...]:
        """Locate display/raw values among already authorized Catalog columns.

        Only booleans leave the reader query. Unknown values remain valid
        filters (possibly with an empty result), rather than a language gate.
        """
        if not any(len(item.sources) > 1 for item in filters):
            return filters
        return await _cancellable_query(self._resolve_filter_sources_sync, filters)

    def _resolve_filter_sources_sync(self, filters, cancellation=None):
        connection = psycopg2.connect(
            host=self.settings.host, port=self.settings.port,
            database=self.settings.database, user=self.settings.reader_user,
        )
        try:
            if cancellation is not None:
                cancellation.attach(connection)
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = %s", (self.settings.statement_timeout_ms,))
                resolved = []
                for item in filters:
                    if len(item.sources) < 2:
                        resolved.append(item)
                        continue
                    checks = [pg_sql.SQL("EXISTS(SELECT 1 FROM {}.{} WHERE {} = %s)").format(
                        pg_sql.Identifier(self.workspace.analytics_schema), pg_sql.Identifier(table),
                        pg_sql.Identifier(column),
                    ) for table, column in item.sources]
                    cursor.execute(pg_sql.SQL("SELECT ") + pg_sql.SQL(", ").join(checks),
                                   (item.value,) * len(checks))
                    matched = tuple(source for source, exists in zip(item.sources, cursor.fetchone()) if exists)
                    resolved.append(replace(item, value_sources=matched))
                return tuple(resolved)
        finally:
            cancellation.close(connection) if cancellation is not None else connection.close()

    def _role_for_context(self, context: ToolContext) -> str:
        return "admin" if "admin" in context.user.group_memberships else "analyst"

    def _run_sql_sync(self, args: RunSqlToolArgs, context: ToolContext, cancellation=None) -> pd.DataFrame:
        # Each attempt owns its own validation; a repair must not inherit it.
        context.metadata.pop("result_validation", None)
        context.metadata.pop("query_audit_id", None)
        context.metadata.pop("audited_sql", None)
        context.metadata.pop("join_multiplicity", None)
        context.metadata.pop("join_evidence", None)
        role = self._role_for_context(context)
        started_at = perf_counter()
        try:
            try:
                result_budget = validate_runtime_result_budget(
                    context.metadata,
                    expected=ResultBudget.aggregate(
                        execution_max_rows=self.settings.max_aggregate_rows,
                        statement_timeout_ms=self.settings.statement_timeout_ms,
                        result_max_bytes=self.settings.max_result_bytes,
                    ),
                )
                if result_budget is not None:
                    context.metadata["result_budget_check"] = {
                        "contract_id": result_budget.contract_id,
                        "query_mode": result_budget.query_mode,
                        "state": "validated",
                        "evidence": result_budget.as_evidence(),
                    }
            except ResultBudgetError as exc:
                context.metadata["result_budget_check"] = {
                    "state": "rejected", "reason": str(exc),
                }
                raise PolicyViolation(f"result_budget:{exc}") from exc
            plan = context.metadata.get("query_plan") or {}
            top_n_values = list((plan.get("top_n") or {}).values())
            partitioned_top_n = bool(plan.get("top_n_partition_dimensions"))
            if result_budget is not None:
                decision = self.policy.evaluate(
                    args.sql, role=role, result_limit=result_budget.aggregate_result_rows,
                    complete_result=True,
                    top_n=None if partitioned_top_n else min(top_n_values) if top_n_values else None,
                )
            else:
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
                if cancellation is not None:
                    cancellation.attach(connection)
                connection.set_session(readonly=True, autocommit=False, isolation_level="REPEATABLE READ")
                timeout = result_budget.statement_timeout_ms if result_budget else self.settings.statement_timeout_ms
                with connection.cursor() as control:
                    control.execute("SET LOCAL statement_timeout = %s", (timeout,))
                with connection.cursor(
                    name="daa_" + uuid.uuid4().hex,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                ) as cursor:
                    probe_sql, probe_count = execution_probe(
                        decision.final_sql,
                        decision.row_limit,
                        policy_limit_applied=decision.policy_limit_applied,
                    )
                    deadline = perf_counter() + timeout / 1_000
                    cursor.execute(probe_sql)
                    fetched, result_bytes = read_bounded_rows(
                        cursor, probe_count=probe_count,
                        max_bytes=result_budget.result_max_bytes if result_budget else self.settings.max_result_bytes,
                        deadline=deadline,
                    )
                    context.metadata["result_bytes"] = result_bytes
                    rows, execution_truncated = cap_probed_rows(
                        fetched,
                        decision.row_limit,
                        policy_limit_applied=decision.policy_limit_applied,
                    )
                    frame = pd.DataFrame(rows, columns=[column.name for column in cursor.description])
                if context.metadata.get("join_source_keys"):
                    probes, uncovered = build_join_probes(
                        decision.final_sql,
                        row_keys=context.metadata["join_source_keys"],
                        source_grains=context.metadata.get("join_source_grains", {}),
                        metric_grains=context.metadata.get("metric_fact_grains", {}),
                        column_schema={self.workspace.analytics_schema: {
                            table: dict.fromkeys(columns, "UNKNOWN")
                            for table, columns in self.workspace.allowed_columns.items()
                        }},
                    )
                    counts, evidence = {}, []
                    check_started = perf_counter()
                    with connection.cursor() as control:
                        for probe in probes:
                            remaining_ms = int((deadline - perf_counter()) * 1_000)
                            if remaining_ms <= 0:
                                raise ResultValidationError(ResultValidation(
                                    "refuse", "查询及关联证据检查超过执行时间预算。", len(frame), tuple(frame.columns)))
                            diagnostic = self.policy.evaluate(probe.sql, role=role, complete_result=True, result_limit=1)
                            control.execute("SET LOCAL statement_timeout = %s", (remaining_ms,))
                            control.execute(diagnostic.final_sql)
                            inputs, facts = (int(n) for n in control.fetchone())
                            # Pigeonhole lower bound: proves repetition, not
                            # the exact maximum copies of any individual row.
                            count = (inputs + facts - 1) // facts if facts else 1
                            counts[probe.name] = count
                            evidence.append({"source_table":probe.table, "aggregate":probe.aggregate,
                                             "duplicate_group_input_rows":inputs, "duplicate_group_distinct_facts":facts,
                                             "multiplicity_lower_bound":count})
                            if count > 1:
                                break
                    context.metadata["join_multiplicity"] = counts
                    context.metadata["join_evidence"] = {
                        "status": "amplified" if any(n>1 for n in counts.values()) else "partial" if uncovered else "checked" if counts else "not_applicable",
                        "checks":evidence, "uncovered":uncovered,
                        "elapsed_ms":int((perf_counter()-check_started)*1_000),
                    }
                    self._record_timing(context, "join_evidence", check_started)
                    usage = context.metadata.get("budget_usage")
                    if usage is not None:
                        usage.record_catalog({"join_evidence":context.metadata["join_evidence"]})
            finally:
                cancellation.close(connection) if cancellation is not None else connection.close()
        except ResultValidationError as exc:
            context.metadata["result_validation"] = exc.validation.as_dict()
            self.audit.record(
                context, role, args.sql, status="execution_error", final_sql=decision.final_sql,
                reason="result_budget:" + exc.validation.reason,
                elapsed_ms=int((perf_counter() - started_at) * 1000),
                row_count=exc.validation.row_count,
            )
            raise
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
            validator = (
                ResultValidator(
                    max_rows=result_budget.aggregate_result_rows,
                    display_row_budget=min(result_budget.preview_rows, result_budget.aggregate_result_rows),
                ) if result_budget else self.result_validator
            )
            time_bucket_grain = context.metadata.get("result_time_grain")
            query_plan = context.metadata.get("query_plan")
            if time_bucket_grain is None and isinstance(query_plan, dict):
                time_bucket_grain = query_plan.get("time_grain")
            validation = validator.validate(
                frame,
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
                complete_required=result_budget is not None,
                join_multiplicity=context.metadata.get("join_multiplicity"),
                exact_columns=bool(context.metadata.get("exact_result_columns", False)),
                metric_value_constraints=context.metadata.get(
                    "metric_value_constraints", {}
                ),
            )
            context.metadata["result_validation"] = validation.as_dict()
            context.metadata["display_result_row_budget"] = result_budget.preview_rows if result_budget else validator.display_row_budget
            context.metadata["execution_row_limit"] = validator.max_rows
            if result_budget:
                context.metadata["result_status"] = (
                    "result_cardinality_exceeded" if execution_truncated else
                    "display_truncated" if len(rows) > result_budget.preview_rows else "complete"
                )
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
            validated_frame = frame
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
            usage = context.metadata.get("budget_usage")
            summary_arguments = dict(
                metric_ids=context.metadata.get("metric_result_columns", ()),
                required_columns=context.metadata.get("required_result_columns", ()),
                column_aliases=context.metadata.get("required_result_column_aliases", {}),
                column_labels=context.metadata.get("result_column_labels", {}),
            )
            if result_budget is not None:
                plan = context.metadata.get("query_plan") or {}
                result_summary = build_result_evidence(
                    validated_frame, **summary_arguments,
                    additive_metric_ids=context.metadata.get("additive_metric_ids", ()),
                    time_column="time" if plan.get("time_grain") else None,
                    top_n=plan.get("top_n"),
                    query_scope=plan,
                    metric_semantics=context.metadata.get("metric_semantics", {}),
                    inspections=usage.result_inspections if usage is not None else (),
                    numerator_overrides=usage.result_numerators if usage is not None else None,
                    max_tokens=usage.result_evidence_limit() if usage is not None else None,
                )
            else:
                # Frozen offline callers retain their historical sample format.
                result_summary = build_result_summary(
                    validated_frame,
                    validation,
                    **summary_arguments,
                )
            context.metadata["validated_result_summary"] = result_summary
            if usage is not None and hasattr(usage, "set_result_summary"):
                usage.set_result_summary(result_summary)
                if result_budget is not None:
                    evidence = json.loads(result_summary.removeprefix(RESULT_EVIDENCE_PREFIX))
                    usage.record_catalog({"result_evidence": {
                        "token_limit": evidence["evidence_token_limit"],
                        "source_row_count": len(validated_frame),
                        "included_row_count": evidence["included_row_count"],
                        "coverage": evidence["coverage"],
                        "scope": evidence["scope"],
                    }})
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
        return frame

    @staticmethod
    def _record_timing(context: ToolContext, phase: str, started_at: float) -> None:
        usage = context.metadata.get("budget_usage")
        if usage is not None and hasattr(usage, "record_timing"):
            usage.record_timing(phase, int((perf_counter() - started_at) * 1000))
