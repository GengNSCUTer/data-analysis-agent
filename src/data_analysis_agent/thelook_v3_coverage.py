"""Coverage-only contracts for the protected TheLook v3 evaluation release.

TheLook v3 deliberately reuses the frozen v2 QuerySpec and Gold renderer.  A
coverage label is not executable SQL semantics, so it must not change a v2
QuerySpec's canonical payload or its identifier.  This module validates the
separate static seed metadata used to balance the future protected release.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .semantic_catalog import Catalog, CatalogLoader
from .thelook_v2_context import THELOOK_V2_WORKSPACE
from .thelook_v2_queryspec import (
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)


THELOOK_V3_COVERAGE_CONTRACT_VERSION = "thelook-v3-coverage-contract-v1"
THELOOK_V3_COVERAGE_SEED_VERSION = "thelook-v3-coverage-seed-v1"
THELOOK_V3_COVERAGE_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "fixtures"
    / "thelook_v3_coverage_contract_v1.json"
)

_SEED_ID_RE = re.compile(r"^thelook-v3-coverage-[0-9]{3,4}$")
_FAMILY_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")
_RISK_TAGS = frozenset(
    {
        "distinct",
        "two_stage_aggregate",
        "status_filter",
        "duration_eligibility",
        "snapshot",
        "multi_metric_cte",
        "high_cardinality_window",
        "time_filter",
        "time_bucket",
    }
)


class TheLookV3CoverageError(ValueError):
    """Raised when a v3 coverage contract or seed is inconsistent."""


@lru_cache(maxsize=1)
def _frozen_thelook_v2_catalog() -> Catalog:
    """Load the pinned Catalog once per process for deterministic bulk audits.

    A 750-seed static audit validates the same frozen v2 workspace repeatedly.
    Re-parsing its YAML for every QuerySpec is neither an extra integrity check
    nor part of the coverage contract; it only makes the pre-admission command
    needlessly slow.  The QuerySpec validator still compares this Catalog's
    versions with each seed's workspace pin on every call.
    """

    return CatalogLoader(THELOOK_V2_WORKSPACE).load()


@dataclass(frozen=True)
class TheLookV3CoverageSeed:
    """One deterministic, non-executable coverage seed.

    ``query_spec`` remains the only source of query semantics.  The remaining
    fields support quota accounting and later release audit only.
    """

    seed_id: str
    scenario_family_id: str
    query_spec: TheLookV2QuerySpec
    program_signature: str
    risk_tags: tuple[str, ...]
    window_candidate: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed_schema_version": THELOOK_V3_COVERAGE_SEED_VERSION,
            "seed_id": self.seed_id,
            "scenario_family_id": self.scenario_family_id,
            "query_spec": self.query_spec.as_dict(),
            "program_signature": self.program_signature,
            "risk_tags": list(self.risk_tags),
            "window_candidate": self.window_candidate,
        }


def load_thelook_v3_coverage_contract(
    path: Path = THELOOK_V3_COVERAGE_CONTRACT_PATH,
) -> Mapping[str, Any]:
    """Load and validate the repository-tracked, machine-readable quota contract."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV3CoverageError("coverage contract is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise TheLookV3CoverageError("coverage contract must be an object")
    validate_thelook_v3_coverage_contract(payload)
    return payload


