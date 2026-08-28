#!/usr/bin/env python3
"""Write a safe aggregate comparison for paired external SQLite diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.post_training_comparison import (
    ComparisonInputError,
    analyze_paired_sqlite_diagnostics,
    load_sqlite_diagnostic_report,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[3]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-report", type=Path, required=True)
    parser.add_argument("--adapter-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--sample-limit", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_path = ensure_path_outside_repository(args.base_report, ROOT)
    adapter_path = ensure_path_outside_repository(args.adapter_report, ROOT)
    output_path = ensure_path_outside_repository(args.output, ROOT)
    report = analyze_paired_sqlite_diagnostics(
        base_report=load_sqlite_diagnostic_report(base_path),
        adapter_report=load_sqlite_diagnostic_report(adapter_path),
        max_new_tokens=args.max_new_tokens,
        sample_limit=args.sample_limit,
    )
    report["input_evidence"] = {
        "base_sqlite_diagnostic_sha256": sha256_file(base_path),
        "adapter_sqlite_diagnostic_sha256": sha256_file(adapter_path),
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
    except (ComparisonInputError, ValueError) as exc:
        print(f"comparison input error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
