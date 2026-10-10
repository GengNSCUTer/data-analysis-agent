from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path

import pytest

from data_analysis_agent.thelook_v3_coverage import (
    TheLookV3CoverageError,
    derive_thelook_v3_program_signature,
    load_thelook_v3_coverage_contract,
    validate_thelook_v3_coverage_contract,
    validate_thelook_v3_coverage_seed,
)
from scripts.post_training.evaluation.build_thelook_v3_coverage_seeds import (
    audit_seed_records,
    build_seeds,
)


_ROOT = Path(__file__).resolve().parents[1]
_SEED_FIXTURE = _ROOT / "data" / "fixtures" / "thelook_v3_coverage_seeds_v2.jsonl"


def _load_fixture() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in _SEED_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_v3_contract_pins_the_existing_twenty_metrics_and_exact_750_case_quotas() -> None:
    contract = load_thelook_v3_coverage_contract()

    assert contract["release"]["target_cases"] == 750
    assert contract["semantic_baseline"]["metric_change_policy"] == "keep_frozen_twenty_metrics"
    assert len(contract["semantic_baseline"]["allowed_metric_ids"]) == 20
    assert len(contract["scenario_families"]) == 21
    assert sum(item["target_cases"] for item in contract["scenario_families"]) == 750
    assert contract["case_quotas"]["fact_domains"]["inventory_snapshot"] == 4

    drifted = copy.deepcopy(contract)
    drifted["semantic_baseline"]["allowed_metric_ids"] = drifted["semantic_baseline"][
        "allowed_metric_ids"
    ][:-1]
    with pytest.raises(TheLookV3CoverageError, match="exactly the frozen twenty metrics"):
        validate_thelook_v3_coverage_contract(drifted)


def test_v3_static_seed_fixture_is_deterministic_and_meets_all_static_quotas() -> None:
    generated = build_seeds()
    fixture = _load_fixture()

    assert fixture == generated
    audit = audit_seed_records(fixture)
    assert audit["seed_count"] == 750
    assert audit["sql_rendered"] is False
    assert audit["database_accessed"] is False
    assert audit["model_called"] is False
    assert audit["risk_tags"]["two_stage_aggregate"] >= 90
    assert audit["risk_tags"]["high_cardinality_window"] >= 36
    assert audit["metric_exposure"].get("current_unsold_inventory_unit_count", 0) == 4
    assert len(audit["scenario_families"]) == 21
    assert audit["v2_query_spec_overlap"] == 0
    assert all(not family_id.startswith("tqs2_") for family_id in audit["scenario_families"])

    assert all(
        set(seed)
        == {
            "seed_schema_version",
            "seed_id",
            "scenario_family_id",
            "query_spec",
            "program_signature",
            "risk_tags",
            "window_candidate",
        }
        for seed in fixture
    )
    assert all(
        not ({"question", "question_variants", "prompt", "sql", "gold_sql", "result"} & set(seed))
        for seed in fixture
    )


def test_v3_high_cardinality_dimensions_use_the_frozen_short_window() -> None:
    contract = load_thelook_v3_coverage_contract()
    dimensions = set(contract["seed_policy"]["high_cardinality_dimensions"])
    expected_days = contract["seed_policy"]["high_cardinality_window_days"]
    seeds = build_seeds()
    high_cardinality = [
        seed["query_spec"]
        for seed in seeds
        if seed["query_spec"].get("dimension") in dimensions
    ]

    assert len(high_cardinality) >= contract["risk_tag_minimum_case_counts"]["high_cardinality_window"]
    assert all(
        (
            date.fromisoformat(spec["time"]["end_exclusive"])
            - date.fromisoformat(spec["time"]["start"])
        ).days
        == expected_days
        for spec in high_cardinality
    )


def test_v3_seed_rejects_tampered_program_signature_and_high_cardinality_risk_tag() -> None:
    seeds = build_seeds()
    signed = copy.deepcopy(seeds[0])
    signed["program_signature"] = "tampered"
    with pytest.raises(TheLookV3CoverageError, match="program_signature"):
        validate_thelook_v3_coverage_seed(signed)

    high_cardinality = next(
        copy.deepcopy(seed)
        for seed in seeds
        if seed["query_spec"]["dimension"] == "customer_city"
    )
    high_cardinality["risk_tags"].remove("high_cardinality_window")
    with pytest.raises(TheLookV3CoverageError, match="risk_tags"):
        validate_thelook_v3_coverage_seed(high_cardinality)

    false_two_stage = copy.deepcopy(seeds[0])
    false_two_stage["risk_tags"] = sorted(false_two_stage["risk_tags"] + ["two_stage_aggregate"])
    with pytest.raises(TheLookV3CoverageError, match="risk_tags"):
        validate_thelook_v3_coverage_seed(false_two_stage)


def test_v3_overlap_audit_rejects_unapproved_protected_queryspec_identity(tmp_path: Path) -> None:
    records = build_seeds()
    protected_path = tmp_path / "protected-v2-cases.jsonl"
    protected_path.write_text(
        json.dumps({"query_spec": {"query_spec_id": records[0]["query_spec"]["query_spec_id"]}})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TheLookV3CoverageError, match="expected 4"):
        audit_seed_records(records, v2_cases=protected_path)


def test_v3_overlap_audit_reports_the_explicit_snapshot_reuse(tmp_path: Path) -> None:
    records = build_seeds()
    snapshot_ids = [
        record["query_spec"]["query_spec_id"]
        for record in records
        if record["scenario_family_id"] == "inventory_unsold_snapshot"
    ]
    protected_path = tmp_path / "protected-v2-cases.jsonl"
    protected_path.write_text(
        "".join(
            json.dumps({"query_spec": {"query_spec_id": query_spec_id}}) + "\n"
            for query_spec_id in snapshot_ids
        ),
        encoding="utf-8",
    )

    audit = audit_seed_records(records, v2_cases=protected_path)
    assert audit["v2_query_spec_overlap"] == 4
    assert audit["v2_reused_family_counts"] == {"inventory_unsold_snapshot": 4}


def test_v3_program_signature_excludes_date_boundaries_but_keeps_program_semantics() -> None:
    records = build_seeds()
    first = records[0]
    spec = validate_thelook_v3_coverage_seed(first).query_spec

    assert derive_thelook_v3_program_signature(spec) == first["program_signature"]
    assert "2019-" not in first["program_signature"]
