#!/usr/bin/env python3
"""Prepare and run an unmodified full-coverage official Spider Test Suite eval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.spider_test_suite import (
    OfficialSpiderTestSuiteError,
    prepare_complete_spider_test_suite_inputs,
    run_unmodified_spider_test_suite,
)
from data_analysis_agent.sqlite_benchmark import BenchmarkInputError, load_predictions


ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--evaluator-root", type=Path, required=True)
    parser.add_argument("--evaluator-commit", required=True)
    parser.add_argument("--test-suite-database-root", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3_600)
    parser.add_argument("--keep-distinct", action="store_true")
    return parser


def _load_json_list(path: Path) -> list[Mapping[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OfficialSpiderTestSuiteError(f"native case file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OfficialSpiderTestSuiteError(f"native case file is not valid JSON: {path}") from exc
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise OfficialSpiderTestSuiteError("native case file must be a JSON list of objects")
    return raw


def _load_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise OfficialSpiderTestSuiteError(f"prediction file does not exist: {path}") from exc
    records: list[Mapping[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OfficialSpiderTestSuiteError(
                f"prediction JSONL is invalid on line {index}"
            ) from exc
        if not isinstance(record, Mapping):
            raise OfficialSpiderTestSuiteError(
                f"prediction JSONL line {index} must be an object"
            )
        records.append(record)
    return records


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output_directory = ensure_path_outside_repository(args.output_directory, ROOT)
        native_cases = _load_json_list(args.cases)
        predictions = load_predictions(_load_jsonl(args.predictions))
        inputs = prepare_complete_spider_test_suite_inputs(
            native_cases=native_cases,
            predictions=predictions,
            output_directory=output_directory,
            repository_root=ROOT,
        )
        evidence = run_unmodified_spider_test_suite(
            evaluator_root=args.evaluator_root,
            expected_evaluator_commit=args.evaluator_commit,
            test_suite_database_root=args.test_suite_database_root,
            native_cases=native_cases,
            inputs=inputs,
            raw_output_path=output_directory / "official-evaluator-output.txt",
            evidence_path=output_directory / "official-evaluator-evidence.json",
            repository_root=ROOT,
            timeout_seconds=args.timeout_seconds,
            keep_distinct=args.keep_distinct,
        )
    except (OfficialSpiderTestSuiteError, BenchmarkInputError, ValueError) as exc:
        print(f"official evaluator input error: {exc}", file=sys.stderr)
        return 2

    print(
        "Official Spider Test Suite evaluator completed "
        f"{evidence.evaluated_cases} complete cases; "
        "raw output and evidence remain outside the repository."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
