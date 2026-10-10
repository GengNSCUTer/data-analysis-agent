#!/usr/bin/env python3
"""Collect a small, evidence-bound SQL DPO candidate batch from SFT train only.

Generation reads only the frozen multi-schema prompt and source identity.  It
does not provide Gold SQL to the model.  Once *all* raw completions are written,
the script re-reads the hash-bound train release, executes candidates through
the workspace Policy -> readonly role -> ResultContract chain, and reads Gold
SQL only for contract-valid candidates to classify an A-tier denotation error.

Raw prompts, completions, SQL and final pairs are research artifacts and must
remain outside the Git worktree.  This is a collector, never a DPO trainer.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from time import perf_counter
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.candidate_sql_generator import (  # noqa: E402
    CandidateSqlGenerationError,
    unwrap_sql_completion,
)
from data_analysis_agent.dpo_candidate_tier import (  # noqa: E402
    GovernedCandidateOutcome,
    classify_rejected_tier,
)
from data_analysis_agent.dpo_pair import DpoPairValidationError, sha256_text  # noqa: E402
from data_analysis_agent.dpo_pair_assembly import attach_rejected_candidate  # noqa: E402
from data_analysis_agent.dpo_pair_seed import build_dpo_pair_seed  # noqa: E402
from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.postgres_runner import PostgresConnectionSettings  # noqa: E402
from data_analysis_agent.result_validator import ResultValidator  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402


RELEASE_VERSION = "multi-schema-ecommerce-sql-only-sft-v1-r1"
PROMPT_VERSION = "multi-schema-sql-only-v1"
MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"
MAX_EXECUTION_ROWS = 1000
DEFAULT_RELEASE_DIR = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/multi-schema-ecommerce-sql-only-v1-r1"
)


class DpoCollectionError(ValueError):
    """The bounded preference-candidate collection contract was violated."""


@dataclass(frozen=True)
class GenerationSource:
    """Prompt-only source plus non-sensitive strata used for batch selection."""

    sample_id: str
    workspace_key: str
    prompt: str
    prompt_sha256: str
    scenario_family_id: str
    result_shape: str
    risk_tags: tuple[str, ...]
    risk_tag_derivation: str

    def selection_record(self) -> dict[str, Any]:
        """Return auditable metadata without exposing the prompt or Gold SQL."""

        return {
            "sample_id": self.sample_id,
            "workspace_key": self.workspace_key,
            "scenario_family_id": self.scenario_family_id,
            "result_shape": self.result_shape,
            "risk_tags": list(self.risk_tags),
            "risk_tag_derivation": self.risk_tag_derivation,
            "prompt_sha256": self.prompt_sha256,
        }


_RESULT_SHAPES = frozenset(
    {"scalar", "state_grouped", "category_grouped", "dimension_grouped", "time_series"}
)
_RISK_TAG_DERIVATION_VERSION = "olist-dpo-risk-tag-backfill-v1"
_SIMPLE_SCALAR_TAG = "simple_scalar_aggregation"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DpoCollectionError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DpoCollectionError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise DpoCollectionError(f"cannot read JSONL: {path}") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise DpoCollectionError(f"non-empty object JSONL required: {path}")
    return rows


def _frozen_split(release_dir: Path, *, split_name: str) -> tuple[dict[str, Any], Path]:
    if split_name not in {"train", "validation"}:
        raise DpoCollectionError("source split must be train or validation")
    manifest = _read_json(release_dir / "manifest.json")
    if manifest.get("release_version") != RELEASE_VERSION:
        raise DpoCollectionError("unsupported multi-schema release version")
    checks = manifest.get("checks")
    if not isinstance(checks, Mapping) or checks.get("status") != "pass":
        raise DpoCollectionError("source release did not pass its audit")
    if checks.get("protected_tests_not_training_sources") is not True:
        raise DpoCollectionError("source release does not prove protected test isolation")
    split_path = release_dir / f"{split_name}.jsonl"
    expected_hash = manifest.get("outputs", {}).get(split_name, {}).get("sha256")
    if not isinstance(expected_hash, str) or sha256_file(split_path) != expected_hash:
        raise DpoCollectionError(f"{split_name} JSONL hash differs from frozen source release")
    return manifest, split_path


def _frozen_train(release_dir: Path) -> tuple[dict[str, Any], Path]:
    """Compatibility wrapper for the train-only DPO collector."""

    return _frozen_split(release_dir, split_name="train")


def _source_from_row(
    row: Mapping[str, Any], *, expected_split: str = "train"
) -> GenerationSource:
    """Project no Gold SQL and no training_text into the model-input boundary."""

    if expected_split not in {"train", "validation"}:
        raise DpoCollectionError("expected source split must be train or validation")
    if row.get("split", {}).get("name") != expected_split:
        raise DpoCollectionError(f"collector accepts {expected_split} rows only")
    if row.get("admission_status") != "admitted" or row.get("prompt_format_version") != PROMPT_VERSION:
        raise DpoCollectionError("source row lacks admitted multi-schema contract")
    sample_id, workspace, prompt = row.get("sample_id"), row.get("workspace_key"), row.get("rendered_prompt")
    if not isinstance(sample_id, str) or not sample_id or workspace not in {"olist", "thelook"}:
        raise DpoCollectionError("source row has invalid identity or workspace")
    if not isinstance(prompt, str) or not prompt.endswith("### SQL"):
        raise DpoCollectionError("source row has invalid SQL-only prompt")
    family_id = row.get("scenario_family_id")
    query_spec = row.get("query_spec")
    if not isinstance(family_id, str) or not family_id.strip():
        raise DpoCollectionError("source row lacks scenario_family_id")
    if not isinstance(query_spec, Mapping):
        raise DpoCollectionError("source row lacks QuerySpec for source stratification")
    result_shape = query_spec.get("result_shape")
    if not isinstance(result_shape, str) or result_shape not in _RESULT_SHAPES:
        raise DpoCollectionError("source row has unsupported QuerySpec result_shape")
    raw_risk_tags, tag_origin = _risk_tags_for_dpo_row(row)
    if (
        not isinstance(raw_risk_tags, list)
        or not raw_risk_tags
        or any(not isinstance(tag, str) or not tag.strip() for tag in raw_risk_tags)
        or len(set(raw_risk_tags)) != len(raw_risk_tags)
    ):
        raise DpoCollectionError("source row lacks non-empty unique risk_tags")
    # Deliberately do not read row["candidate_sql"] or row["training_text"] here.
    return GenerationSource(
        sample_id=sample_id,
        workspace_key=str(workspace),
        prompt=prompt,
        prompt_sha256=sha256_text(prompt),
        scenario_family_id=family_id,
        result_shape=result_shape,
        risk_tags=tuple(sorted(raw_risk_tags)),
        risk_tag_derivation=tag_origin,
    )


def _risk_tags_for_dpo_row(row: Mapping[str, Any]) -> tuple[list[str], str]:
    """Return frozen tags or the sole permitted legacy Olist metadata repair.

    The v3.1 Olist source release predates the explicit
    ``simple_scalar_aggregation`` stratum.  A blank tag list is eligible for
    this repair only when its pinned QuerySpec proves it is one scalar metric;
    no SQL, prompt, result or model output is inspected.  All other empty or
    malformed tag lists remain fail-closed.
    """

    raw_tags = row.get("risk_tags")
    if isinstance(raw_tags, list) and raw_tags:
        return list(raw_tags), "frozen_source"
    query_spec = row.get("query_spec")
    if (
        raw_tags == []
        and row.get("workspace_key") == "olist"
        and isinstance(query_spec, Mapping)
        and query_spec.get("result_shape") == "scalar"
        and isinstance(query_spec.get("metric_ids"), list)
        and len(query_spec["metric_ids"]) == 1
        and isinstance(query_spec["metric_ids"][0], str)
    ):
        return [_SIMPLE_SCALAR_TAG], _RISK_TAG_DERIVATION_VERSION
    return list(raw_tags) if isinstance(raw_tags, list) else [], "invalid"


def _shape_schedule(candidates: Sequence[GenerationSource], *, requested: int) -> list[str]:
    """Allocate a balanced, deterministic quota over a workspace's shapes.

    Each available result shape must receive at least one source.  Remaining
    slots are distributed so per-shape counts differ by at most one; rarer
    shapes receive the deterministic remainder first.  This makes the small
    batch a genuine result-shape stratum rather than a sample-ID hash slice.
    """

    counts = Counter(source.result_shape for source in candidates)
    shapes = sorted(counts)
    if requested < len(shapes):
        raise DpoCollectionError(
            "sources_per_workspace is too small to cover every available result shape"
        )
    base, extra = divmod(requested, len(shapes))
    quotas = dict.fromkeys(shapes, base)
    # Prefer a rarer shape for the remainder only when it has enough distinct
    # rows to satisfy that extra slot.  A small rare stratum must not make an
    # otherwise valid batch impossible (for example 2 scalar, 1 grouped,
    # 1 series rows with four requested sources).
    for _ in range(extra):
        available = [shape for shape in shapes if counts[shape] > quotas[shape]]
        if not available:
            raise DpoCollectionError("insufficient workspace rows for a result-shape quota")
        # First distribute the remainder across different shapes.  Only when
        # all other strata have no spare rows may one shape receive a second
        # remainder slot; that is the only legitimate way a sparse release can
        # deviate from the otherwise <= 1 quota difference.
        not_yet_extended = [shape for shape in available if quotas[shape] == base]
        shape = min(
            not_yet_extended or available,
            key=lambda item: (counts[item], item),
        )
        quotas[shape] += 1
    if any(counts[shape] < quota for shape, quota in quotas.items()):
        raise DpoCollectionError("insufficient workspace rows for a result-shape quota")
    return [shape for shape in shapes for _ in range(quotas[shape])]


def _select_workspace_sources(
    candidates: Sequence[GenerationSource], *, requested: int
) -> list[GenerationSource]:
    """Select a shape-balanced set covering new families and uncommon risks.

    A `scenario_family_id` can occur with many date/surface variants.  The
    selector admits at most one from a family until that is impossible.  For
    each preallocated result-shape slot, it then prefers candidates containing
    risk tags not yet represented; a rarity tie-break avoids repeatedly taking
    dominant tags such as ``time_filter``.  The final hash tie-break is stable
    and independent of line order.
    """

    schedule = _shape_schedule(candidates, requested=requested)
    risk_frequency = Counter(tag for source in candidates for tag in source.risk_tags)
    chosen: list[GenerationSource] = []
    chosen_ids: set[str] = set()
    chosen_families: set[str] = set()
    covered_risk_tags: set[str] = set()
    for result_shape in schedule:
        eligible = [
            source
            for source in candidates
            if source.result_shape == result_shape and source.sample_id not in chosen_ids
        ]
        if not eligible:
            raise DpoCollectionError("result-shape quota cannot be materialized without duplication")

        def rank(source: GenerationSource) -> tuple[int, int, float, str]:
            novel_tags = set(source.risk_tags).difference(covered_risk_tags)
            return (
                int(source.scenario_family_id not in chosen_families),
                len(novel_tags),
                sum(1.0 / risk_frequency[tag] for tag in novel_tags),
                # `min` below uses this final identity only to make an exact
                # score tie deterministic, not as the original selector.
                sha256_text(source.sample_id),
            )

        # Minimise a negated quality tuple.  Do not sort on prompt or SQL.
        selected = min(
            eligible,
            key=lambda source: (-rank(source)[0], -rank(source)[1], -rank(source)[2], rank(source)[3]),
        )
        chosen.append(selected)
        chosen_ids.add(selected.sample_id)
        chosen_families.add(selected.scenario_family_id)
        covered_risk_tags.update(selected.risk_tags)
    return chosen


def select_generation_sources_with_audit(
    rows: Sequence[Mapping[str, Any]], *, per_workspace: int, expected_split: str = "train"
) -> tuple[list[GenerationSource], dict[str, Any]]:
    """Choose sources while making legacy missing-risk metadata explicit.

    The frozen SFT release predates the DPO risk-tag requirement for a small
    number of Olist rows.  The only permitted blank-list repair is the pinned
    one-metric scalar derivation in ``_risk_tags_for_dpo_row``; every repaired
    row is counted in the external selection manifest. Other source-contract
    violations still fail closed; this function must never silently skip a
    validation/test row or a malformed prompt.
    """

    if per_workspace <= 0:
        raise DpoCollectionError("per-workspace source count must be positive")
    by_workspace: dict[str, list[GenerationSource]] = defaultdict(list)
    derived_by_workspace: Counter[str] = Counter()
    derived_sample_ids: list[str] = []
    for row in rows:
        try:
            source = _source_from_row(row, expected_split=expected_split)
        except DpoCollectionError as exc:
            if str(exc) != "source row lacks non-empty unique risk_tags":
                raise
            sample_id, workspace = row.get("sample_id"), row.get("workspace_key")
            if not isinstance(sample_id, str) or workspace not in {"olist", "thelook"}:
                raise
            raise
        _, tag_origin = _risk_tags_for_dpo_row(row)
        if tag_origin == _RISK_TAG_DERIVATION_VERSION:
            derived_by_workspace[source.workspace_key] += 1
            derived_sample_ids.append(source.sample_id)
        by_workspace[source.workspace_key].append(source)
    if set(by_workspace) != {"olist", "thelook"}:
        raise DpoCollectionError(
            f"source release must contain both Olist and TheLook {expected_split} rows"
        )
    selected: list[GenerationSource] = []
    for workspace in ("olist", "thelook"):
        candidates = list(by_workspace[workspace])
        if len(candidates) < per_workspace:
            raise DpoCollectionError(
                f"insufficient {workspace} {expected_split} rows for requested batch"
            )
        selected.extend(_select_workspace_sources(candidates, requested=per_workspace))
    return selected, {
        "derivation_version": _RISK_TAG_DERIVATION_VERSION,
        "derived_tag": _SIMPLE_SCALAR_TAG,
        "count": len(derived_sample_ids),
        "by_workspace": dict(sorted(derived_by_workspace.items())),
        "sample_ids": sorted(derived_sample_ids),
    }


def select_generation_sources(
    rows: Sequence[Mapping[str, Any]], *, per_workspace: int, expected_split: str = "train"
) -> list[GenerationSource]:
    """Compatibility projection of the audited, prompt-only selector."""

    selected, _ = select_generation_sources_with_audit(
        rows, per_workspace=per_workspace, expected_split=expected_split
    )
    return selected


def summarize_generation_selection(
    sources: Sequence[GenerationSource],
    *,
    per_workspace: int,
    source_exclusions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a safe, reviewable selection manifest with no prompt or SQL."""

    by_workspace: dict[str, list[GenerationSource]] = defaultdict(list)
    for source in sources:
        by_workspace[source.workspace_key].append(source)
    summary: dict[str, Any] = {}
    for workspace in ("olist", "thelook"):
        selected = by_workspace.get(workspace, [])
        if len(selected) != per_workspace:
            raise DpoCollectionError("selection summary does not meet workspace quota")
        summary[workspace] = {
            "selected_source_count": len(selected),
            "scenario_family_count": len({source.scenario_family_id for source in selected}),
            "result_shape_counts": dict(sorted(Counter(source.result_shape for source in selected).items())),
            "risk_tag_counts": dict(sorted(Counter(tag for source in selected for tag in source.risk_tags).items())),
            "sources": [source.selection_record() for source in selected],
        }
    return {
        "method": "workspace_quota + balanced_result_shape + unique_scenario_family + risk_tag_novelty",
        "sources_per_workspace": per_workspace,
        "workspaces": summary,
        "source_exclusions": dict(source_exclusions or {}),
    }


