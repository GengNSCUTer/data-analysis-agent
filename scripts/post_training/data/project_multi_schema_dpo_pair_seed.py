#!/usr/bin/env python3
"""Project exactly one admitted multi-schema *train* row into a DPO pair seed.

The output contains the original prompt and Gold SQL, so it must live outside
the Git worktree.  This CPU-only script neither loads a model nor accesses a
database.  It deliberately refuses validation/test rows: v1 starts pair
collection from training data only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.dpo_pair import DpoPairValidationError  # noqa: E402
from data_analysis_agent.dpo_pair_seed import build_dpo_pair_seed  # noqa: E402
from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402


DEFAULT_RELEASE_DIR = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/multi-schema-ecommerce-sql-only-v1-r1"
)


class DpoSeedProjectionError(ValueError):
    """The frozen release does not support a traceable one-row projection."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(release_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DpoSeedProjectionError("cannot read frozen release manifest") from exc
    if not isinstance(manifest, dict):
        raise DpoSeedProjectionError("release manifest must be an object")
    if manifest.get("checks", {}).get("status") != "pass":
        raise DpoSeedProjectionError("release manifest is not passing")
    if manifest.get("checks", {}).get("protected_tests_not_training_sources") is not True:
        raise DpoSeedProjectionError("release does not prove protected test isolation")
    if not isinstance(manifest.get("release_version"), str):
        raise DpoSeedProjectionError("release manifest lacks release version")
    return manifest


def _read_target_row(train_path: Path, sample_id: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for number, line in enumerate(train_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DpoSeedProjectionError(f"train JSONL is invalid at line {number}") from exc
        if isinstance(row, dict) and row.get("sample_id") == sample_id:
            matches.append(row)
    if len(matches) != 1:
        raise DpoSeedProjectionError("sample_id must identify exactly one train row")
    return matches[0]


def project_seed(*, release_dir: Path, sample_id: str, output_path: Path) -> dict[str, Any]:
    release_dir = ensure_path_outside_repository(release_dir, ROOT)
    output_path = ensure_path_outside_repository(output_path, ROOT)
    if output_path.exists():
        raise DpoSeedProjectionError("output path already exists")
    manifest = _read_manifest(release_dir)
    train_path = release_dir / "train.jsonl"
    expected = manifest.get("outputs", {}).get("train", {}).get("sha256")
    if not isinstance(expected, str) or sha256_file(train_path) != expected:
        raise DpoSeedProjectionError("train JSONL hash differs from frozen release manifest")
    row = _read_target_row(train_path, sample_id)
    if row.get("split", {}).get("name") != "train":
        raise DpoSeedProjectionError("this seed projector only accepts train rows")
    seed = build_dpo_pair_seed(row, release_version=manifest["release_version"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(seed.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "pair_id": seed.pair_id,
        "split": seed.split,
        "workspace_key": seed.workspace_key,
        "sample_id": seed.source["sample_id"],
        "query_spec_id": seed.source["query_spec_id"],
        "prompt_sha256": seed.source["prompt_sha256"],
        "chosen_sql_sha256": seed.source["canonical_gold_sql_sha256"],
        "output_outside_repository": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        print(json.dumps(project_seed(release_dir=args.release_dir, sample_id=args.sample_id, output_path=args.output_path), ensure_ascii=False, sort_keys=True))
    except (DpoSeedProjectionError, DpoPairValidationError, OSError) as exc:
        print(f"DPO pair seed projection error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
