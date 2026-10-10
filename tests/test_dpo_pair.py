from __future__ import annotations

from copy import deepcopy

import pytest

from data_analysis_agent.dpo_pair import (
    DPO_PAIR_SCHEMA_VERSION,
    DpoPairValidationError,
    sha256_text,
    validate_dpo_pair,
)


def _pair() -> dict[str, object]:
    prompt = "### Task\nGenerate SQL.\n### SQL"
    chosen = "SELECT COUNT(*) AS completed_order_count FROM analytics.fact_orders AS o WHERE o.order_status = 'delivered';"
    rejected = "SELECT COUNT(*) AS completed_order_count FROM analytics.fact_orders AS o;"
    return {
        "schema_version": DPO_PAIR_SCHEMA_VERSION,
        "pair_id": "dpo-olist-0001",
        "split": "train",
        "source": {
            "release_version": "multi-schema-ecommerce-sql-only-sft-v1-r1",
            "release_split": "train",
            "workspace_key": "olist",
            "sample_id": "multi-schema-olist-0001",
            "query_spec_id": "qs_0001",
            "scenario_family_id": "completed_orders_scalar",
            "program_id": "orders_status_filter",
            "result_shape": "scalar",
            "risk_tags": ["status_filter"],
            "risk_tag_derivation": "frozen_source",
            "prompt_sha256": sha256_text(prompt),
            "canonical_gold_sql_sha256": sha256_text(chosen),
        },
        "prompt": prompt,
        "chosen": {"sql": chosen, "sql_sha256": sha256_text(chosen)},
        "rejected": {
            "sql": rejected,
            "sql_sha256": sha256_text(rejected),
            "tier": "A",
            "evidence": {
                "policy_status": "accepted",
                "execution_status": "executed",
                "result_contract_valid": True,
                "gold_denotation_state": "denotation_mismatch",
            },
            "provenance": {
                "model_id": "Qwen/Qwen2.5-Coder-1.5B",
                "model_revision": "df3ce67c0e24480f20468b6ef2894622d69eb73b",
                "parent_adapter_model_sha256": "a" * 64,
                "decode": {"do_sample": True, "temperature": 0.7},
            },
        },
    }


def test_validates_a_evidence_pair_without_runtime_dependencies() -> None:
    result = validate_dpo_pair(_pair())

    assert result.workspace_key == "olist"
    assert result.tier == "A"
    assert result.chosen_sql_sha256 != result.rejected_sql_sha256


def test_rejects_protected_test_as_a_dpo_source() -> None:
    pair = _pair()
    pair["split"] = "test"
    pair["source"] = {**pair["source"], "release_split": "test"}  # type: ignore[arg-type]

    with pytest.raises(DpoPairValidationError, match="train or validation"):
        validate_dpo_pair(pair)


def test_rejects_presentation_only_negative() -> None:
    pair = _pair()
    chosen = pair["chosen"]["sql"]  # type: ignore[index]
    rejected = "  select count(*) as completed_order_count from analytics.fact_orders as o where o.order_status = 'delivered';  "
    pair["rejected"] = {**pair["rejected"], "sql": rejected, "sql_sha256": sha256_text(rejected)}  # type: ignore[arg-type]
    assert chosen != rejected

    with pytest.raises(DpoPairValidationError, match="presentation"):
        validate_dpo_pair(pair)


def test_rejects_evidence_that_does_not_match_declared_tier() -> None:
    pair = _pair()
    pair["rejected"] = {
        **pair["rejected"],  # type: ignore[arg-type]
        "tier": "C",
    }

    with pytest.raises(DpoPairValidationError, match="does not match"):
        validate_dpo_pair(pair)


def test_diagnostic_policy_rejection_requires_explicit_opt_in() -> None:
    pair = _pair()
    pair["rejected"] = {
        **pair["rejected"],  # type: ignore[arg-type]
        "tier": "D",
        "evidence": {
            "policy_status": "rejected",
            "execution_status": "not_run",
            "result_contract_valid": False,
            "gold_denotation_state": "not_evaluated",
        },
    }

    with pytest.raises(DpoPairValidationError, match="not permitted"):
        validate_dpo_pair(pair)

    assert validate_dpo_pair(pair, allow_diagnostic_tier_d=True).tier == "D"


def test_rejects_source_prompt_hash_drift() -> None:
    pair = deepcopy(_pair())
    pair["source"]["prompt_sha256"] = "b" * 64  # type: ignore[index]

    with pytest.raises(DpoPairValidationError, match="prompt hash"):
        validate_dpo_pair(pair)
