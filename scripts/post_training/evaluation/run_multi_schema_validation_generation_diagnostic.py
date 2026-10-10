#!/usr/bin/env python3
"""Diagnose frozen SFT validation generation without creating DPO data.

The runner samples only ``validation.jsonl`` from the frozen multi-schema SFT
release.  It never writes a preference pair and never changes model weights.
Gold SQL is deliberately read only after every raw completion is persisted,
then only to classify the aggregate error distribution.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import random
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
for directory in (ROOT, SOURCE_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from data_analysis_agent.dpo_candidate_tier import (  # noqa: E402
    GovernedCandidateOutcome,
    classify_rejected_tier,
)
from data_analysis_agent.dpo_pair import sha256_text  # noqa: E402
from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from scripts.post_training.data.collect_multi_schema_dpo_candidates import (  # noqa: E402
    DEFAULT_RELEASE_DIR,
    DpoCollectionError,
    _evaluate_candidate,
    _frozen_split,
    _generate_one,
    _gold_sql_after_generation,
    _load_parent,
    _read_jsonl,
    _write_jsonl,
    select_generation_sources_with_audit,
    sha256_file,
    summarize_generation_selection,
)


DIAGNOSTIC_SCHEMA_VERSION = "multi-schema-validation-generation-diagnostic-v1"


def _tier_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Classify outcomes for diagnosis without creating a DPO rejected side."""

    counts: Counter[str] = Counter()
    for row in rows:
        if row.get("skip_reason") == "generation_error":
            counts["generation_error"] += 1
            continue
        if row.get("skip_reason") == "normalization_error":
            counts["normalization_error"] += 1
            continue
        evidence = row.get("evidence")
        if not isinstance(evidence, Mapping):
            raise DpoCollectionError("diagnostic candidate lacks governance evidence")
        outcome = GovernedCandidateOutcome(
            policy_status=str(evidence.get("policy_status")),
            execution_status=str(evidence.get("execution_status")),
            result_contract_valid=bool(evidence.get("result_contract_valid")),
            gold_denotation_state=str(evidence.get("gold_denotation_state")),
        )
        tier = classify_rejected_tier(outcome)
        counts["gold_match" if tier is None else tier] += 1
    return dict(sorted(counts.items()))


def _governance_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        evidence = row.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        if evidence.get("policy_status") == "accepted":
            counts["policy_accepted"] += 1
        if evidence.get("execution_status") == "executed":
            counts["postgres_executed"] += 1
        if evidence.get("result_contract_valid") is True:
            counts["result_contract_valid"] += 1
    return dict(sorted(counts.items()))


