"""Isolated SQLite execution support for offline Text-to-SQL benchmarks.

This module deliberately sits outside the Vanna/FastAPI runtime.  It exists to
evaluate benchmark candidates against their native SQLite databases without
turning the production PostgreSQL agent into a multi-dialect service.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Any, Final, Iterable, Mapping

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from data_analysis_agent.text_to_sql_output import normalize_text_to_sql_candidate


SQLITE_DIALECT: Final = "sqlite"
FORBIDDEN_SQLITE_NODES: Final[tuple[type[exp.Expression], ...]] = (
    exp.Alter,
    exp.Attach,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Detach,
    exp.Drop,
    exp.Insert,
    exp.Into,
    exp.Merge,
    exp.Pragma,
    exp.Transaction,
    exp.TruncateTable,
    exp.Update,
)
FORBIDDEN_SQLITE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
    }
)
AUTHORIZE_DENY_ACTIONS: Final[frozenset[int]] = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_ANALYZE", None),
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_PRAGMA", None),
        getattr(sqlite3, "SQLITE_REINDEX", None),
        getattr(sqlite3, "SQLITE_TRANSACTION", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
    )
    if action is not None
)


class SqliteBenchmarkPolicyViolation(ValueError):
    """A benchmark candidate is unsafe or not a single read-only query."""


class BenchmarkInputError(ValueError):
    """A normalized benchmark case or prediction is invalid."""


@dataclass(frozen=True)
class SqliteBenchmarkSettings:
    """Server-owned resource limits for one local benchmark execution."""

    statement_timeout_ms: int = 5_000
    max_rows: int = 1_000
    progress_handler_steps: int = 1_000

    def __post_init__(self) -> None:
        if self.statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be greater than zero")
        if self.max_rows <= 0:
            raise ValueError("max_rows must be greater than zero")
        if self.progress_handler_steps <= 0:
            raise ValueError("progress_handler_steps must be greater than zero")


@dataclass(frozen=True)
class SqliteBenchmarkPolicyDecision:
    """Normalized candidate after the offline SQLite policy has accepted it."""

    original_sql: str
    final_sql: str
    row_limit: int
    status: str = "allowed"
    reason: str = "SQL passed the isolated SQLite benchmark policy"


@dataclass(frozen=True)
class SqliteExecutionOutcome:
    """Non-throwing execution evidence safe to write into a local report."""

    status: str
    original_sql: str
    final_sql: str | None
    row_limit: int | None
    elapsed_ms: int
    row_count: int | None
    columns: tuple[str, ...]
    error_type: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkCase:
    """Dataset-neutral case locator; question text is intentionally not retained."""

    case_id: str
    database_id: str
    database_path: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BenchmarkCase":
        return cls(
            case_id=_required_string(raw, "case_id"),
            database_id=_required_string(raw, "database_id"),
            database_path=_required_relative_path(raw, "database_path"),
        )


@dataclass(frozen=True)
class SqlPrediction:
    """One model candidate associated with a normalized benchmark case."""

    case_id: str
    candidate_sql: str
    candidate_index: int = 0
    generated_tokens: int | None = None
    generation_elapsed_ms: int | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SqlPrediction":
        case_id = _required_string(raw, "case_id")
        candidate_sql = _required_string(raw, "candidate_sql")
        candidate_index = _optional_non_negative_int(raw, "candidate_index", 0)
        generated_tokens = _optional_non_negative_int(raw, "generated_tokens", None)
        generation_elapsed_ms = _optional_non_negative_int(
            raw, "generation_elapsed_ms", None
        )
        return cls(
            case_id=case_id,
            candidate_sql=candidate_sql,
            candidate_index=candidate_index,
            generated_tokens=generated_tokens,
            generation_elapsed_ms=generation_elapsed_ms,
        )


@dataclass(frozen=True)
class BenchmarkRunMetadata:
    """Versions needed to compare two offline candidate-generation runs."""

    dataset_id: str
    dataset_version: str
    model_id: str
    model_version: str
    prompt_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "dataset_id",
            "dataset_version",
            "model_id",
            "model_version",
            "prompt_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True)
class OfficialExSummary:
    """Evidence emitted by an unmodified official benchmark evaluator."""

    dataset_id: str
    evaluator_name: str
    evaluator_version: str
    execution_accuracy: float
    evaluated_cases: int
    source: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OfficialExSummary":
        dataset_id = _required_string(raw, "dataset_id")
        evaluator_name = _required_string(raw, "evaluator_name")
        evaluator_version = _required_string(raw, "evaluator_version")
        source = _required_string(raw, "source")
        execution_accuracy = raw.get("execution_accuracy")
        if (
            isinstance(execution_accuracy, bool)
            or not isinstance(execution_accuracy, (int, float))
            or not 0 <= float(execution_accuracy) <= 1
        ):
            raise BenchmarkInputError("execution_accuracy must be a number in [0, 1]")
        evaluated_cases = _optional_non_negative_int(raw, "evaluated_cases", None)
        if evaluated_cases is None or evaluated_cases == 0:
            raise BenchmarkInputError("evaluated_cases must be greater than zero")
        return cls(
            dataset_id=dataset_id,
            evaluator_name=evaluator_name,
            evaluator_version=evaluator_version,
            execution_accuracy=float(execution_accuracy),
            evaluated_cases=evaluated_cases,
            source=source,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SqliteBenchmarkPolicy:
    """AST validation and result-size normalization for offline SQLite SQL."""

    def __init__(self, max_rows: int = 1_000):
        if max_rows <= 0:
            raise ValueError("max_rows must be greater than zero")
        self.max_rows = max_rows

    def evaluate(self, sql: str) -> SqliteBenchmarkPolicyDecision:
        if not sql.strip():
            raise SqliteBenchmarkPolicyViolation("SQL must not be empty")
        try:
            statements = [statement for statement in parse(sql, read=SQLITE_DIALECT) if statement]
        except SqlglotError as exc:
            raise SqliteBenchmarkPolicyViolation(f"SQL parse failed: {exc}") from exc
        if len(statements) != 1:
            raise SqliteBenchmarkPolicyViolation("exactly one SQL statement is allowed")

        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise SqliteBenchmarkPolicyViolation("only SELECT or read-only WITH queries are allowed")
        forbidden_node = next(
            (
                node
                for node in statement.walk()
                if isinstance(node, FORBIDDEN_SQLITE_NODES)
            ),
            None,
        )
        if forbidden_node is not None:
            raise SqliteBenchmarkPolicyViolation(
                f"forbidden SQLite operation: {forbidden_node.key}"
            )

        for function in statement.find_all(exp.Func):
            function_name = (function.name or function.sql_name()).lower()
            if (
                function_name in FORBIDDEN_SQLITE_FUNCTIONS
                or function_name.startswith("pragma_")
            ):
                raise SqliteBenchmarkPolicyViolation(
                    f"SQLite function is not allowed: {function_name}"
                )

        limit_expression = statement.args.get("limit")
        if limit_expression is None:
            statement = statement.limit(self.max_rows)
            row_limit = self.max_rows
        else:
            value = limit_expression.expression
            if not isinstance(value, exp.Literal) or not value.is_int:
                raise SqliteBenchmarkPolicyViolation("LIMIT must be a literal integer")
            requested = int(value.this)
            if requested <= 0:
                raise SqliteBenchmarkPolicyViolation("LIMIT must be greater than zero")
            row_limit = min(requested, self.max_rows)
            if requested > self.max_rows:
                statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_rows)))

        return SqliteBenchmarkPolicyDecision(
            original_sql=sql,
            final_sql=statement.sql(dialect=SQLITE_DIALECT),
            row_limit=row_limit,
        )


class ReadOnlySqliteExecutor:
    """Execute one benchmark SQL candidate against an immutable SQLite file.

    The executor returns execution evidence instead of surfacing model SQL
    failures as exceptions.  Callers can therefore keep a complete report of
    parser, policy, timeout and SQLite execution outcomes.
    """

    def __init__(
        self,
        settings: SqliteBenchmarkSettings | None = None,
        policy: SqliteBenchmarkPolicy | None = None,
    ):
        self.settings = settings or SqliteBenchmarkSettings()
        self.policy = policy or SqliteBenchmarkPolicy(self.settings.max_rows)

    def execute(self, database_path: Path, sql: str) -> SqliteExecutionOutcome:
        started_at = perf_counter()
        normalized_sql = normalize_text_to_sql_candidate(sql)
        try:
            decision = self.policy.evaluate(normalized_sql)
        except SqliteBenchmarkPolicyViolation as exc:
            return SqliteExecutionOutcome(
                status="policy_rejected",
                original_sql=sql,
                final_sql=None,
                row_limit=None,
                elapsed_ms=_elapsed_ms(started_at),
                row_count=None,
                columns=(),
                error_type="policy",
                error_message=str(exc),
            )

        try:
            rows, columns = self._execute_read_only(database_path, decision.final_sql)
        except sqlite3.OperationalError as exc:
            is_timeout = "interrupted" in str(exc).lower()
            return SqliteExecutionOutcome(
                status="timeout" if is_timeout else "execution_error",
                original_sql=sql,
                final_sql=decision.final_sql,
                row_limit=decision.row_limit,
                elapsed_ms=_elapsed_ms(started_at),
                row_count=None,
                columns=(),
                error_type="timeout" if is_timeout else "sqlite_operational_error",
                error_message="SQLite query timed out" if is_timeout else str(exc),
            )
        except sqlite3.DatabaseError as exc:
            return SqliteExecutionOutcome(
                status="execution_error",
                original_sql=sql,
                final_sql=decision.final_sql,
                row_limit=decision.row_limit,
                elapsed_ms=_elapsed_ms(started_at),
                row_count=None,
                columns=(),
                error_type="sqlite_database_error",
                error_message=str(exc),
            )

        return SqliteExecutionOutcome(
            status="executed",
            original_sql=sql,
            final_sql=decision.final_sql,
            row_limit=decision.row_limit,
            elapsed_ms=_elapsed_ms(started_at),
            row_count=len(rows),
            columns=columns,
        )

    def _execute_read_only(
        self, database_path: Path, final_sql: str
    ) -> tuple[list[sqlite3.Row], tuple[str, ...]]:
        resolved_path = database_path.resolve(strict=True)
        connection = sqlite3.connect(resolved_path.as_uri() + "?mode=ro", uri=True)
        deadline = perf_counter() + self.settings.statement_timeout_ms / 1_000
        try:
            try:
                connection.enable_load_extension(False)
            except AttributeError:  # pragma: no cover - Python builds without extension support
                pass
            connection.execute("PRAGMA query_only = ON")
            connection.set_authorizer(_read_only_authorizer)
            connection.set_progress_handler(
                lambda: int(perf_counter() >= deadline),
                self.settings.progress_handler_steps,
            )
            cursor = connection.execute(final_sql)
            rows = cursor.fetchmany(self.settings.max_rows)
            columns = tuple(column[0] for column in cursor.description or ())
            return rows, columns
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()


def resolve_benchmark_database_path(database_root: Path, relative_path: str) -> Path:
    """Resolve a declared benchmark database without allowing root escape."""

    root = database_root.resolve(strict=True)
    candidate = (root / relative_path).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise BenchmarkInputError("database_path must stay below the database root")
    if not candidate.is_file():
        raise BenchmarkInputError("database_path must refer to a SQLite database file")
    return candidate


def load_normalized_cases(items: Iterable[Mapping[str, Any]]) -> list[BenchmarkCase]:
    """Load unique normalized cases without carrying benchmark question text."""

    cases = [BenchmarkCase.from_mapping(item) for item in items]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise BenchmarkInputError("case_id values must be unique")
    return cases


def load_predictions(items: Iterable[Mapping[str, Any]]) -> list[SqlPrediction]:
    """Load candidates while preventing duplicate `(case_id, candidate_index)` records."""

    predictions = [SqlPrediction.from_mapping(item) for item in items]
    keys = [(prediction.case_id, prediction.candidate_index) for prediction in predictions]
    if len(keys) != len(set(keys)):
        raise BenchmarkInputError("each case_id/candidate_index pair must be unique")
    return predictions


def normalize_bird_dev_cases(items: Iterable[Mapping[str, Any]]) -> list[BenchmarkCase]:
    """Build stable BIRD-dev database locators from the native ``dev.json`` list.

    BIRD's questions, evidence strings and gold SQL remain in the source dataset
    directory.  The normalized in-memory records intentionally retain only the
    official list order, database ID and database-relative SQLite path.
    """

    return _normalize_native_sqlite_cases(items, dataset_id="bird_dev")


def normalize_spider_dev_cases(items: Iterable[Mapping[str, Any]]) -> list[BenchmarkCase]:
    """Build stable Spider-dev database locators from the native ``dev.json`` list."""

    return _normalize_native_sqlite_cases(items, dataset_id="spider_dev")


def run_sqlite_benchmark(
    *,
    cases: Iterable[BenchmarkCase],
    predictions: Iterable[SqlPrediction],
    metadata: BenchmarkRunMetadata,
    database_root: Path,
    settings: SqliteBenchmarkSettings | None = None,
    official_ex: OfficialExSummary | None = None,
) -> dict[str, Any]:
    """Execute externally generated SQL and return a normalized local report.

    This function deliberately does not calculate execution accuracy from its
    own SQLite runs.  BIRD/Spider EX must come from the benchmark's unmodified
    official evaluator and can be attached through ``official_ex``.
    """

    case_list = list(cases)
    case_by_id = {case.case_id: case for case in case_list}
    if len(case_by_id) != len(case_list):
        raise BenchmarkInputError("case_id values must be unique")
    prediction_list = list(predictions)
    unknown_prediction_cases = sorted(
        {prediction.case_id for prediction in prediction_list} - set(case_by_id)
    )
    if unknown_prediction_cases:
        raise BenchmarkInputError(
            "predictions reference unknown case_id values: "
            + ", ".join(unknown_prediction_cases)
        )
    if official_ex is not None and official_ex.dataset_id != metadata.dataset_id:
        raise BenchmarkInputError("official EX dataset_id must match the run dataset_id")

    grouped_predictions: dict[str, list[SqlPrediction]] = defaultdict(list)
    for prediction in prediction_list:
        grouped_predictions[prediction.case_id].append(prediction)
    for candidates in grouped_predictions.values():
        candidates.sort(key=lambda candidate: candidate.candidate_index)

    resolved_databases = {
        case.case_id: resolve_benchmark_database_path(database_root, case.database_path)
        for case in case_list
        if grouped_predictions.get(case.case_id)
    }
    execution_settings = settings or SqliteBenchmarkSettings()
    executor = ReadOnlySqliteExecutor(settings=execution_settings)
    records: list[dict[str, Any]] = []
    for case in case_list:
        candidates = grouped_predictions.get(case.case_id, [])
        if not candidates:
            records.append(
                {
                    "case_id": case.case_id,
                    "database_id": case.database_id,
                    "database_path": case.database_path,
                    "dialect": SQLITE_DIALECT,
                    "candidate_index": None,
                    "candidate_count": 0,
                    "generated_tokens": None,
                    "generation_elapsed_ms": None,
                    "execution": {
                        "status": "missing_prediction",
                        "original_sql": None,
                        "final_sql": None,
                        "row_limit": None,
                        "elapsed_ms": 0,
                        "row_count": None,
                        "columns": [],
                        "error_type": "input",
                        "error_message": "No SQL candidate was supplied for this case",
                    },
                }
            )
            continue
        for prediction in candidates:
            outcome = executor.execute(
                resolved_databases[case.case_id], prediction.candidate_sql
            )
            records.append(
                {
                    "case_id": case.case_id,
                    "database_id": case.database_id,
                    "database_path": case.database_path,
                    "dialect": SQLITE_DIALECT,
                    "candidate_index": prediction.candidate_index,
                    "candidate_count": len(candidates),
                    "generated_tokens": prediction.generated_tokens,
                    "generation_elapsed_ms": prediction.generation_elapsed_ms,
                    "execution": outcome.as_dict(),
                }
            )

    status_counts = Counter(record["execution"]["status"] for record in records)
    policy_allowed = sum(
        record["execution"]["final_sql"] is not None for record in records
    )
    official_evaluation = (
        official_ex.as_dict()
        if official_ex is not None
        else {
            "status": "not_run",
            "execution_accuracy": None,
            "note": (
                "Local SQLite execution is diagnostic only. Run the benchmark's "
                "unmodified official evaluator before reporting EX."
            ),
        }
    )
    return {
        "report_version": "1",
        "mode": "offline_sqlite_benchmark",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "metadata": asdict(metadata),
        "dialect": SQLITE_DIALECT,
        "settings": asdict(execution_settings),
        "official_evaluation": official_evaluation,
        "summary": {
            "total_cases": len(case_list),
            "total_candidate_records": len(records),
            "cases_with_predictions": sum(
                bool(grouped_predictions.get(case.case_id)) for case in case_list
            ),
            "cases_missing_predictions": status_counts["missing_prediction"],
            "policy_allowed_candidates": policy_allowed,
            "executed_candidates": status_counts["executed"],
            "execution_error_candidates": status_counts["execution_error"],
            "timeout_candidates": status_counts["timeout"],
            "policy_rejected_candidates": status_counts["policy_rejected"],
            "status_counts": dict(sorted(status_counts.items())),
        },
        "records": records,
    }


def _normalize_native_sqlite_cases(
    items: Iterable[Mapping[str, Any]], *, dataset_id: str
) -> list[BenchmarkCase]:
    normalized = []
    for index, item in enumerate(items):
        database_id = _required_string(item, "db_id")
        normalized.append(
            BenchmarkCase(
                case_id=f"{dataset_id}:{index:05d}",
                database_id=database_id,
                database_path=f"{database_id}/{database_id}.sqlite",
            )
        )
    return load_normalized_cases(normalized_case.__dict__ for normalized_case in normalized)


def _read_only_authorizer(
    action_code: int,
    _parameter_one: str | None,
    _parameter_two: str | None,
    _database_name: str | None,
    _trigger_or_view: str | None,
) -> int:
    return sqlite3.SQLITE_DENY if action_code in AUTHORIZE_DENY_ACTIONS else sqlite3.SQLITE_OK


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkInputError(f"{key} must be a non-empty string")
    return value.strip()


def _required_relative_path(raw: Mapping[str, Any], key: str) -> str:
    value = _required_string(raw, key)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BenchmarkInputError(f"{key} must be a relative path below the database root")
    return path.as_posix()


def _optional_non_negative_int(
    raw: Mapping[str, Any], key: str, default: int | None
) -> int | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BenchmarkInputError(f"{key} must be a non-negative integer when provided")
    return value


def _elapsed_ms(started_at: float) -> int:
    return round((perf_counter() - started_at) * 1_000)
