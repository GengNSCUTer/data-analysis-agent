#!/usr/bin/env python3
"""Build the structural/query-instance plan for the Olist domain SFT v2 release.

The release deliberately distinguishes semantic families, query instances and
surface forms.  Five Chinese forms are stored as one instance's overlay; they
are not counted as five independent samples.  This command only constructs
external structural inputs.  Gold admission, runtime prompt rebuilding and
SFT tokenization remain separate gates.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime, validate_query_spec  # noqa: E402
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from scripts.post_training.data.build_olist_surface_form_pilot import render_variants  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import family_id  # noqa: E402


TARGETS = {"train": 2400, "validation": 600, "in_domain_test": 600}
OLD_SPLITS = {"train", "validation", "in_domain_test"}
DATE_WINDOWS = (
    ("2016-10-01", "2017-01-01"),
    ("2017-01-01", "2017-04-01"),
    ("2017-04-01", "2017-07-01"),
    ("2017-07-01", "2017-10-01"),
    ("2017-10-01", "2018-01-01"),
    ("2018-01-01", "2018-04-01"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {line_no} must be an object")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def _external_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("release output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _protected(path: Path) -> frozenset[str]:
    payload = json.loads(_external_file(path, "protected summary").read_text(encoding="utf-8"))
    values = payload.get("family_fingerprints") if isinstance(payload, dict) else None
    if payload.get("summary_version") != "olist-protected-family-summary-v1" or not isinstance(values, list):
        raise ValueError("protected summary does not satisfy the hash-only contract")
    if any(not isinstance(value, str) or len(value) != 64 for value in values):
        raise ValueError("protected family fingerprints must be SHA-256 strings")
    return frozenset(values)


def _protected_fingerprint(family: str) -> str:
    return hashlib.sha256(f"olist-protected-family-summary-v1:{family}".encode()).hexdigest()


def _stable(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _time_variants(spec: QuerySpec, catalog: Any) -> list[QuerySpec]:
    """Return deterministic legal QuerySpecs in the same semantic family."""
    if spec.time.mode == "all_time":
        return [spec]
    result: list[QuerySpec] = []
    for start, end in DATE_WINDOWS:
        candidate = QuerySpec.create(
            metric_ids=spec.metric_ids,
            result_shape=spec.result_shape,
            dimension=spec.dimension,
            time=QueryTime(spec.time.mode, start, end, spec.time.grain),
            join_program_id=spec.join_program_id,
            workspace=spec.workspace,
            attribution_rule_id=spec.attribution_rule_id,
        )
        result.append(validate_query_spec(candidate, catalog))
    return result


def _instance(split: str, index: int, spec: QuerySpec) -> dict[str, Any]:
    seed_id = f"olist-domain-sft-v2-{split}-{index:04d}"
    family = family_id(spec)
    variants = render_variants(spec, family, seed_id)
    return {
        "seed_id": seed_id,
        "split": split,
        "family_id": family,
        "sql_program_id": spec.join_program_id,
        "metric_ids": list(spec.metric_ids),
        "result_shape": spec.result_shape,
        "dimension": spec.dimension,
        "time": spec.time.as_dict(),
        "join_program_id": spec.join_program_id,
        "query_spec": spec.as_dict(),
        "surface_form_policy": "five_forms_one_query_instance",
        "question_variants": variants,
        "primary_variant_id": variants[0]["variant_id"],
    }


def _load_sources(source: Path, old: Path, protected: Path) -> tuple[list[tuple[QuerySpec, str | None]], dict[str, str], frozenset[str], Any]:
    catalog = CatalogLoader().load()
    old_rows = _read_jsonl(_external_file(old, "Medium v1 QuerySpecs"), "Medium v1 QuerySpecs")
    old_by_family: dict[str, str] = {}
    old_specs: list[tuple[QuerySpec, str | None]] = []
    for row in old_rows:
        spec = validate_query_spec(QuerySpec.from_mapping(row["query_spec"]), catalog)
        family = family_id(spec)
        if row.get("family_id") != family:
            raise ValueError(f"Medium v1 family identity drift: {row.get('seed_id')}")
        split = row.get("split")
        if split not in OLD_SPLITS:
            raise ValueError(f"unsupported Medium v1 split: {split}")
        if family in old_by_family and old_by_family[family] != split:
            raise ValueError(f"Medium v1 family crosses splits: {family}")
        old_by_family[family] = split
        old_specs.append((spec, split))
    if Counter(split for _, split in old_specs) != Counter({"train": 720, "validation": 240, "in_domain_test": 240}):
        raise ValueError("Medium v1 split counts do not match the frozen 720/240/240 release")
    protected_fingerprints = _protected(protected)
    source_rows = _read_jsonl(_external_file(source, "candidate QuerySpecs"), "candidate QuerySpecs")
    candidates: dict[str, QuerySpec] = {}
    for row in source_rows:
        spec = validate_query_spec(QuerySpec.from_mapping(row["query_spec"]), catalog)
        family = family_id(spec)
        if family != row.get("family_id"):
            raise ValueError("candidate family identity drift")
        if family not in old_by_family and _protected_fingerprint(family) not in protected_fingerprints:
            candidates.setdefault(family, spec)
    ordered_new = sorted(candidates.values(), key=lambda spec: _stable({"family": family_id(spec), "query_spec": spec.as_dict()}))
    return old_specs + [(spec, None) for spec in ordered_new], old_by_family, protected_fingerprints, catalog


def build(source: Path, old: Path, protected: Path, output_dir: Path) -> dict[str, Any]:
    output = _new_dir(output_dir)
    all_specs, old_by_family, protected_fingerprints, catalog = _load_sources(source, old, protected)
    old_train = [spec for spec, split in all_specs if split == "train"]
    old_validation = [spec for spec, split in all_specs if split == "validation"]
    old_test_families = {family_id(spec) for spec, split in all_specs if split == "in_domain_test"}
    new_specs = [spec for spec, split in all_specs if split is None]
    if any(family_id(spec) in old_test_families for spec in new_specs):
        raise ValueError("new candidates overlap the frozen Medium v1 test family set")
    # Put repeatable (date-bounded/series) families into train first. This lets
    # the train instance target grow without pretending that language forms are
    # new semantic families.
    repeatable = [spec for spec in new_specs if spec.time.mode != "all_time"]
    nonrepeatable = [spec for spec in new_specs if spec.time.mode == "all_time"]
    new_train = sorted(repeatable + nonrepeatable, key=lambda spec: _stable(spec.as_dict()))[:194]
    remaining = [spec for spec in new_specs if family_id(spec) not in {family_id(item) for item in new_train}]
    new_validation = sorted(remaining, key=lambda spec: _stable(spec.as_dict()))[:360]
    remaining = [spec for spec in remaining if family_id(spec) not in {family_id(item) for item in new_validation}]
    new_test = sorted(remaining, key=lambda spec: _stable(spec.as_dict()))[:600]
    if len(new_train) != 194 or len(new_validation) != 360 or len(new_test) != 600:
        raise ValueError("candidate pool is too small for the v2 split family allocation")

    train_bases = old_train + new_train
    train_options = [_time_variants(spec, catalog) for spec in train_bases]
    instances: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "in_domain_test": []}
    cursors = [0] * len(train_options)
    while len(instances["train"]) < TARGETS["train"]:
        progressed = False
        for idx, options in enumerate(train_options):
            if cursors[idx] >= len(options):
                continue
            cursors[idx] += 1
            instances["train"].append(_instance("train", len(instances["train"]) + 1, options[cursors[idx] - 1]))
            progressed = True
            if len(instances["train"]) == TARGETS["train"]:
                break
        if not progressed:
            raise ValueError("train family/date-window capacity is below 2,400 query instances")
    for split, specs in (("validation", old_validation + new_validation), ("in_domain_test", new_test)):
        for spec in specs:
            instances[split].append(_instance(split, len(instances[split]) + 1, spec))
        if len(instances[split]) != TARGETS[split]:
            raise AssertionError(f"{split} count mismatch")

    # Family isolation is the semantic holdout invariant. Date variants may
    # repeat only within train, never across splits.
    families_by_split = {split: {row["family_id"] for row in rows} for split, rows in instances.items()}
    if families_by_split["train"] & families_by_split["validation"] or families_by_split["train"] & families_by_split["in_domain_test"] or families_by_split["validation"] & families_by_split["in_domain_test"]:
        raise AssertionError("family IDs cross formal splits")
    output.mkdir(parents=True)
    structural = [row for split in instances.values() for row in split]
    (output / "structural_seeds.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in structural), encoding="utf-8")
    materializer = [{key: row[key] for key in ("seed_id", "split", "metric_ids", "result_shape", "dimension", "time", "join_program_id")} for row in structural]
    (output / "materializer_seeds.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in materializer), encoding="utf-8")
    overlays = {"schema_version": "3", "language": "zh", "prompt_version": "olist-candidate-sql-v1", "variant_policy": "five_forms_one_query_instance; variants are not independent semantic samples", "cases": [variant for row in structural for variant in row["question_variants"]]}
    (output / "question_variants.json").write_text(json.dumps(overlays, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "olist-domain-sft-release-v2",
        "counts": {"query_instances": {split: len(rows) for split, rows in instances.items()}, "families": {split: len(families) for split, families in families_by_split.items()}, "surface_variants": sum(len(row["question_variants"]) for row in structural)},
        "targets": TARGETS,
        "family_isolation": {"train_validation_overlap": sorted(families_by_split["train"] & families_by_split["validation"]), "train_test_overlap": sorted(families_by_split["train"] & families_by_split["in_domain_test"]), "validation_test_overlap": sorted(families_by_split["validation"] & families_by_split["in_domain_test"])},
        "coverage": {"metrics": sorted({metric for row in structural for metric in row["metric_ids"]}), "time_grains": sorted({row["time"]["grain"] for row in structural if row["time"]["mode"] == "series"}), "result_shapes": dict(Counter(row["result_shape"] for row in structural))},
        "source": {"candidate_queryspec_sha256": _sha256(source), "medium_queryspec_sha256": _sha256(old), "protected_summary_sha256": _sha256(protected), "old_test_families_excluded": len(old_test_families), "protected_family_hashes": len(protected_fingerprints), "unused_new_families": len(new_specs) - len(new_train) - len(new_validation) - len(new_test)},
        "checks": {"five_variants_not_counted_as_instances": True, "old_train_validation_reused": True, "old_in_domain_test_untouched": True, "gold_admitted": False, "runtime_prompts_rebuilt": False, "sft_materialized": False, "gpu_used": False},
    }
    (output / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-queryspecs", type=Path, required=True)
    parser.add_argument("--medium-queryspecs", type=Path, required=True)
    parser.add_argument("--protected-summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_queryspecs, args.medium_queryspecs, args.protected_summary, args.output_dir), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
