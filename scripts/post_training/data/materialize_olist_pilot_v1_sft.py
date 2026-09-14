#!/usr/bin/env python3
"""Materialize admitted Olist runtime Prompt -> Gold SQL SFT splits.

All inputs and outputs remain external. The command binds the admitted Gold
assembly and the rebuilt production runtime prompts by seed ID, refuses split
or family drift, audits exact causal-LM length using a local tokenizer, and
writes train/validation/in-domain-test JSONL atomically. It does not train.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import (
    OLIST_CANDIDATE_SQL_PROMPT_VERSION,
)  # noqa: E402
from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE, OLIST_WORKSPACE  # noqa: E402
from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402
from data_analysis_agent.olist_surface_contract import (  # noqa: E402
    OLIST_V3_1_VARIANT_IDS,
    OLIST_V3_1_VARIANT_KIND_BY_ID,
    OLIST_V3_1_VARIANTS_PER_SEED,
)


CONTRACT_VERSION = "olist-v3-1-release-sft-v1"
# Olist's production Catalog + QueryPlan prompt is materially longer than the
# historical SQLite benchmark prompt. Pilot v1 fit 2,304, but the ten-metric
# Medium v1 maximum is 2,915; 3,072 is the smallest practical 256-aligned cap
# that preserves every frozen release row without truncation.
DEFAULT_MAX_SEQ_LENGTH = 3072


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-assembly-dir", type=Path, required=True)
    parser.add_argument("--runtime-prompt-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-train", type=int, required=True)
    parser.add_argument("--expected-validation", type=int, required=True)
    parser.add_argument("--expected-in-domain-test", type=int, required=True)
    parser.add_argument("--max-seq-length", type=int, default=DEFAULT_MAX_SEQ_LENGTH)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_existing(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_dir():
        raise FileNotFoundError(resolved)
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("SFT output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must be an object")
    return result


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has invalid JSON at line {line_no}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {line_no} must be an object")
        rows.append(row)
    return rows


def load_tokenizer(path: Path) -> Any:
    from transformers import AutoTokenizer

    path = _external_existing_dir(path, "tokenizer directory")
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    if tokenizer.eos_token_id is None:
        raise ValueError("tokenizer must define an EOS token")
    return tokenizer


def _workspace_for_pin(raw: Any):
    if not isinstance(raw, Mapping):
        raise ValueError("admission assembly must contain a workspace pin")
    try:
        pin = WorkspacePin(**dict(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("admission assembly workspace pin is invalid") from exc
    if pin == WorkspacePin.current(OLIST_WORKSPACE):
        return OLIST_WORKSPACE
    if pin == WorkspacePin.current(OLIST_V3_WORKSPACE):
        return OLIST_V3_WORKSPACE
    raise ValueError("admission assembly workspace is unsupported")


def _load_assembly(
    directory: Path, expected_rows: int
) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
    directory = _external_existing_dir(directory, "admission assembly directory")
    manifest = _read_json(
        directory / "admission_assembly_manifest.json", "admission assembly manifest"
    )
    records_path = _external_existing(
        directory / "admitted_records.jsonl", "admitted records"
    )
    evidence = manifest.get("output", {}).get("admitted_records_jsonl", {})
    workspace = _workspace_for_pin(manifest.get("workspace"))
    if (
        manifest.get("workspace") != WorkspacePin.current(workspace).as_dict()
        or manifest.get("checks", {}).get("status") != "pass"
    ):
        raise ValueError(
            "admission assembly does not match the current passing workspace"
        )
    if evidence.get("rows") != expected_rows or evidence.get("sha256") != sha256_file(
        records_path
    ):
        raise ValueError("admission assembly records do not match manifest")
    return manifest, _read_jsonl(records_path, "admitted records"), workspace


def _load_runtime(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = _external_existing_dir(directory, "runtime prompt directory")
    manifest = _read_json(
        directory / "runtime_prompt_manifest.json", "runtime prompt manifest"
    )
    records_path = _external_existing(
        directory / "runtime_candidates.jsonl", "runtime candidates"
    )
    evidence = manifest.get("output", {}).get("runtime_candidates_jsonl", {})
    if (
        manifest.get("workspace", {}).get("prompt_version")
        != OLIST_CANDIDATE_SQL_PROMPT_VERSION
    ):
        raise ValueError("runtime prompts use an unexpected production prompt version")
    if manifest.get("checks", {}).get("router_rebuilt") is not True or evidence.get(
        "sha256"
    ) != sha256_file(records_path):
        raise ValueError("runtime prompt evidence is incomplete or mismatched")
    return manifest, _read_jsonl(records_path, "runtime candidates")


def _query_spec_id(row: Mapping[str, Any]) -> str | None:
    """Read the stable ID from runtime rows or from admission's nested QuerySpec."""
    direct = row.get("query_spec_id")
    if isinstance(direct, str) and direct:
        return direct
    spec = row.get("query_spec")
    value = spec.get("query_spec_id") if isinstance(spec, Mapping) else None
    return value if isinstance(value, str) and value else None


