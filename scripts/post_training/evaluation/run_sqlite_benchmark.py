#!/usr/bin/env python3
"""Run isolated SQLite diagnostics for native BIRD or Spider development cases.

This runner consumes model predictions produced elsewhere.  It intentionally
does not call an LLM, alter a benchmark database, or calculate benchmark EX.
Use the original benchmark evaluator separately, then attach its summary with
``--official-ex``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping

from data_analysis_agent.sqlite_benchmark import (
    BenchmarkInputError,
    BenchmarkRunMetadata,
    OfficialExSummary,
    SqliteBenchmarkSettings,
    load_predictions,
    normalize_bird_dev_cases,
    normalize_spider_dev_cases,
    run_sqlite_benchmark,
)


ROOT = Path(__file__).resolve().parents[3]
CASE_NORMALIZERS: dict[str, Callable[[Iterable[Mapping[str, Any]]], list]] = {
    "bird_dev": normalize_bird_dev_cases,
    "spider_dev": normalize_spider_dev_cases,
}


def _load_json_list(path: Path) -> list[Mapping[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkInputError(f"case file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkInputError(f"case file is not valid JSON: {path}") from exc
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise BenchmarkInputError("native benchmark case file must contain a JSON list of objects")
    return raw


def _load_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise BenchmarkInputError(f"prediction file does not exist: {path}") from exc
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkInputError(
                f"prediction JSONL is invalid on line {line_number}"
            ) from exc
        if not isinstance(record, Mapping):
            raise BenchmarkInputError(
                f"prediction JSONL line {line_number} must be an object"
            )
        records.append(record)
    return records


def _load_official_ex(path: Path | None) -> OfficialExSummary | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkInputError(f"official EX summary does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkInputError(f"official EX summary is not valid JSON: {path}") from exc
    if not isinstance(raw, Mapping):
        raise BenchmarkInputError("official EX summary must be a JSON object")
    return OfficialExSummary.from_mapping(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=sorted(CASE_NORMALIZERS), required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        required=True,
        help="Native BIRD/Spider dev.json path; it is read but never copied to reports.",
    )
    parser.add_argument(
        "--database-root",
        type=Path,
        required=True,
        help="Root containing <db_id>/<db_id>.sqlite benchmark databases.",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        required=True,
        help="JSONL with case_id, candidate_sql and optional candidate telemetry.",
    )
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="Evaluate the first N native cases for a bounded smoke; never an official metric.",
    )
    parser.add_argument("--statement-timeout-ms", type=int, default=5_000)
    parser.add_argument("--max-rows", type=int, default=1_000)
    parser.add_argument(
        "--official-ex",
        type=Path,
        help="JSON emitted from an unmodified official evaluator; never a locally inferred EX.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cases = CASE_NORMALIZERS[args.dataset](_load_json_list(args.cases))
        if args.max_cases is not None:
            if args.max_cases <= 0:
                raise BenchmarkInputError("max cases must be positive")
            cases = cases[: args.max_cases]
        predictions = load_predictions(_load_jsonl(args.predictions))
        report = run_sqlite_benchmark(
            cases=cases,
            predictions=predictions,
            metadata=BenchmarkRunMetadata(
                dataset_id=args.dataset,
                dataset_version=args.dataset_version,
                model_id=args.model_id,
                model_version=args.model_version,
                prompt_version=args.prompt_version,
            ),
            database_root=args.database_root,
            settings=SqliteBenchmarkSettings(
                statement_timeout_ms=args.statement_timeout_ms,
                max_rows=args.max_rows,
            ),
            official_ex=_load_official_ex(args.official_ex),
        )
    except (BenchmarkInputError, ValueError) as exc:
        print(f"benchmark input error: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = report["summary"]
    print(
        "SQLite benchmark diagnostics: "
        f"{summary['executed_candidates']}/{summary['total_candidate_records']} candidates executed; "
        f"{summary['policy_rejected_candidates']} policy rejected; "
        f"{summary['execution_error_candidates']} execution errors; "
        f"{summary['timeout_candidates']} timeouts."
    )
    if report["official_evaluation"].get("status") == "not_run":
        print("Official EX: not run (local diagnostics do not calculate EX).")
    else:
        print(
            "Official EX: "
            f"{report['official_evaluation']['execution_accuracy']:.4f} "
            f"from {report['official_evaluation']['evaluator_name']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
