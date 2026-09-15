from __future__ import annotations

import copy
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
_SEED_FIXTURE = _ROOT / "data" / "fixtures" / "thelook_v3_coverage_seeds_v1.jsonl"


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
    assert len(contract["scenario_families"]) == 20
    assert sum(item["target_cases"] for item in contract["scenario_families"]) == 750
    assert contract["case_quotas"]["fact_domains"]["inventory_snapshot"] == 0

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
    assert audit["metric_exposure"].get("current_unsold_inventory_unit_count", 0) == 0
    assert len(audit["scenario_families"]) == 20
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


def test_v3_overlap_audit_rejects_a_protected_queryspec_identity(tmp_path: Path) -> None:
    records = build_seeds()
    protected_path = tmp_path / "protected-v2-cases.jsonl"
    protected_path.write_text(
        json.dumps({"query_spec": {"query_spec_id": records[0]["query_spec"]["query_spec_id"]}})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TheLookV3CoverageError, match="overlap 1 protected v2 QuerySpecs"):
        audit_seed_records(records, v2_cases=protected_path)


def test_v3_program_signature_excludes_date_boundaries_but_keeps_program_semantics() -> None:
    records = build_seeds()
    first = records[0]
    spec = validate_thelook_v3_coverage_seed(first).query_spec

    assert derive_thelook_v3_program_signature(spec) == first["program_signature"]
    assert "2019-" not in first["program_signature"]
