#!/usr/bin/env python3
"""Export a non-reversible Olist protected-family summary outside the repository.

This command never reads the protected holdout. Its external approved input is
limited to reviewed structural family IDs and provenance hashes; the output is
the only summary form accepted by the QuerySpec materialization contract.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import (  # noqa: E402
    PROTECTED_EVIDENCE_VERSION,
    PROTECTED_SUMMARY_VERSION,
    family_fingerprint,
    sha256_file,
)


APPROVED_FAMILY_IDS_VERSION = "olist-approved-protected-family-ids-v1"
EXPORTER_VERSION = "olist-protected-family-summary-exporter-v1"
EVIDENCE_VERSION = PROTECTED_EVIDENCE_VERSION
_FAMILY_ID_RE = re.compile(r"^family_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_APPROVED_INPUT_FIELDS = frozenset(
    {
        "approved_input_version",
        "workspace",
        "protected_source_manifest_sha256",
        "review_reference",
        "family_ids",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-family-ids-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp; set this for byte-reproducible output.",
    )
    return parser.parse_args()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_external_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _require_external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("protected summary output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(f"protected summary output already exists: {resolved}")
    return resolved


def _load_approved_input(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("approved family input must be valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _APPROVED_INPUT_FIELDS:
        raise ValueError("approved family input fields do not match the v1 contract")
    if value["approved_input_version"] != APPROVED_FAMILY_IDS_VERSION:
        raise ValueError("unsupported approved family input version")
    try:
        workspace = WorkspacePin(**value["workspace"])
    except (TypeError, ValueError) as exc:
        raise ValueError("approved family input has an invalid workspace pin") from exc
    if workspace != WorkspacePin.current():
        raise ValueError("approved family input workspace does not match the current pin")
    source_hash = value["protected_source_manifest_sha256"]
    if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
        raise ValueError("protected source manifest hash must be a SHA-256 string")
    review_reference = value["review_reference"]
    if not isinstance(review_reference, str) or not review_reference.strip() or len(review_reference) > 200:
        raise ValueError("review reference must be a short non-empty string")
    family_ids = value["family_ids"]
    if not isinstance(family_ids, list) or not family_ids:
        raise ValueError("approved family input must contain at least one family ID")
    if any(not isinstance(item, str) or not _FAMILY_ID_RE.fullmatch(item) for item in family_ids):
        raise ValueError("approved family IDs must use the v1 family ID format")
    if family_ids != sorted(family_ids) or len(family_ids) != len(set(family_ids)):
        raise ValueError("approved family IDs must be sorted and unique")
    return value


def export_protected_family_summary(
    approved_family_ids_json: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    approved_path = _require_external_file(approved_family_ids_json, "approved family input")
    output_path = _require_external_new_dir(output_dir)
    approved = _load_approved_input(approved_path)
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    fingerprints = sorted(family_fingerprint(family_id) for family_id in approved["family_ids"])
    summary = {
        "summary_version": PROTECTED_SUMMARY_VERSION,
        "family_fingerprints": fingerprints,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        summary_path = staging / "protected_family_summary.json"
        summary_path.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
        evidence = {
            "evidence_version": EVIDENCE_VERSION,
            "exporter_version": EXPORTER_VERSION,
            "generated_at": generated_at,
            "approved_input_version": APPROVED_FAMILY_IDS_VERSION,
            "approved_input_sha256": sha256_file(approved_path),
            "protected_source_manifest_sha256": approved["protected_source_manifest_sha256"],
            "review_reference": approved["review_reference"],
            "workspace": WorkspacePin.current().as_dict(),
            "family_count": len(fingerprints),
            "protected_summary_version": PROTECTED_SUMMARY_VERSION,
            "protected_summary_sha256": sha256_file(summary_path),
        }
        evidence_path = staging / "protected_family_summary_evidence.json"
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "exporter_version": EXPORTER_VERSION,
        "output_dir": str(output_path),
        "family_count": len(fingerprints),
        "protected_summary_sha256": evidence["protected_summary_sha256"],
        "evidence_sha256": sha256_file(output_path / "protected_family_summary_evidence.json"),
        "protected_holdout_raw_read": False,
    }


def main() -> int:
    args = parse_args()
    result = export_protected_family_summary(
        args.approved_family_ids_json,
        args.output_dir,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