PRIMARY_VARIANT_SELECTION_POLICY = "sha256_ranked_split_bucket_eight_way_quota_v2"


def _variant_form_id(seed_id: str, row: Mapping[str, Any]) -> str:
    variant_id = row.get("variant_id")
    prefix = f"{seed_id}-"
    if not isinstance(variant_id, str) or not variant_id.startswith(prefix):
        raise ValueError(f"runtime variant does not bind its seed: {variant_id}")
    form_id = variant_id[len(prefix) :]
    if form_id not in OLIST_V3_1_VARIANT_KIND_BY_ID:
        raise ValueError(
            f"runtime variant has an unsupported surface form: {variant_id}"
        )
    if row.get("variant_kind") != OLIST_V3_1_VARIANT_KIND_BY_ID[form_id]:
        raise ValueError(f"runtime variant kind drifted for {variant_id}")
    return form_id


def _variants_by_form(
    seed_id: str, variants: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Require the v3.1 eight-form overlay; legacy forms are never reinterpreted."""
    by_form: dict[str, dict[str, Any]] = {}
    for row in variants:
        form_id = _variant_form_id(seed_id, row)
        if form_id in by_form:
            raise ValueError(
                f"runtime seed {seed_id} has duplicate surface form {form_id}"
            )
        by_form[form_id] = row
    if len(variants) != OLIST_V3_1_VARIANTS_PER_SEED or set(by_form) != set(
        OLIST_V3_1_VARIANT_IDS
    ):
        raise ValueError(f"runtime seed {seed_id} must contain v1-v8 exactly once")
    return by_form


def _exact_variant_quotas(rows: int) -> dict[str, int]:
    """Return the fixed, near-equal form quota for one formal split."""
    if rows <= 0:
        raise ValueError("primary-variant quota requires a positive split size")
    base, remainder = divmod(rows, OLIST_V3_1_VARIANTS_PER_SEED)
    # Split totals are deterministic: train=3,000 yields 375 each; for 750,
    # v1-v6 receive 94 and v7-v8 receive 93.  Bucket-level residual slots are
    # subsequently assigned to meet these exact split totals.
    return {
        form_id: base + int(index < remainder)
        for index, form_id in enumerate(OLIST_V3_1_VARIANT_IDS)
    }


def _stable_rank(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def select_primary_variants(
    admitted_by_seed: Mapping[str, Mapping[str, Any]],
    runtime_by_seed: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Assign one surface per row with exact split quotas and bucket balance.

    A raw ``hash % 8`` is repeatable but can skew a small evaluation bucket.
    This allocator first reserves equal ``split × primary_bucket`` quotas (at
    most one apart), then assigns every residual slot so each full split meets
    its exact fixed quota.  Stable SHA-256 ranks make the result rebuildable
    without depending on input JSONL ordering or process randomness.
    """
    if set(admitted_by_seed) != set(runtime_by_seed):
        raise ValueError("admission/runtime seed sets must be identical release sets")
    grouped: dict[tuple[str, str], list[str]] = {}
    forms_by_seed: dict[str, dict[str, dict[str, Any]]] = {}
    for seed_id, admitted in admitted_by_seed.items():
        split = admitted.get("split")
        bucket = admitted.get("primary_bucket")
        if (
            not isinstance(split, str)
            or not split
            or not isinstance(bucket, str)
            or not bucket
        ):
            raise ValueError(f"admitted row lacks split/bucket identity: {seed_id}")
        forms_by_seed[seed_id] = _variants_by_form(seed_id, runtime_by_seed[seed_id])
        grouped.setdefault((split, bucket), []).append(seed_id)

    groups_by_split: dict[str, list[tuple[str, str]]] = {}
    for group in grouped:
        groups_by_split.setdefault(group[0], []).append(group)
    allocation: dict[tuple[str, str], dict[str, int]] = {}
    for split, groups in groups_by_split.items():
        groups = sorted(
            groups,
            key=lambda item: _stable_rank(PRIMARY_VARIANT_SELECTION_POLICY, *item),
        )
        split_total = sum(len(grouped[group]) for group in groups)
        target = _exact_variant_quotas(split_total)
        allocation[split, "__target__"] = target
        residual = dict(target)
        remainders: dict[tuple[str, str], int] = {}
        for group in groups:
            base, remainder = divmod(len(grouped[group]), OLIST_V3_1_VARIANTS_PER_SEED)
            allocation[group] = dict.fromkeys(OLIST_V3_1_VARIANT_IDS, base)
            for form_id in OLIST_V3_1_VARIANT_IDS:
                residual[form_id] -= base
            remainders[group] = remainder
        if any(value < 0 for value in residual.values()):
            raise AssertionError("bucket base quotas exceed the split-level target")
        for group in groups:
            remainder = remainders[group]
            candidates = sorted(
                OLIST_V3_1_VARIANT_IDS,
                key=lambda form_id: (
                    -residual[form_id],
                    _stable_rank(PRIMARY_VARIANT_SELECTION_POLICY, *group, form_id),
                ),
            )
            for form_id in candidates[:remainder]:
                if residual[form_id] <= 0:
                    raise AssertionError(
                        "cannot allocate an exact split-level primary-form quota"
                    )
                allocation[group][form_id] += 1
                residual[form_id] -= 1
        if any(residual.values()):
            raise AssertionError("primary-form residual quota did not close")

    primary_by_seed: dict[str, dict[str, Any]] = {}
    for group, seed_ids in grouped.items():
        slots = [
            form_id
            for form_id in OLIST_V3_1_VARIANT_IDS
            for _ in range(allocation[group][form_id])
        ]
        if len(slots) != len(seed_ids):
            raise AssertionError(
                "primary-form bucket allocation does not match row count"
            )
        ordered_seeds = sorted(
            seed_ids,
            key=lambda seed_id: _stable_rank(
                PRIMARY_VARIANT_SELECTION_POLICY, *group, seed_id
            ),
        )
        for seed_id, form_id in zip(ordered_seeds, slots, strict=True):
            primary_by_seed[seed_id] = forms_by_seed[seed_id][form_id]
    return primary_by_seed


def primary_variant_selection_report(
    splits: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Verify and serialize the exact split/bucket primary-form allocation."""
    report: dict[str, Any] = {"by_split": {}, "by_split_bucket": {}}
    for split, rows in splits.items():
        actual = dict.fromkeys(OLIST_V3_1_VARIANT_IDS, 0)
        by_bucket: dict[str, dict[str, int]] = {}
        for row in rows:
            seed_id = row.get("seed_id")
            if not isinstance(seed_id, str):
                raise ValueError("SFT row lacks a seed ID for surface selection audit")
            form_id = _variant_form_id(
                seed_id,
                {
                    "variant_id": row.get("primary_variant_id"),
                    "variant_kind": row.get("language_variant_kind"),
                },
            )
            actual[form_id] += 1
            bucket = row.get("primary_bucket")
            if not isinstance(bucket, str) or not bucket:
                raise ValueError(
                    "SFT row lacks primary_bucket for surface selection audit"
                )
            counts = by_bucket.setdefault(
                bucket, dict.fromkeys(OLIST_V3_1_VARIANT_IDS, 0)
            )
            counts[form_id] += 1
        expected = _exact_variant_quotas(len(rows))
        if actual != expected:
            raise ValueError(
                f"primary-form split quota drifted for {split}: {actual} != {expected}"
            )
        bucket_report = {}
        for bucket, counts in sorted(by_bucket.items()):
            values = tuple(counts.values())
            if max(values) - min(values) > 1:
                raise ValueError(
                    f"primary-form bucket quota drifted for {split}/{bucket}: {counts}"
                )
            bucket_report[bucket] = {
                "actual": counts,
                "max_minus_min": max(values) - min(values),
            }
        report["by_split"][split] = {"actual": actual, "expected": expected}
        report["by_split_bucket"][split] = bucket_report
    return report


def build_rows(
    admitted: list[dict[str, Any]],
    runtime: list[dict[str, Any]],
    tokenizer: Any,
    max_seq_length: int,
    expected_splits: Mapping[str, int],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    if max_seq_length <= 0:
        raise ValueError("max_seq_length must be positive")
    admitted_by_seed = {str(row.get("seed_id")): row for row in admitted}
    # Runtime materialization deliberately contains eight typed surface forms per
    # query instance. SFT must keep one semantic row per instance; silently
    # taking the last dict entry would make the chosen form depend on JSONL
    # ordering. Require v1-v8 exactly once and choose with fixed split/bucket
    # quotas rather than pseudo-random modulo selection.
    runtime_by_seed: dict[str, list[dict[str, Any]]] = {}
    for row in runtime:
        runtime_by_seed.setdefault(str(row.get("seed_id")), []).append(row)
    expected_rows = sum(expected_splits.values())
    if len(admitted_by_seed) != expected_rows or set(admitted_by_seed) != set(
        runtime_by_seed
    ):
        raise ValueError("admission/runtime seed sets must be identical release sets")
    primary_by_seed = select_primary_variants(admitted_by_seed, runtime_by_seed)
    splits = {name: [] for name in expected_splits}
    exclusions: list[dict[str, Any]] = []
    families_by_split: dict[str, set[str]] = {name: set() for name in expected_splits}
    family_split: dict[str, str] = {}
    query_spec_ids: set[str] = set()
    for index, seed_id in enumerate(admitted_by_seed, 1):
        admitted_row = admitted_by_seed[seed_id]
        runtime_row = primary_by_seed[seed_id]
        split = admitted_row.get("split")
        if split not in splits:
            raise ValueError("admitted row has unsupported split")
        identity_fields = ("split", "family_id", "sql_program_id", "primary_bucket")
        if any(
            admitted_row.get(field) != runtime_row.get(field)
            for field in identity_fields
        ):
            raise ValueError(f"runtime identity drift for {seed_id}")
        query_spec_id = _query_spec_id(admitted_row)
        if query_spec_id is None or query_spec_id != _query_spec_id(runtime_row):
            raise ValueError(f"runtime QuerySpec identity drift for {seed_id}")
        family_id = str(admitted_row.get("family_id"))
        families_by_split[split].add(family_id)
        prior_split = family_split.get(family_id)
        if prior_split is not None and prior_split != split:
            raise ValueError(f"family {family_id} crosses splits")
        family_split.setdefault(family_id, split)
        if query_spec_id in query_spec_ids:
            raise ValueError(f"duplicate QuerySpec ID {query_spec_id}")
        query_spec_ids.add(query_spec_id)
        prompt = runtime_row.get("prompt")
        sql = admitted_row.get("gold_sql")
        if (
            not isinstance(prompt, str)
            or not isinstance(sql, str)
            or not prompt.endswith("### SQL")
        ):
            raise ValueError(f"invalid runtime prompt or Gold SQL for {seed_id}")
        training_text = prompt + "\n" + sql.strip()
        prompt_tokens = len(
            tokenizer(prompt + "\n", add_special_tokens=False)["input_ids"]
        )
        target_tokens = (
            len(tokenizer(sql.strip(), add_special_tokens=False)["input_ids"]) + 1
        )
        sequence_tokens = prompt_tokens + target_tokens
        sample_id = f"olist-release-{index:04d}"
        if sequence_tokens > max_seq_length:
            exclusions.append(
                {
                    "sample_id": sample_id,
                    "seed_id": seed_id,
                    "split": split,
                    "family_id": family_id,
                    "sequence_tokens": sequence_tokens,
                    "prompt_tokens": prompt_tokens,
                    "target_plus_eos_tokens": target_tokens,
                    "reason": "sequence_exceeds_frozen_contract",
                    "eligible_for_sft": False,
                }
            )
            continue
        splits[split].append(
            {
                "sample_id": sample_id,
                "split": {"name": split},
                "prompt_format_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
                "seed_id": seed_id,
                "query_spec_id": query_spec_id,
                "family_id": family_id,
                "sql_program_id": admitted_row["sql_program_id"],
                "primary_bucket": admitted_row["primary_bucket"],
                "language_variant_id": runtime_row["variant_id"],
                "language_variant_kind": runtime_row["variant_kind"],
                "primary_variant_id": runtime_row["variant_id"],
                "surface_variant_count": OLIST_V3_1_VARIANTS_PER_SEED,
                "surface_form_policy": "eight_forms_one_query_instance",
                "primary_variant_selection_policy": PRIMARY_VARIANT_SELECTION_POLICY,
                "rendered_prompt": prompt,
                "candidate_sql": sql.strip(),
                "training_text": training_text,
                "admission_status": "admitted",
                "execution_outcome": {"postgres_reader_result_contract": "pass"},
                "token_length": {
                    "sequence_tokens": sequence_tokens,
                    "prompt_tokens": prompt_tokens,
                    "target_plus_eos_tokens": target_tokens,
                },
            }
        )
    if exclusions:
        raise ValueError(
            "release does not permit length exclusions; inspect external exclusion evidence"
        )
    if {name: len(rows) for name, rows in splits.items()} != dict(expected_splits):
        raise ValueError("materialized split counts differ from the release contract")
    split_family_sets = [{row["family_id"] for row in rows} for rows in splits.values()]
    for left_index, left in enumerate(split_family_sets):
        for right in split_family_sets[left_index + 1 :]:
            if left & right:
                raise ValueError("family IDs cross splits")
    return splits, exclusions


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def materialize(
    assembly_dir: Path,
    runtime_dir: Path,
    tokenizer_dir: Path,
    output_dir: Path,
    *,
    max_seq_length: int,
    expected_splits: Mapping[str, int],
    generated_at: str | None = None,
) -> dict[str, Any]:
    output_dir = _external_new_dir(output_dir)
    if set(expected_splits) != {"train", "validation", "in_domain_test"} or any(
        value <= 0 for value in expected_splits.values()
    ):
        raise ValueError(
            "expected split counts must be positive train/validation/in_domain_test values"
        )
    assembly_manifest, admitted, workspace = _load_assembly(
        assembly_dir, sum(expected_splits.values())
    )
    runtime_manifest, runtime = _load_runtime(runtime_dir)
    runtime_workspace_id = runtime_manifest.get("workspace", {}).get("workspace_id")
    # Old v2 prompt manifests predate an explicit workspace_id.  Keep their
    # frozen compatibility only for the default v2 assembly; v3 must carry
    # the explicit isolated workspace identity.
    if runtime_workspace_id is None and workspace != OLIST_WORKSPACE:
        raise ValueError("v3 runtime prompts must record an explicit workspace ID")
    if (
        runtime_workspace_id is not None
        and runtime_workspace_id != workspace.workspace_id
    ):
        raise ValueError("runtime prompts do not match the admitted workspace")
    tokenizer = load_tokenizer(tokenizer_dir)
    splits, exclusions = build_rows(
        admitted, runtime, tokenizer, max_seq_length, expected_splits
    )
    primary_selection = primary_variant_selection_report(splits)
    generated_at = (
        generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        (staging / "final_evaluation_only").mkdir()
        (staging / "exclusions").mkdir()
        staging_paths = {
            "train": staging / "train.jsonl",
            "validation": staging / "validation.jsonl",
            "in_domain_test": staging
            / "final_evaluation_only"
            / "in_domain_test.jsonl",
        }
        final_paths = {
            "train": output_dir / "train.jsonl",
            "validation": output_dir / "validation.jsonl",
            "in_domain_test": output_dir
            / "final_evaluation_only"
            / "in_domain_test.jsonl",
        }
        for split, path in staging_paths.items():
            write_jsonl(path, splits[split])
        exclusion_path = staging / "exclusions" / "length.jsonl"
        write_jsonl(exclusion_path, exclusions)
        split_metadata = {}
        for split, rows in splits.items():
            lengths = [row["token_length"]["sequence_tokens"] for row in rows]
            split_metadata[split] = {
                "rows": len(rows),
                "families": len({row["family_id"] for row in rows}),
                "query_specs": len({row["query_spec_id"] for row in rows}),
                "sha256": sha256_file(staging_paths[split]),
                "max_sequence_tokens": max(lengths),
                "min_sequence_tokens": min(lengths),
                "role": "parameter_updates"
                if split == "train"
                else "validation_only"
                if split == "validation"
                else "final_evaluation_only",
            }
        audit = {
            "audit_version": CONTRACT_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current(workspace).as_dict(),
            "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            "source": {
                "admission_assembly_manifest_sha256": sha256_file(
                    _external_existing(
                        Path(assembly_dir) / "admission_assembly_manifest.json",
                        "assembly manifest",
                    )
                ),
                "runtime_prompt_manifest_sha256": sha256_file(
                    _external_existing(
                        Path(runtime_dir) / "runtime_prompt_manifest.json",
                        "runtime prompt manifest",
                    )
                ),
            },
            "tokenizer": {
                "dir": str(Path(tokenizer_dir).resolve()),
                "eos_token_id": tokenizer.eos_token_id,
            },
            "training_length_contract": {
                "max_seq_length": max_seq_length,
                "formula": "exact rendered runtime prompt + canonical SQL + EOS",
                "silent_truncation": False,
            },
            "policy": {
                "split_strategy": "olist_family_isolated_v1",
                "primary_group": "family_id",
                "test_storage": "final_evaluation_only",
                "test_forbidden_for_training": True,
                "surface_form_policy": "eight_forms_one_query_instance",
                "primary_variant_selection_policy": PRIMARY_VARIANT_SELECTION_POLICY,
                "surface_variant_ids": list(OLIST_V3_1_VARIANT_IDS),
                "primary_variant_selection": primary_selection,
            },
            "splits": split_metadata,
            "outputs": {
                # The audit remains valid after the atomic staging-directory rename.
                "train_jsonl": str(final_paths["train"]),
                "validation_jsonl": str(final_paths["validation"]),
                "in_domain_test_jsonl": str(final_paths["in_domain_test"]),
            },
            "exclusions": {
                "path": str(output_dir / "exclusions" / "length.jsonl"),
                "rows": 0,
                "sha256": sha256_file(exclusion_path),
                "contains_question_or_sql": False,
            },
            "checks": {
                "status": "pass",
                "family_split_overlap": [],
                "query_spec_split_overlap": [],
                "sql_program_split_overlap_allowed": True,
                "all_gold_admitted": True,
                "runtime_contract_rebuilt": True,
                "primary_surface_split_quotas_exact": True,
                "primary_surface_bucket_balance_at_most_one": True,
                "protected_holdout_raw_read": False,
                "in_domain_test_forbidden_for_training": True,
                "gpu_used": False,
            },
        }
        (staging / "split_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return audit


def main() -> int:
    args = parse_args()
    expected_splits = {
        "train": args.expected_train,
        "validation": args.expected_validation,
        "in_domain_test": args.expected_in_domain_test,
    }
    result = materialize(
        args.admission_assembly_dir,
        args.runtime_prompt_dir,
        args.tokenizer_dir,
        args.output_dir,
        max_seq_length=args.max_seq_length,
        expected_splits=expected_splits,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
