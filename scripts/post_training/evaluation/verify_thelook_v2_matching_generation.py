#!/usr/bin/env python3
"""Verify a complete TheLook v2 Base/Adapter generation pair before Gold access.

This command deliberately has no PostgreSQL or model dependency.  Its output
marker is the prerequisite for the later candidate execution and Gold
denotation audit; it contains only hashes and safe metadata.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.thelook_v2_matching import (
    TheLookV2MatchingError,
    read_generation_cases,
    sha256_file,
    verify_matching_generation,
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


def _report(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(
            f"{label} safe report is unavailable or invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise TheLookV2MatchingError(f"{label} safe report must be an object")
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
    cases, _ = read_generation_cases(args.cases_jsonl, args.manifest)
    marker = verify_matching_generation(
        base_report=_report(args.base_safe_report, "base"),
        adapter_report=_report(args.adapter_safe_report, "adapter"),
        base_completions=args.base_completions,
        adapter_completions=args.adapter_completions,
        expected_case_ids=[case.case_id for case in cases],
        expected_cases_sha256=sha256_file(args.cases_jsonl),
        expected_manifest_sha256=sha256_file(args.manifest),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(marker, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"TheLook v2 matching verification error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
