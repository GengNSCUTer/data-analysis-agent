"""Manual-review contract for the Phase-B single-turn capability baseline.

Automatic runtime evidence can show routing, tool, policy and terminal
states. It cannot prove metric semantics or that a natural-language answer is
grounded. Those fields remain explicitly pending until a human reviews the
external raw artifact for a case.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


REVIEW_FIELDS = (
    "metric_semantics",
    "time_scope",
    "answer_grounded",
    "chart_semantics",
    "review_status",
    "review_notes_redacted",
)
LABEL_VALUES = frozenset({"pass", "fail", "not_applicable", "pending_manual"})
REVIEW_STATUSES = frozenset({"pending", "complete"})


class ProductCapabilityReviewError(ValueError):
    """Raised when a manual review does not follow the frozen contract."""


def build_single_turn_review_template(case_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Build a review file that contains no question, SQL or result values."""
    if len(case_ids) != len(set(case_ids)):
        raise ProductCapabilityReviewError("case IDs must be unique")
    return {
        "review_version": "product-capability-baseline-v1-single-turn-review-v1",
        "phase": "phase_b",
        "cases": {
            case_id: {
                "metric_semantics": "pending_manual",
                "time_scope": "pending_manual",
                "answer_grounded": "pending_manual",
                "chart_semantics": "not_applicable",
                "review_status": "pending",
                "review_notes_redacted": "",
            }
            for case_id in case_ids
        },
    }


def validate_manual_review(
    raw: Mapping[str, Any], *, known_case_ids: set[str]
) -> dict[str, dict[str, str]]:
    """Validate human labels without accepting raw answers or SQL text."""
    if raw.get("phase") != "phase_b":
        raise ProductCapabilityReviewError("review must target phase_b")
    cases = raw.get("cases")
    if not isinstance(cases, Mapping):
        raise ProductCapabilityReviewError("review cases must be a mapping")
    unknown = set(cases) - known_case_ids
    if unknown:
        raise ProductCapabilityReviewError(f"unknown case IDs: {sorted(unknown)}")
    validated: dict[str, dict[str, str]] = {}
    forbidden = ("select", "insert", "update", "delete", "with ", "result_rows", "completion")
    for case_id, value in cases.items():
        if not isinstance(value, Mapping):
            raise ProductCapabilityReviewError(f"review for {case_id} must be an object")
        if set(value) - set(REVIEW_FIELDS):
            raise ProductCapabilityReviewError(f"review for {case_id} contains unsupported fields")
        if set(value) != set(REVIEW_FIELDS):
            raise ProductCapabilityReviewError(f"review for {case_id} must contain all review fields")
        item: dict[str, str] = {}
        for field in REVIEW_FIELDS:
            field_value = value[field]
            if not isinstance(field_value, str):
                raise ProductCapabilityReviewError(f"{case_id}.{field} must be a string")
            if field in {"metric_semantics", "time_scope", "answer_grounded", "chart_semantics"}:
                if field_value not in LABEL_VALUES:
                    raise ProductCapabilityReviewError(f"invalid label: {case_id}.{field}")
            elif field == "review_status" and field_value not in REVIEW_STATUSES:
                raise ProductCapabilityReviewError(f"invalid review status: {case_id}.{field}")
            elif field == "review_notes_redacted":
                if len(field_value) > 500:
                    raise ProductCapabilityReviewError(f"{case_id}.review_notes_redacted is too long")
                lowered = field_value.lower()
                if any(token in lowered for token in forbidden):
                    raise ProductCapabilityReviewError(f"{case_id}.review_notes_redacted contains raw evidence")
            item[field] = field_value
        if item["review_status"] == "complete" and any(
            item[field] == "pending_manual"
            for field in ("metric_semantics", "time_scope", "answer_grounded", "chart_semantics")
        ):
            raise ProductCapabilityReviewError(f"completed review for {case_id} still has pending labels")
        validated[str(case_id)] = item
    return validated
