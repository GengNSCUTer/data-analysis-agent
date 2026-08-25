from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from scripts.verify_post_training_sft_artifacts import verify_manifest


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write(path: Path, value: bytes) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return sha256(value), len(value)


def manifest_for(external_root: Path) -> dict[str, object]:
    split_dir = external_root / "split"
    experiment_dir = external_root / "experiment"
    split_audit_hash, _ = write(split_dir / "split_audit.json", b"split audit")
    train_hash, _ = write(split_dir / "train.jsonl", b"train rows")
    validation_hash, _ = write(split_dir / "validation.jsonl", b"validation rows")
    evidence_hash, _ = write(experiment_dir / "sft_smoke.json", b"evidence")
    model_hash, model_bytes = write(
        experiment_dir / "adapter_final" / "adapter_model.safetensors", b"adapter weights"
    )
    config_hash, _ = write(experiment_dir / "adapter_final" / "adapter_config.json", b"config")
    reload_hash, _ = write(experiment_dir / "adapter_validation.json", b"reload")
    return {
        "manifest_id": "test_sft_smoke",
        "status": "passed_smoke_not_quality_claim",
        "data": {
            "split_audit_external_path": str(split_dir / "split_audit.json"),
            "split_audit_sha256": split_audit_hash,
            "train_jsonl_sha256": train_hash,
            "validation_jsonl_sha256": validation_hash,
        },
        "artifacts": {
            "external_experiment_dir": str(experiment_dir),
            "sft_evidence_sha256": evidence_hash,
            "adapter_model_bytes": model_bytes,
            "adapter_model_sha256": model_hash,
            "adapter_config_sha256": config_hash,
            "adapter_reload_evidence_sha256": reload_hash,
        },
    }


def test_verifier_accepts_external_hashed_artifacts_without_reading_rows(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    manifest_path = repository_root / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_for(tmp_path / "external")), encoding="utf-8")

    report = verify_manifest(manifest_path, repository_root)

    assert report["status"] == "passed"
    assert report["checked_artifacts"] == [
        "split_audit",
        "train_jsonl",
        "validation_jsonl",
        "sft_evidence",
        "adapter_model",
        "adapter_config",
        "adapter_reload_evidence",
    ]
    assert report["raw_training_rows_printed"] is False


def test_verifier_rejects_hash_mismatch_or_artifact_inside_repository(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    manifest_path = repository_root / "manifest.yaml"
    document = manifest_for(tmp_path / "external")
    manifest_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    adapter = tmp_path / "external" / "experiment" / "adapter_final" / "adapter_model.safetensors"
    adapter.write_bytes(b"changed")

    with pytest.raises(ValueError, match="adapter_model SHA-256 mismatch"):
        verify_manifest(manifest_path, repository_root)

    inside_document = manifest_for(repository_root / "not-external")
    manifest_path.write_text(yaml.safe_dump(inside_document), encoding="utf-8")
    with pytest.raises(ValueError, match="must remain outside the Git working tree"):
        verify_manifest(manifest_path, repository_root)
