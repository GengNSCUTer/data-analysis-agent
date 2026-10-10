from __future__ import annotations

from pathlib import Path

import pytest

from data_analysis_agent.product_capability_review import (
    ProductCapabilityReviewError,
    build_single_turn_review_template,
    validate_manual_review,
)
from scripts.run_product_capability_online import _safe_context_budget, _safe_event_summary


def test_review_template_contains_only_pending_safe_fields() -> None:
    template = build_single_turn_review_template(["pcv1-b-scalar-gmv"])
    assert template["phase"] == "phase_b"
    assert template["cases"]["pcv1-b-scalar-gmv"]["review_status"] == "pending"
    assert "sql" not in str(template).lower()


def test_completed_review_requires_non_pending_labels() -> None:
    raw = build_single_turn_review_template(["pcv1-b-scalar-gmv"])
    raw["cases"]["pcv1-b-scalar-gmv"]["review_status"] = "complete"
    with pytest.raises(ProductCapabilityReviewError, match="still has pending"):
        validate_manual_review(raw, known_case_ids={"pcv1-b-scalar-gmv"})


def test_review_rejects_unknown_case_and_raw_evidence() -> None:
    raw = build_single_turn_review_template(["pcv1-b-scalar-gmv"])
    raw["cases"]["pcv1-b-scalar-gmv"]["review_notes_redacted"] = "SELECT raw result"
    with pytest.raises(ProductCapabilityReviewError, match="raw evidence"):
        validate_manual_review(raw, known_case_ids={"pcv1-b-scalar-gmv"})


def test_review_rejects_unknown_case() -> None:
    raw = build_single_turn_review_template(["pcv1-b-scalar-gmv"])
    with pytest.raises(ProductCapabilityReviewError, match="unknown case"):
        validate_manual_review(raw, known_case_ids={"pcv1-b-scalar-orders"})


def test_safe_event_summary_detects_current_simple_text_component() -> None:
    summary = _safe_event_summary(
        [{"rich": {"type": "text"}, "simple": {"type": "text", "text": "已完成"}}]
    )

    assert summary["has_text_response"] is True
    assert summary["answer_sha256"]


def test_safe_context_budget_keeps_only_structural_provenance() -> None:
    safe = _safe_context_budget(
        {
            "version": "context-budget-v1",
            "input_chars": 1_500,
            "omitted_turns": 2,
            "summary_inserted": True,
            "untrusted_history_text": "SELECT secret_value FROM private_table",
            "summary": {
                "version": "history-summary-v1",
                "source_turn_start": 1,
                "source_turn_end": 2,
                "source_sha256": "a" * 64,
                "untrusted_message": "用户原文",
            },
        }
    )

    assert safe is not None
    assert safe["input_chars"] == 1_500
    assert safe["summary"]["source_sha256"] == "a" * 64
    assert "secret_value" not in str(safe)
    assert "用户原文" not in str(safe)