def _load_parent(model_dir: Path, adapter_dir: Path, expected_adapter_sha256: str) -> tuple[Any, Any, dict[str, Any]]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    manifest = _read_json(model_dir / "download_manifest.json")
    if manifest.get("model_id") != MODEL_ID or not isinstance(manifest.get("revision"), str):
        raise DpoCollectionError("model directory does not match frozen Qwen2.5-Coder-1.5B")
    model_file, config_file = adapter_dir / "adapter_model.safetensors", adapter_dir / "adapter_config.json"
    if not model_file.is_file() or not config_file.is_file():
        raise DpoCollectionError("parent adapter artifacts are incomplete")
    adapter_sha = sha256_file(model_file)
    if adapter_sha != expected_adapter_sha256:
        raise DpoCollectionError("parent adapter SHA-256 differs from the frozen argument")
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        model_dir, local_files_only=True, torch_dtype=torch.bfloat16, device_map={"": 0}
    )
    model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=False)
    model.eval()
    return tokenizer, model, {
        "model_id": manifest["model_id"], "model_revision": manifest["revision"],
        "parent_adapter_model_sha256": adapter_sha,
        "adapter_config_sha256": sha256_file(config_file),
    }


def _generate_one(
    tokenizer: Any, model: Any, prompt: str, *, max_input_tokens: int,
    max_new_tokens: int, temperature: float, top_p: float, seed: int,
) -> tuple[str, int, int]:
    import torch

    encoded = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
    input_length = int(encoded["input_ids"].shape[-1])
    if input_length > max_input_tokens:
        raise DpoCollectionError("source prompt exceeds frozen input token limit")
    torch.manual_seed(seed)
    started = perf_counter()
    with torch.inference_mode():
        output = model.generate(
            **{key: value.to("cuda:0") for key, value in encoded.items()},
            do_sample=True, temperature=temperature, top_p=top_p, num_beams=1,
            max_new_tokens=max_new_tokens, pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id, use_cache=True,
        )
    tokens = output[0, input_length:]
    completion = tokenizer.decode(tokens, skip_special_tokens=True)
    if not completion.strip():
        raise CandidateSqlGenerationError("empty parent completion")
    return completion, int(tokens.shape[-1]), round((perf_counter() - started) * 1000)


