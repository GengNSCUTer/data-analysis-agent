from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from data_analysis_agent.product_capability_contract import (
    ProductCapabilityContractError,
    validate_product_capability_suite,
    validate_safe_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def planned_suite() -> dict[str, object]:
    return yaml.safe_load(
        (ROOT / "evals/cases/product_capability_v1.yaml").read_text(encoding="utf-8")
    )


def _routing_case(case_id: str = "pcv1-a-answerable-gmv") -> dict[str, object]:
    return {
        "case_id": case_id,
        "category": "answerable_database",
        "question": "统计GMV",
        "expected": {
            "requires_database": True,
            "terminal_state": "answerable",
        },
    }


def test_partial_suite_is_valid_but_not_a_complete_suite(
    planned_suite: dict[str, object],
) -> None:
    validate_product_capability_suite(planned_suite)

    with pytest.raises(ProductCapabilityContractError, match="browser_flows must contain 8"):
        validate_product_capability_suite(planned_suite, require_complete=True)


def test_suite_rejects_duplicate_case_ids_across_sections(
    planned_suite: dict[str, object],
) -> None:
    raw = deepcopy(planned_suite)
    raw["routing_cases"] = [_routing_case("pcv1-a-answerable-gmv")]
    raw["single_turn_cases"] = [
        {
            **_routing_case("pcv1-a-answerable-gmv"),
            "case_id": "pcv1-a-answerable-gmv",
        }
    ]

    with pytest.raises(ProductCapabilityContractError, match="duplicate case_id"):
        validate_product_capability_suite(raw)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("routing_cases",), [{"case_id": "pcv1-a-answerable-gmv", "category": "x", "question": "x", "expected_sql": "SELECT 1"}], "forbidden field"),
        (("forbidden_uses",), ["sft_training"], "forbidden_uses"),
        (("routing_cases",), [{"case_id": "multi-schema-olist-0001", "category": "x", "question": "x", "expected": {"requires_database": True, "terminal_state": "answerable"}}], "invalid case_id"),
    ],
)
def test_suite_rejects_leakage_and_boundary_violations(
    planned_suite: dict[str, object],
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    raw = deepcopy(planned_suite)
    raw[path[0]] = value

    with pytest.raises(ProductCapabilityContractError, match=message):
        validate_product_capability_suite(raw)


def test_suite_rejects_incomplete_conversation_script(planned_suite: dict[str, object]) -> None:
    raw = deepcopy(planned_suite)
    raw["conversation_scripts"] = [
        {
            "case_id": "pcv1-c-date-followup",
            "category": "clarification_carry_forward",
            "steps": [
                {
                    "step_id": "turn-1",
                    "message": "本月GMV是多少",
                    "expected": {"terminal_state": "missing_time"},
                }
            ],
        }
    ]

    with pytest.raises(ProductCapabilityContractError, match="requires at least two turns"):
        validate_product_capability_suite(raw)


def test_safe_evidence_allows_only_aggregate_fields() -> None:
    records = validate_safe_evidence(
        {
            "suite_id": "product-capability-baseline-v1",
            "phase": "phase_a",
            "records": [
                {
                    "case_id": "pcv1-a-answerable-gmv",
                    "status": "pass",
                    "failure_layer": None,
                    "evidence_codes": ["route_matches"],
                    "artifact_sha256": "a" * 64,
                    "latency_ms": 12,
                }
            ],
        },
        known_case_ids={"pcv1-a-answerable-gmv"},
        phase="phase_a",
    )

    assert records[0].case_id == "pcv1-a-answerable-gmv"
    assert records[0].artifact_sha256 == "a" * 64


def test_safe_evidence_rejects_raw_sql_or_unknown_case() -> None:
    raw = {
        "suite_id": "product-capability-baseline-v1",
        "phase": "phase_a",
        "records": [
            {
                "case_id": "pcv1-a-answerable-gmv",
                "status": "pass",
                "evidence_codes": [],
                "sql_text": "SELECT hidden",
            }
        ],
    }

    with pytest.raises(ProductCapabilityContractError, match="unsafe or unsupported"):
        validate_safe_evidence(raw, known_case_ids={"pcv1-a-answerable-gmv"}, phase="phase_a")
