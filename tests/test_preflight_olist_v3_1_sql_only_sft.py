"""Unit tests for v3.1 SQL-only LoRA CPU preflight guards."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts.post_training.training import (
    preflight_olist_v3_1_sql_only_sft as preflight,
)


def _args(**overrides: object) -> argparse.Namespace:
    values = dict(preflight.FROZEN_TRAINING)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_frozen_training_configuration_accepts_exact_contract() -> None:
    assert preflight.frozen_training_from_args(_args()) == preflight.FROZEN_TRAINING


def test_frozen_training_configuration_rejects_drift() -> None:
    with pytest.raises(preflight.OlistV31PreflightError, match="learning_rate"):
        preflight.frozen_training_from_args(_args(learning_rate=2e-4))


def test_verify_split_fingerprints_requires_final_test_role_and_hash() -> None:
    audit = {
        "splits": {
            "train": {
                "sha256": preflight.EXPECTED_SPLIT_SHA256["train"],
                "role": "parameter_updates",
            },
            "validation": {
                "sha256": preflight.EXPECTED_SPLIT_SHA256["validation"],
                "role": "validation_only",
            },
            "in_domain_test": {
                "sha256": preflight.EXPECTED_SPLIT_SHA256["in_domain_test"],
                "role": "final_evaluation_only",
            },
        }
    }
    preflight.verify_split_fingerprints(audit)
    audit["splits"]["in_domain_test"]["role"] = "validation_only"
    with pytest.raises(preflight.OlistV31PreflightError, match="in_domain_test"):
        preflight.verify_split_fingerprints(audit)


def test_verify_release_audit_rejects_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_path = tmp_path / "release.json"
    split_path = tmp_path / "split.json"
    split_path.write_text("{}\n", encoding="utf-8")
    release_path.write_text(
        json.dumps({"checks": {}, "counts": {}, "source": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(preflight, "EXPECTED_RELEASE_AUDIT_SHA256", "0" * 64)

    with pytest.raises(preflight.OlistV31PreflightError, match="SHA-256 drifted"):
        preflight.verify_release_audit(release_path, split_path)
