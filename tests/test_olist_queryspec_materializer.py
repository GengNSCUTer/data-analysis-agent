from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import scripts.post_training.data.materialize_olist_queryspecs as materializer_module
from scripts.post_training.data.materialize_olist_queryspecs import (
    PROTECTED_SUMMARY_VERSION,
    family_fingerprint,
    materialize,
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_protected_summary(path: Path, fingerprints: list[str] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "summary_version": PROTECTED_SUMMARY_VERSION,
                "family_fingerprints": fingerprints or [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _seed(
    seed_id: str,
    split: str,
    metric_ids: list[str],
    result_shape: str,
    dimension: str | None,
    join_program_id: str,
    *,
    time: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "seed_id": seed_id,
        "split": split,
        "metric_ids": metric_ids,
        "result_shape": result_shape,
        "dimension": dimension,
        "time": time or {"mode": "all_time", "start": None, "end_exclusive": None, "grain": None},
        "join_program_id": join_program_id,
    }


def test_materialize_writes_external_structural_artifacts_and_audit(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    _write_jsonl(
        seeds,
        [
            _seed("seed-gmv", "train", ["gmv"], "scalar", None, "JP01_item_scalar"),
            _seed("seed-review", "validation", ["positive_review_rate"], "scalar", None, "JP03_review_scalar"),
            _seed("seed-state", "in_domain_test", ["paid_order_count"], "state_grouped", "customer_state", "JP05_customer_geo_order"),
        ],
    )
    _write_protected_summary(protected)

    manifest = materialize(seeds, protected, output, generated_at="2026-09-04T00:00:00+00:00")

    assert manifest["counts"]["accepted_rows"] == 3
    assert manifest["counts"]["families"] == 3
    assert manifest["checks"]["sql_executed"] is False
    assert manifest["checks"]["protected_holdout_raw_read"] is False
    query_rows = [json.loads(line) for line in (output / "query_specs.jsonl").read_text(encoding="utf-8").splitlines()]
    gold_rows = [json.loads(line) for line in (output / "gold_sql.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["query_spec"]["query_spec_id"] for row in query_rows} == {
        row["gold_artifact"]["query_spec_id"] for row in gold_rows
    }
    assert all("question" not in row for row in query_rows)


def test_materialize_rejects_a_second_date_variant_of_the_same_family(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    _write_jsonl(
        seeds,
        [
            _seed("seed-jan", "train", ["gmv"], "scalar", None, "JP01_item_scalar", time={"mode": "absolute_range", "start": "2017-01-01", "end_exclusive": "2017-02-01", "grain": None}),
            _seed("seed-feb", "train", ["gmv"], "scalar", None, "JP01_item_scalar", time={"mode": "absolute_range", "start": "2017-02-01", "end_exclusive": "2017-03-01", "grain": None}),
        ],
    )
    _write_protected_summary(protected)

    manifest = materialize(seeds, protected, output)

    assert manifest["counts"]["accepted_rows"] == 1
    assert manifest["counts"]["rejections_by_reason"] == {"duplicate_family": 1}


def test_materialize_rejects_a_protected_family_without_reading_holdout_content(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    _write_jsonl(seeds, [_seed("seed-gmv", "train", ["gmv"], "scalar", None, "JP01_item_scalar")])

    first_output = tmp_path / "first-output"
    _write_protected_summary(protected)
    first = materialize(seeds, protected, first_output)
    family = first["splits"]["train"]["family_ids"][0]
    _write_protected_summary(protected, [family_fingerprint(family)])

    manifest = materialize(seeds, protected, output)

    assert manifest["counts"]["accepted_rows"] == 0
    assert manifest["counts"]["protected_family_collisions"] == 1
    assert manifest["checks"]["status"] == "rejected_all"


def test_materialize_fails_closed_when_a_program_crosses_splits(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    _write_jsonl(
        seeds,
        [
            _seed("seed-gmv", "train", ["gmv"], "scalar", None, "JP01_item_scalar"),
            _seed("seed-items", "validation", ["item_count"], "scalar", None, "JP01_item_scalar"),
        ],
    )
    _write_protected_summary(protected)

    with pytest.raises(ValueError, match="SQL programs cross splits"):
        materialize(seeds, protected, output)

    assert not output.exists()


def test_materialize_records_validator_rejections_without_writing_them_as_gold(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    _write_jsonl(
        seeds,
        [
            _seed("seed-valid", "train", ["gmv"], "scalar", None, "JP01_item_scalar"),
            _seed("seed-sensitive", "train", ["gmv"], "category_grouped", "seller_id", "JP07_category_item"),
        ],
    )
    _write_protected_summary(protected)

    manifest = materialize(seeds, protected, output)

    assert manifest["counts"]["accepted_rows"] == 1
    assert manifest["counts"]["rejections_by_reason"] == {"sensitive_dimension_not_displayable": 1}
    rejection = json.loads((output / "materialization_rejections.jsonl").read_text(encoding="utf-8"))
    assert rejection["seed_id"] == "seed-sensitive"
    assert "sql" not in rejection


def test_materialize_rejects_non_structural_seed_fields(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    invalid = _seed("seed-question", "train", ["gmv"], "scalar", None, "JP01_item_scalar")
    invalid["question"] = "这个字段不能进入物化器"
    _write_jsonl(seeds, [invalid])
    _write_protected_summary(protected)

    manifest = materialize(seeds, protected, output)

    assert manifest["counts"]["accepted_rows"] == 0
    assert manifest["counts"]["rejections_by_reason"] == {"unsupported_query_feature": 1}


def test_materialize_rejects_a_renderer_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seeds = tmp_path / "seeds.jsonl"
    protected = tmp_path / "protected-summary.json"
    output = tmp_path / "external-output"
    _write_jsonl(seeds, [_seed("seed-gmv", "train", ["gmv"], "scalar", None, "JP01_item_scalar")])
    _write_protected_summary(protected)
    original = materializer_module.render_gold_sql

    def tampered_renderer(spec: object) -> object:
        return replace(original(spec), sql_sha256="0" * 64)

    monkeypatch.setattr(materializer_module, "render_gold_sql", tampered_renderer)
    manifest = materialize(seeds, protected, output)

    assert manifest["counts"]["accepted_rows"] == 0
    assert manifest["counts"]["rejections_by_reason"] == {"renderer_hash_mismatch": 1}
