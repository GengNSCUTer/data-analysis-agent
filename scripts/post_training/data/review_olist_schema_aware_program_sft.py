#!/usr/bin/env python3
"""Create a bounded, redacted coverage review for Schema-aware Program SFT.

This command is an Olist-only structural review after the independent audit.
It reports counts and distributions of metrics, query shapes, time grains,
join programs, and token lengths.  It deliberately never prints or writes
questions, prompts, SQL, plan JSON, result rows, or model artifacts, and it
does not read TheLook, a database, an LLM, or a GPU.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.post_training.data.materialize_olist_schema_aware_program_sft import (  # noqa: E402
    SchemaAwareMaterializationError,
    _read_json,
    _read_jsonl,
    sha256_file,
)


REVIEW_VERSION = "olist-schema-aware-program-sft-review-v1"
TASK_A = "sql"
TASK_B = "schema_link_plan"
EXPECTED_METRICS = {
    "average_delivery_days",
    "average_order_value",
    "average_review_score",
    "cancellation_rate",
    "freight_amount",
    "gmv",
    "item_count",
    "on_time_delivery_rate",
    "paid_order_count",
    "positive_review_rate",
}


class SchemaAwareReviewError(ValueError):
    """Raised when the redacted review input violates its audit contract."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--materialization-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-per-stratum", type=int, default=2)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def _external_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or not resolved.is_file():
        raise SchemaAwareReviewError(f"{label} is not an external readable file")
    return resolved


def _external_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or not resolved.is_dir():
        raise SchemaAwareReviewError(f"{label} is not an external readable directory")
    return resolved


