from __future__ import annotations

from scripts.post_training.evaluation.run_multi_schema_validation_generation_diagnostic import (
    _tier_counts,
)


def test_validation_diagnostic_summarizes_governed_outcomes_without_pairs() -> None:
    rows = [
        {
            "evidence": {
                "policy_status": "accepted",
                "execution_status": "executed",
                "result_contract_valid": True,
                "gold_denotation_state": "ordered_denotation_match",
            }
        },
        {
            "evidence": {
                "policy_status": "accepted",
                "execution_status": "executed",
                "result_contract_valid": True,
                "gold_denotation_state": "denotation_mismatch",
            }
        },
        {
            "evidence": {
                "policy_status": "accepted",
                "execution_status": "executed",
                "result_contract_valid": False,
                "gold_denotation_state": "not_evaluated",
            }
        },
        {
            "evidence": {
                "policy_status": "accepted",
                "execution_status": "error",
                "result_contract_valid": False,
                "gold_denotation_state": "not_evaluated",
            }
        },
        {
            "evidence": {
                "policy_status": "rejected",
                "execution_status": "not_run",
                "result_contract_valid": False,
                "gold_denotation_state": "not_evaluated",
            }
        },
        {"skip_reason": "normalization_error"},
    ]

    assert _tier_counts(rows) == {
        "A": 1,
        "B": 1,
        "C": 1,
        "D": 1,
        "gold_match": 1,
        "normalization_error": 1,
    }