def validate_thelook_v3_coverage_contract(contract: Mapping[str, Any]) -> None:
    """Check arithmetic and semantic invariants before any seed is accepted."""

    if contract.get("contract_version") != THELOOK_V3_COVERAGE_CONTRACT_VERSION:
        raise TheLookV3CoverageError("unexpected coverage contract version")
    release = _mapping(contract, "release")
    target_cases = _positive_int(release.get("target_cases"), "release.target_cases")
    if release.get("split") != "cross_schema_final_test":
        raise TheLookV3CoverageError("coverage contract must define a final-test split")

    semantic = _mapping(contract, "semantic_baseline")
    metric_ids = semantic.get("allowed_metric_ids")
    if (
        not isinstance(metric_ids, list)
        or len(metric_ids) != 20
        or len(set(metric_ids)) != 20
        or set(metric_ids) != set(METRIC_FACT_DOMAINS)
    ):
        raise TheLookV3CoverageError("coverage contract must pin exactly the frozen twenty metrics")
    if semantic.get("metric_change_policy") != "keep_frozen_twenty_metrics":
        raise TheLookV3CoverageError("coverage contract must not silently add metrics")

    quota = _mapping(contract, "case_quotas")
    fact_domains = _mapping(quota, "fact_domains")
    if set(fact_domains) != set(METRIC_FACT_DOMAINS.values()):
        raise TheLookV3CoverageError("fact-domain quota keys do not match the frozen registry")
    _require_sum(fact_domains, target_cases, "fact-domain quotas", allow_zero=True)

    result_shapes = _mapping(quota, "result_shapes")
    if set(result_shapes) != {"scalar", "dimension_grouped", "time_series"}:
        raise TheLookV3CoverageError("result-shape quotas are incomplete")
    _require_sum(result_shapes, target_cases, "result-shape quotas")

    time_grains = _mapping(quota, "time_series_grains")
    if set(time_grains) != {"day", "week", "month", "quarter", "year"}:
        raise TheLookV3CoverageError("time-series grain quotas are incomplete")
    _require_sum(
        time_grains,
        _positive_int(result_shapes["time_series"], "time_series quota"),
        "time-series grain quotas",
    )

    dimensions = _mapping(quota, "dimension_grouped")
    _require_sum(
        dimensions,
        _positive_int(result_shapes["dimension_grouped"], "dimension_grouped quota"),
        "dimension-grouped quotas",
    )

    multiplicity = _mapping(quota, "metric_multiplicity")
    multiplicity_counts = {
        key: multiplicity[key]
        for key in ("one_metric", "two_metrics", "three_metrics", "four_metrics")
    }
    _require_sum(multiplicity_counts, target_cases, "metric-multiplicity quotas")
    exposure_by_domain = _mapping(multiplicity, "case_exposure_by_fact_domain")
    if set(exposure_by_domain) != set(fact_domains):
        raise TheLookV3CoverageError(
            "metric exposure bounds must be specified for every frozen fact domain"
        )
    for domain, bounds in exposure_by_domain.items():
        if not isinstance(bounds, Mapping):
            raise TheLookV3CoverageError(
                f"metric exposure bounds for fact domain {domain} must be an object"
            )
        minimum = _nonnegative_int(bounds.get("minimum"), f"{domain}.minimum")
        maximum = _nonnegative_int(bounds.get("maximum"), f"{domain}.maximum")
        if minimum > maximum:
            raise TheLookV3CoverageError(
                f"metric exposure bounds are inverted for fact domain {domain}"
            )

    families = contract.get("scenario_families")
    if not isinstance(families, list) or len(families) != 20:
        raise TheLookV3CoverageError("coverage contract must define exactly twenty scenario families")
    family_ids: set[str] = set()
    family_by_domain: dict[str, int] = dict.fromkeys(fact_domains, 0)
    for family in families:
        if not isinstance(family, Mapping):
            raise TheLookV3CoverageError("scenario family must be an object")
        family_id = family.get("id")
        domain = family.get("fact_domain")
        if not isinstance(family_id, str) or not _FAMILY_ID_RE.fullmatch(family_id):
            raise TheLookV3CoverageError("scenario family has an invalid id")
        if family_id in family_ids:
            raise TheLookV3CoverageError("scenario family ids must be unique")
        if domain not in family_by_domain:
            raise TheLookV3CoverageError("scenario family has an unknown fact domain")
        family_ids.add(family_id)
        family_by_domain[str(domain)] += _positive_int(
            family.get("target_cases"), f"scenario family {family_id} target"
        )
    if family_by_domain != {key: int(value) for key, value in fact_domains.items()}:
        raise TheLookV3CoverageError("scenario-family quotas do not reconcile to fact domains")

    surface = _mapping(contract, "surface_contract")
    variant_ids = surface.get("variant_ids")
    variant_kinds = surface.get("variant_kinds")
    primary_quota = _mapping(surface, "primary_variant_global_quota")
    if not isinstance(variant_ids, list) or len(variant_ids) != 8 or len(set(variant_ids)) != 8:
        raise TheLookV3CoverageError("exactly eight surface variants are required")
    if not isinstance(variant_kinds, list) or len(variant_kinds) != len(variant_ids):
        raise TheLookV3CoverageError("surface variant kinds do not match variant ids")
    if set(primary_quota) != set(variant_ids):
        raise TheLookV3CoverageError("primary surface quota ids do not match variants")
    if surface.get("variant_count_per_query_spec") != len(variant_ids):
        raise TheLookV3CoverageError("surface variant count does not match the variant list")
    _require_sum(primary_quota, target_cases, "primary surface quotas")

    risk_minima = _mapping(contract, "risk_tag_minimum_case_counts")
    required_risk_minima = {
        "distinct",
        "two_stage_aggregate",
        "status_filter",
        "duration_eligibility",
        "snapshot",
        "multi_metric_cte",
        "high_cardinality_window",
        "time_bucket",
    }
    if set(risk_minima) != required_risk_minima:
        raise TheLookV3CoverageError("risk-tag minimum quotas are incomplete")
    for tag, minimum in risk_minima.items():
        _nonnegative_int(minimum, f"risk-tag minimum {tag}")


