"""Classify governed parent completions for the SQL DPO pair contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CandidateTier = Literal["A", "B", "C", "D"]
DenotationState = Literal[
    "ordered_denotation_match", "bag_denotation_match", "column_mismatch",
    "row_count_mismatch", "denotation_mismatch", "not_evaluated",
]


@dataclass(frozen=True)
class GovernedCandidateOutcome:
    policy_status: Literal["accepted", "rejected"]
    execution_status: Literal["executed", "error", "not_run"]
    result_contract_valid: bool
    gold_denotation_state: DenotationState


def classify_rejected_tier(outcome: GovernedCandidateOutcome) -> CandidateTier | None:
    """Return the DPO tier for a genuine error, or ``None`` for no usable pair.

    An exact/bag Gold match is a successful parent output, not a rejected
    example.  Incoherent states are rejected rather than guessed.
    """

    if outcome.policy_status == "rejected":
        if outcome.execution_status == "not_run" and not outcome.result_contract_valid and outcome.gold_denotation_state == "not_evaluated":
            return "D"
        raise ValueError("policy-rejected candidate has incompatible downstream evidence")
    if outcome.execution_status == "error":
        if not outcome.result_contract_valid and outcome.gold_denotation_state == "not_evaluated":
            return "C"
        raise ValueError("execution-error candidate has incompatible result evidence")
    if outcome.execution_status != "executed":
        raise ValueError("policy-accepted candidate must execute or report an execution error")
    if not outcome.result_contract_valid:
        if outcome.gold_denotation_state == "not_evaluated":
            return "B"
        raise ValueError("ResultContract-invalid candidate must not have Gold denotation")
    if outcome.gold_denotation_state in {"ordered_denotation_match", "bag_denotation_match"}:
        return None
    if outcome.gold_denotation_state in {
        "column_mismatch", "row_count_mismatch", "denotation_mismatch",
    }:
        return "A"
    raise ValueError("ResultContract-valid candidate requires a Gold denotation state")
