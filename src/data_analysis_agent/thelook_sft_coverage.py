"""Independent TheLook SQL-only SFT seed contract.

This module deliberately does not reuse the protected TheLook v3 release
contract.  It validates static train/validation metadata before Gold SQL or
natural-language materialization.  A ``program_id`` is the split unit; its
five fixed date-window samples must remain in the same split.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from datetime import date
from typing import Any, Mapping

from .semantic_catalog import Catalog, CatalogLoader
from .thelook_v2_context import THELOOK_V2_WORKSPACE
from .thelook_v2_queryspec import (
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)
from .thelook_v3_coverage import derive_thelook_v3_program_signature, derive_thelook_v3_risk_tags


ROOT = Path(__file__).resolve().parents[2]
THELOOK_SFT_CONTRACT_PATH = ROOT / "data" / "fixtures" / "thelook_sft_coverage_contract_v1.json"
THELOOK_SFT_CONTRACT_VERSION = "thelook-sql-only-sft-coverage-contract-v1"
THELOOK_SFT_SEED_VERSION = "thelook-sql-only-sft-seed-v1"
_SEED_ID_RE = re.compile(r"^thelook-sft-(train|validation)-[0-9]{4}$")
_PROGRAM_ID_RE = re.compile(r"^thelook-sft-program-[0-9]{4}$")
_SPLITS = frozenset({"train", "validation"})


class TheLookSftCoverageError(ValueError):
    """Raised when a TheLook SFT contract or seed is inconsistent."""


@dataclass(frozen=True)
class TheLookSftSeed:
    seed_id: str
    split: str
    program_id: str
    variant_index: int
    scenario_family_id: str
    query_spec: TheLookV2QuerySpec
    program_signature: str
    risk_tags: tuple[str, ...]
    window_candidate: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed_schema_version": THELOOK_SFT_SEED_VERSION,
            "seed_id": self.seed_id,
            "split": self.split,
            "program_id": self.program_id,
            "variant_index": self.variant_index,
            "scenario_family_id": self.scenario_family_id,
            "query_spec": self.query_spec.as_dict(),
            "program_signature": self.program_signature,
            "risk_tags": list(self.risk_tags),
            "window_candidate": self.window_candidate,
        }


def load_thelook_sft_contract(path: Path = THELOOK_SFT_CONTRACT_PATH) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookSftCoverageError("TheLook SFT coverage contract is unavailable or invalid") from exc
    validate_thelook_sft_contract(payload)
    return payload


def validate_thelook_sft_contract(contract: Mapping[str, Any]) -> None:
    if contract.get("contract_version") != THELOOK_SFT_CONTRACT_VERSION:
        raise TheLookSftCoverageError("unexpected TheLook SFT contract version")
    targets = _mapping(contract, "targets")
    for key in ("programs", "samples_per_program", "train_programs", "validation_programs", "train_samples", "validation_samples"):
        _positive_int(targets.get(key), f"targets.{key}")
    if targets["programs"] != targets["train_programs"] + targets["validation_programs"]:
        raise TheLookSftCoverageError("train and validation program targets do not sum to total")
    if targets["train_samples"] != targets["train_programs"] * targets["samples_per_program"]:
        raise TheLookSftCoverageError("train sample target does not reconcile to programs")
    if targets["validation_samples"] != targets["validation_programs"] * targets["samples_per_program"]:
        raise TheLookSftCoverageError("validation sample target does not reconcile to programs")
    semantic = _mapping(contract, "semantic_rules")
    if semantic.get("same_fact_domain_for_multi_metric") is not True:
        raise TheLookSftCoverageError("cross-fact multi-metric rule must remain enabled")
    if semantic.get("snapshot_policy") != "excluded_until_snapshot_queryspec_is_expanded":
        raise TheLookSftCoverageError("snapshot policy is not frozen")
    if semantic.get("high_cardinality_window_days") != 7:
        raise TheLookSftCoverageError("high-cardinality window must be frozen at seven days")
    execution = _mapping(contract, "execution_contract")
    if execution.get("analyst_execution_row_cap") != 1_000:
        raise TheLookSftCoverageError("SFT execution cap must be 1,000 rows")
    if execution.get("display_preview_row_budget") != 200:
        raise TheLookSftCoverageError("SFT preview budget must be 200 rows")
    if execution.get("statement_timeout_ms") != 5_000:
        raise TheLookSftCoverageError("SFT statement timeout must be 5,000 ms")
    families = contract.get("scenario_families")
    if not isinstance(families, list) or len(families) != 20:
        raise TheLookSftCoverageError("exactly twenty non-snapshot families are required")
    ids: set[str] = set()
    total = 0
    for family in families:
        if not isinstance(family, Mapping) or not isinstance(family.get("id"), str):
            raise TheLookSftCoverageError("invalid scenario family")
        if family["id"] in ids:
            raise TheLookSftCoverageError("scenario family ids must be unique")
        ids.add(family["id"])
        total += _positive_int(family.get("programs"), f"family {family['id']} programs")
    if total != targets["programs"]:
        raise TheLookSftCoverageError("family programs do not sum to total programs")


def validate_thelook_sft_seed(
    value: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
    catalog: Catalog | None = None,
) -> TheLookSftSeed:
    active = contract or load_thelook_sft_contract()
    validate_thelook_sft_contract(active)
    if not isinstance(value, Mapping):
        raise TheLookSftCoverageError("seed must be an object")
    allowed = {
        "seed_schema_version", "seed_id", "split", "program_id", "variant_index",
        "scenario_family_id", "query_spec", "program_signature", "risk_tags", "window_candidate",
    }
    unknown = set(value) - allowed
    if unknown:
        raise TheLookSftCoverageError(f"seed has unknown fields: {sorted(unknown)}")
    if value.get("seed_schema_version") != THELOOK_SFT_SEED_VERSION:
        raise TheLookSftCoverageError("unexpected seed schema version")
    seed_id = value.get("seed_id")
    split = value.get("split")
    program_id = value.get("program_id")
    if not isinstance(seed_id, str) or not _SEED_ID_RE.fullmatch(seed_id):
        raise TheLookSftCoverageError("invalid seed_id")
    if split not in _SPLITS or not seed_id.startswith(f"thelook-sft-{split}-"):
        raise TheLookSftCoverageError("seed_id and split do not agree")
    if not isinstance(program_id, str) or not _PROGRAM_ID_RE.fullmatch(program_id):
        raise TheLookSftCoverageError("invalid program_id")
    variant_index = value.get("variant_index")
    if not isinstance(variant_index, int) or not 0 <= variant_index < int(active["targets"]["samples_per_program"]):
        raise TheLookSftCoverageError("variant_index is outside the frozen sample count")
    family_map = {str(row["id"]): str(row["fact_domain"]) for row in active["scenario_families"]}
    family_id = value.get("scenario_family_id")
    if family_id not in family_map:
        raise TheLookSftCoverageError("unknown scenario family")
    raw_spec = value.get("query_spec")
    if not isinstance(raw_spec, Mapping):
        raise TheLookSftCoverageError("seed must contain a QuerySpec object")
    spec = validate_thelook_v2_query_spec(
        TheLookV2QuerySpec.from_mapping(raw_spec),
        catalog=catalog or CatalogLoader(THELOOK_V2_WORKSPACE).load(),
    )
    if spec.metric_ids == ("current_unsold_inventory_unit_count",):
        raise TheLookSftCoverageError("snapshot programs are excluded from SFT v1")
    if family_map[family_id] != METRIC_FACT_DOMAINS[spec.metric_ids[0]]:
        raise TheLookSftCoverageError("family fact domain does not match QuerySpec")
    if spec.dimension in {"customer_city", "product_brand", "user_state"}:
        if spec.time.mode != "absolute_range" or not spec.time.start or not spec.time.end_exclusive:
            raise TheLookSftCoverageError("high-cardinality SFT dimensions require an absolute range")
        expected_days = int(active["semantic_rules"]["high_cardinality_window_days"])
        if (date.fromisoformat(spec.time.end_exclusive) - date.fromisoformat(spec.time.start)).days != expected_days:
            raise TheLookSftCoverageError("high-cardinality SFT window has the wrong duration")
    expected_signature = derive_thelook_v3_program_signature(spec)
    if value.get("program_signature") != expected_signature:
        raise TheLookSftCoverageError("program_signature does not match QuerySpec")
    tags = value.get("risk_tags")
    if not isinstance(tags, list) or tuple(tags) != derive_thelook_v3_risk_tags(spec):
        raise TheLookSftCoverageError("risk_tags do not match QuerySpec")
    window = value.get("window_candidate")
    if not isinstance(window, str) or not window:
        raise TheLookSftCoverageError("window_candidate is required")
    return TheLookSftSeed(
        seed_id=seed_id,
        split=str(split),
        program_id=program_id,
        variant_index=variant_index,
        scenario_family_id=str(family_id),
        query_spec=spec,
        program_signature=expected_signature,
        risk_tags=tuple(tags),
        window_candidate=window,
    )


def audit_thelook_sft_seeds(
    records: list[Mapping[str, Any]],
    contract: Mapping[str, Any] | None = None,
    protected_cases: Path | None = None,
) -> dict[str, Any]:
    active = contract or load_thelook_sft_contract()
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    seeds = [validate_thelook_sft_seed(row, active, catalog) for row in records]
    targets = active["targets"]
    if len(seeds) != targets["train_samples"] + targets["validation_samples"]:
        raise TheLookSftCoverageError("seed count does not match train + validation targets")
    ids = [seed.seed_id for seed in seeds]
    specs = [seed.query_spec.query_spec_id for seed in seeds]
    if len(set(ids)) != len(ids) or len(set(specs)) != len(specs):
        raise TheLookSftCoverageError("seed_id and QuerySpec IDs must be unique")
    program_splits: dict[str, str] = {}
    for seed in seeds:
        previous = program_splits.setdefault(seed.program_id, seed.split)
        if previous != seed.split:
            raise TheLookSftCoverageError("a program appears in both train and validation")
    if len(program_splits) != targets["programs"]:
        raise TheLookSftCoverageError("program count does not match contract")
    family_targets = {str(row["id"]): int(row["programs"]) for row in active["scenario_families"]}
    from collections import Counter
    family_programs = Counter((seed.scenario_family_id, seed.program_id) for seed in seeds)
    for family_id, target in family_targets.items():
        count = sum(1 for family, _ in family_programs if family == family_id)
        if count != target:
            raise TheLookSftCoverageError(f"family {family_id} has {count} programs, expected {target}")
    split_counts = Counter(seed.split for seed in seeds)
    split_program_counts = Counter(program_splits.values())
    expected_split_samples = {"train": int(targets["train_samples"]), "validation": int(targets["validation_samples"])}
    if dict(split_counts) != expected_split_samples:
        raise TheLookSftCoverageError("split sample quotas are not exact")
    if dict(split_program_counts) != {"train": int(targets["train_programs"]), "validation": int(targets["validation_programs"])}:
        raise TheLookSftCoverageError("split program quotas are not exact")
    if any(sum(1 for seed in seeds if seed.program_id == program) != int(targets["samples_per_program"]) for program in program_splits):
        raise TheLookSftCoverageError("every program must have exactly five variants")
    protected_overlap = {"query_spec_ids": 0, "window_candidates": 0}
    if protected_cases is not None:
        protected_specs: set[str] = set()
        protected_windows: set[str] = set()
        for line in protected_cases.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            spec = row.get("query_spec")
            if isinstance(spec, Mapping) and isinstance(spec.get("query_spec_id"), str):
                protected_specs.add(spec["query_spec_id"])
            if isinstance(row.get("window_candidate"), str):
                protected_windows.add(row["window_candidate"])
        protected_overlap["query_spec_ids"] = len(set(specs) & protected_specs)
        protected_overlap["window_candidates"] = len({seed.window_candidate for seed in seeds} & protected_windows)
        if any(protected_overlap.values()):
            raise TheLookSftCoverageError(f"protected test overlap detected: {protected_overlap}")
    return {
        "seed_count": len(seeds),
        "program_count": len(program_splits),
        "split_samples": dict(sorted(split_counts.items())),
        "split_programs": dict(sorted(split_program_counts.items())),
        "family_programs": dict(sorted((family, count) for family, count in Counter(family for family, _ in family_programs).items())),
        "fact_domains": dict(sorted(Counter(METRIC_FACT_DOMAINS[seed.query_spec.metric_ids[0]] for seed in seeds).items())),
        "result_shapes": dict(sorted(Counter(seed.query_spec.result_shape for seed in seeds).items())),
        "metric_multiplicity": dict(sorted(Counter(len(seed.query_spec.metric_ids) for seed in seeds).items())),
        "protected_overlap": protected_overlap,
        "sql_rendered": False,
        "database_accessed": False,
        "model_called": False,
    }


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise TheLookSftCoverageError(f"contract field {key!r} must be an object")
    return nested


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise TheLookSftCoverageError(f"{label} must be a positive integer")
    return value