def _validate_args(args: argparse.Namespace) -> None:
    if args.samples_per_source <= 0 or args.sources_per_workspace <= 0:
        raise DpoCollectionError("source and completion counts must be positive")
    if args.max_input_tokens != 3072 or args.max_new_tokens <= 0:
        raise DpoCollectionError("diagnostic must use 3072 input cap and positive completion cap")
    if not 0 < args.temperature <= 2 or not 0 < args.top_p <= 1:
        raise DpoCollectionError("sampling parameters are outside the bounded contract")


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    import numpy as np
    import torch
    from transformers import set_seed

    _validate_args(args)
    if not torch.cuda.is_available():
        raise DpoCollectionError("CUDA is required for validation generation diagnostic")
    release_dir = ensure_path_outside_repository(args.release_dir, ROOT)
    model_dir = ensure_path_outside_repository(args.model_dir, ROOT)
    adapter_dir = ensure_path_outside_repository(args.parent_adapter_dir, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise DpoCollectionError("diagnostic output directory must be new")

    manifest, validation_path = _frozen_split(release_dir, split_name="validation")
    sources, risk_tag_backfill = select_generation_sources_with_audit(
        _read_jsonl(validation_path),
        per_workspace=args.sources_per_workspace,
        expected_split="validation",
    )
    selection = summarize_generation_selection(
        sources,
        per_workspace=args.sources_per_workspace,
        source_exclusions=risk_tag_backfill,
    )
    selection["source_split"] = "validation"

    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    gpu_uuid = gpu_uuid if gpu_uuid.startswith("GPU-") else "GPU-" + gpu_uuid
    if gpu_uuid != args.expected_gpu_uuid:
        raise DpoCollectionError("CUDA UUID differs from explicit diagnostic guard")

    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer, model, parent = _load_parent(
        model_dir, adapter_dir, args.expected_parent_adapter_sha256
    )
    output_dir.mkdir(parents=True)
    selection_path = output_dir / "selection-manifest.json"
    selection_path.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    raw_rows: list[dict[str, Any]] = []
    # No Gold SQL is read during validation completion generation.
    for source_index, source in enumerate(sources):
        for sample_index in range(args.samples_per_source):
            generation_seed = args.seed + source_index * 10_000 + sample_index
            try:
                completion, tokens, elapsed_ms = _generate_one(
                    tokenizer,
                    model,
                    source.prompt,
                    max_input_tokens=args.max_input_tokens,
                    max_new_tokens=args.max_new_tokens,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    seed=generation_seed,
                )
            except Exception as exc:
                raw_rows.append(
                    {
                        "source_sample_id": source.sample_id,
                        "workspace_key": source.workspace_key,
                        "prompt_sha256": source.prompt_sha256,
                        "sample_index": sample_index,
                        "generation_seed": generation_seed,
                        "generation_error": type(exc).__name__,
                    }
                )
                continue
            raw_rows.append(
                {
                    "source_sample_id": source.sample_id,
                    "workspace_key": source.workspace_key,
                    "prompt_sha256": source.prompt_sha256,
                    "sample_index": sample_index,
                    "generation_seed": generation_seed,
                    "raw_completion": completion,
                    "raw_completion_sha256": sha256_text(completion),
                    "generated_tokens": tokens,
                    "generation_elapsed_ms": elapsed_ms,
                }
            )

    raw_path = output_dir / "raw-completions.jsonl"
    _write_jsonl(raw_path, raw_rows)
    (output_dir / "generation-completed").write_text("completed\n", encoding="utf-8")

    # Re-bind the validation hash immediately before the delayed Gold read.
    _, refreshed_validation_path = _frozen_split(release_dir, split_name="validation")
    source_rows = _gold_sql_after_generation(
        refreshed_validation_path, {source.sample_id for source in sources}
    )
    governed_rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if "raw_completion" not in raw:
            governed_rows.append({**raw, "eligible": False, "skip_reason": "generation_error"})
            continue
        governed_rows.append(
            _evaluate_candidate(source_rows[str(raw["source_sample_id"])], raw)
        )
    governed_path = output_dir / "governed-candidates.jsonl"
    _write_jsonl(governed_path, governed_rows)

    audit = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": "completed",
        "source_release": {
            "version": manifest["release_version"],
            "split": "validation",
            "validation_sha256": sha256_file(validation_path),
        },
        "selection": {
            **selection,
            "samples_per_source": args.samples_per_source,
            "selection_manifest_sha256": sha256_file(selection_path),
        },
        "parent": parent,
        "decode": {
            "do_sample": True,
            "num_beams": 1,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_input_tokens": args.max_input_tokens,
            "max_new_tokens": args.max_new_tokens,
            "base_seed": args.seed,
        },
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
            "name": gpu.name,
            "uuid": gpu_uuid,
        },
        "counts": {
            "raw_attempts": len(raw_rows),
            "generation_errors": sum("generation_error" in row for row in raw_rows),
            "governance": _governance_counts(governed_rows),
            "outcome_tiers": _tier_counts(governed_rows),
        },
        "artifacts": {
            "raw_completions_sha256": sha256_file(raw_path),
            "governed_candidates_sha256": sha256_file(governed_path),
            "raw_artifacts_outside_repository": True,
        },
        "boundaries": {
            "source_split": "validation",
            "validation_diagnostic_only": True,
            "dpo_pairs_written": False,
            "dpo_training_started": False,
            "adapter_best_unchanged": True,
            "protected_tests_read": False,
            "gold_sql_read_after_raw_generation_only": True,
            "production_default_unchanged": True,
        },
    }
    (output_dir / "safe-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--parent-adapter-dir", type=Path, required=True)
    parser.add_argument("--expected-parent-adapter-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sources-per-workspace", type=int, default=6)
    parser.add_argument("--samples-per-source", type=int, default=4)
    parser.add_argument("--max-input-tokens", type=int, default=3072)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        print(json.dumps(diagnose(parse_args()), ensure_ascii=False, sort_keys=True))
    except (DpoCollectionError, OSError, ValueError) as exc:
        print(f"validation generation diagnostic error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
