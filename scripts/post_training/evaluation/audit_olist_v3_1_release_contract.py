#!/usr/bin/env python3
"""Audit the complete Olist v3.1 release against its structural and surface contracts.

This is intentionally a release-level verifier, not a unit test for a single
template.  It binds structural Gold evidence, the eight-form question overlay,
runtime prompt reconstruction, and the final SFT rows by hashes and stable IDs.
It never creates SQL, calls a model, executes a query, or starts GPU work.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_surface_contract import (  # noqa: E402
    OLIST_V3_1_SURFACE_VERSION,
    OLIST_V3_1_VARIANT_IDS,
    OLIST_V3_1_VARIANT_KIND_BY_ID,
    OLIST_V3_1_VARIANT_POLICY,
    OLIST_V3_1_VARIANT_SCHEMA_VERSION,
    OLIST_V3_1_VARIANTS_PER_SEED,
    contains_latin_token,
)
from scripts.post_training.data.build_olist_v3_balanced_release_candidates import (  # noqa: E402
    RELEASE_VERSION,
    balance_coverage_report,
)
from scripts.post_training.data.materialize_olist_pilot_v1_sft import (  # noqa: E402
    primary_variant_selection_report,
    sha256_file,
)
from scripts.post_training.evaluation.admit_olist_v3_balanced_gold_release import (  # noqa: E402
    EXPECTED_ROWS,
    load_full_v3_gold_rows,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structural-dir", type=Path, required=True)
    parser.add_argument("--admission-dir", type=Path, required=True)
    parser.add_argument("--surface-dir", type=Path, required=True)
    parser.add_argument("--runtime-prompt-dir", type=Path, required=True)
    parser.add_argument("--sft-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def _external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or not resolved.is_dir():
        raise ValueError(
            f"{label} must be an existing directory outside the Git worktree"
        )
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or resolved.exists():
        raise ValueError(
            "contract audit output must be a new directory outside the Git worktree"
        )
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be readable valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has invalid JSONL at line {number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} has a non-object row at line {number}")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def _load_admitted(admission_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(
        admission_dir / "admission_assembly_manifest.json", "admission manifest"
    )
    path = admission_dir / "admitted_records.jsonl"
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    rows = _read_jsonl(path, "admitted records")
    if (
        manifest.get("checks", {}).get("status") != "pass"
        or output.get("rows") != EXPECTED_ROWS
        or output.get("sha256") != sha256_file(path)
        or len(rows) != EXPECTED_ROWS
        or any(row.get("admission_status") != "admitted" for row in rows)
    ):
        raise ValueError(
            "admission evidence is not a complete hash-bound passing release"
        )
    return manifest, rows


def _load_surface(
    surface_dir: Path, seed_ids: set[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(surface_dir / "surface_manifest.json", "surface manifest")
    path = surface_dir / "question_variants.json"
    payload = _read_json(path, "question variants")
    if (
        manifest.get("surface_version") != OLIST_V3_1_SURFACE_VERSION
        or manifest.get("counts", {}).get("query_instances") != EXPECTED_ROWS
        or manifest.get("counts", {}).get("variants_per_instance")
        != OLIST_V3_1_VARIANTS_PER_SEED
        or manifest.get("output", {}).get("question_variants_json", {}).get("sha256")
        != sha256_file(path)
        or payload.get("schema_version") != OLIST_V3_1_VARIANT_SCHEMA_VERSION
        or payload.get("language") != "zh"
        or payload.get("variant_policy") != OLIST_V3_1_VARIANT_POLICY
    ):
        raise ValueError(
            "surface manifest/payload does not match the v3.1 eight-form contract"
        )
    cases = payload.get("cases")
    if (
        not isinstance(cases, list)
        or len(cases) != len(seed_ids) * OLIST_V3_1_VARIANTS_PER_SEED
    ):
        raise ValueError("surface overlay has an invalid total case count")
    by_seed: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    normalized_questions: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping) or set(case) != {
            "variant_id",
            "variant_kind",
            "seed_id",
            "question",
        }:
            raise ValueError("surface row fields drifted")
        seed_id = case["seed_id"]
        variant_id = case["variant_id"]
        variant_kind = case["variant_kind"]
        question = case["question"]
        if not all(
            isinstance(item, str) and item.strip()
            for item in (seed_id, variant_id, variant_kind, question)
        ):
            raise ValueError("surface row has an empty identity/value")
        if seed_id not in seed_ids or not variant_id.startswith(f"{seed_id}-"):
            raise ValueError("surface row has an unknown/unbound seed")
        form_id = variant_id.removeprefix(f"{seed_id}-")
        if (
            form_id not in OLIST_V3_1_VARIANT_KIND_BY_ID
            or variant_kind != OLIST_V3_1_VARIANT_KIND_BY_ID[form_id]
        ):
            raise ValueError("surface form ID/kind drifted")
        if form_id in by_seed[seed_id] or contains_latin_token(question):
            raise ValueError(
                "surface has a duplicate form or non-Chinese primary question"
            )
        normalized = " ".join(question.split())
        if normalized in normalized_questions:
            raise ValueError("surface has an exact/normalized duplicate question")
        normalized_questions.add(normalized)
        by_seed[seed_id][form_id] = dict(case)
    expected_forms = set(OLIST_V3_1_VARIANT_IDS)
    if set(by_seed) != seed_ids or any(
        set(forms) != expected_forms for forms in by_seed.values()
    ):
        raise ValueError(
            "surface overlay does not contain v1-v8 exactly once per admitted seed"
        )
    return manifest, [dict(case) for case in cases]


def _load_runtime(
    runtime_dir: Path, surface_cases: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(
        runtime_dir / "runtime_prompt_manifest.json", "runtime manifest"
    )
    path = runtime_dir / "runtime_candidates.jsonl"
    rows = _read_jsonl(path, "runtime candidates")
    expected = {str(case["variant_id"]): case for case in surface_cases}
    actual = {str(row.get("variant_id")): row for row in rows}
    if (
        manifest.get("checks", {}).get("router_rebuilt") is not True
        or manifest.get("checks", {}).get("catalog_rebuilt") is not True
        or manifest.get("checks", {}).get("query_plan_rebuilt") is not True
        or manifest.get("checks", {}).get("result_contract_rebuilt") is not True
        or manifest.get("output", {}).get("runtime_candidates_jsonl", {}).get("sha256")
        != sha256_file(path)
        or len(rows) != len(expected)
        or len(actual) != len(rows)
        or set(actual) != set(expected)
    ):
        raise ValueError(
            "runtime prompt evidence is not a complete rebuilt v3.1 overlay"
        )
    for variant_id, surface in expected.items():
        row = actual[variant_id]
        if (
            row.get("seed_id") != surface["seed_id"]
            or row.get("variant_kind") != surface["variant_kind"]
            or row.get("question") != surface["question"]
            or not isinstance(row.get("prompt"), str)
            or not row.get("prompt", "").endswith("### SQL")
        ):
            raise ValueError(f"runtime identity/prompt drifted for {variant_id}")
    return manifest, rows


def _load_sft(sft_dir: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    audit = _read_json(sft_dir / "split_audit.json", "SFT split audit")
    paths = {
        "train": sft_dir / "train.jsonl",
        "validation": sft_dir / "validation.jsonl",
        "in_domain_test": sft_dir / "final_evaluation_only" / "in_domain_test.jsonl",
    }
    splits = {split: _read_jsonl(path, f"SFT {split}") for split, path in paths.items()}
    if (
        audit.get("checks", {}).get("status") != "pass"
        or audit.get("checks", {}).get("primary_surface_split_quotas_exact") is not True
        or audit.get("checks", {}).get("primary_surface_bucket_balance_at_most_one")
        is not True
        or audit.get("policy", {}).get("surface_form_policy")
        != "eight_forms_one_query_instance"
        or audit.get("policy", {}).get("primary_variant_selection") is None
    ):
        raise ValueError("SFT audit does not record the v3.1 primary-surface contract")
    if {split: len(rows) for split, rows in splits.items()} != {
        "train": 3000,
        "validation": 750,
        "in_domain_test": 750,
    }:
        raise ValueError("SFT split sizes drifted")
    return audit, splits


def audit_release(
    *,
    structural_dir: Path,
    admission_dir: Path,
    surface_dir: Path,
    runtime_prompt_dir: Path,
    sft_dir: Path,
    output_dir: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Validate all v3.1 assets and atomically write a hash-bound report."""
    structural_dir = _external_existing_dir(structural_dir, "structural directory")
    admission_dir = _external_existing_dir(admission_dir, "admission directory")
    surface_dir = _external_existing_dir(surface_dir, "surface directory")
    runtime_prompt_dir = _external_existing_dir(
        runtime_prompt_dir, "runtime prompt directory"
    )
    sft_dir = _external_existing_dir(sft_dir, "SFT directory")
    output_dir = _external_new_dir(output_dir)

    structural_manifest, structural_rows = load_full_v3_gold_rows(structural_dir)
    if structural_manifest.get("release_version") != RELEASE_VERSION:
        raise ValueError("contract audit only accepts the v3.1 structural release")
    structural_balance = balance_coverage_report(structural_rows)
    admission_manifest, admitted = _load_admitted(admission_dir)
    structural_manifest_sha256 = sha256_file(
        structural_dir / "materialization_manifest.json"
    )
    admission_manifest_sha256 = sha256_file(
        admission_dir / "admission_assembly_manifest.json"
    )
    if (
        admission_manifest.get("source", {}).get("structural_manifest_sha256")
        != structural_manifest_sha256
    ):
        raise ValueError(
            "admission manifest is not bound to the supplied structural release"
        )
    structural_by_seed = {str(row["seed_id"]): row for row in structural_rows}
    admitted_by_seed = {str(row.get("seed_id")): row for row in admitted}
    if set(admitted_by_seed) != set(structural_by_seed):
        raise ValueError("admission and structural seed sets differ")
    for seed_id, structural in structural_by_seed.items():
        admitted_row = admitted_by_seed[seed_id]
        if (
            admitted_row.get("split") != structural["split"]
            or admitted_row.get("family_id") != structural["family_id"]
            or admitted_row.get("gold_sql_sha256")
            != structural["gold_artifact"]["sql_sha256"]
        ):
            raise ValueError(f"admission/Gold identity drifted for {seed_id}")

    surface_manifest, surface_cases = _load_surface(
        surface_dir, set(structural_by_seed)
    )
    runtime_manifest, runtime_rows = _load_runtime(runtime_prompt_dir, surface_cases)
    sft_audit, sft_splits = _load_sft(sft_dir)
    surface_manifest_sha256 = sha256_file(surface_dir / "surface_manifest.json")
    variants_sha256 = sha256_file(surface_dir / "question_variants.json")
    runtime_manifest_sha256 = sha256_file(
        runtime_prompt_dir / "runtime_prompt_manifest.json"
    )
    if (
        surface_manifest.get("source", {}).get("admission_manifest_sha256")
        != admission_manifest_sha256
        or runtime_manifest.get("input", {}).get("admission_assembly_manifest_sha256")
        != admission_manifest_sha256
        or runtime_manifest.get("input", {}).get("question_variants_sha256")
        != variants_sha256
        or sft_audit.get("source", {}).get("admission_assembly_manifest_sha256")
        != admission_manifest_sha256
        or sft_audit.get("source", {}).get("runtime_prompt_manifest_sha256")
        != runtime_manifest_sha256
    ):
        raise ValueError(
            "release layer manifests are not hash-bound to their direct inputs"
        )
    primary_surface = primary_variant_selection_report(sft_splits)
    runtime_by_variant = {str(row["variant_id"]): row for row in runtime_rows}
    for split, rows in sft_splits.items():
        for row in rows:
            seed_id = str(row.get("seed_id"))
            variant_id = row.get("primary_variant_id")
            runtime = runtime_by_variant.get(str(variant_id))
            admitted_row = admitted_by_seed.get(seed_id)
            if runtime is None or admitted_row is None:
                raise ValueError("SFT row does not bind a runtime/admitted record")
            if (
                row.get("language_variant_id") != variant_id
                or row.get("language_variant_kind") != runtime.get("variant_kind")
                or row.get("rendered_prompt") != runtime.get("prompt")
                or row.get("candidate_sql") != admitted_row.get("gold_sql")
                or row.get("primary_bucket") != admitted_row.get("primary_bucket")
                or row.get("surface_variant_count") != OLIST_V3_1_VARIANTS_PER_SEED
                or row.get("split", {}).get("name") != split
            ):
                raise ValueError(
                    f"SFT runtime/Gold/surface identity drifted for {seed_id}"
                )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        report = {
            "audit_version": "olist-v3-1-release-contract-audit-v1",
            "generated_at": generated_at or "unfixed-runtime-time",
            "release_version": RELEASE_VERSION,
            "source": {
                "structural_manifest_sha256": structural_manifest_sha256,
                "admission_manifest_sha256": admission_manifest_sha256,
                "surface_manifest_sha256": surface_manifest_sha256,
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "sft_split_audit_sha256": sha256_file(sft_dir / "split_audit.json"),
            },
            "counts": {
                "structural_rows": len(structural_rows),
                "admitted_rows": len(admitted),
                "surface_cases": len(surface_cases),
                "runtime_rows": len(runtime_rows),
                "sft_rows": {split: len(rows) for split, rows in sft_splits.items()},
            },
            "structural_balance": structural_balance,
            "primary_surface": primary_surface,
            "checks": {
                "status": "pass",
                "gold_admission_hash_bound": True,
                "eight_forms_per_query_spec": True,
                "pure_chinese_primary_surface": True,
                "normalized_surface_duplicates": 0,
                "runtime_identity_rebuilt": True,
                "primary_surface_split_quotas_exact": True,
                "primary_surface_bucket_balance_at_most_one": True,
                "sft_runtime_gold_identity_bound": True,
                "sql_executed": False,
                "model_called": False,
                "gpu_used": False,
            },
        }
        report_path = staging / "release_contract_audit.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "audit_version": report["audit_version"],
                    "output": {
                        "release_contract_audit_json": {
                            "sha256": sha256_file(report_path)
                        }
                    },
                    "checks": report["checks"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def main() -> int:
    args = parse_args()
    report = audit_release(
        structural_dir=args.structural_dir,
        admission_dir=args.admission_dir,
        surface_dir=args.surface_dir,
        runtime_prompt_dir=args.runtime_prompt_dir,
        sft_dir=args.sft_dir,
        output_dir=args.output_dir,
        generated_at=args.generated_at,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
