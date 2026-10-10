"""Contracts for the staged product-capability baseline.

This module defines *test metadata*, not SQL generation or product runtime
behavior.  It deliberately allows a planned suite to have empty later phases,
while ``require_complete=True`` rejects every missing phase before a complete
baseline run can begin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any


SUITE_ID = "product-capability-baseline-v1"
SUITE_USAGE = "runtime_regression_only"
WORKSPACE_ID = "olist"
PHASE_NAMES = frozenset({"phase_a", "phase_b", "phase_c", "phase_d", "phase_e"})
CASE_SECTIONS = {
    "routing_cases": 12,
    "single_turn_cases": 16,
    "conversation_scripts": 10,
    "browser_flows": 8,
}
FORBIDDEN_USES = frozenset(
    {
        "sft_training",
        "sft_validation",
        "dpo_source",
        "few_shot",
        "local_model_checkpoint_selection",
        "protected_final_test",
    }
)
FORBIDDEN_CASE_ID_PREFIXES = (
    "olist-v3-1-final-",
    "thelook-v3-final-",
    "multi-schema-olist-",
    "multi-schema-thelook-",
)
CASE_ID_PATTERN = re.compile(r"^pcv1-[abcd]-[a-z0-9]+(?:-[a-z0-9]+)*$")
SQL_TEXT_PATTERN = re.compile(
    r"\b(?:select|insert|update|delete|merge|create|alter|drop|truncate|grant|revoke)\b",
    re.IGNORECASE,
)
FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "gold_sql",
        "canonical_sql",
        "expected_sql",
        "raw_sql",
        "sql_text",
        "result_rows",
        "raw_completion",
        "completion_text",
    }
)
SAFE_EVIDENCE_RECORD_KEYS = frozenset(
    {
        "case_id",
        "status",
        "failure_layer",
        "evidence_codes",
        "artifact_sha256",
        "latency_ms",
        "manual_review_status",
        "runtime",
        "automatic_labels",
        "manual_labels",
    }
)


class ProductCapabilityContractError(ValueError):
    """Raised when baseline metadata violates isolation or schema rules."""


@dataclass(frozen=True)
class CapabilityCase:
    """A single A/B baseline question and its expected structural behavior."""

    case_id: str
    category: str
    question: str
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class ConversationStep:
    """One C-phase turn with a state-transition expectation."""

    step_id: str
    message: str
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class ConversationScript:
    """A multi-turn C-phase scenario."""

    case_id: str
    category: str
    steps: tuple[ConversationStep, ...]


@dataclass(frozen=True)
class BrowserFlow:
    """A D-phase browser flow; live calls are explicitly opt-in later."""

    case_id: str
    category: str
    transport_mode: str
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class CapabilityEvidence:
    """One safe, aggregate-only evidence record emitted by a future runner."""

    case_id: str
    status: str
    failure_layer: str | None
    evidence_codes: tuple[str, ...]
    artifact_sha256: str | None = None
    latency_ms: int | None = None
    manual_review_status: str | None = None


def validate_product_capability_suite(
    raw: Mapping[str, Any], *, require_complete: bool = False
) -> None:
    """Validate the v1 suite without reading databases, models, or SSE.

    A Phase-0 skeleton is valid when ``require_complete`` is false.  A runner
    that claims to execute the full A--D baseline must pass true and therefore
    cannot silently omit cases or coverage quotas.
    """

    _require(raw.get("suite_id") == SUITE_ID, "unexpected suite_id")
    _require(raw.get("workspace_id") == WORKSPACE_ID, "unexpected workspace_id")
    _require(raw.get("usage") == SUITE_USAGE, "unexpected usage")
    _require(
        isinstance(raw.get("forbidden_uses"), list)
        and set(raw["forbidden_uses"]) == FORBIDDEN_USES,
        "forbidden_uses must exactly preserve the runtime-only isolation boundary",
    )
    _require(
        raw.get("case_targets") == CASE_SECTIONS,
        "case_targets must exactly match the frozen A/B/C/D quotas",
    )
    _validate_phase_status(raw.get("phase_status"))

    all_case_ids: set[str] = set()
    for section, expected_count in CASE_SECTIONS.items():
        items = raw.get(section)
        _require(isinstance(items, list), f"{section} must be a list")
        _require(len(items) <= expected_count, f"{section} exceeds its frozen quota")
        if require_complete:
            _require(
                len(items) == expected_count,
                f"{section} must contain {expected_count} cases before a full baseline run",
            )
        for item in items:
            case_id = _validate_case_item(section, item)
            _require(case_id not in all_case_ids, f"duplicate case_id: {case_id}")
            all_case_ids.add(case_id)


def validate_safe_evidence(
    raw: Mapping[str, Any], *, known_case_ids: set[str], phase: str
) -> tuple[CapabilityEvidence, ...]:
    """Validate a safe report without accepting question, SQL, or result text."""

    _require(raw.get("suite_id") == SUITE_ID, "safe evidence has unexpected suite_id")
    _require(raw.get("phase") == phase and phase in PHASE_NAMES, "safe evidence has invalid phase")
    records = raw.get("records")
    _require(isinstance(records, list), "safe evidence records must be a list")

    seen_ids: set[str] = set()
    validated: list[CapabilityEvidence] = []
    for record in records:
        _require(isinstance(record, Mapping), "safe evidence record must be an object")
        keys = set(record)
        _require(
            keys <= SAFE_EVIDENCE_RECORD_KEYS,
            "safe evidence record contains an unsafe or unsupported field",
        )
        _require("case_id" in record and "status" in record, "safe evidence record lacks identity")
        case_id = _require_case_id(record["case_id"])
        _require(case_id in known_case_ids, f"safe evidence references unknown case_id: {case_id}")
        _require(case_id not in seen_ids, f"safe evidence duplicates case_id: {case_id}")
        seen_ids.add(case_id)
        _reject_forbidden_content(record)

        evidence_codes = record.get("evidence_codes", [])
        _require(
            isinstance(evidence_codes, list) and all(isinstance(code, str) for code in evidence_codes),
            "evidence_codes must be a list of strings",
        )
        latency_ms = record.get("latency_ms")
        _require(
            latency_ms is None or (isinstance(latency_ms, int) and latency_ms >= 0),
            "latency_ms must be a non-negative integer when present",
        )
        artifact_sha256 = record.get("artifact_sha256")
        _require(
            artifact_sha256 is None
            or (isinstance(artifact_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)),
            "artifact_sha256 must be a sha256 digest when present",
        )
        validated.append(
            CapabilityEvidence(
                case_id=case_id,
                status=_require_nonempty_string(record["status"], "status"),
                failure_layer=_optional_string(record.get("failure_layer"), "failure_layer"),
                evidence_codes=tuple(evidence_codes),
                artifact_sha256=artifact_sha256,
                latency_ms=latency_ms,
                manual_review_status=_optional_string(
                    record.get("manual_review_status"), "manual_review_status"
                ),
            )
        )
    return tuple(validated)


def _validate_phase_status(value: Any) -> None:
    _require(isinstance(value, Mapping), "phase_status must be an object")
    expected = {"phase_a", "phase_b", "phase_c", "phase_d"}
    _require(set(value) == expected, "phase_status must declare phase_a through phase_d")
    _require(
        all(status in {"planned", "ready", "complete"} for status in value.values()),
        "phase_status contains an invalid value",
    )


def _validate_case_item(section: str, item: Any) -> str:
    _require(isinstance(item, Mapping), f"{section} item must be an object")
    _reject_forbidden_content(item)
    case_id = _require_case_id(item.get("case_id"))
    _require_nonempty_string(item.get("category"), f"{case_id}.category")

    if section in {"routing_cases", "single_turn_cases"}:
        _require_nonempty_string(item.get("question"), f"{case_id}.question")
        expected = item.get("expected")
        _require(isinstance(expected, Mapping), f"{case_id}.expected must be an object")
        _require(
            "requires_database" in expected and isinstance(expected["requires_database"], bool),
            f"{case_id}.expected.requires_database must be boolean",
        )
        _require_nonempty_string(expected.get("terminal_state"), f"{case_id}.expected.terminal_state")
    elif section == "conversation_scripts":
        steps = item.get("steps")
        _require(isinstance(steps, list) and len(steps) >= 2, f"{case_id}.steps requires at least two turns")
        seen_steps: set[str] = set()
        for step in steps:
            _require(isinstance(step, Mapping), f"{case_id}.step must be an object")
            _reject_forbidden_content(step)
            step_id = _require_nonempty_string(step.get("step_id"), f"{case_id}.step_id")
            _require(step_id not in seen_steps, f"{case_id} repeats step_id: {step_id}")
            seen_steps.add(step_id)
            _require_nonempty_string(step.get("message"), f"{case_id}.{step_id}.message")
            _require(isinstance(step.get("expected"), Mapping), f"{case_id}.{step_id}.expected must be an object")
    elif section == "browser_flows":
        _require(item.get("transport_mode") in {"stub", "live"}, f"{case_id}.transport_mode is invalid")
        expected = item.get("expected")
        _require(isinstance(expected, Mapping), f"{case_id}.expected must be an object")
        _require(
            isinstance(expected.get("loading_terminal_state"), str),
            f"{case_id}.expected.loading_terminal_state is required",
        )
    return case_id


def _reject_forbidden_content(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require(str(key) not in FORBIDDEN_METADATA_KEYS, f"forbidden field: {key}")
            _reject_forbidden_content(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_forbidden_content(item)
    elif isinstance(value, str):
        _require(not SQL_TEXT_PATTERN.search(value), "contract contains SQL-like text")


def _require_case_id(value: Any) -> str:
    case_id = _require_nonempty_string(value, "case_id")
    _require(CASE_ID_PATTERN.fullmatch(case_id) is not None, f"invalid case_id: {case_id}")
    _require(
        not case_id.startswith(FORBIDDEN_CASE_ID_PREFIXES),
        f"protected case identifier is forbidden: {case_id}",
    )
    return case_id


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty_string(value, field_name)


def _require_nonempty_string(value: Any, field_name: str) -> str:
    _require(isinstance(value, str) and value.strip(), f"{field_name} must be a non-empty string")
    return value.strip()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProductCapabilityContractError(message)