def _workspace_for_key(workspace_key: str) -> Any:
    if workspace_key == "olist":
        return OLIST_V3_WORKSPACE
    if workspace_key == "thelook":
        return THELOOK_V2_WORKSPACE
    raise DpoCollectionError("unsupported workspace")


def _database_for_key(workspace_key: str) -> str:
    return "data_analysis_agent" if workspace_key == "olist" else "thelook_analytics"


def _execute_governed(sql: str, source_row: Mapping[str, Any]) -> tuple[GovernedCandidateOutcome, Any | None]:
    """Run one candidate through exactly Policy -> reader role -> ResultContract."""

    import pandas as pd
    import psycopg2
    import psycopg2.extras

    workspace_key = str(source_row["workspace_key"])
    workspace = _workspace_for_key(workspace_key)
    policy = SqlPolicy(analyst_limit=MAX_EXECUTION_ROWS, admin_limit=MAX_EXECUTION_ROWS, workspace=workspace)
    try:
        decision = policy.evaluate(sql, role="analyst")
    except PolicyViolation:
        return GovernedCandidateOutcome("rejected", "not_run", False, "not_evaluated"), None
    settings = replace(PostgresConnectionSettings.from_environment(), database=_database_for_key(workspace_key))
    try:
        connection = psycopg2.connect(
            host=settings.host, port=settings.port, database=settings.database, user="postgres"
        )
        try:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                # workspace.reader_role is a repository-owned fixed identifier,
                # never model or user input.
                cursor.execute(f"SET LOCAL ROLE {workspace.reader_role}")
                cursor.execute("SET LOCAL statement_timeout = %s", (settings.statement_timeout_ms,))
                cursor.execute(decision.final_sql)
                frame = pd.DataFrame([dict(row) for row in cursor.fetchmany(MAX_EXECUTION_ROWS)])
        finally:
            connection.close()
    except Exception:
        return GovernedCandidateOutcome("accepted", "error", False, "not_evaluated"), None

    contract = source_row.get("result_contract")
    spec = source_row.get("query_spec")
    if not isinstance(contract, Mapping) or not isinstance(spec, Mapping):
        raise DpoCollectionError("source row lacks result contract or QuerySpec")
    time = spec.get("time")
    grain = time.get("grain") if isinstance(time, Mapping) else None
    validation = ResultValidator(MAX_EXECUTION_ROWS).validate(
        frame,
        required_columns=contract.get("required_result_columns", ()),
        metric_columns=contract.get("metric_result_columns", ()),
        time_column=contract.get("result_time_column"),
        time_bucket_grain=grain if isinstance(grain, str) else None,
        requested_start=contract.get("requested_start"),
        requested_end=contract.get("requested_end"),
        limit_applied=decision.policy_limit_applied and len(frame) >= decision.row_limit,
        exact_columns=bool(contract.get("exact_result_columns", False)),
        metric_value_constraints=contract.get("metric_value_constraints", {}),
    )
    return GovernedCandidateOutcome("accepted", "executed", validation.safe_to_answer, "not_evaluated"), frame


