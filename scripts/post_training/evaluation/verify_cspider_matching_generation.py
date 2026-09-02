#!/usr/bin/env python3
"""Verify CSpider Base/Adapter generation differs only by adapter loading.

This verifier runs before SQLite diagnostics. It checks external generation
evidence and prediction case identities without writing questions, SQL text,
database identifiers, database paths, or result rows into its safe report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from typing import Any

from data_analysis_agent.external_artifacts import ensure_path_outside_repository


ROOT = Path(__file__).resolve().parents[3]
CSPIDER_DATASET = "cspider_validation"
CSPIDER_CASE_PREFIX = "cspider_validation"
REQUIRED_MODEL_KEYS = (
    "id",
    "revision",
    "download_manifest_sha256",
    "base_weight_mode",
    "load_in_4bit",
    "quant_type",
    "double_quant",
    "compute_dtype",
)
REQUIRED_COMPARISON_KEYS = (
    "prompt_format_version",
    "dataset",
    "case_id_prefix",
    "max_input_tokens",
    "cases_sha256",
    "tables_sha256",
    "cspider_acquisition_manifest_sha256",
    "decode",
    "gold_sql_read_for_generation",
    "raw_questions_or_prompts_written",
    "raw_database_rows_read",
)
REQUIRED_DECODE_KEYS = ("do_sample", "num_beams", "max_new_tokens", "seed")


class MatchingGenerationError(ValueError):
    """The pair cannot support a one-variable CSpider comparison."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MatchingGenerationError(f"{label} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise MatchingGenerationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise MatchingGenerationError(f"{label} must be a JSON object")
    return value


