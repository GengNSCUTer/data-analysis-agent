from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_analysis_agent.olist_queryspec import WorkspacePin
from scripts.post_training.data.export_olist_protected_family_summary import (
    APPROVED_FAMILY_IDS_VERSION,
    EVIDENCE_VERSION,
    EXPORTER_VERSION,
    export_protected_family_summary,
)
from scripts.post_training.data.materialize_olist_queryspecs import (
    PROTECTED_SUMMARY_VERSION,
    family_fingerprint,
)


def _approved_input(*, family_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "approved_input_version": APPROVED_FAMILY_IDS_VERSION,
        "workspace": WorkspacePin.current().as_dict(),
        "protected_source_manifest_sha256": "a" * 64,
        "review_reference": "manual-review-2026-09-04",
        "family_ids": family_ids or ["family_0123456789abcdef01234567"],
    }


def _write_approved(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_export_writes_only_fingerprints_and_hash_evidence(tmp_path: Path) -> None:
    approved = tmp_path / "approved.json"
    output = tmp_path / "protected-summary"
    family_ids = ["family_0123456789abcdef01234567", "family_abcdef0123456789abcdef01"]
    _write_approved(approved, _approved_input(family_ids=family_ids))

    result = export_protected_family_summary(
        approved,
        output,
        generated_at="2026-09-04T00:00:00+00:00",
    )

    summary = json.loads((output / "protected_family_summary.json").read_text(encoding="utf-8"))
    evidence = json.loads((output / "protected_family_summary_evidence.json").read_text(encoding="utf-8"))
    assert summary == {
        "summary_version": PROTECTED_SUMMARY_VERSION,
        "family_fingerprints": sorted(family_fingerprint(item) for item in family_ids),
    }
    assert evidence["evidence_version"] == EVIDENCE_VERSION
    assert evidence["exporter_version"] == EXPORTER_VERSION
    assert evidence["family_count"] == 2
    assert evidence["workspace"] == WorkspacePin.current().as_dict()
    assert "family_ids" not in evidence
    assert "question" not in (output / "protected_family_summary_evidence.json").read_text(encoding="utf-8")
    assert result["protected_holdout_raw_read"] is False


def test_export_is_byte_reproducible_with_a_frozen_timestamp(tmp_path: Path) -> None:
    approved = tmp_path / "approved.json"
    _write_approved(approved, _approved_input())
    first = tmp_path / "first"
    second = tmp_path / "second"

    export_protected_family_summary(approved, first, generated_at="2026-09-04T00:00:00+00:00")
    export_protected_family_summary(approved, second, generated_at="2026-09-04T00:00:00+00:00")

    assert (first / "protected_family_summary.json").read_bytes() == (
        second / "protected_family_summary.json"
    ).read_bytes()
    assert (first / "protected_family_summary_evidence.json").read_bytes() == (
        second / "protected_family_summary_evidence.json"
    ).read_bytes()


def test_export_rejects_unapproved_raw_or_unsorted_input(tmp_path: Path) -> None:
    approved = tmp_path / "approved.json"
    raw = _approved_input()
    raw["question"] = "protected text must never reach the exporter"
    _write_approved(approved, raw)

    with pytest.raises(ValueError, match="fields do not match"):
        export_protected_family_summary(approved, tmp_path / "output")

    unsorted = _approved_input(
        family_ids=["family_abcdef0123456789abcdef01", "family_0123456789abcdef01234567"]
    )
    _write_approved(approved, unsorted)

    with pytest.raises(ValueError, match="sorted and unique"):
        export_protected_family_summary(approved, tmp_path / "output-2")


def test_export_rejects_repository_paths_and_workspace_drift(tmp_path: Path) -> None:
    approved = tmp_path / "approved.json"
    drifted = _approved_input()
    workspace = dict(drifted["workspace"])
    workspace["metric_version"] = "unexpected"
    drifted["workspace"] = workspace
    _write_approved(approved, drifted)

    with pytest.raises(ValueError, match="workspace does not match"):
        export_protected_family_summary(approved, tmp_path / "output")

    with pytest.raises(ValueError, match="must stay outside"):
        export_protected_family_summary(Path("data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl"), tmp_path / "output-2")
