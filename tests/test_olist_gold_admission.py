from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from data_analysis_agent.olist_queryspec import QuerySpec, WorkspacePin, render_gold_sql
from scripts.post_training.evaluation.admit_olist_gold_batch import (
    MAX_BATCH_ROWS,
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
