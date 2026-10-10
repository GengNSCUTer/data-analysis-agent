from __future__ import annotations

from copy import deepcopy

import pytest

from data_analysis_agent.dpo_pair import DpoPairValidationError, sha256_text
from data_analysis_agent.dpo_pair_assembly import attach_rejected_candidate
from data_analysis_agent.dpo_pair_seed import build_dpo_pair_seed


def _row() -> dict[str, object]:
    prompt = "### Task\nGenerate exactly one query.\n### SQL"
    sql = "SELECT COUNT(*) AS completed_order_count FROM analytics.fact_orders AS o WHERE o.order_status = 'delivered';"
    return {
        "admission_status": "admitted", "split": {"name": "train"},
        "workspace_key": "olist", "sample_id": "multi-schema-olist-0001",
        "query_spec_id": "qs_0001", "scenario_family_id": "orders_scalar",
        "program_id": "program_0001", "risk_tags": ["status_filter"],
        "query_spec": {"result_shape": "scalar"},
        "prompt_format_version": "multi-schema-sql-only-v1", "rendered_prompt": prompt,
        "candidate_sql": sql, "canonical_gold_sql_sha256": sha256_text(sql),
        "training_text": f"{prompt}\n{sql}",
    }


def _seed() -> dict[str, object]:
    return build_dpo_pair_seed(_row(), release_version="multi-schema-ecommerce-sql-only-sft-v1-r1").as_dict()


def _candidate() -> dict[str, object]:
    raw = "```sql\nSELECT COUNT(*) AS completed_order_count FROM analytics.fact_orders AS o;\n```"
    sql = "SELECT COUNT(*) AS completed_order_count FROM analytics.fact_orders AS o;"
    return {
        "source_sample_id": "multi-schema-olist-0001",
        "prompt_sha256": _seed()["source"]["prompt_sha256"],  # type: ignore[index]
        "raw_completion": raw,
        "raw_completion_sha256": sha256_text(raw),
        "candidate_sql": sql,
        "candidate_sql_sha256": sha256_text(sql),
        "tier": "A",
        "evidence": {
            "policy_status": "accepted", "execution_status": "executed",
            "result_contract_valid": True, "gold_denotation_state": "denotation_mismatch",
        },
        "provenance": {
            "model_id": "Qwen/Qwen2.5-Coder-1.5B",
            "model_revision": "df3ce67c0e24480f20468b6ef2894622d69eb73b",
            "parent_adapter_model_sha256": "a" * 64,
            "decode": {"do_sample": True, "temperature": 0.7},
        },
    }


def test_attaches_a_parent_completion_with_bound_governance_evidence() -> None:
    pair, validated = attach_rejected_candidate(_seed(), candidate=_candidate())

    assert validated.tier == "A"
    assert pair["rejected"]["provenance"]["raw_completion_sha256"] == sha256_text(_candidate()["raw_completion"])  # type: ignore[index]
    assert pair["rejected"]["sql"] != pair["chosen"]["sql"]


def test_rejects_candidate_bound_to_another_prompt_or_source() -> None:
    candidate = deepcopy(_candidate())
    candidate["source_sample_id"] = "multi-schema-olist-9999"
    with pytest.raises(DpoPairValidationError, match="different source"):
        attach_rejected_candidate(_seed(), candidate=candidate)

    candidate = deepcopy(_candidate())
    candidate["prompt_sha256"] = "b" * 64
    with pytest.raises(DpoPairValidationError, match="prompt hash"):
        attach_rejected_candidate(_seed(), candidate=candidate)


def test_rejects_unshared_normalization_or_a_policy_only_negative() -> None:
    candidate = deepcopy(_candidate())
    candidate["candidate_sql"] = "SELECT 1;"
    candidate["candidate_sql_sha256"] = sha256_text("SELECT 1;")
    with pytest.raises(DpoPairValidationError, match="normalized"):
        attach_rejected_candidate(_seed(), candidate=candidate)

    candidate = deepcopy(_candidate())
    candidate["tier"] = "D"
    with pytest.raises(DpoPairValidationError, match="A/B/C"):
        attach_rejected_candidate(_seed(), candidate=candidate)


def test_rejects_when_evidence_does_not_support_declared_tier() -> None:
    candidate = deepcopy(_candidate())
    candidate["tier"] = "B"
    with pytest.raises(DpoPairValidationError, match="does not match"):
        attach_rejected_candidate(_seed(), candidate=candidate)
