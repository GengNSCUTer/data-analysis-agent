"""Self-contained contract tests for the Olist v3.1 release audit gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from data_analysis_agent.olist_surface_contract import (
    OLIST_V3_1_SURFACE_VERSION,
    OLIST_V3_1_VARIANT_IDS,
    OLIST_V3_1_VARIANT_KIND_BY_ID,
    OLIST_V3_1_VARIANT_POLICY,
    OLIST_V3_1_VARIANT_SCHEMA_VERSION,
)
from scripts.post_training.evaluation import audit_olist_v3_1_release_contract as audit


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_sft_split_audit(
    directory: Path, *, corrupt_validation_hash: bool = False
) -> None:
    """Write a self-contained, production-cardinality SFT audit fixture."""
    paths = {
        "train": directory / "train.jsonl",
        "validation": directory / "validation.jsonl",
        "in_domain_test": directory / "final_evaluation_only" / "in_domain_test.jsonl",
    }
    paths["in_domain_test"].parent.mkdir()
    rows = {"train": 3000, "validation": 750, "in_domain_test": 750}
    roles = {
        "train": "parameter_updates",
        "validation": "validation_only",
        "in_domain_test": "final_evaluation_only",
    }
    for split, path in paths.items():
        row = json.dumps({"split": {"name": split}}, sort_keys=True) + "\n"
        path.write_text(row * rows[split], encoding="utf-8")
    validation_hash = audit.sha256_file(paths["validation"])
    _write_json(
        directory / "split_audit.json",
        {
            "checks": {
                "status": "pass",
                "primary_surface_split_quotas_exact": True,
                "primary_surface_bucket_balance_at_most_one": True,
            },
            "policy": {
                "surface_form_policy": "eight_forms_one_query_instance",
                "primary_variant_selection": {"fixture": True},
            },
            "splits": {
                split: {
                    "rows": rows[split],
                    "sha256": (
                        "0" * 64
                        if split == "validation" and corrupt_validation_hash
                        else validation_hash
                        if split == "validation"
                        else audit.sha256_file(path)
                    ),
                    "role": roles[split],
                }
                for split, path in paths.items()
            },
        },
    )


def test_sft_loader_accepts_hash_bound_production_cardinalities(tmp_path: Path) -> None:
    """A self-contained valid SFT fixture passes hash, count, and role gates."""
    directory = tmp_path / "sft"
    directory.mkdir()
    _write_sft_split_audit(directory)

    audit_record, splits = audit._load_sft(directory)

    assert audit_record["checks"]["status"] == "pass"
    assert {split: len(rows) for split, rows in splits.items()} == {
        "train": 3000,
        "validation": 750,
        "in_domain_test": 750,
    }


def test_sft_loader_rejects_hash_drift(tmp_path: Path) -> None:
    """A split-audit hash cannot bless a subsequently changed SFT file."""
    directory = tmp_path / "sft"
    directory.mkdir()
    _write_sft_split_audit(directory, corrupt_validation_hash=True)

    with pytest.raises(ValueError, match="SFT validation file does not match"):
        audit._load_sft(directory)


def test_sft_identity_rejects_wrong_admission_split() -> None:
    """A row cannot move between splits while retaining a plausible local shape."""
    gold_sql = "SELECT 1 AS metric"
    gold_sha256 = hashlib.sha256(gold_sql.encode("utf-8")).hexdigest()
    row = {
        "seed_id": "seed-1",
        "primary_variant_id": "seed-1-v1",
        "language_variant_id": "seed-1-v1",
        "language_variant_kind": "formal_request",
        "rendered_prompt": "问题\n### SQL",
        "candidate_sql": gold_sql,
        "primary_bucket": "single_scalar",
        "family_id": "family-1",
        "query_spec_id": "qs-1",
        "surface_variant_count": 8,
        "split": {"name": "validation"},
    }
    admitted = {
        "seed_id": "seed-1",
        "split": "train",
        "family_id": "family-1",
        "query_spec": {"query_spec_id": "qs-1"},
        "gold_sql": gold_sql,
        "gold_sql_sha256": gold_sha256,
        "primary_bucket": "single_scalar",
    }
    runtime = {
        "variant_id": "seed-1-v1",
        "split": "train",
        "family_id": "family-1",
        "variant_kind": "formal_request",
        "query_spec": {"query_spec_id": "qs-1"},
    }

    with pytest.raises(ValueError, match="SFT/admission split identity drifted"):
        audit._verify_sft_split_identity(
            {"validation": [row]}, {"seed-1": admitted}, {"seed-1-v1": runtime}
        )


def _surface_cases(*, first_question: str | None = None) -> list[dict[str, str]]:
    return [
        {
            "variant_id": f"seed-1-{form_id}",
            "variant_kind": OLIST_V3_1_VARIANT_KIND_BY_ID[form_id],
            "seed_id": "seed-1",
            "question": first_question
            if index == 1 and first_question is not None
            else f"请查询第{index}种指标口径。",
        }
        for index, form_id in enumerate(OLIST_V3_1_VARIANT_IDS, 1)
    ]


def _write_surface(
    directory: Path, cases: list[dict[str, str]], *, manifest_hash: str | None = None
) -> Path:
    directory.mkdir()
    payload_path = directory / "question_variants.json"
    _write_json(
        payload_path,
        {
            "schema_version": OLIST_V3_1_VARIANT_SCHEMA_VERSION,
            "language": "zh",
            "variant_policy": OLIST_V3_1_VARIANT_POLICY,
            "cases": cases,
        },
    )
    _write_json(
        directory / "surface_manifest.json",
        {
            "surface_version": OLIST_V3_1_SURFACE_VERSION,
            "counts": {"query_instances": 1, "variants_per_instance": 8},
            "output": {
                "question_variants_json": {
                    "sha256": manifest_hash or audit.sha256_file(payload_path)
                }
            },
        },
    )
    return payload_path


def test_surface_loader_rejects_manifest_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale manifest cannot be used to bless changed question forms."""
    monkeypatch.setattr(audit, "EXPECTED_ROWS", 1)
    directory = tmp_path / "surface"
    _write_surface(directory, _surface_cases(), manifest_hash="0" * 64)

    with pytest.raises(ValueError, match="surface manifest/payload"):
        audit._load_surface(directory, {"seed-1"})


