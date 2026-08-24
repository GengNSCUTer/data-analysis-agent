#!/usr/bin/env python3
"""Generate one frozen Ollama SQL candidate per native SQLite benchmark case.

The script never executes generated SQL.  Feed its external JSONL output into
``scripts/run_sqlite_benchmark.py`` for policy/execution diagnostics, then use
an unmodified official evaluator separately for a publishable benchmark score.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from data_analysis_agent.frozen_sqlite_baseline import (
    BaselineGenerationError,
    OllamaChatClient,
    append_predictions,
    ensure_path_outside_repository,
    generate_predictions,
    load_existing_prediction_case_ids,
    load_native_generation_cases,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_json_list(path: Path) -> list[Mapping[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineGenerationError(f"case file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BaselineGenerationError(f"case file is not valid JSON: {path}") from exc
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise BaselineGenerationError("native case file must contain a JSON list of objects")
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("bird_dev", "spider_dev"), required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--model-version",
        required=True,
        help="Immutable local model digest or other exact model revision.",
    )
    parser.add_argument(
        "--prompt-version", default="sqlite-frozen-baseline-v1", required=False
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-schema-characters", type=int, default=20_000)
    parser.add_argument(
        "--max-cases", type=int, help="Optional ordered smoke-run cap; omit for all cases."
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append only missing candidate_index=0 records to an external JSONL file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.prompt_version != "sqlite-frozen-baseline-v1":
            raise BaselineGenerationError("only sqlite-frozen-baseline-v1 is implemented")
        if args.max_cases is not None and args.max_cases <= 0:
            raise BaselineGenerationError("max_cases must be greater than zero")
        output_path = ensure_path_outside_repository(args.output, ROOT)
        existing_case_ids = load_existing_prediction_case_ids(output_path)
        if output_path.exists() and not args.resume:
            raise BaselineGenerationError(
                "prediction output already exists; use --resume or choose a new path"
            )
        cases = load_native_generation_cases(
            _load_json_list(args.cases), dataset_id=args.dataset
        )
        selected_cases = [case for case in cases if case.case_id not in existing_case_ids]
        if args.max_cases is not None:
            selected_cases = selected_cases[: args.max_cases]
        predictions = generate_predictions(
            cases=selected_cases,
            database_root=args.database_root,
            client=OllamaChatClient(
                model=args.model,
                base_url=args.base_url,
                seed=args.seed,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
            ),
            max_schema_characters=args.max_schema_characters,
            on_prediction=lambda prediction: append_predictions(output_path, [prediction]),
        )
    except (BaselineGenerationError, ValueError) as exc:
        print(f"baseline input error: {exc}", file=sys.stderr)
        return 2

    print(
        "Frozen SQLite baseline generated "
        f"{len(predictions)} candidate records for {args.dataset}; "
        f"model={args.model}; model_version={args.model_version}; "
        f"prompt_version={args.prompt_version}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