def _new_output(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or resolved.exists():
        raise SchemaAwareReviewError("review output must be a new external directory")
    return resolved


def _read_rows(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        rows = _read_jsonl(_external_file(path, label), label)
    except SchemaAwareMaterializationError as exc:
        raise SchemaAwareReviewError(str(exc)) from exc
    return rows


def _require_text(row: Mapping[str, Any], field: str, label: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise SchemaAwareReviewError(f"{label} has no non-empty {field}")
    return value


def _plan(row: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    target = _require_text(row, "target_text", label)
    try:
        value = json.loads(target)
    except json.JSONDecodeError as exc:
        raise SchemaAwareReviewError(f"{label} has invalid plan JSON") from exc
    if not isinstance(value, Mapping) or "sql" in value:
        raise SchemaAwareReviewError(f"{label} has an invalid or SQL-bearing plan")
    return value


def _counter_to_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _sample_key(row: Mapping[str, Any]) -> tuple[str, str]:
    pair_id = _require_text(row, "pair_id", "Task B event")
    plan_id = _require_text(row, "schema_link_plan_id", "Task B event")
    return pair_id, plan_id


def _summarize_split(
    split: str,
    task_a_rows: list[dict[str, Any]],
    task_b_rows: list[dict[str, Any]],
    *,
    sample_per_stratum: int,
) -> dict[str, Any]:
    if len(task_a_rows) != len(task_b_rows):
        raise SchemaAwareReviewError(f"{split} Task A/Task B counts differ")
    a_pairs = {_require_text(row, "pair_id", f"{split} Task A") for row in task_a_rows}
    b_pairs = {_require_text(row, "pair_id", f"{split} Task B") for row in task_b_rows}
    if a_pairs != b_pairs:
        raise SchemaAwareReviewError(f"{split} Task A/Task B pair IDs differ")

    metrics: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    time_modes: Counter[str] = Counter()
    time_grains: Counter[str] = Counter()
    join_programs: Counter[str] = Counter()
    registries: Counter[str] = Counter()
    sequence_lengths: list[int] = []
    plan_samples: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in task_b_rows:
        plan = _plan(row, f"{split} Task B")
        metric_programs = plan.get("metric_programs")
        if not isinstance(metric_programs, list) or not metric_programs:
            raise SchemaAwareReviewError(f"{split} Task B plan has no metric programs")
        metric_ids = []
        for program in metric_programs:
            if not isinstance(program, Mapping):
                raise SchemaAwareReviewError(f"{split} Task B metric program is invalid")
            metric_id = _require_text(program, "metric_id", f"{split} Task B metric program")
            metric_ids.append(metric_id)
            metrics[metric_id] += 1
        shape = _require_text(plan, "result_shape", f"{split} Task B plan")
        time = plan.get("time")
        if not isinstance(time, Mapping):
            raise SchemaAwareReviewError(f"{split} Task B plan has no time object")
        mode = _require_text(time, "mode", f"{split} Task B time")
        raw_grain = time.get("grain")
        if raw_grain is None and mode != "series":
            # Scalar/grouped queries still have a bounded date range but no
            # time-bucket grain.  Record that structural fact explicitly.
            grain = "none"
        elif isinstance(raw_grain, str) and raw_grain:
            grain = raw_grain
        else:
            raise SchemaAwareReviewError(f"{split} Task B time has invalid grain")
        join_program = _require_text(plan, "join_program_id", f"{split} Task B plan")
        registry = _require_text(row, "schema_link_registry_version", f"{split} Task B")
        shapes[shape] += 1
        time_modes[mode] += 1
        time_grains[grain] += 1
        join_programs[join_program] += 1
        registries[registry] += 1
        token_length = row.get("token_length")
        if not isinstance(token_length, Mapping) or not isinstance(
            token_length.get("sequence_tokens"), int
        ):
            raise SchemaAwareReviewError(f"{split} Task B token length is invalid")
        sequence_lengths.append(token_length["sequence_tokens"])
        stratum = (shape, mode, join_program)
        values = plan_samples.setdefault(stratum, [])
        if len(values) < sample_per_stratum:
            pair_id, plan_id = _sample_key(row)
            values.append({"pair_id": pair_id, "schema_link_plan_id": plan_id})

    lengths = sorted(sequence_lengths)
    return {
        "query_instances": len(task_a_rows),
        "task_a_events": len(task_a_rows),
        "task_b_events": len(task_b_rows),
        "families": len(
            {
                _require_text(row, "source_family_id", f"{split} Task A")
                for row in task_a_rows
            }
        ),
        "metrics_referenced": _counter_to_dict(metrics),
        "result_shapes": _counter_to_dict(shapes),
        "time_modes": _counter_to_dict(time_modes),
        "time_grains": _counter_to_dict(time_grains),
        "join_programs": _counter_to_dict(join_programs),
        "schema_link_registry_versions": _counter_to_dict(registries),
        "task_b_sequence_tokens": {
            "min": lengths[0],
            "max": lengths[-1],
            "p50": lengths[(len(lengths) - 1) // 2],
            "max_seq_length": 3072,
            "over_limit": sum(value > 3072 for value in lengths),
        },
        "bounded_plan_samples": [
            {
                "result_shape": shape,
                "time_mode": mode,
                "join_program_id": join_program,
                "pairs": values,
            }
            for (shape, mode, join_program), values in sorted(plan_samples.items())
        ],
    }


def review(
    audit_report_path: Path,
    materialization_dir: Path,
    output_dir: Path,
    *,
    sample_per_stratum: int,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if sample_per_stratum <= 0:
        raise SchemaAwareReviewError("sample-per-stratum must be positive")
    audit_path = _external_file(audit_report_path, "audit report")
    materialization = _external_dir(materialization_dir, "materialization directory")
    output = _new_output(output_dir)
    try:
        audit = _read_json(audit_path, "audit report")
    except SchemaAwareMaterializationError as exc:
        raise SchemaAwareReviewError(str(exc)) from exc
    checks = audit.get("checks")
    if audit.get("audit_version") != "olist-schema-aware-program-sft-audit-v1" or not isinstance(
        checks, Mapping
    ) or checks.get("status") != "pass":
        raise SchemaAwareReviewError("input audit report is not a passing Schema-aware audit")
    task_a_rows = {
        split: _read_rows(materialization / f"{split}_task_a.jsonl", f"{split} Task A")
        for split in ("train", "validation")
    }
    task_b_rows = {
        split: _read_rows(materialization / f"{split}_task_b.jsonl", f"{split} Task B")
        for split in ("train", "validation")
    }
    summaries = {
        split: _summarize_split(
            split,
            task_a_rows[split],
            task_b_rows[split],
            sample_per_stratum=sample_per_stratum,
        )
        for split in ("train", "validation")
    }
    all_metrics = set().union(
        *(summary["metrics_referenced"] for summary in summaries.values())
    )
    if all_metrics != EXPECTED_METRICS:
        raise SchemaAwareReviewError(
            "Olist metric coverage differs from the frozen ten-metric contract"
        )
    generated_at = generated_at or "unknown"
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        report = {
            "review_version": REVIEW_VERSION,
            "generated_at": generated_at,
            "source": {
                "audit_report_sha256": sha256_file(audit_path),
                "materialization_audit_sha256": sha256_file(
                    materialization / "materialization_audit.json"
                ),
            },
            "splits": summaries,
            "checks": {
                "status": "pass",
                "passing_audit_required": True,
                "all_ten_metrics_referenced": True,
                "task_a_task_b_pair_counts_match": True,
                "task_b_no_over_limit": all(
                    summary["task_b_sequence_tokens"]["over_limit"] == 0
                    for summary in summaries.values()
                ),
                "contains_question_or_prompt_or_sql": False,
                "contains_plan_json": False,
                "contains_result_rows": False,
                "thelook_read": False,
                "database_read": False,
                "llm_called": False,
                "gpu_used": False,
            },
        }
        (staging / "review-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = review(
        args.audit_report,
        args.materialization_dir,
        args.output_dir,
        sample_per_stratum=args.sample_per_stratum,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SchemaAwareReviewError, SchemaAwareMaterializationError) as exc:
        print(f"Schema-aware SFT review error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