def _required_mapping(value: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    child = value.get(key)
    if not isinstance(child, Mapping):
        raise MatchingGenerationError(f"{label} lacks object field {key}")
    return child


def _required_string(value: Mapping[str, Any], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise MatchingGenerationError(f"{label} lacks non-empty string field {key}")
    return item


def _required_non_negative_int(value: Mapping[str, Any], key: str, label: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise MatchingGenerationError(f"{label} lacks non-negative integer field {key}")
    return item


def _required_bool(value: Mapping[str, Any], key: str, label: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise MatchingGenerationError(f"{label} lacks boolean field {key}")
    return item


def _prediction_case_ids(path: Path) -> list[str]:
    """Read only prediction record identities; never retain candidate SQL."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise MatchingGenerationError("prediction JSONL does not exist") from exc
    case_ids: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MatchingGenerationError(
                f"prediction JSONL has invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(item, Mapping):
            raise MatchingGenerationError("prediction JSONL rows must be objects")
        case_id = item.get("case_id")
        if not isinstance(case_id, str):
            raise MatchingGenerationError("prediction JSONL row lacks a string case_id")
        if item.get("candidate_index", 0) != 0:
            raise MatchingGenerationError("matching generation only supports candidate_index=0")
        case_ids.append(case_id)
    return case_ids


def _expected_case_ids(expected_case_count: int) -> list[str]:
    if expected_case_count <= 0:
        raise MatchingGenerationError("expected case count must be positive")
    return [f"{CSPIDER_CASE_PREFIX}:{index:05d}" for index in range(expected_case_count)]


def _validate_generation_evidence(
    evidence: Mapping[str, Any], *, label: str, expected_case_count: int, prediction_path: Path
) -> None:
    if evidence.get("run_label") != label:
        raise MatchingGenerationError(f"{label} evidence has a different run label")
    model = _required_mapping(evidence, "model", label)
    comparison = _required_mapping(evidence, "comparison_contract", label)
    generation = _required_mapping(evidence, "generation", label)
    for key in REQUIRED_MODEL_KEYS:
        if key not in model:
            raise MatchingGenerationError(f"{label} evidence lacks model field {key}")
    for key in REQUIRED_COMPARISON_KEYS:
        if key not in comparison:
            raise MatchingGenerationError(f"{label} evidence lacks comparison field {key}")
    if comparison.get("dataset") != CSPIDER_DATASET:
        raise MatchingGenerationError(f"{label} evidence is not CSpider validation")
    if comparison.get("case_id_prefix") != CSPIDER_CASE_PREFIX:
        raise MatchingGenerationError(f"{label} evidence has an invalid case ID prefix")
    for key in ("cases_sha256", "tables_sha256", "cspider_acquisition_manifest_sha256"):
        _required_string(comparison, key, label)
    decode = _required_mapping(comparison, "decode", label)
    for key in REQUIRED_DECODE_KEYS:
        if key not in decode:
            raise MatchingGenerationError(f"{label} evidence lacks decode field {key}")
    if decode.get("do_sample") is not False or decode.get("num_beams") != 1:
        raise MatchingGenerationError(f"{label} evidence is not greedy decoding")
    if model.get("base_weight_mode") != "bf16_lora" or model.get("load_in_4bit") is not False:
        raise MatchingGenerationError(f"{label} evidence is not unquantized bf16 loading")
    if model.get("compute_dtype") != "bfloat16":
        raise MatchingGenerationError(f"{label} evidence does not use bfloat16 compute")
    for key in (
        "gold_sql_read_for_generation",
        "raw_questions_or_prompts_written",
        "raw_database_rows_read",
    ):
        if _required_bool(comparison, key, label) is not False:
            raise MatchingGenerationError(f"{label} evidence violates a generation data boundary")
    if _required_non_negative_int(generation, "native_case_count", label) != expected_case_count:
        raise MatchingGenerationError(f"{label} native case count differs from the expected contract")
    if generation.get("max_cases") is not None:
        raise MatchingGenerationError(f"{label} evidence was generated as a bounded smoke")
    if _required_non_negative_int(generation, "generated_this_invocation", label) != expected_case_count:
        raise MatchingGenerationError(f"{label} did not generate the complete validation coverage")
    if _required_non_negative_int(generation, "existing_prediction_case_count", label) != 0:
        raise MatchingGenerationError(f"{label} generation was resumed rather than freshly frozen")
    prediction_hash = _required_string(generation, "prediction_jsonl_sha256", label)
    if prediction_hash != sha256_file(prediction_path):
        raise MatchingGenerationError(f"{label} prediction JSONL hash differs from its evidence")


def _validate_adapter_pair(base: Mapping[str, Any], adapter: Mapping[str, Any]) -> None:
    base_adapter = _required_mapping(base, "adapter", "base")
    adapter_adapter = _required_mapping(adapter, "adapter", "adapter")
    if base_adapter != {"enabled": False}:
        raise MatchingGenerationError("base evidence must declare adapter disabled only")
    if adapter_adapter.get("enabled") is not True:
        raise MatchingGenerationError("adapter evidence must declare adapter enabled")
    for key in ("adapter_config_sha256", "adapter_model_sha256"):
        _required_string(adapter_adapter, key, "adapter")
    _required_non_negative_int(adapter_adapter, "adapter_model_bytes", "adapter")
    base_model = _required_mapping(base, "model", "base")
    adapter_model = _required_mapping(adapter, "model", "adapter")
    base_comparison = _required_mapping(base, "comparison_contract", "base")
    adapter_comparison = _required_mapping(adapter, "comparison_contract", "adapter")
    for key in REQUIRED_MODEL_KEYS:
        if base_model.get(key) != adapter_model.get(key):
            raise MatchingGenerationError(f"base and adapter model field differs: {key}")
    for key in REQUIRED_COMPARISON_KEYS:
        if base_comparison.get(key) != adapter_comparison.get(key):
            raise MatchingGenerationError(f"base and adapter comparison field differs: {key}")


def verify_matching_generation(
    *,
    base_evidence: Mapping[str, Any],
    adapter_evidence: Mapping[str, Any],
    base_predictions: Path,
    adapter_predictions: Path,
    expected_case_count: int,
) -> dict[str, Any]:
    """Validate a complete CSpider pair and return a report safe to persist."""

    _validate_generation_evidence(
        base_evidence,
        label="base",
        expected_case_count=expected_case_count,
        prediction_path=base_predictions,
    )
    _validate_generation_evidence(
        adapter_evidence,
        label="adapter",
        expected_case_count=expected_case_count,
        prediction_path=adapter_predictions,
    )
    _validate_adapter_pair(base_evidence, adapter_evidence)
    expected_case_ids = _expected_case_ids(expected_case_count)
    if _prediction_case_ids(base_predictions) != expected_case_ids:
        raise MatchingGenerationError("base prediction case IDs are not complete CSpider source order")
    if _prediction_case_ids(adapter_predictions) != expected_case_ids:
        raise MatchingGenerationError("adapter prediction case IDs are not complete CSpider source order")

    base_comparison = _required_mapping(base_evidence, "comparison_contract", "base")
    base_model = _required_mapping(base_evidence, "model", "base")
    decode = _required_mapping(base_comparison, "decode", "base")
    return {
        "report_version": "1",
        "scope": {
            "dataset": CSPIDER_DATASET,
            "expected_case_count": expected_case_count,
            "matching_contract_verified_before_sqlite_diagnostics": True,
            "gold_sql_read_for_generation": False,
            "raw_question_or_sql_written": False,
            "database_identifiers_written": False,
            "result_rows_written": False,
        },
        "invariants": {
            "model_id": base_model["id"],
            "model_revision": base_model["revision"],
            "base_weight_mode": base_model["base_weight_mode"],
            "compute_dtype": base_model["compute_dtype"],
            "prompt_format_version": base_comparison["prompt_format_version"],
            "max_input_tokens": base_comparison["max_input_tokens"],
            "greedy_decode": True,
            "decode_seed": decode["seed"],
            "max_new_tokens": decode["max_new_tokens"],
            "adapter_loaded_transition": "false_to_true",
        },
        "input_evidence": {
            "base_cases_sha256": base_comparison["cases_sha256"],
            "tables_sha256": base_comparison["tables_sha256"],
            "cspider_acquisition_manifest_sha256": base_comparison[
                "cspider_acquisition_manifest_sha256"
            ],
            "base_prediction_jsonl_sha256": sha256_file(base_predictions),
            "adapter_prediction_jsonl_sha256": sha256_file(adapter_predictions),
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-evidence", type=Path, required=True)
    parser.add_argument("--adapter-evidence", type=Path, required=True)
    parser.add_argument("--base-predictions", type=Path, required=True)
    parser.add_argument("--adapter-predictions", type=Path, required=True)
    parser.add_argument("--expected-case-count", type=int, default=1034)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base_evidence_path = ensure_path_outside_repository(args.base_evidence, ROOT)
        adapter_evidence_path = ensure_path_outside_repository(args.adapter_evidence, ROOT)
        base_predictions = ensure_path_outside_repository(args.base_predictions, ROOT)
        adapter_predictions = ensure_path_outside_repository(args.adapter_predictions, ROOT)
        output_path = ensure_path_outside_repository(args.output, ROOT)
        report = verify_matching_generation(
            base_evidence=_load_json_object(base_evidence_path, "base evidence"),
            adapter_evidence=_load_json_object(adapter_evidence_path, "adapter evidence"),
            base_predictions=base_predictions,
            adapter_predictions=adapter_predictions,
            expected_case_count=args.expected_case_count,
        )
    except (MatchingGenerationError, ValueError) as exc:
        print(f"CSpider matching generation error: {exc}", file=sys.stderr)
        return 2
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["scope"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
