from __future__ import annotations

import pytest

from data_analysis_agent.dpo_candidate_tier import (
    GovernedCandidateOutcome,
    classify_rejected_tier,
)


def _outcome(**changes: object) -> GovernedCandidateOutcome:
    values: dict[str, object] = {
        "policy_status": "accepted", "execution_status": "executed",
        "result_contract_valid": True, "gold_denotation_state": "denotation_mismatch",
    }
    values.update(changes)
    return GovernedCandidateOutcome(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("outcome", "tier"),
    [
        (_outcome(), "A"),
        (_outcome(gold_denotation_state="column_mismatch"), "A"),
        (_outcome(gold_denotation_state="row_count_mismatch"), "A"),
        (_outcome(result_contract_valid=False, gold_denotation_state="not_evaluated"), "B"),
        (_outcome(execution_status="error", result_contract_valid=False, gold_denotation_state="not_evaluated"), "C"),
        (_outcome(policy_status="rejected", execution_status="not_run", result_contract_valid=False, gold_denotation_state="not_evaluated"), "D"),
    ],
)
def test_classifies_only_evidence_coherent_error_tiers(
    outcome: GovernedCandidateOutcome, tier: str
) -> None:
    assert classify_rejected_tier(outcome) == tier


def test_gold_matches_are_not_rejected_examples() -> None:
    assert classify_rejected_tier(_outcome(gold_denotation_state="ordered_denotation_match")) is None
    assert classify_rejected_tier(_outcome(gold_denotation_state="bag_denotation_match")) is None


def test_rejects_incoherent_downstream_evidence() -> None:
    with pytest.raises(ValueError, match="must not have Gold"):
        classify_rejected_tier(_outcome(result_contract_valid=False))
    with pytest.raises(ValueError, match="incompatible"):
        classify_rejected_tier(_outcome(policy_status="rejected"))
