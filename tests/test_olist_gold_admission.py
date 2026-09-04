from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from data_analysis_agent.olist_queryspec import QuerySpec, WorkspacePin, render_gold_sql
from scripts.post_training.evaluation.admit_olist_gold_batch import (
    MAX_BATCH_ROWS,
    _validate_selection,
    load_materialized_gold_rows,
    metric_constraints,
    parse_review_response,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_materialization(directory: Path, rows: list[dict[str, object]]) -> None:
    directory.mkdir()
    gold_path = directory / "gold_sql.jsonl"
    query_specs_path = directory / "query_specs.jsonl"
    gold_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    query_specs_path.write_text(
        "".join(
            json.dumps(
                {
                    key: row[key]
                    for key in ("seed_id", "split", "family_id", "sql_program_id", "query_spec")
                },
                sort_keys=True,
            )
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    (directory / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "workspace": WorkspacePin.current().as_dict(),
                "outputs": {
                    "gold_sql_jsonl": {"rows": len(rows), "sha256": _sha256(gold_path)},
                    "query_specs_jsonl": {
                        "rows": len(rows),
                        "sha256": _sha256(query_specs_path),
                    },
                },
                "source": {
                    "protected_summary_sha256": "a" * 64,
                    "protected_evidence_sha256": "b" * 64,
                },
                "checks": {"status": "pass"},
            }
        ),
        encoding="utf-8",
    )


def _row(seed_id: str) -> dict[str, object]:
    spec = QuerySpec.create_validated(metric_ids=("gmv",), result_shape="scalar")
    artifact = render_gold_sql(spec)
    return {
        "seed_id": seed_id,
        "split": "train",
        "family_id": "family_0123456789abcdef01234567",
        "sql_program_id": spec.join_program_id,
        "query_spec": spec.as_dict(),
        "gold_artifact": {
            "query_spec_id": spec.query_spec_id,
            "sql": artifact.sql,
            "sql_sha256": artifact.sql_sha256,
        },
    }


def test_admission_loader_verifies_manifest_and_gold_hash(tmp_path: Path) -> None:
    directory = tmp_path / "external-materialization"
    _write_materialization(directory, [_row("seed-1")])

    manifest, rows = load_materialized_gold_rows(directory)

    assert manifest["outputs"]["gold_sql_jsonl"]["rows"] == 1
    assert rows[0]["seed_id"] == "seed-1"


def test_admission_loader_rejects_more_than_the_small_batch_limit(tmp_path: Path) -> None:
    directory = tmp_path / "external-materialization"
    _write_materialization(directory, [_row(f"seed-{index}") for index in range(MAX_BATCH_ROWS + 1)])

    with pytest.raises(ValueError, match="1-6 Gold rows"):
        load_materialized_gold_rows(directory)


def test_admission_loader_selects_a_bounded_subset_in_source_order(tmp_path: Path) -> None:
    directory = tmp_path / "external-materialization"
    _write_materialization(directory, [_row(f"seed-{index}") for index in range(8)])

    _, rows = load_materialized_gold_rows(
        directory,
        seed_ids=["seed-4", "seed-1", "seed-6"],
    )

    assert [row["seed_id"] for row in rows] == ["seed-1", "seed-4", "seed-6"]


def test_admission_selection_rejects_duplicate_unknown_and_oversized_ids() -> None:
    available = {"seed-1", "seed-2", "seed-3", "seed-4", "seed-5", "seed-6", "seed-7"}
    with pytest.raises(ValueError, match="unique"):
        _validate_selection(["seed-1", "seed-1"], available)
    with pytest.raises(ValueError, match="unknown"):
        _validate_selection(["seed-unknown"], available)
    with pytest.raises(ValueError, match="1-6"):
        _validate_selection(sorted(available), available)


def test_admission_loader_rejects_non_passing_or_mismatched_source_sets(tmp_path: Path) -> None:
    directory = tmp_path / "external-materialization"
    rows = [_row("seed-1"), _row("seed-2")]
    _write_materialization(directory, rows)

    manifest_path = directory / "materialization_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checks"] = {"status": "failed"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="passing structural audit"):
        load_materialized_gold_rows(directory)

    _write_materialization(tmp_path / "second-external-materialization", rows)
    second = tmp_path / "second-external-materialization"
    gold_path = second / "gold_sql.jsonl"
    gold_path.write_text(
        "".join(json.dumps(rows[0], sort_keys=True) + "\n" for _ in range(2)),
        encoding="utf-8",
    )
    manifest_path = second / "materialization_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"]["gold_sql_jsonl"]["sha256"] = _sha256(gold_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Gold seed IDs are not unique"):
        load_materialized_gold_rows(second)


def test_review_response_is_strict_and_metric_constraints_are_deterministic() -> None:
    assert parse_review_response(
        '{"verdict":"pass","issues":[],"rationale":"matches frozen scope"}'
    ) == {"verdict": "pass", "issues": [], "rationale": "matches frozen scope"}
    with pytest.raises(ValueError, match="unsupported verdict"):
        parse_review_response('{"verdict":"approve","issues":[],"rationale":"x"}')
    assert metric_constraints(("positive_review_rate", "paid_order_count")) == {
        "positive_review_rate": {"minimum": 0, "maximum": 1},
        "paid_order_count": {"minimum": 0, "integer_like": True},
    }
