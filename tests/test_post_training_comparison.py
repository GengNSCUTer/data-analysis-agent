from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_analysis_agent.post_training_comparison import (
    ComparisonInputError,
    analyze_paired_sqlite_diagnostics,
)
from scripts.analyze_post_training_comparison import main


SECRET_SQL = "SELECT sensitive_column FROM internal_table"
SECRET_ERROR = "no such column: sensitive_column"


def record(
    case_id: str,
    *,
    status: str,
    generated_tokens: int,
    sql: str = SECRET_SQL,
    error_message: str | None = None,
    database_id: str = "private_database",
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "candidate_index": 0,
        "generated_tokens": generated_tokens,
        "database_id": database_id,
        "execution": {
            "status": status,
            "original_sql": sql,
            "error_message": error_message,
        },
    }


def report(*records: dict[str, object]) -> dict[str, object]:
    return {"report_version": "1", "records": list(records)}


def test_paired_analysis_reports_safe_transitions_without_raw_content() -> None:
    base = report(
        record("case:001", status="executed", generated_tokens=256),
        record(
            "case:002",
            status="execution_error",
            generated_tokens=24,
            error_message=SECRET_ERROR,
        ),
        record(
            "case:003",
            status="policy_rejected",
            generated_tokens=16,
            sql="SELECT 1; SELECT 2",
            error_message="exactly one SQL statement is allowed",
        ),
    )
    adapter = report(
        record(
            "case:001",
            status="execution_error",
            generated_tokens=31,
            error_message=SECRET_ERROR,
        ),
        record("case:002", status="executed", generated_tokens=11),
        record("case:003", status="executed", generated_tokens=12),
    )

    analyzed = analyze_paired_sqlite_diagnostics(
        base_report=base,
        adapter_report=adapter,
        max_new_tokens=256,
        sample_limit=2,
    )
    serialized = json.dumps(analyzed)

    assert analyzed["status_transitions"] == {
        "executed -> execution_error": 1,
        "execution_error -> executed": 1,
        "policy_rejected -> executed": 1,
    }
    assert analyzed["error_category_transitions"]["executed -> no_such_column"] == 1
    assert (
        analyzed["adapter"]["error_detail_counts"]["no_such_column_unqualified_or_other"]
        == 1
    )
    assert analyzed["changed_case_samples"]["executed -> no_such_column"] == ["case:001"]
    assert analyzed["base"]["generated_token_distribution"]["at_generation_cap"] == 1
    assert analyzed["adapter"]["completion_presentation_counts"]["direct_query"] == 3
    assert SECRET_SQL not in serialized
    assert SECRET_ERROR not in serialized
    assert "private_database" not in serialized


def test_paired_analysis_rejects_mismatched_cases() -> None:
    with pytest.raises(ComparisonInputError, match="identical case IDs"):
        analyze_paired_sqlite_diagnostics(
            base_report=report(record("case:001", status="executed", generated_tokens=1)),
            adapter_report=report(record("case:002", status="executed", generated_tokens=1)),
            max_new_tokens=256,
            sample_limit=1,
        )


def test_cli_rejects_output_inside_repository(tmp_path: Path) -> None:
    base_path = tmp_path / "base.json"
    adapter_path = tmp_path / "adapter.json"
    base_path.write_text(
        json.dumps(report(record("case:001", status="executed", generated_tokens=1))),
        encoding="utf-8",
    )
    adapter_path.write_text(
        json.dumps(report(record("case:001", status="executed", generated_tokens=1))),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="artifact output must stay outside"):
        main(
            [
                "--base-report",
                str(base_path),
                "--adapter-report",
                str(adapter_path),
                "--output",
                "analysis-inside-repository.json",
            ]
        )
