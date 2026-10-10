from __future__ import annotations

import pytest

from scripts.post_training.evaluation.summarize_multischema_matching_evaluation import (
    MatchingSummaryError,
    build_summary,
)


def _olist_comparison() -> dict[str, object]:
    return {
        "case_count": 2,
        "comparison_contract": {"dataset": "olist-final", "test_jsonl_sha256": "a"},
        "base_summary": {"result_contract_valid": 1},
        "adapter_summary": {"result_contract_valid": 2},
    }


def _olist_gold() -> dict[str, object]:
    return {
        "scope": {"mode": "post_generation_olist_gold_denotation_audit", "case_count": 2},
        "input_evidence": {"comparison_contract": _olist_comparison()["comparison_contract"]},
        "summary": {"adapter_gold_denotation_matches": 2},
    }


def _thelook() -> dict[str, object]:
    return {
        "report_schema_version": "thelook-matching-evaluation-v1",
        "dataset": "thelook-final",
        "case_count": 2,
        "base_summary": {"ordered_denotation_match": 0},
        "adapter_summary": {"ordered_denotation_match": 1},
        "ordered_match_transitions": {"False -> True": 1},
    }


def test_terminal_summary_requires_both_gold_stages_and_preserves_metric_boundary() -> None:
    summary = build_summary(
        olist_comparison=_olist_comparison(),
        olist_gold_audit=_olist_gold(),
        thelook_evaluation=_thelook(),
    )

    assert summary["status"] == "complete"
    assert summary["olist"]["gold_denotation"]["adapter_gold_denotation_matches"] == 2
    assert summary["thelook"]["gold_denotation"]["adapter"]["ordered_denotation_match"] == 1
    assert summary["boundaries"]["olist_contract_valid_is_not_gold_accuracy"] is True


def test_terminal_summary_rejects_olist_gold_evidence_for_another_generation_pair() -> None:
    gold = _olist_gold()
    gold["input_evidence"] = {"comparison_contract": {"dataset": "other"}}

    with pytest.raises(MatchingSummaryError, match="does not bind"):
        build_summary(
            olist_comparison=_olist_comparison(),
            olist_gold_audit=gold,
            thelook_evaluation=_thelook(),
        )
