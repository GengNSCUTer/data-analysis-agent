"""Strict external bridge for the official Spider Test Suite evaluator.

The official evaluator and its test-suite databases stay outside this repository.
This module prepares complete ordered input files and invokes the evaluator
unchanged. It deliberately refuses partial prediction files because the upstream
Spider evaluator zips lines inside its single session and would otherwise report
an incomplete denominator as though it were a full development result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence

from data_analysis_agent.sqlite_benchmark import BenchmarkInputError, SqlPrediction


class OfficialSpiderTestSuiteError(ValueError):
    """The official evaluator boundary cannot be safely prepared or invoked."""


@dataclass(frozen=True)
class SpiderOfficialEvaluationInputs:
    """External files consumed by an unmodified official evaluator."""

    gold_path: Path
    prediction_path: Path
    case_count: int
    gold_sha256: str
    prediction_sha256: str


@dataclass(frozen=True)
class OfficialSpiderTestSuiteEvidence:
    """Non-sensitive evidence emitted after one official evaluator invocation."""

    evaluator_name: str
    evaluator_commit: str
    evaluated_cases: int
    etype: str
    plug_value: bool
    keep_distinct: bool
    gold_sha256: str
    prediction_sha256: str
    raw_output_sha256: str
    return_code: int
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def prepare_complete_spider_test_suite_inputs(
    *,
    native_cases: Iterable[Mapping[str, Any]],
    predictions: Iterable[SqlPrediction],
    output_directory: Path,
    repository_root: Path,
) -> SpiderOfficialEvaluationInputs:
    """Write complete ordered Spider gold/prediction files outside Git.

    Gold SQL is used only by the official evaluator after generation has ended.
    It never enters candidate generation, model prompts, the project repository,
    or the returned evidence object.
    """

    cases = list(native_cases)
    if not cases:
        raise OfficialSpiderTestSuiteError("native Spider case list must not be empty")
    expected_case_ids = [f"spider_dev:{index:05d}" for index in range(len(cases))]
    normalized_cases = [_normalize_native_case(case, index) for index, case in enumerate(cases)]
    prediction_by_case = _require_complete_single_predictions(
        predictions=predictions,
        expected_case_ids=expected_case_ids,
    )
    directory = _ensure_external_directory(output_directory, repository_root)
    gold_path = directory / "gold.txt"
    prediction_path = directory / "predictions.txt"
    if gold_path.exists() or prediction_path.exists():
        raise OfficialSpiderTestSuiteError(
            "official evaluator input files already exist; choose a new external output directory"
        )

    gold_lines: list[str] = []
    prediction_lines: list[str] = []
    for case_id, database_id, gold_sql in normalized_cases:
        candidate_sql = prediction_by_case[case_id]
        _require_single_line_sql(candidate_sql, "candidate SQL")
        _require_single_line_sql(gold_sql, "gold SQL")
        if "\t" in database_id or "\n" in database_id or "\r" in database_id:
            raise OfficialSpiderTestSuiteError("native db_id cannot contain a tab or newline")
        gold_lines.append(f"{gold_sql}\t{database_id}\n")
        prediction_lines.append(f"{candidate_sql}\n")

    gold_path.write_text("".join(gold_lines), encoding="utf-8")
    prediction_path.write_text("".join(prediction_lines), encoding="utf-8")
    return SpiderOfficialEvaluationInputs(
        gold_path=gold_path,
        prediction_path=prediction_path,
        case_count=len(normalized_cases),
        gold_sha256=_sha256_file(gold_path),
        prediction_sha256=_sha256_file(prediction_path),
    )


def verify_unmodified_official_evaluator(
    *, evaluator_root: Path, expected_commit: str
) -> str:
    """Verify a clean checkout of the exact official evaluator revision."""

    root = evaluator_root.resolve(strict=True)
    if not (root / "evaluation.py").is_file():
        raise OfficialSpiderTestSuiteError("official evaluator root lacks evaluation.py")
    if not expected_commit or any(character.isspace() for character in expected_commit):
        raise OfficialSpiderTestSuiteError("expected evaluator commit must be a non-empty revision")
    actual_commit = _git_output(root, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise OfficialSpiderTestSuiteError(
            "official evaluator commit does not match the frozen expected revision"
        )
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise OfficialSpiderTestSuiteError("official evaluator worktree must be clean")
    return actual_commit


def run_unmodified_spider_test_suite(
    *,
    evaluator_root: Path,
    expected_evaluator_commit: str,
    test_suite_database_root: Path,
    native_cases: Sequence[Mapping[str, Any]],
    inputs: SpiderOfficialEvaluationInputs,
    raw_output_path: Path,
    evidence_path: Path,
    repository_root: Path,
    timeout_seconds: int = 3_600,
    python_executable: str = sys.executable,
    keep_distinct: bool = False,
) -> OfficialSpiderTestSuiteEvidence:
    """Invoke the official evaluator without parsing or re-computing its score."""

    if timeout_seconds <= 0:
        raise OfficialSpiderTestSuiteError("timeout_seconds must be greater than zero")
    if inputs.case_count != len(native_cases):
        raise OfficialSpiderTestSuiteError("prepared evaluator input count does not match native cases")
    evaluator_commit = verify_unmodified_official_evaluator(
        evaluator_root=evaluator_root,
        expected_commit=expected_evaluator_commit,
    )
    database_root = test_suite_database_root.resolve(strict=True)
    _validate_test_suite_layout(database_root, native_cases)
    output_path = _ensure_external_file_path(raw_output_path, repository_root)
    summary_path = _ensure_external_file_path(evidence_path, repository_root)
    if output_path.exists() or summary_path.exists():
        raise OfficialSpiderTestSuiteError(
            "official evaluator output/evidence already exists; choose new external paths"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        python_executable,
        str(evaluator_root / "evaluation.py"),
        "--gold",
        str(inputs.gold_path),
        "--pred",
        str(inputs.prediction_path),
        "--db",
        str(database_root),
        "--etype",
        "exec",
    ]
    if keep_distinct:
        command.append("--keep_distinct")
    try:
        with output_path.open("x", encoding="utf-8") as output_file:
            completed = subprocess.run(
                command,
                cwd=evaluator_root,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout_seconds,
                text=True,
            )
    except subprocess.TimeoutExpired as exc:
        raise OfficialSpiderTestSuiteError("official evaluator timed out; inspect external output") from exc
    except OSError as exc:
        raise OfficialSpiderTestSuiteError("official evaluator process could not start") from exc
    if completed.returncode != 0:
        raise OfficialSpiderTestSuiteError(
            "official evaluator failed; inspect the external raw output"
        )

    evidence = OfficialSpiderTestSuiteEvidence(
        evaluator_name="taoyds/test-suite-sql-eval",
        evaluator_commit=evaluator_commit,
        evaluated_cases=inputs.case_count,
        etype="exec",
        plug_value=False,
        keep_distinct=keep_distinct,
        gold_sha256=inputs.gold_sha256,
        prediction_sha256=inputs.prediction_sha256,
        raw_output_sha256=_sha256_file(output_path),
        return_code=completed.returncode,
        status="official_output_saved_not_reinterpreted",
    )
    summary_path.write_text(
        json.dumps(evidence.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return evidence


def _normalize_native_case(case: Mapping[str, Any], index: int) -> tuple[str, str, str]:
    try:
        database_id = case["db_id"]
        gold_sql = case["query"]
    except KeyError as exc:
        raise OfficialSpiderTestSuiteError(
            "native Spider case must contain db_id and query for official evaluation"
        ) from exc
    if not isinstance(database_id, str) or not database_id.strip():
        raise OfficialSpiderTestSuiteError("native Spider db_id must be a non-empty string")
    if not isinstance(gold_sql, str) or not gold_sql.strip():
        raise OfficialSpiderTestSuiteError("native Spider query must be a non-empty string")
    return f"spider_dev:{index:05d}", database_id, gold_sql.strip()


def _require_complete_single_predictions(
    *, predictions: Iterable[SqlPrediction], expected_case_ids: Sequence[str]
) -> dict[str, str]:
    prediction_list = list(predictions)
    duplicate_or_nonprimary = [
        prediction
        for prediction in prediction_list
        if prediction.candidate_index != 0
    ]
    if duplicate_or_nonprimary:
        raise OfficialSpiderTestSuiteError(
            "official Test Suite evaluation requires exactly candidate_index=0 for every case"
        )
    prediction_by_case = {prediction.case_id: prediction.candidate_sql for prediction in prediction_list}
    if len(prediction_by_case) != len(prediction_list):
        raise OfficialSpiderTestSuiteError("official Test Suite predictions must not repeat case_id")
    expected = set(expected_case_ids)
    actual = set(prediction_by_case)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise OfficialSpiderTestSuiteError(
            "official Test Suite evaluation requires complete exact case coverage"
        )
    return {case_id: prediction_by_case[case_id] for case_id in expected_case_ids}


def _validate_test_suite_layout(
    database_root: Path, native_cases: Sequence[Mapping[str, Any]]) -> None:
    database_ids = {_normalize_native_case(case, index)[1] for index, case in enumerate(native_cases)}
    missing = sorted(
        database_id
        for database_id in database_ids
        if not (database_root / database_id / f"{database_id}.sqlite").is_file()
    )
    if missing:
        raise OfficialSpiderTestSuiteError(
            "test-suite database root is missing required Spider database files"
        )


def _ensure_external_directory(path: Path, repository_root: Path) -> Path:
    directory = path.resolve()
    repository = repository_root.resolve(strict=True)
    if directory.is_relative_to(repository):
        raise OfficialSpiderTestSuiteError("official evaluator inputs must stay outside the repository")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _ensure_external_file_path(path: Path, repository_root: Path) -> Path:
    resolved_path = path.resolve()
    repository = repository_root.resolve(strict=True)
    if resolved_path.is_relative_to(repository):
        raise OfficialSpiderTestSuiteError("official evaluator outputs must stay outside the repository")
    return resolved_path


def _git_output(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise OfficialSpiderTestSuiteError("git is required to verify the official evaluator") from exc
    if completed.returncode != 0:
        raise OfficialSpiderTestSuiteError("could not inspect the official evaluator repository")
    return completed.stdout.strip()


def _require_single_line_sql(sql: str, field_name: str) -> None:
    if "\r" in sql or "\n" in sql or "\t" in sql:
        raise OfficialSpiderTestSuiteError(f"{field_name} must be one line for the official evaluator")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
