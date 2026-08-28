#!/usr/bin/env python3
"""Verify non-sensitive hashes for the external QLoRA SFT smoke artifacts.

The verifier deliberately reads only bytes needed for SHA-256 and file-size
checks. It never parses, prints, or copies raw training rows, questions, SQL,
model weights, checkpoints, or result rows into the Git working tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def external_path(path: Path, repository_root: Path, name: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(repository_root.resolve()):
        raise ValueError(f"{name} must remain outside the Git working tree: {resolved}")
    return resolved


def verify_manifest(manifest_path: Path, repository_root: Path) -> dict[str, object]:
    document = mapping(yaml.safe_load(manifest_path.read_text(encoding="utf-8")), "manifest")
    if document.get("status") != "passed_smoke_not_quality_claim":
        raise ValueError("manifest does not declare a passed engineering smoke")

    data = mapping(document.get("data"), "data")
    artifacts = mapping(document.get("artifacts"), "artifacts")
    split_audit = external_path(
        Path(str(data["split_audit_external_path"])), repository_root, "split audit"
    )
    experiment_dir = external_path(
        Path(str(artifacts["external_experiment_dir"])), repository_root, "experiment directory"
    )
    split_dir = split_audit.parent

    expected: list[tuple[str, Path, str, int | None]] = [
        ("split_audit", split_audit, str(data["split_audit_sha256"]), None),
        ("train_jsonl", split_dir / "train.jsonl", str(data["train_jsonl_sha256"]), None),
        (
            "validation_jsonl",
            split_dir / "validation.jsonl",
            str(data["validation_jsonl_sha256"]),
            None,
        ),
        ("sft_evidence", experiment_dir / "sft_smoke.json", str(artifacts["sft_evidence_sha256"]), None),
        (
            "adapter_model",
            experiment_dir / "adapter_final" / "adapter_model.safetensors",
            str(artifacts["adapter_model_sha256"]),
            int(artifacts["adapter_model_bytes"]),
        ),
        (
            "adapter_config",
            experiment_dir / "adapter_final" / "adapter_config.json",
            str(artifacts["adapter_config_sha256"]),
            None,
        ),
        (
            "adapter_reload_evidence",
            experiment_dir / "adapter_validation.json",
            str(artifacts["adapter_reload_evidence_sha256"]),
            None,
        ),
    ]

    checked: list[str] = []
    for name, path, expected_hash, expected_bytes in expected:
        path = external_path(path, repository_root, name)
        if not path.is_file():
            raise FileNotFoundError(f"missing {name}: {path}")
        if len(expected_hash) != 64 or any(character not in "0123456789abcdef" for character in expected_hash):
            raise ValueError(f"{name} has an invalid expected SHA-256")
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise ValueError(f"{name} SHA-256 mismatch")
        if expected_bytes is not None and path.stat().st_size != expected_bytes:
            raise ValueError(f"{name} byte-size mismatch")
        checked.append(name)

    return {
        "manifest_id": document.get("manifest_id"),
        "status": "passed",
        "checked_artifacts": checked,
        "raw_training_rows_printed": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("evals/manifests/post_training_sft_smoke_v1.yaml"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[3]
    report = verify_manifest(args.manifest, repository_root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
