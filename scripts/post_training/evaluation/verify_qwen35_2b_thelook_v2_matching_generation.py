#!/usr/bin/env python3
"""Verify the Qwen3.5-2B TheLook v2 pair before any Gold SQL is read."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
for directory in (ROOT, SOURCE_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.qwen35_thelook_v2_matching import verify_matching_generation
from data_analysis_agent.thelook_v2_matching import (
    TheLookV2MatchingError,
    read_generation_cases,
    sha256_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-safe-report", type=Path, required=True)
    parser.add_argument("--adapter-safe-report", type=Path, required=True)
    parser.add_argument("--base-completions", type=Path, required=True)
    parser.add_argument("--adapter-completions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise TheLookV2MatchingError(f"{label} must be an object")
    return value


def main() -> int:
    args = parse_args()
    for path in (
        args.cases_jsonl,
        args.manifest,
        args.base_safe_report,
        args.adapter_safe_report,
        args.base_completions,
        args.adapter_completions,
    ):
        ensure_path_outside_repository(path, ROOT)
    output = ensure_path_outside_repository(args.output, ROOT)
    if output.exists():
        raise TheLookV2MatchingError("matching marker output must be new")
    cases, manifest = read_generation_cases(args.cases_jsonl, args.manifest)
    marker = verify_matching_generation(
        base_report=_read_json(args.base_safe_report, "Base safe report"),
        adapter_report=_read_json(args.adapter_safe_report, "Adapter safe report"),
        base_completions=args.base_completions,
        adapter_completions=args.adapter_completions,
        expected_case_ids=[case.case_id for case in cases],
        expected_cases_sha256=sha256_file(args.cases_jsonl),
        expected_manifest_sha256=sha256_file(args.manifest),
        workspace=manifest["workspace"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"case_count": marker["case_count"], "verified": True}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"Qwen3.5 matching verification error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
