from __future__ import annotations

from pathlib import Path

import yaml

from data_analysis_agent.product_capability_contract import (
    validate_product_capability_suite,
    validate_safe_evidence,
)
from scripts.run_product_capability_conversations import _compare_step, _select_scripts
from scripts.run_product_capability_online import _safe_runtime


ROOT = Path(__file__).resolve().parents[1]


def _suite() -> dict[str, object]:
    return yaml.safe_load(
        (ROOT / "evals/cases/product_capability_v1.yaml").read_text(encoding="utf-8")
    )


def test_phase_c_freezes_ten_scripts_and_required_categories() -> None:
    suite = _suite()
    validate_product_capability_suite(suite)
    scripts = suite["conversation_scripts"]
    assert len(scripts) == 10
    assert len({item["case_id"] for item in scripts}) == 10
    assert {item["category"] for item in scripts} == {
        "clarification_carry_forward",
        "dimension_or_sort_change",
        "trusted_result_followup",
        "followup_without_trusted_result",
        "conversation_isolation",
        "conversation_reload",
        "context_budget",
    }


def test_matching_step_requires_server_owned_route_and_memory_projection() -> None:
    expected = {
        "terminal_state": "answerable",
        "requires_database": True,
        "intent": "data_query",
        "evidence_mode": "database_result",
        "memory": {
            "metric_ids": ["gmv"],
            "time_range": ["2017-01-01", "2017-12-31"],
        },
    }
    runtime = {
        "route_state": "answerable",
        "route_requires_database": True,
        "route_intent": "data_query",
        "route_evidence_mode": "database_result",
        "termination_reason": "completed",
        "sql_calls_used": 1,
        "context_truncated": False,
        "working_memory": {
            "metric_ids": ["gmv"],
            "time_range": {"start": "2017-01-01", "end": "2017-12-31"},
            "dimensions": [],
            "pending_missing": [],
            "previous_result_summary_present": False,
        },
    }
    assert _compare_step(expected, runtime) == ("pass", [])


def test_followup_without_trusted_summary_must_not_be_treated_as_answerable() -> None:
    expected = {
        "terminal_state": "clarification_required",
        "requires_database": False,
        "intent": "clarification_required",
        "evidence_mode": "clarification",
        "trusted_result_summary": False,
    }
    runtime = {
        "route_state": "clarification_required",
        "route_requires_database": False,
        "route_intent": "clarification_required",
        "route_evidence_mode": "clarification",
        "sql_calls_used": 0,
        "context_truncated": False,
        "working_memory": {
            "previous_result_summary_present": False,
        },
    }
    assert _compare_step(expected, runtime) == ("pass", [])


def test_conversation_safe_report_schema_rejects_raw_step_content() -> None:
    safe = {
        "suite_id": "product-capability-baseline-v1",
        "phase": "phase_c",
        "records": [
            {
                "case_id": "pcv1-c-date-followup-1",
                "status": "pass",
                "failure_layer": None,
                "evidence_codes": ["all_steps_matched"],
                "artifact_sha256": "a" * 64,
                "latency_ms": 100,
                "manual_review_status": "pending",
                "runtime": {
                    "step_count": 2,
                    "passed_steps": 2,
                    "steps": [{"step_id": "turn-1", "status": "pass"}],
                },
            }
        ],
    }
    assert validate_safe_evidence(
        safe,
        known_case_ids={"pcv1-c-date-followup-1"},
        phase="phase_c",
    )[0].case_id == "pcv1-c-date-followup-1"


def test_safe_runtime_does_not_project_sql_or_error_text_from_audit_evidence() -> None:
    safe = _safe_runtime(
        {
            "termination_reason": "execution_error",
            "error_type": "SafeSqlExecutionError",
            "catalog_trace": {},
            "repair_evidence": {},
            "original_sql": "SELECT private_value FROM private_table",
            "error_message": "relation private_table does not exist",
        }
    )

    serialized = str(safe)
    assert "private_value" not in serialized
    assert "private_table" not in serialized


def test_targeted_phase_c_selection_rejects_unknown_case_ids() -> None:
    scripts = [
        {"case_id": "pcv1-c-date-followup-1"},
        {"case_id": "pcv1-c-result-followup-2"},
    ]
    selected = _select_scripts(scripts, ("pcv1-c-result-followup-2",))
    assert selected == [{"case_id": "pcv1-c-result-followup-2"}]

    import pytest

    with pytest.raises(ValueError, match="unknown Phase-C"):
        _select_scripts(scripts, ("pcv1-c-not-real",))