def derive_thelook_v3_program_signature(spec: TheLookV2QuerySpec) -> str:
    """Derive a readable, stable program label without date boundaries.

    A seed may legitimately use several admitted date windows for one program.
    Those date windows must not masquerade as different scenario families, so
    ``start`` and ``end_exclusive`` are deliberately absent from this value.
    """

    validated = validate_thelook_v2_query_spec(spec, catalog=_frozen_thelook_v2_catalog())
    fact_domain = METRIC_FACT_DOMAINS[validated.metric_ids[0]]
    metrics = "+".join(validated.metric_ids)
    time = validated.time
    time_part = time.mode if time.grain is None else f"{time.mode}:{time.grain}"
    dimension = validated.dimension or "none"
    return "|".join(
        (
            fact_domain,
            validated.join_program_id,
            validated.result_shape,
            dimension,
            time_part,
            metrics,
        )
    )


def derive_thelook_v3_risk_tags(
    spec: TheLookV2QuerySpec,
    contract: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Derive coverage risks from executable QuerySpec semantics only.

    Risk metadata is useful for quota auditing only when it cannot be claimed
    by an unrelated query.  This function is therefore shared by the seed
    builder and validator: callers do not hand-author a convenient tag list.
    """

    active_contract = contract or load_thelook_v3_coverage_contract()
    validated = validate_thelook_v2_query_spec(spec, catalog=_frozen_thelook_v2_catalog())
    metrics = set(validated.metric_ids)
    tags: set[str] = set()
    if validated.result_shape == "time_series":
        tags.add("time_bucket")
    elif validated.time.mode == "absolute_range":
        tags.add("time_filter")
    if len(validated.metric_ids) > 1:
        tags.add("multi_metric_cte")
    if metrics & {"completed_customer_count", "unique_session_count"}:
        tags.add("distinct")
    if metrics & {"average_order_value", "average_items_per_completed_order"}:
        tags.add("two_stage_aggregate")
    if metrics & {
        "completed_sale_amount",
        "completed_item_count",
        "completed_order_count",
        "cancelled_order_count",
        "returned_order_count",
        "return_rate",
    }:
        tags.add("status_filter")
    if metrics & {
        "average_fulfillment_days",
        "average_dispatch_days",
        "average_transit_days",
        "average_post_delivery_return_days",
        "average_days_to_sale",
    }:
        tags.add("duration_eligibility")
    if metrics == {"current_unsold_inventory_unit_count"}:
        tags.add("snapshot")
    if validated.dimension in set(active_contract["seed_policy"]["high_cardinality_dimensions"]):
        tags.add("high_cardinality_window")
    return tuple(sorted(tags))


def validate_thelook_v3_coverage_seed(
    value: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
) -> TheLookV3CoverageSeed:
    """Validate a static coverage seed without rendering or executing SQL."""

    active_contract = contract or load_thelook_v3_coverage_contract()
    validate_thelook_v3_coverage_contract(active_contract)
    if not isinstance(value, Mapping):
        raise TheLookV3CoverageError("coverage seed must be an object")
    allowed = {
        "seed_schema_version",
        "seed_id",
        "scenario_family_id",
        "query_spec",
        "program_signature",
        "risk_tags",
        "window_candidate",
    }
    unknown = set(value) - allowed
    if unknown:
        raise TheLookV3CoverageError(f"coverage seed has unknown fields: {sorted(unknown)}")
    if value.get("seed_schema_version") != THELOOK_V3_COVERAGE_SEED_VERSION:
        raise TheLookV3CoverageError("coverage seed has an unexpected version")

    seed_id = value.get("seed_id")
    if not isinstance(seed_id, str) or not _SEED_ID_RE.fullmatch(seed_id):
        raise TheLookV3CoverageError("coverage seed has an invalid seed_id")
    family_id = value.get("scenario_family_id")
    family_map = {
        str(item["id"]): str(item["fact_domain"])
        for item in active_contract["scenario_families"]
    }
    if family_id not in family_map:
        raise TheLookV3CoverageError("coverage seed references an unknown scenario family")

    raw_spec = value.get("query_spec")
    if not isinstance(raw_spec, Mapping):
        raise TheLookV3CoverageError("coverage seed must contain a QuerySpec object")
    spec = validate_thelook_v2_query_spec(
        TheLookV2QuerySpec.from_mapping(raw_spec),
        catalog=_frozen_thelook_v2_catalog(),
    )
    if any(
        metric not in active_contract["semantic_baseline"]["allowed_metric_ids"]
        for metric in spec.metric_ids
    ):
        raise TheLookV3CoverageError("coverage seed uses an unfrozen metric")
    fact_domain = METRIC_FACT_DOMAINS[spec.metric_ids[0]]
    if family_map[family_id] != fact_domain:
        raise TheLookV3CoverageError("coverage seed fact domain does not match its scenario family")
    if family_id == spec.query_spec_id:
        raise TheLookV3CoverageError("scenario_family_id must not reuse QuerySpec identity")

    signature = value.get("program_signature")
    expected_signature = derive_thelook_v3_program_signature(spec)
    if signature != expected_signature:
        raise TheLookV3CoverageError("coverage seed program_signature does not match its QuerySpec")

    raw_risk_tags = value.get("risk_tags")
    if not isinstance(raw_risk_tags, list) or not raw_risk_tags:
        raise TheLookV3CoverageError("coverage seed requires non-empty risk_tags")
    risk_tags = tuple(raw_risk_tags)
    if (
        any(not isinstance(tag, str) or tag not in _RISK_TAGS for tag in risk_tags)
        or len(set(risk_tags)) != len(risk_tags)
        or tuple(sorted(risk_tags)) != risk_tags
    ):
        raise TheLookV3CoverageError("coverage seed risk_tags must be unique, sorted, known tags")
    expected_risk_tags = derive_thelook_v3_risk_tags(spec, active_contract)
    if risk_tags != expected_risk_tags:
        raise TheLookV3CoverageError(
            "coverage seed risk_tags do not match the deterministic QuerySpec risks"
        )

    window_candidate = value.get("window_candidate")
    if not isinstance(window_candidate, str) or not window_candidate:
        raise TheLookV3CoverageError("coverage seed must identify its window candidate")
    return TheLookV3CoverageSeed(
        seed_id=seed_id,
        scenario_family_id=str(family_id),
        query_spec=spec,
        program_signature=expected_signature,
        risk_tags=risk_tags,
        window_candidate=window_candidate,
    )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise TheLookV3CoverageError(f"coverage contract field {key!r} must be an object")
    return nested


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise TheLookV3CoverageError(f"{label} must be a positive integer")
    return value


def _require_sum(
    values: Mapping[str, Any], expected: int, label: str, *, allow_zero: bool = False
) -> None:
    validator = _nonnegative_int if allow_zero else _positive_int
    total = sum(validator(value, f"{label}.{key}") for key, value in values.items())
    if total != expected:
        raise TheLookV3CoverageError(f"{label} must sum to {expected}, got {total}")


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise TheLookV3CoverageError(f"{label} must be a non-negative integer")
    return value
