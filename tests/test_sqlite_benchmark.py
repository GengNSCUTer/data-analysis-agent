from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from data_analysis_agent.sqlite_benchmark import (
    BenchmarkRunMetadata,
    BenchmarkInputError,
    OfficialExSummary,
    ReadOnlySqliteExecutor,
    SqliteBenchmarkPolicy,
    SqliteBenchmarkPolicyViolation,
    SqliteBenchmarkSettings,
    load_normalized_cases,
    load_predictions,
    normalize_bird_dev_cases,
    resolve_benchmark_database_path,
    run_sqlite_benchmark,
)


@pytest.fixture()
def benchmark_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "benchmark.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE metrics (metric_id INTEGER PRIMARY KEY, value INTEGER)")
        connection.executemany(
            "INSERT INTO metrics (value) VALUES (?)",
            [(1,), (2,), (3,), (4,)],
        )
        connection.commit()
    finally:
        connection.close()
    return database_path


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM metrics",
        "SELECT 1; DROP TABLE metrics",
        "PRAGMA table_info(metrics)",
        "ATTACH DATABASE 'other.sqlite' AS other",
        "WITH changed AS (DELETE FROM metrics RETURNING metric_id) SELECT * FROM changed",
        "SELECT load_extension('extension')",
        "SELECT * FROM pragma_table_info('metrics')",
    ],
)
def test_sqlite_benchmark_policy_rejects_unsafe_queries(sql: str) -> None:
    policy = SqliteBenchmarkPolicy(max_rows=2)

    with pytest.raises(SqliteBenchmarkPolicyViolation):
        policy.evaluate(sql)


def test_sqlite_benchmark_policy_normalizes_limit() -> None:
    decision = SqliteBenchmarkPolicy(max_rows=2).evaluate(
        "SELECT value FROM metrics ORDER BY value LIMIT 99"
    )

    assert decision.row_limit == 2
    assert decision.final_sql.endswith("LIMIT 2")


def test_read_only_sqlite_executor_returns_execution_evidence(
    benchmark_database: Path,
) -> None:
    executor = ReadOnlySqliteExecutor(
        settings=SqliteBenchmarkSettings(statement_timeout_ms=1_000, max_rows=2)
    )

    outcome = executor.execute(
        benchmark_database, "SELECT metric_id, value FROM metrics ORDER BY metric_id"
    )

    assert outcome.status == "executed"
    assert outcome.row_count == 2
    assert outcome.columns == ("metric_id", "value")
    assert outcome.final_sql is not None
    assert outcome.final_sql.endswith("LIMIT 2")


