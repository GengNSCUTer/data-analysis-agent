#!/usr/bin/env python3
"""Compare redacted Olist candidate outcomes across question-language conditions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import (
    OlistCandidateEvaluationError,
    build_safe_locale_comparison,
)
from data_analysis_agent.post_training_comparison import sha256_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--target-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def load_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OlistCandidateEvaluationError("safe report does not exist") from exc
    except json.JSONDecodeError as exc:
        raise OlistCandidateEvaluationError("safe report is not valid JSON") from exc
    if not isinstance(value, dict):
        raise OlistCandidateEvaluationError("safe report must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_path = ensure_path_outside_repository(args.source_report, ROOT)
    target_path = ensure_path_outside_repository(args.target_report, ROOT)
    output_path = ensure_path_outside_repository(args.output, ROOT)
    report = build_safe_locale_comparison(
        load_report(source_path), load_report(target_path)
    )
    report["input_evidence"] = {
        "source_safe_report_sha256": sha256_file(source_path),
        "target_safe_report_sha256": sha256_file(target_path),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OlistCandidateEvaluationError, ValueError) as exc:
        print(f"olist candidate locale comparison input error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