@pytest.mark.parametrize(
    ("first_question", "error"),
    [
        ("请查询 GMV。", "duplicate form or non-Chinese"),
        ("请查询第2种指标口径。", "exact/normalized duplicate question"),
    ],
)
def test_surface_loader_rejects_non_chinese_or_duplicate_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_question: str,
    error: str,
) -> None:
    """The primary Chinese surface cannot silently drift or collapse forms."""
    monkeypatch.setattr(audit, "EXPECTED_ROWS", 1)
    directory = tmp_path / "surface"
    _write_surface(directory, _surface_cases(first_question=first_question))

    with pytest.raises(ValueError, match=error):
        audit._load_surface(directory, {"seed-1"})


def test_audit_release_rejects_sft_canonical_gold_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A materially changed supervised SQL must not pass the release gate."""
    structural_dir = tmp_path / "structural"
    admission_dir = tmp_path / "admission"
    surface_dir = tmp_path / "surface"
    runtime_dir = tmp_path / "runtime"
    sft_dir = tmp_path / "sft"
    for directory in (structural_dir, admission_dir, surface_dir, runtime_dir, sft_dir):
        directory.mkdir()
    for directory, filename in (
        (structural_dir, "materialization_manifest.json"),
        (admission_dir, "admission_assembly_manifest.json"),
        (surface_dir, "surface_manifest.json"),
        (surface_dir, "question_variants.json"),
        (runtime_dir, "runtime_prompt_manifest.json"),
        (sft_dir, "split_audit.json"),
    ):
        _write_json(directory / filename, {})

    structural = {
        "seed_id": "seed-1",
        "split": "train",
        "family_id": "family-1",
        "gold_artifact": {
            "sql_sha256": "930d017714464f6730efe204a2d76d900acf3c2b833dc6e59a29f6a96575eac8"
        },
        "query_spec": {"query_spec_id": "qs-1"},
    }
    admitted = {
        "seed_id": "seed-1",
        "split": "train",
        "family_id": "family-1",
        "gold_sql_sha256": "930d017714464f6730efe204a2d76d900acf3c2b833dc6e59a29f6a96575eac8",
        "gold_sql": "SELECT 1 AS gmv",
        "primary_bucket": "single_scalar",
        "query_spec": {"query_spec_id": "qs-1"},
    }
    runtime = {
        "variant_id": "seed-1-v1",
        "variant_kind": "formal_request",
        "seed_id": "seed-1",
        "family_id": "family-1",
        "split": "train",
        "question": "请查询成交额。",
        "prompt": "prompt\n### SQL",
        "query_spec": {"query_spec_id": "qs-1"},
    }
    row = {
        "seed_id": "seed-1",
        "primary_variant_id": "seed-1-v1",
        "language_variant_id": "seed-1-v1",
        "language_variant_kind": "formal_request",
        "rendered_prompt": "prompt\n### SQL",
        "candidate_sql": "SELECT 2 AS gmv",
        "primary_bucket": "single_scalar",
        "family_id": "family-1",
        "query_spec_id": "qs-1",
        "surface_variant_count": 8,
        "split": {"name": "train"},
    }
    structural_hash = audit.sha256_file(
        structural_dir / "materialization_manifest.json"
    )
    admission_hash = audit.sha256_file(
        admission_dir / "admission_assembly_manifest.json"
    )
    surface_hash = audit.sha256_file(surface_dir / "surface_manifest.json")
    variants_hash = audit.sha256_file(surface_dir / "question_variants.json")
    runtime_hash = audit.sha256_file(runtime_dir / "runtime_prompt_manifest.json")

    monkeypatch.setattr(
        audit,
        "_load_admitted",
        lambda _: (
            {"source": {"structural_manifest_sha256": structural_hash}},
            [admitted],
        ),
    )
    monkeypatch.setattr(
        audit,
        "_load_surface",
        lambda *_: (
            {"source": {"admission_manifest_sha256": admission_hash}},
            [runtime],
        ),
    )
    monkeypatch.setattr(
        audit,
        "_load_runtime",
        lambda *_: (
            {
                "input": {
                    "admission_assembly_manifest_sha256": admission_hash,
                    "question_variants_sha256": variants_hash,
                }
            },
            [runtime],
        ),
    )
    monkeypatch.setattr(
        audit,
        "_load_sft",
        lambda _: (
            {
                "source": {
                    "admission_assembly_manifest_sha256": admission_hash,
                    "runtime_prompt_manifest_sha256": runtime_hash,
                }
            },
            {"train": [row], "validation": [], "in_domain_test": []},
        ),
    )
    monkeypatch.setattr(
        audit,
        "load_full_v3_gold_rows",
        lambda _: ({"release_version": audit.RELEASE_VERSION}, [structural]),
    )
    monkeypatch.setattr(audit, "balance_coverage_report", lambda _: {"status": "pass"})
    monkeypatch.setattr(
        audit, "primary_variant_selection_report", lambda _: {"status": "pass"}
    )

    with pytest.raises(ValueError, match="SFT canonical Gold SQL identity drifted"):
        audit.audit_release(
            structural_dir=structural_dir,
            admission_dir=admission_dir,
            surface_dir=surface_dir,
            runtime_prompt_dir=runtime_dir,
            sft_dir=sft_dir,
            output_dir=tmp_path / "new-audit",
            generated_at="2026-09-14T00:00:00+00:00",
        )


def test_audit_release_writes_passing_safe_report_after_bound_loaders_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full audit writes a pass report after hash-bound loaders succeed."""
    structural_dir = tmp_path / "structural"
    admission_dir = tmp_path / "admission"
    surface_dir = tmp_path / "surface"
    runtime_dir = tmp_path / "runtime"
    sft_dir = tmp_path / "sft"
    for directory in (structural_dir, admission_dir, surface_dir, runtime_dir, sft_dir):
        directory.mkdir()
    for directory, filename in (
        (structural_dir, "materialization_manifest.json"),
        (admission_dir, "admission_assembly_manifest.json"),
        (surface_dir, "surface_manifest.json"),
        (surface_dir, "question_variants.json"),
        (runtime_dir, "runtime_prompt_manifest.json"),
        (sft_dir, "split_audit.json"),
    ):
        _write_json(directory / filename, {})

    gold_sql = "SELECT 1 AS gmv"
    gold_sha256 = hashlib.sha256(gold_sql.encode("utf-8")).hexdigest()
    structural_hash = audit.sha256_file(
        structural_dir / "materialization_manifest.json"
    )
    admission_hash = audit.sha256_file(
        admission_dir / "admission_assembly_manifest.json"
    )
    variants_hash = audit.sha256_file(surface_dir / "question_variants.json")
    runtime_hash = audit.sha256_file(runtime_dir / "runtime_prompt_manifest.json")
    structural = {
        "seed_id": "seed-1",
        "split": "train",
        "family_id": "family-1",
        "gold_artifact": {"sql_sha256": gold_sha256},
        "query_spec": {"query_spec_id": "qs-1"},
    }
    admitted = {
        "seed_id": "seed-1",
        "split": "train",
        "family_id": "family-1",
        "gold_sql_sha256": gold_sha256,
        "gold_sql": gold_sql,
        "primary_bucket": "single_scalar",
        "query_spec": {"query_spec_id": "qs-1"},
    }
    runtime = {
        "variant_id": "seed-1-v1",
        "variant_kind": "formal_request",
        "seed_id": "seed-1",
        "family_id": "family-1",
        "split": "train",
        "question": "请查询成交额。",
        "prompt": "问题\n### SQL",
        "query_spec": {"query_spec_id": "qs-1"},
    }
    row = {
        "seed_id": "seed-1",
        "primary_variant_id": "seed-1-v1",
        "language_variant_id": "seed-1-v1",
        "language_variant_kind": "formal_request",
        "rendered_prompt": "问题\n### SQL",
        "candidate_sql": gold_sql,
        "primary_bucket": "single_scalar",
        "family_id": "family-1",
        "query_spec_id": "qs-1",
        "surface_variant_count": 8,
        "split": {"name": "train"},
    }
    monkeypatch.setattr(
        audit,
        "load_full_v3_gold_rows",
        lambda _: ({"release_version": audit.RELEASE_VERSION}, [structural]),
    )
    monkeypatch.setattr(audit, "balance_coverage_report", lambda _: {"status": "pass"})
    monkeypatch.setattr(
        audit,
        "_load_admitted",
        lambda _: (
            {"source": {"structural_manifest_sha256": structural_hash}},
            [admitted],
        ),
    )
    monkeypatch.setattr(
        audit,
        "_load_surface",
        lambda *_: ({"source": {"admission_manifest_sha256": admission_hash}}, []),
    )
    monkeypatch.setattr(
        audit,
        "_load_runtime",
        lambda *_: (
            {
                "input": {
                    "admission_assembly_manifest_sha256": admission_hash,
                    "question_variants_sha256": variants_hash,
                }
            },
            [runtime],
        ),
    )
    monkeypatch.setattr(
        audit,
        "_load_sft",
        lambda _: (
            {
                "source": {
                    "admission_assembly_manifest_sha256": admission_hash,
                    "runtime_prompt_manifest_sha256": runtime_hash,
                }
            },
            {"train": [row], "validation": [], "in_domain_test": []},
        ),
    )
    monkeypatch.setattr(
        audit, "primary_variant_selection_report", lambda _: {"status": "pass"}
    )

    output_dir = tmp_path / "new-audit"
    report = audit.audit_release(
        structural_dir=structural_dir,
        admission_dir=admission_dir,
        surface_dir=surface_dir,
        runtime_prompt_dir=runtime_dir,
        sft_dir=sft_dir,
        output_dir=output_dir,
        generated_at="2026-09-14T00:00:00+00:00",
    )

    assert report["checks"]["status"] == "pass"
    assert (output_dir / "release_contract_audit.json").is_file()
    assert (output_dir / "manifest.json").is_file()
