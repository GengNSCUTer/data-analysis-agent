#!/usr/bin/env python3
"""Generate one deterministic Chinese question for each admitted Olist release row.

The question is an external natural-language overlay.  It expresses only the
already frozen QuerySpec and is not used as a second source of business logic.
Runtime prompt construction still rebuilds Router, Catalog, QueryPlan and the
result contract before the row can enter SFT data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import OLIST_CANDIDATE_SQL_PROMPT_VERSION  # noqa: E402
from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402
from scripts.post_training.data.generate_olist_pilot_v1_questions import render_question  # noqa: E402


OVERLAY_VERSION = "1"
MAX_ROWS = 1500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-assembly-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _external_existing(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _external_new(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("question overlay must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def load_admitted_rows(directory: Path) -> list[dict[str, Any]]:
    directory = directory.resolve()
    if directory.is_relative_to(ROOT) or not directory.is_dir():
        raise ValueError("admission assembly directory must exist outside the Git worktree")
    manifest_path = _external_existing(directory / "admission_assembly_manifest.json", "assembly manifest")
    records_path = _external_existing(directory / "admitted_records.jsonl", "admitted records")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("workspace") != WorkspacePin.current().as_dict():
        raise ValueError("assembly workspace differs from current pin")
    if manifest.get("checks", {}).get("status") != "pass":
        raise ValueError("assembly did not pass")
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    rows = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line]
    if not 1 <= len(rows) <= MAX_ROWS or output.get("rows") != len(rows):
        raise ValueError("admission record count is outside the release contract")
    if output.get("sha256") != sha256_file(records_path):
        raise ValueError("assembly admitted records hash mismatch")
    if any(row.get("admission_status") != "admitted" for row in rows):
        raise ValueError("all release records must be admitted before question generation")
    if len({row.get("seed_id") for row in rows}) != len(rows):
        raise ValueError("admitted records have duplicate seed IDs")
    return rows


def generate(admission_assembly_dir: Path, output_json: Path) -> dict[str, Any]:
    rows = load_admitted_rows(admission_assembly_dir)
    output_json = _external_new(output_json)
    cases = []
    for row in rows:
        spec = row.get("query_spec")
        if not isinstance(spec, dict):
            raise ValueError("admitted record has no QuerySpec")
        cases.append({"seed_id": row["seed_id"], "question": render_question(spec)})
    payload = {
        "schema_version": OVERLAY_VERSION,
        "language": "zh",
        "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
        "cases": cases,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    staging = output_json.parent / f".{output_json.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(output_json)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return {"rows": len(cases), "sha256": sha256_file(output_json), "output": str(output_json)}


def main() -> int:
    args = parse_args()
    print(json.dumps(generate(args.admission_assembly_dir, args.output_json), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