def test_read_only_sqlite_executor_keeps_database_unchanged(
    benchmark_database: Path,
) -> None:
    executor = ReadOnlySqliteExecutor()

    outcome = executor.execute(benchmark_database, "DELETE FROM metrics")

    assert outcome.status == "policy_rejected"
    connection = sqlite3.connect(benchmark_database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 4
    finally:
        connection.close()


def test_read_only_sqlite_executor_uses_shared_model_output_normalization(
    benchmark_database: Path,
) -> None:
    outcome = ReadOnlySqliteExecutor(
        settings=SqliteBenchmarkSettings(max_rows=2)
    ).execute(
        benchmark_database,
        "SELECT value FROM metrics ORDER BY value;\n\n### Answer\n| value |\n| --- |",
    )

    assert outcome.status == "executed"
    assert outcome.original_sql.endswith("| --- |")
    assert outcome.final_sql is not None
    assert "###" not in outcome.final_sql
    assert outcome.row_count == 2


def test_sqlite_benchmark_malformed_candidate_is_rejected_and_batch_continues(
    tmp_path: Path, benchmark_database: Path
) -> None:
    database_root = tmp_path / "databases"
    target_directory = database_root / "sample"
    target_directory.mkdir(parents=True)
    benchmark_database.replace(target_directory / "sample.sqlite")
    cases = load_normalized_cases(
        [
            {
                "case_id": "spider_dev:00000",
                "database_id": "sample",
                "database_path": "sample/sample.sqlite",
            },
            {
                "case_id": "spider_dev:00001",
                "database_id": "sample",
                "database_path": "sample/sample.sqlite",
            },
        ]
    )
    predictions = load_predictions(
        [
            {
                "case_id": "spider_dev:00000",
                "candidate_sql": "SELECT 'unterminated FROM metrics",
            },
            {
                "case_id": "spider_dev:00001",
                "candidate_sql": "SELECT value FROM metrics ORDER BY value",
            },
        ]
    )

    report = run_sqlite_benchmark(
        cases=cases,
        predictions=predictions,
        metadata=BenchmarkRunMetadata(
            dataset_id="spider_dev",
            dataset_version="test",
            model_id="test-model",
            model_version="base",
            prompt_version="prompt-v1",
        ),
        database_root=database_root,
        settings=SqliteBenchmarkSettings(max_rows=2),
    )

    malformed, valid = report["records"]
    assert malformed["execution"]["status"] == "policy_rejected"
    assert malformed["execution"]["error_type"] == "policy"
    assert "SQL parse failed" in malformed["execution"]["error_message"]
    assert valid["execution"]["status"] == "executed"
    assert valid["execution"]["row_count"] == 2
    assert report["summary"]["policy_rejected_candidates"] == 1
    assert report["summary"]["executed_candidates"] == 1


def test_read_only_sqlite_executor_reports_sql_errors(benchmark_database: Path) -> None:
    outcome = ReadOnlySqliteExecutor().execute(
        benchmark_database, "SELECT missing_column FROM metrics"
    )

    assert outcome.status == "execution_error"
    assert outcome.error_type == "sqlite_operational_error"
    assert outcome.row_count is None


def test_read_only_sqlite_executor_enforces_statement_timeout(
    benchmark_database: Path,
) -> None:
    executor = ReadOnlySqliteExecutor(
        settings=SqliteBenchmarkSettings(
            statement_timeout_ms=1,
            max_rows=2,
            progress_handler_steps=1,
        )
    )

    outcome = executor.execute(
        benchmark_database,
        "WITH RECURSIVE counter(value) AS ("
        "SELECT 1 UNION ALL SELECT value + 1 FROM counter"
        ") SELECT SUM(value) FROM counter",
    )

    assert outcome.status == "timeout"
    assert outcome.error_type == "timeout"


def test_normalized_case_and_prediction_loading_rejects_duplicates() -> None:
    with pytest.raises(BenchmarkInputError, match="case_id values must be unique"):
        load_normalized_cases(
            [
                {"case_id": "case-1", "database_id": "db", "database_path": "db.sqlite"},
                {"case_id": "case-1", "database_id": "db", "database_path": "db.sqlite"},
            ]
        )

    with pytest.raises(BenchmarkInputError, match="case_id/candidate_index"):
        load_predictions(
            [
                {"case_id": "case-1", "candidate_sql": "SELECT 1"},
                {"case_id": "case-1", "candidate_sql": "SELECT 2"},
            ]
        )


def test_database_path_cannot_escape_benchmark_root(tmp_path: Path) -> None:
    outside_database = tmp_path / "outside.sqlite"
    sqlite3.connect(outside_database).close()
    database_root = tmp_path / "root"
    database_root.mkdir()

    with pytest.raises(BenchmarkInputError, match="below the database root"):
        resolve_benchmark_database_path(database_root, "../outside.sqlite")


def test_bird_normalization_retains_only_stable_sqlite_locator() -> None:
    cases = normalize_bird_dev_cases(
        [
            {
                "db_id": "financial",
                "question": "Question text must not enter a report",
                "SQL": "SELECT private_gold_sql",
            }
        ]
    )

    assert cases[0].case_id == "bird_dev:00000"
    assert cases[0].database_id == "financial"
    assert cases[0].database_path == "financial/financial.sqlite"
    assert not hasattr(cases[0], "question")


def test_sqlite_benchmark_report_records_candidates_without_claiming_ex(
    tmp_path: Path, benchmark_database: Path
) -> None:
    database_root = tmp_path / "databases"
    database_root.mkdir()
    target_directory = database_root / "sample"
    target_directory.mkdir()
    target_database = target_directory / "sample.sqlite"
    benchmark_database.replace(target_database)
    cases = load_normalized_cases(
        [
            {
                "case_id": "bird_dev:00000",
                "database_id": "sample",
                "database_path": "sample/sample.sqlite",
            },
            {
                "case_id": "bird_dev:00001",
                "database_id": "sample",
                "database_path": "sample/sample.sqlite",
            },
        ]
    )
    predictions = load_predictions(
        [
            {
                "case_id": "bird_dev:00000",
                "candidate_sql": "SELECT value FROM metrics ORDER BY value",
                "candidate_index": 0,
                "generated_tokens": 14,
                "generation_elapsed_ms": 8,
            },
            {
                "case_id": "bird_dev:00000",
                "candidate_sql": "SELECT missing FROM metrics",
                "candidate_index": 1,
            },
        ]
    )

    report = run_sqlite_benchmark(
        cases=cases,
        predictions=predictions,
        metadata=BenchmarkRunMetadata(
            dataset_id="bird_dev",
            dataset_version="bird-2025-dev",
            model_id="test-model",
            model_version="base",
            prompt_version="prompt-v1",
        ),
        database_root=database_root,
        settings=SqliteBenchmarkSettings(max_rows=2),
    )

    assert report["mode"] == "offline_sqlite_benchmark"
    assert report["official_evaluation"]["status"] == "not_run"
    assert report["summary"] == {
        "total_cases": 2,
        "total_candidate_records": 3,
        "cases_with_predictions": 1,
        "cases_missing_predictions": 1,
        "policy_allowed_candidates": 2,
        "executed_candidates": 1,
        "execution_error_candidates": 1,
        "timeout_candidates": 0,
        "policy_rejected_candidates": 0,
        "status_counts": {
            "executed": 1,
            "execution_error": 1,
            "missing_prediction": 1,
        },
    }
    assert report["records"][0]["execution"]["row_count"] == 2
    assert report["records"][0]["candidate_count"] == 2
    assert all("question" not in record for record in report["records"])


def test_sqlite_benchmark_rejects_official_ex_for_other_dataset(
    tmp_path: Path, benchmark_database: Path
) -> None:
    database_root = tmp_path / "databases"
    database_root.mkdir()
    target_directory = database_root / "sample"
    target_directory.mkdir()
    benchmark_database.replace(target_directory / "sample.sqlite")
    cases = load_normalized_cases(
        [
            {
                "case_id": "bird_dev:00000",
                "database_id": "sample",
                "database_path": "sample/sample.sqlite",
            }
        ]
    )
    metadata = BenchmarkRunMetadata(
        dataset_id="bird_dev",
        dataset_version="bird-2025-dev",
        model_id="test-model",
        model_version="base",
        prompt_version="prompt-v1",
    )
    official_ex = OfficialExSummary.from_mapping(
        {
            "dataset_id": "spider_dev",
            "evaluator_name": "official-spider",
            "evaluator_version": "1",
            "execution_accuracy": 0.5,
            "evaluated_cases": 1,
            "source": "official-run.json",
        }
    )

    with pytest.raises(BenchmarkInputError, match="official EX dataset_id"):
        run_sqlite_benchmark(
            cases=cases,
            predictions=[],
            metadata=metadata,
            database_root=database_root,
            official_ex=official_ex,
        )
