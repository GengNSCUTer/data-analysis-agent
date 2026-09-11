from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.post_training.data.review_olist_schema_aware_program_sft import (
    EXPECTED_METRICS,
    SchemaAwareReviewError,
    review,
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "olist-external"
    root.mkdir()
    materialization = root / "materialization"
    materialization.mkdir()
    (materialization / "exclusions").mkdir()
    (materialization / "exclusions" / "length.jsonl").write_text("", encoding="utf-8")
    (materialization / "materialization_audit.json").write_text("{}\n", encoding="utf-8")
    audit_path = root / "audit-report.json"
    audit_path.write_text(
        json.dumps({
            "audit_version": "olist-schema-aware-program-sft-audit-v1",
            "checks": {"status": "pass"},
        })
        + "\n",
        encoding="utf-8",
    )
    plan = {
        "metric_programs": [{"metric_id": metric} for metric in EXPECTED_METRICS],
        "result_shape": "scalar",
        "time": {"mode": "all", "grain": None},
        "join_program_id": "JP01",
    }
    task_a = {
        "pair_id": "pair-1",
        "source_family_id": "family-1",
        "token_length": {"sequence_tokens": 100},
    }
    task_b = {
        "pair_id": "pair-1",
        "source_family_id": "family-1",
        "schema_link_registry_version": "registry-v1",
        "schema_link_plan_id": "slp-1",
        "target_text": json.dumps(plan, sort_keys=True),
        "token_length": {"sequence_tokens": 120},
    }
    for split in ("train", "validation"):
        _write_jsonl(materialization / f"{split}_task_a.jsonl", [task_a])
        _write_jsonl(materialization / f"{split}_task_b.jsonl", [task_b])
    return audit_path, materialization


def test_review_writes_redacted_coverage_report(tmp_path: Path) -> None:
    audit_path, materialization = _prepare(tmp_path)
    output = tmp_path / "review"

    report = review(
        audit_path,
        materialization,
        output,
        sample_per_stratum=1,
        generated_at="2026-09-11T20:05:00+08:00",
    )

    assert report["checks"]["status"] == "pass"
    assert report["checks"]["contains_question_or_prompt_or_sql"] is False
    assert set(report["splits"]["train"]["metrics_referenced"]) == EXPECTED_METRICS
    assert (output / "review-report.json").is_file()
    serialized = (output / "review-report.json").read_text(encoding="utf-8")
    assert "target_text" not in serialized


def test_review_rejects_metric_coverage_drift(tmp_path: Path) -> None:
    audit_path, materialization = _prepare(tmp_path)
    replacement = json.dumps(
        {
            "metric_programs": [{"metric_id": "gmv"}],
            "result_shape": "scalar",
            "time": {"mode": "all", "grain": None},
            "join_program_id": "JP01",
        }
    )
    for split in ("train", "validation"):
        task_b = materialization / f"{split}_task_b.jsonl"
        row = json.loads(task_b.read_text(encoding="utf-8"))
        row["target_text"] = replacement
        task_b.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(SchemaAwareReviewError, match="metric coverage"):
        review(audit_path, materialization, tmp_path / "review", sample_per_stratum=1)


def test_review_rejects_task_a_task_b_pair_mismatch(tmp_path: Path) -> None:
    audit_path, materialization = _prepare(tmp_path)
    task_b = materialization / "validation_task_b.jsonl"
    row = json.loads(task_b.read_text(encoding="utf-8"))
    row["pair_id"] = "different-pair"
    task_b.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(SchemaAwareReviewError, match="Task A/Task B pair IDs differ"):
        review(audit_path, materialization, tmp_path / "review", sample_per_stratum=1)
