"""Attach an evidence-bound parent completion to a SQL DPO chosen-side seed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .dpo_pair import (
    DPO_PAIR_SCHEMA_VERSION,
    DpoPairValidationError,
    ValidatedDpoPair,
    sha256_text,
    validate_dpo_pair,
)
from .dpo_pair_seed import DPO_PAIR_SEED_SCHEMA_VERSION
from .candidate_sql_generator import unwrap_sql_completion


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DpoPairValidationError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DpoPairValidationError(f"{label} must be non-empty text")
    return value


def _verified_seed(seed: Mapping[str, Any]) -> tuple[Mapping[str, Any], str, str]:
    if seed.get("schema_version") != DPO_PAIR_SEED_SCHEMA_VERSION:
        raise DpoPairValidationError("unsupported DPO pair seed schema")
    if seed.get("rejected") is not None:
        raise DpoPairValidationError("a pair seed must not already contain rejected SQL")
    source = _mapping(seed.get("source"), "seed source")
    prompt = _text(seed.get("prompt"), "seed prompt")
    chosen = _mapping(seed.get("chosen"), "seed chosen")
    chosen_sql = _text(chosen.get("sql"), "seed chosen SQL")
    if source.get("prompt_sha256") != sha256_text(prompt):
        raise DpoPairValidationError("seed prompt hash drift")
    if source.get("canonical_gold_sql_sha256") != sha256_text(chosen_sql):
        raise DpoPairValidationError("seed Gold SQL hash drift")
    if chosen.get("sql_sha256") != sha256_text(chosen_sql):
        raise DpoPairValidationError("seed chosen SQL hash drift")
    return source, prompt, chosen_sql


def attach_rejected_candidate(
    seed: Mapping[str, Any], *, candidate: Mapping[str, Any]
) -> tuple[dict[str, Any], ValidatedDpoPair]:
    """Make one final DPO pair from a seed and a parent-generated completion.

    ``candidate`` must be produced by a later collector, but this pure function
    proves it was generated for this exact seed before accepting its SQL or
    governance result.  It does not infer evidence or repair candidate SQL.

    Required candidate structure::

        {
          "source_sample_id": "...", "prompt_sha256": "...",
          "raw_completion": "...", "raw_completion_sha256": "...",
          "candidate_sql": "...", "candidate_sql_sha256": "...",
          "tier": "A" | "B" | "C",
          "evidence": {...}, "provenance": {...}
        }
    """

    source, prompt, chosen_sql = _verified_seed(seed)
    if _text(candidate.get("source_sample_id"), "candidate source_sample_id").strip() != source.get("sample_id"):
        raise DpoPairValidationError("candidate belongs to a different source sample")
    if candidate.get("prompt_sha256") != sha256_text(prompt):
        raise DpoPairValidationError("candidate prompt hash does not bind the seed")

    raw_completion = _text(candidate.get("raw_completion"), "candidate raw_completion")
    if candidate.get("raw_completion_sha256") != sha256_text(raw_completion):
        raise DpoPairValidationError("candidate raw completion hash mismatch")
    candidate_sql = _text(candidate.get("candidate_sql"), "candidate SQL")
    if candidate.get("candidate_sql_sha256") != sha256_text(candidate_sql):
        raise DpoPairValidationError("candidate SQL hash mismatch")
    if unwrap_sql_completion(raw_completion) != candidate_sql:
        raise DpoPairValidationError("candidate SQL must be the shared normalized raw completion")

    tier = _text(candidate.get("tier"), "candidate tier").strip()
    if tier not in {"A", "B", "C"}:
        raise DpoPairValidationError("only A/B/C candidates may become DPO training pairs")
    evidence = _mapping(candidate.get("evidence"), "candidate evidence")
    provenance = dict(_mapping(candidate.get("provenance"), "candidate provenance"))
    # Preserve raw-completion evidence without making the raw completion part
    # of the final training target. The collector stores raw outputs outside
    # the repository; the pair only records their immutable fingerprint.
    provenance["raw_completion_sha256"] = sha256_text(raw_completion)

    pair = {
        "schema_version": DPO_PAIR_SCHEMA_VERSION,
        "pair_id": _text(seed.get("pair_id"), "seed pair_id"),
        "split": _text(seed.get("split"), "seed split"),
        "source": dict(source),
        "prompt": prompt,
        "chosen": {"sql": chosen_sql, "sql_sha256": sha256_text(chosen_sql)},
        "rejected": {
            "sql": candidate_sql,
            "sql_sha256": sha256_text(candidate_sql),
            "tier": tier,
            "evidence": dict(evidence),
            "provenance": provenance,
        },
    }
    return pair, validate_dpo_pair(pair)
