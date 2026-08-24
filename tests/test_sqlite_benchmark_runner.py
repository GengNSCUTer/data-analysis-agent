from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts.run_sqlite_benchmark import main


def _write_native_bird_fixture(tmp_path: Path) -> tuple[Path, Path]:
    database_root = tmp_path / "databases"
    database_directory = database_root / "sample"
    database_directory.mkdir(parents=True)
    database_path = database_directory / "sample.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE metrics (value INTEGER)")
        connection.execute("INSERT INTO metrics VALUES (7)")
        connection.commit()
    finally:
        connection.close()

    cases_path = tmp_path / "bird_dev.json"
    cases_path.write_text(
        json.dumps(
            [
                {
                    "db_id": "sample",
                    "question": "Question content must not enter the report",
                    "SQL": "SELECT secret_gold_sql",
                }
            ]
        ),
        encoding="utf-8",
    )
    return cases_path, database_root


def test_sqlite_benchmark_cli_writes_diagnostic_report_without_questions(
    tmp_path: Path,
) -> None:
    cases_path, database_root = _write_native_bird_fixture(tmp_path)
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text(
        json.dumps(
            {
                "case_id": "bird_dev:00000",
                "candidate_sql": "SELECT value FROM metrics",
                "generated_tokens": 5,
                "generation_elapsed_ms": 3,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    exit_code = main(
        [
            "--dataset",
            "bird_dev",
            "--cases",
            str(cases_path),
            "--database-root",
            str(database_root),
            "--predictions",
            str(predictions_path),
            "--dataset-version",
            "bird-dev-fixture",
            "--model-id",
            "fixture-model",
            "--model-version",
            "base",
            "--prompt-version",
            "prompt-v1",
            "--output",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["summary"]["executed_candidates"] == 1
    assert report["official_evaluation"]["status"] == "not_run"
    assert "Question content" not in report_path.read_text(encoding="utf-8")
    assert "secret_gold_sql" not in report_path.read_text(encoding="utf-8")


def test_sqlite_benchmark_cli_returns_input_error_for_unknown_prediction_case(
    tmp_path: Path,
) -> None:
    cases_path, database_root = _write_native_bird_fixture(tmp_path)
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text(
        json.dumps({"case_id": "unknown", "candidate_sql": "SELECT 1"}) + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--dataset",
            "bird_dev",
            "--cases",
            str(cases_path),
            "--database-root",
            str(database_root),
            "--predictions",
            str(predictions_path),
            "--dataset-version",
            "bird-dev-fixture",
            "--model-id",
            "fixture-model",
            "--model-version",
            "base",
            "--prompt-version",
            "prompt-v1",
            "--output",
            str(tmp_path / "report.json"),
        ]
    )

    assert exit_code == 2
