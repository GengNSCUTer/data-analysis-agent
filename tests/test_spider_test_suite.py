from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from data_analysis_agent.spider_test_suite import (
    OfficialSpiderTestSuiteError,
    prepare_complete_spider_test_suite_inputs,
    run_unmodified_spider_test_suite,
    verify_unmodified_official_evaluator,
)
from data_analysis_agent.sqlite_benchmark import SqlPrediction


def _native_cases() -> list[dict[str, str]]:
    return [
        {"db_id": "demo", "query": "SELECT value FROM items"},
        {"db_id": "demo", "query": "SELECT COUNT(*) FROM items"},
    ]


def _complete_predictions() -> list[SqlPrediction]:
    return [
        SqlPrediction(case_id="spider_dev:00000", candidate_sql="SELECT value FROM items"),
        SqlPrediction(case_id="spider_dev:00001", candidate_sql="SELECT COUNT(*) FROM items"),
    ]


def _make_evaluator_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "official-evaluator"
    root.mkdir()
    (root / "evaluation.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "assert Path(sys.argv[sys.argv.index(\"--gold\") + 1]).is_file()\n"
        "assert Path(sys.argv[sys.argv.index(\"--pred\") + 1]).is_file()\n"
        "print(\"official evaluator fixture output\")\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "evaluation.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, commit


def _make_test_suite_database_root(tmp_path: Path) -> Path:
    root = tmp_path / "test-suite-databases"
    database_directory = root / "demo"
    database_directory.mkdir(parents=True)
    connection = sqlite3.connect(database_directory / "demo.sqlite")
    try:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.commit()
    finally:
        connection.close()
    return root


def test_prepare_requires_complete_exact_single_candidate_coverage(tmp_path: Path) -> None:
    with pytest.raises(OfficialSpiderTestSuiteError, match="complete exact case coverage"):
        prepare_complete_spider_test_suite_inputs(
            native_cases=_native_cases(),
            predictions=_complete_predictions()[:1],
            output_directory=tmp_path / "external",
            repository_root=tmp_path / "repository",
        )

    with pytest.raises(OfficialSpiderTestSuiteError, match="candidate_index=0"):
        prepare_complete_spider_test_suite_inputs(
            native_cases=_native_cases(),
            predictions=[
                *_complete_predictions(),
                SqlPrediction(
                    case_id="spider_dev:00000",
                    candidate_sql="SELECT 1",
                    candidate_index=1,
                ),
            ],
            output_directory=tmp_path / "external-second",
            repository_root=tmp_path / "repository",
        )


def test_prepare_keeps_gold_and_prediction_inputs_outside_repository(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    inputs = prepare_complete_spider_test_suite_inputs(
        native_cases=_native_cases(),
        predictions=_complete_predictions(),
        output_directory=tmp_path / "external",
        repository_root=repository_root,
    )

    assert inputs.case_count == 2
    assert inputs.gold_path.parent == tmp_path / "external"
    assert inputs.prediction_path.parent == tmp_path / "external"
    assert len(inputs.gold_sha256) == 64
    assert len(inputs.prediction_sha256) == 64
    assert not inputs.gold_path.is_relative_to(repository_root)
    assert not inputs.prediction_path.is_relative_to(repository_root)

    with pytest.raises(OfficialSpiderTestSuiteError, match="outside the repository"):
        prepare_complete_spider_test_suite_inputs(
            native_cases=_native_cases(),
            predictions=_complete_predictions(),
            output_directory=repository_root / "forbidden",
            repository_root=repository_root,
        )


def test_prepare_folds_formatting_whitespace_but_preserves_literals(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    cases = [{"db_id": "demo", "query": "SELECT 'a  b' FROM items"}]
    predictions = [
        SqlPrediction(
            case_id="spider_dev:00000",
            candidate_sql="SELECT 'a  b'\nFROM items",
        )
    ]

    inputs = prepare_complete_spider_test_suite_inputs(
        native_cases=cases,
        predictions=predictions,
        output_directory=tmp_path / "external",
        repository_root=repository_root,
    )

    assert inputs.prediction_path.read_text(encoding="utf-8") == "SELECT 'a  b' FROM items\n"


def test_prepare_rejects_line_comments_when_folding_sql(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    with pytest.raises(OfficialSpiderTestSuiteError, match="line comment"):
        prepare_complete_spider_test_suite_inputs(
            native_cases=_native_cases(),
            predictions=[
                SqlPrediction(
                    case_id="spider_dev:00000",
                    candidate_sql="SELECT value FROM items -- keep\nWHERE value IS NOT NULL",
                ),
                _complete_predictions()[1],
            ],
            output_directory=tmp_path / "external-comments",
            repository_root=repository_root,
        )


def test_verify_official_evaluator_rejects_dirty_worktree(tmp_path: Path) -> None:
    evaluator_root, commit = _make_evaluator_repository(tmp_path)
    assert verify_unmodified_official_evaluator(
        evaluator_root=evaluator_root,
        expected_commit=commit,
    ) == commit

    (evaluator_root / "evaluation.py").write_text("changed", encoding="utf-8")
    with pytest.raises(OfficialSpiderTestSuiteError, match="worktree must be clean"):
        verify_unmodified_official_evaluator(
            evaluator_root=evaluator_root,
            expected_commit=commit,
        )


def test_run_official_evaluator_writes_only_external_evidence(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    native_cases = _native_cases()
    inputs = prepare_complete_spider_test_suite_inputs(
        native_cases=native_cases,
        predictions=_complete_predictions(),
        output_directory=tmp_path / "external-inputs",
        repository_root=repository_root,
    )
    evaluator_root, commit = _make_evaluator_repository(tmp_path)
    database_root = _make_test_suite_database_root(tmp_path)
    raw_output = tmp_path / "external-results" / "official-output.txt"
    evidence_path = tmp_path / "external-results" / "evidence.json"

    evidence = run_unmodified_spider_test_suite(
        evaluator_root=evaluator_root,
        expected_evaluator_commit=commit,
        test_suite_database_root=database_root,
        native_cases=native_cases,
        inputs=inputs,
        raw_output_path=raw_output,
        evidence_path=evidence_path,
        repository_root=repository_root,
    )

    assert evidence.status == "official_output_saved_not_reinterpreted"
    assert evidence.evaluated_cases == 2
    assert evidence.return_code == 0
    assert raw_output.is_file()
    stored = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert stored["raw_output_sha256"] == evidence.raw_output_sha256
    assert "fixture output" not in evidence_path.read_text(encoding="utf-8")