def _gold_sql_after_generation(train_path: Path, selected_ids: set[str]) -> dict[str, Mapping[str, Any]]:
    """The sole post-generation source read that retains Gold SQL text."""

    selected: dict[str, Mapping[str, Any]] = {}
    for row in _read_jsonl(train_path):
        sample_id = row.get("sample_id")
        if sample_id in selected_ids:
            if not isinstance(sample_id, str) or sample_id in selected:
                raise DpoCollectionError("selected train rows have duplicate identities")
            risk_tags, tag_origin = _risk_tags_for_dpo_row(row)
            if tag_origin == "invalid":
                raise DpoCollectionError("selected train row has invalid DPO risk tags")
            # Metadata-only in-memory overlay.  It preserves the frozen JSONL
            # byte-for-byte while binding the same deterministic tag used at
            # source selection into the later DPO pair seed.
            selected[sample_id] = {
                **row,
                "risk_tags": risk_tags,
                "dpo_risk_tag_derivation": tag_origin,
            }
    if set(selected) != selected_ids:
        raise DpoCollectionError("post-generation Gold read does not cover selected source IDs")
    return selected


def _evaluate_candidate(source_row: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    raw_completion = str(raw["raw_completion"])
    try:
        candidate_sql = unwrap_sql_completion(raw_completion)
    except CandidateSqlGenerationError:
        return {**raw, "eligible": False, "skip_reason": "normalization_error"}
    candidate_sql_sha256 = sha256_text(candidate_sql)
    outcome, frame = _execute_governed(candidate_sql, source_row)
    if outcome.result_contract_valid:
        gold_sql = source_row.get("candidate_sql")
        if not isinstance(gold_sql, str) or sha256_text(gold_sql) != source_row.get("canonical_gold_sql_sha256"):
            raise DpoCollectionError("post-generation source Gold SQL hash drift")
        gold_outcome, gold_frame = _execute_governed(gold_sql, source_row)
        if not gold_outcome.result_contract_valid or gold_frame is None:
            raise DpoCollectionError("admitted Gold SQL no longer passes governed execution")
        from scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit import compare_denotations
        outcome = replace(outcome, gold_denotation_state=compare_denotations(gold_frame, frame))
    try:
        tier = classify_rejected_tier(outcome)
    except ValueError as exc:
        raise DpoCollectionError("incoherent candidate governance evidence") from exc
    evidence = {
        "policy_status": outcome.policy_status,
        "execution_status": outcome.execution_status,
        "result_contract_valid": outcome.result_contract_valid,
        "gold_denotation_state": outcome.gold_denotation_state,
    }
    if tier not in {"A", "B", "C"}:
        return {**raw, "candidate_sql": candidate_sql, "candidate_sql_sha256": candidate_sql_sha256, "evidence": evidence, "eligible": False, "skip_reason": "gold_match" if tier is None else "policy_only"}
    return {**raw, "candidate_sql": candidate_sql, "candidate_sql_sha256": candidate_sql_sha256, "evidence": evidence, "tier": tier, "eligible": True}


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def select_best_rejected(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Choose one auditable A/B/C error per semantic source instance."""

    eligible = [row for row in candidates if row.get("eligible") is True]
    if not eligible:
        return None
    priority = {"A": 0, "B": 1, "C": 2}
    for row in eligible:
        if row.get("tier") not in priority:
            raise DpoCollectionError("eligible candidate has unsupported DPO tier")
        sql = row.get("candidate_sql")
        if not isinstance(sql, str) or row.get("candidate_sql_sha256") != sha256_text(sql):
            raise DpoCollectionError("eligible candidate SQL hash is absent or drifted")
    return min(
        eligible,
        key=lambda item: (priority[str(item["tier"])], str(item["candidate_sql_sha256"])),
    )


def assess_pair_readiness(
    pairs: Sequence[Mapping[str, Any]],
    sources: Sequence[GenerationSource],
    *,
    minimum_pairs_per_workspace: int,
) -> dict[str, Any]:
    """Apply the frozen small-batch stop condition without authorizing DPO.

    This is intentionally stricter than merely counting rows: each workspace
    needs enough A/B/C pairs across at least two shapes, three scenario
    families, and two risk tags.  Passing only means the artifacts are worth a
    separate human pair-quality review; it never starts a DPO Trainer.
    """

    if minimum_pairs_per_workspace <= 0:
        raise DpoCollectionError("minimum pairs per workspace must be positive")
    permitted_ids = {source.sample_id for source in sources}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        source = pair.get("source")
        if not isinstance(source, Mapping):
            raise DpoCollectionError("assembled pair lacks source metadata")
        sample_id = source.get("sample_id")
        workspace = source.get("workspace_key")
        if not isinstance(sample_id, str) or sample_id not in permitted_ids:
            raise DpoCollectionError("assembled pair is not bound to the selected source batch")
        if workspace not in {"olist", "thelook"}:
            raise DpoCollectionError("assembled pair has an unsupported workspace")
        grouped[str(workspace)].append(pair)

    workspaces: dict[str, Any] = {}
    all_ready = True
    for workspace in ("olist", "thelook"):
        workspace_pairs = grouped[workspace]
        families: set[str] = set()
        shapes: set[str] = set()
        tags: set[str] = set()
        tiers: Counter[str] = Counter()
        for pair in workspace_pairs:
            source = pair["source"]
            rejected = pair.get("rejected")
            assert isinstance(source, Mapping)
            families.add(str(source.get("scenario_family_id")))
            shapes.add(str(source.get("result_shape")))
            source_tags = source.get("risk_tags")
            if isinstance(source_tags, list):
                tags.update(str(tag) for tag in source_tags)
            if isinstance(rejected, Mapping):
                tiers.update([str(rejected.get("tier"))])
        stop_reasons: list[str] = []
        if len(workspace_pairs) < minimum_pairs_per_workspace:
            stop_reasons.append("insufficient_A_B_C_pairs")
        if len(families) < min(3, minimum_pairs_per_workspace):
            stop_reasons.append("insufficient_scenario_family_coverage")
        if len(shapes) < min(2, minimum_pairs_per_workspace):
            stop_reasons.append("insufficient_result_shape_coverage")
        if len(tags) < min(2, minimum_pairs_per_workspace):
            stop_reasons.append("insufficient_risk_tag_coverage")
        if stop_reasons:
            all_ready = False
        workspaces[workspace] = {
            "pair_count": len(workspace_pairs),
            "tier_counts": dict(sorted(tiers.items())),
            "scenario_family_count": len(families),
            "result_shapes": sorted(shapes),
            "risk_tags": sorted(tags),
            "stop_reasons": stop_reasons,
        }
    return {
        "minimum_pairs_per_workspace": minimum_pairs_per_workspace,
        "status": "ready_for_pair_quality_review" if all_ready else "stop_no_dpo",
        "dpo_training_authorized": False,
        "workspaces": workspaces,
    }


def collect(args: argparse.Namespace) -> dict[str, Any]:
    import numpy as np
    import torch
    from transformers import set_seed

    if not torch.cuda.is_available():
        raise DpoCollectionError("CUDA is required for parent completion collection")
    if args.samples_per_source <= 0 or args.sources_per_workspace <= 0:
        raise DpoCollectionError("source and completion counts must be positive")
    if args.minimum_pairs_per_workspace > args.sources_per_workspace:
        raise DpoCollectionError("minimum pairs per workspace cannot exceed selected sources")
    if args.max_input_tokens != 3072 or args.max_new_tokens <= 0:
        raise DpoCollectionError("collector must use 3072 input cap and positive completion cap")
    if not 0 < args.temperature <= 2 or not 0 < args.top_p <= 1:
        raise DpoCollectionError("sampling parameters are outside the bounded contract")
    release_dir = ensure_path_outside_repository(args.release_dir, ROOT)
    model_dir = ensure_path_outside_repository(args.model_dir, ROOT)
    adapter_dir = ensure_path_outside_repository(args.parent_adapter_dir, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise DpoCollectionError("collector output directory must be new")
    manifest, train_path = _frozen_train(release_dir)
    sources, source_exclusions = select_generation_sources_with_audit(
        _read_jsonl(train_path), per_workspace=args.sources_per_workspace
    )
    selection = summarize_generation_selection(
        sources,
        per_workspace=args.sources_per_workspace,
        source_exclusions=source_exclusions,
    )
    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    gpu_uuid = gpu_uuid if gpu_uuid.startswith("GPU-") else "GPU-" + gpu_uuid
    if gpu_uuid != args.expected_gpu_uuid:
        raise DpoCollectionError("CUDA UUID differs from explicit collector guard")
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer, model, parent = _load_parent(model_dir, adapter_dir, args.expected_parent_adapter_sha256)
    output_dir.mkdir(parents=True)
    selection_path = output_dir / "selection-manifest.json"
    selection_path.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    raw_rows: list[dict[str, Any]] = []
    # No Gold SQL is read after this point until every raw completion is on disk.
    for source_index, source in enumerate(sources):
        for sample_index in range(args.samples_per_source):
            generation_seed = args.seed + source_index * 10_000 + sample_index
            try:
                raw_completion, tokens, elapsed_ms = _generate_one(
                    tokenizer, model, source.prompt, max_input_tokens=args.max_input_tokens,
                    max_new_tokens=args.max_new_tokens, temperature=args.temperature,
                    top_p=args.top_p, seed=generation_seed,
                )
            except Exception as exc:
                raw_rows.append({"source_sample_id": source.sample_id, "workspace_key": source.workspace_key, "prompt_sha256": source.prompt_sha256, "sample_index": sample_index, "generation_seed": generation_seed, "generation_error": type(exc).__name__})
                continue
            raw_rows.append({"source_sample_id": source.sample_id, "workspace_key": source.workspace_key, "prompt_sha256": source.prompt_sha256, "sample_index": sample_index, "generation_seed": generation_seed, "raw_completion": raw_completion, "raw_completion_sha256": sha256_text(raw_completion), "generated_tokens": tokens, "generation_elapsed_ms": elapsed_ms})
    raw_path = output_dir / "raw-completions.jsonl"
    _write_jsonl(raw_path, raw_rows)
    (output_dir / "generation-completed").write_text("completed\n", encoding="utf-8")

    # Re-validate file-level release identity before the deliberately delayed Gold read.
    _, refreshed_train_path = _frozen_train(release_dir)
    source_rows = _gold_sql_after_generation(refreshed_train_path, {source.sample_id for source in sources})
    evaluated: list[dict[str, Any]] = []
    for raw in raw_rows:
        if "raw_completion" not in raw:
            evaluated.append({**raw, "eligible": False, "skip_reason": "generation_error"})
            continue
        evaluated.append(_evaluate_candidate(source_rows[str(raw["source_sample_id"])], raw))

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluated:
        if row.get("eligible") is True:
            by_source[str(row["source_sample_id"])].append(row)
    pairs: list[dict[str, Any]] = []
    pair_failures: list[str] = []
    for source in sources:
        candidate = select_best_rejected(by_source.get(source.sample_id, []))
        if candidate is None:
            continue
        seed = build_dpo_pair_seed(source_rows[source.sample_id], release_version=manifest["release_version"]).as_dict()
        payload = {
            "source_sample_id": candidate["source_sample_id"], "prompt_sha256": candidate["prompt_sha256"],
            "raw_completion": candidate["raw_completion"], "raw_completion_sha256": candidate["raw_completion_sha256"],
            "candidate_sql": candidate["candidate_sql"], "candidate_sql_sha256": sha256_text(str(candidate["candidate_sql"])),
            "tier": candidate["tier"], "evidence": candidate["evidence"],
            "provenance": {**parent, "decode": {"do_sample": True, "num_beams": 1, "temperature": args.temperature, "top_p": args.top_p, "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens, "base_seed": args.seed}, "generation_seed": candidate["generation_seed"]},
        }
        try:
            pair, _ = attach_rejected_candidate(seed, candidate=payload)
        except DpoPairValidationError as exc:
            pair_failures.append(f"{source.sample_id}:{type(exc).__name__}")
            continue
        pairs.append(pair)
    _write_jsonl(output_dir / "pairs.jsonl", pairs)
    readiness = assess_pair_readiness(
        pairs,
        sources,
        minimum_pairs_per_workspace=args.minimum_pairs_per_workspace,
    )
    # This safe audit deliberately excludes prompt/SQL/raw completion/result rows.
    audit = {
        "schema_version": "multi-schema-sql-dpo-small-batch-audit-v1",
        "status": "completed",
        "source_release": {"version": manifest["release_version"], "train_sha256": sha256_file(train_path)},
        "selection": {
            **selection,
            "samples_per_source": args.samples_per_source,
            "selection_manifest_sha256": sha256_file(selection_path),
        },
        "parent": parent,
        "decode": {"do_sample": True, "num_beams": 1, "temperature": args.temperature, "top_p": args.top_p, "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens, "base_seed": args.seed},
        "gpu": {"cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "physical_nvidia_smi_device": args.physical_nvidia_smi_device, "name": gpu.name, "uuid": gpu_uuid},
        "counts": {"raw_attempts": len(raw_rows), "generation_errors": sum("generation_error" in row for row in raw_rows), "eligible_by_tier": dict(sorted(Counter(str(row.get("tier")) for row in evaluated if row.get("eligible") is True).items())), "skipped_by_reason": dict(sorted(Counter(str(row.get("skip_reason")) for row in evaluated if row.get("eligible") is False).items())), "selected_pairs": len(pairs), "pair_assembly_failures": len(pair_failures)},
        "pair_readiness": readiness,
        "artifacts": {"raw_completions_sha256": sha256_file(raw_path), "pairs_sha256": sha256_file(output_dir / "pairs.jsonl"), "raw_and_pairs_outside_repository": True},
        "boundaries": {"train_only": True, "protected_tests_read": False, "gold_sql_read_after_raw_generation_only": True, "production_default_unchanged": True, "dpo_training_started": False},
    }
    (output_dir / "safe-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--parent-adapter-dir", type=Path, required=True)
    parser.add_argument("--expected-parent-adapter-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sources-per-workspace", type=int, default=6)
    parser.add_argument("--samples-per-source", type=int, default=4)
    parser.add_argument("--minimum-pairs-per-workspace", type=int, default=3)
    parser.add_argument("--max-input-tokens", type=int, default=3072)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        print(json.dumps(collect(args), ensure_ascii=False, sort_keys=True))
    except (DpoCollectionError, DpoPairValidationError, OSError) as exc:
        print(f"DPO candidate collection error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
