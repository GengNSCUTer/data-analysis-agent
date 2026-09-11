#!/usr/bin/env python3
"""Materialize paired Olist SQL/program SFT events outside the Git worktree.

This is the second, offline-only unit of Schema-aware Program SFT.  It reads
the already admitted Olist Release v2 SQL-only *train* and *validation* rows.
Task A is copied byte-for-byte at the Prompt/SQL boundary.  Task B is derived
from the same row's validated QuerySpec as a canonical ``SchemaLinkPlan`` JSON
target.  The command never reads TheLook inputs, calls an LLM, executes SQL,
or trains a model.

The release's final in-domain test is deliberately not an input argument.  A
full admission record file is used only as immutable provenance for the passed
train/validation seed IDs; rows outside those IDs are never materialized.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import (  # noqa: E402
    OLIST_CANDIDATE_SQL_PROMPT_VERSION,
)
from data_analysis_agent.olist_queryspec import (  # noqa: E402
    QuerySpec,
    WorkspacePin,
    validate_query_spec,
)
from data_analysis_agent.olist_schema_link_plan import (  # noqa: E402
    SchemaLinkPlan,
    derive_schema_link_plan,
    validate_schema_link_plan,
)
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402


CONTRACT_VERSION = "olist-schema-aware-program-sft-v1"
TASK_A = "sql"
TASK_B = "schema_link_plan"
DEFAULT_MAX_SEQ_LENGTH = 3072
RELEASE_PATH_MARKER = "olist-domain-sft-release-v2"
PROGRAM_TASK_HEADER = (
    "### Training-only auxiliary task\n"
    "Do not generate SQL for this auxiliary task. Using the server-provided "
    "Catalog, Query Plan, Result Contract, and question below, emit only the "
    "canonical SchemaLinkPlan JSON object for this exact query instance. "
    "Do not add Markdown, prose, or SQL."
)


class SchemaAwareMaterializationError(ValueError):
    """Raised when an external release input violates the frozen contract."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-split-audit", type=Path, required=True)
    parser.add_argument("--source-train-jsonl", type=Path, required=True)
    parser.add_argument("--source-validation-jsonl", type=Path, required=True)
    parser.add_argument("--admission-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-train", type=int, default=2400)
    parser.add_argument("--expected-validation", type=int, default=600)
    parser.add_argument("--max-seq-length", type=int, default=DEFAULT_MAX_SEQ_LENGTH)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaAwareMaterializationError(
            f"{label} is missing or invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SchemaAwareMaterializationError(f"{label} must be a JSON object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SchemaAwareMaterializationError(f"{label} is unreadable: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SchemaAwareMaterializationError(
                f"{label} has invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise SchemaAwareMaterializationError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(value)
    if not rows:
        raise SchemaAwareMaterializationError(f"{label} is empty")
    return rows


def _external_existing(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise SchemaAwareMaterializationError(
            f"{label} must stay outside the Git worktree"
        )
    if not resolved.is_file():
        raise SchemaAwareMaterializationError(f"{label} does not exist: {resolved}")
    return resolved


def _external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise SchemaAwareMaterializationError(
            f"{label} must stay outside the Git worktree"
        )
    if not resolved.is_dir():
        raise SchemaAwareMaterializationError(f"{label} does not exist: {resolved}")
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise SchemaAwareMaterializationError(
            "schema-aware output must stay outside the Git worktree"
        )
    if resolved.exists():
        raise SchemaAwareMaterializationError(
            f"schema-aware output already exists: {resolved}"
        )
    return resolved


def _require_olist_release_path(path: Path, label: str) -> Path:
    resolved = _external_existing(path, label)
    lowered = str(resolved).lower()
    if RELEASE_PATH_MARKER not in lowered or "thelook" in lowered:
        raise SchemaAwareMaterializationError(
            f"{label} is not an allowed Olist Release v2 asset: {resolved}"
        )
    return resolved


def _require_nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaAwareMaterializationError(f"{label} must be non-empty text")
    return value


def _split_name(row: Mapping[str, Any]) -> str:
    split = row.get("split")
    if not isinstance(split, Mapping):
        raise SchemaAwareMaterializationError("source SFT row has no split object")
    return _require_nonempty_text(split.get("name"), "source SFT split name")


def _source_identity(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(
        _require_nonempty_text(row.get(field), f"source SFT {field}")
        for field in (
            "sample_id",
            "seed_id",
            "query_spec_id",
            "family_id",
            "sql_program_id",
        )
    )


def load_source_split(
    path: Path, expected_split: str, expected_rows: int
) -> list[dict[str, Any]]:
    """Load one existing SQL-only split and prove its Task A boundary is intact."""

    path = _require_olist_release_path(path, f"source {expected_split} JSONL")
    rows = _read_jsonl(path, f"source {expected_split} JSONL")
    if len(rows) != expected_rows:
        raise SchemaAwareMaterializationError(
            f"source {expected_split} row count differs: expected {expected_rows}, got {len(rows)}"
        )
    seen_sample_ids: set[str] = set()
    seen_seed_ids: set[str] = set()
    for row in rows:
        if _split_name(row) != expected_split:
            raise SchemaAwareMaterializationError(
                f"source {expected_split} contains a different split"
            )
        sample_id, seed_id, _, _, _ = _source_identity(row)
        if sample_id in seen_sample_ids or seed_id in seen_seed_ids:
            raise SchemaAwareMaterializationError(
                f"source {expected_split} has duplicate sample or seed identity"
            )
        seen_sample_ids.add(sample_id)
        seen_seed_ids.add(seed_id)
        if row.get("prompt_format_version") != OLIST_CANDIDATE_SQL_PROMPT_VERSION:
            raise SchemaAwareMaterializationError(
                "source SFT row has unexpected production prompt version"
            )
        if (
            row.get("admission_status") != "admitted"
            or row.get("execution_outcome", {}).get("postgres_reader_result_contract")
            != "pass"
        ):
            raise SchemaAwareMaterializationError(
                "source SFT row lacks admitted PostgreSQL evidence"
            )
        prompt = _require_nonempty_text(
            row.get("rendered_prompt"), "source rendered_prompt"
        )
        sql = _require_nonempty_text(row.get("candidate_sql"), "source candidate_sql")
        if (
            not prompt.endswith("### SQL")
            or row.get("training_text") != prompt + "\n" + sql
        ):
            raise SchemaAwareMaterializationError(
                "source SFT runtime Prompt and SQL target boundary has drifted"
            )
    return rows


def _load_source_audit(
    path: Path, train_path: Path, validation_path: Path, expected: Mapping[str, int]
) -> dict[str, Any]:
    path = _require_olist_release_path(path, "source split audit")
    audit = _read_json(path, "source split audit")
    if audit.get("audit_version") != "olist-release-sft-v2":
        raise SchemaAwareMaterializationError(
            "source split audit has an unsupported version"
        )
    if audit.get("checks", {}).get("status") != "pass":
        raise SchemaAwareMaterializationError("source split audit did not pass")
    policy = audit.get("policy", {})
    if (
        not isinstance(policy, Mapping)
        or policy.get("test_forbidden_for_training") is not True
    ):
        raise SchemaAwareMaterializationError(
            "source split audit does not isolate the final test"
        )
    splits = audit.get("splits")
    if not isinstance(splits, Mapping):
        raise SchemaAwareMaterializationError(
            "source split audit has no split evidence"
        )
    for split, source_path in (("train", train_path), ("validation", validation_path)):
        evidence = splits.get(split)
        if not isinstance(evidence, Mapping):
            raise SchemaAwareMaterializationError(
                f"source split audit has no {split} evidence"
            )
        if evidence.get("rows") != expected[split] or evidence.get(
            "sha256"
        ) != sha256_file(source_path):
            raise SchemaAwareMaterializationError(
                f"source {split} does not match its release audit"
            )
    return audit


def _load_admission_records(
    admission_dir: Path, expected_manifest_sha256: str
) -> dict[str, dict[str, Any]]:
    directory = _external_existing_dir(admission_dir, "admission directory")
    lowered = str(directory).lower()
    if RELEASE_PATH_MARKER not in lowered or "thelook" in lowered:
        raise SchemaAwareMaterializationError(
            "admission directory is not an allowed Olist Release v2 asset"
        )
    manifest_path = _external_existing(
        directory / "admission_assembly_manifest.json", "admission manifest"
    )
    records_path = _external_existing(
        directory / "admitted_records.jsonl", "admitted records"
    )
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise SchemaAwareMaterializationError(
            "admission manifest does not match the source split audit"
        )
    manifest = _read_json(manifest_path, "admission manifest")
    if manifest.get("checks", {}).get("status") != "pass":
        raise SchemaAwareMaterializationError("admission manifest did not pass")
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    if not isinstance(output, Mapping) or output.get("sha256") != sha256_file(
        records_path
    ):
        raise SchemaAwareMaterializationError(
            "admitted records do not match admission manifest"
        )
    result: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(records_path, "admitted records"):
        seed_id = _require_nonempty_text(row.get("seed_id"), "admission seed_id")
        if seed_id in result:
            raise SchemaAwareMaterializationError(
                "admitted records have duplicate seed IDs"
            )
        result[seed_id] = row
    return result


def load_tokenizer(path: Path) -> Any:
    from transformers import AutoTokenizer

    directory = _external_existing_dir(path, "tokenizer directory")
    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True)
    if tokenizer.eos_token_id is None:
        raise SchemaAwareMaterializationError("tokenizer must define an EOS token")
    return tokenizer


def render_program_prompt(runtime_prompt: str) -> str:
    """Build a compact Task B context without mutating the Task A prompt.

    Only the semantic Catalog, Query Plan, and Question sections are needed to
    supervise structural linking.  Dropping the repeated SQL-generation
    preamble both avoids contradictory ``### SQL`` selectors and keeps the
    auxiliary event inside the no-truncation budget.
    """

    if not runtime_prompt.endswith("### SQL"):
        raise SchemaAwareMaterializationError(
            "Task B source runtime prompt must end in ### SQL"
        )
    catalog_marker = "### Server-provided Semantic Catalog"
    plan_marker = "### Server-provided Query Plan"
    question_marker = "### Question"
    catalog_start = runtime_prompt.find(catalog_marker)
    plan_start = runtime_prompt.find(plan_marker)
    question_start = runtime_prompt.find(question_marker)
    if not (0 <= catalog_start < plan_start < question_start):
        raise SchemaAwareMaterializationError(
            "Task B source runtime prompt lacks ordered Catalog/Query Plan/Question sections"
        )
    catalog = runtime_prompt[catalog_start:plan_start].strip()
    plan = runtime_prompt[plan_start:question_start].strip()
    question = runtime_prompt[question_start : -len("### SQL")].strip()
    if not catalog or not plan or not question:
        raise SchemaAwareMaterializationError(
            "Task B source runtime sections are empty"
        )
    return "\n\n".join(
        (PROGRAM_TASK_HEADER, catalog, plan, question, "### SchemaLinkPlan JSON")
    )


def serialize_schema_link_plan(plan: SchemaLinkPlan) -> str:
    """Serialize the complete ID-bearing plan target in canonical key order."""

    return _canonical_json(plan.as_dict())


def _token_length(tokenizer: Any, prompt: str, target: str) -> dict[str, int]:
    prompt_tokens = len(tokenizer(prompt + "\n", add_special_tokens=False)["input_ids"])
    target_plus_eos_tokens = (
        len(tokenizer(target, add_special_tokens=False)["input_ids"]) + 1
    )
    return {
        "prompt_tokens": prompt_tokens,
        "target_plus_eos_tokens": target_plus_eos_tokens,
        "sequence_tokens": prompt_tokens + target_plus_eos_tokens,
    }


def _source_provenance(
    source: Mapping[str, Any], admission: Mapping[str, Any]
) -> dict[str, str]:
    prompt = _require_nonempty_text(source.get("rendered_prompt"), "source prompt")
    sql = _require_nonempty_text(source.get("candidate_sql"), "source SQL")
    return {
        "source_sample_id": _require_nonempty_text(
            source.get("sample_id"), "source sample_id"
        ),
        "source_seed_id": _require_nonempty_text(
            source.get("seed_id"), "source seed_id"
        ),
        "source_query_spec_id": _require_nonempty_text(
            source.get("query_spec_id"), "source query_spec_id"
        ),
        "source_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "source_gold_sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "admission_gold_sql_sha256": _require_nonempty_text(
            admission.get("gold_sql_sha256"), "admission gold_sql_sha256"
        ),
    }


def _validate_source_admission_pair(
    source: Mapping[str, Any],
    admission: Mapping[str, Any],
    expected_split: str,
    catalog: Catalog,
) -> QuerySpec:
    if (
        admission.get("admission_status") != "admitted"
        or admission.get("split") != expected_split
    ):
        raise SchemaAwareMaterializationError(
            "source row does not have an admitted matching split"
        )
    for field in ("family_id", "sql_program_id"):
        if source.get(field) != admission.get(field):
            raise SchemaAwareMaterializationError(
                f"source/admission {field} identity drift"
            )
    source_sql = _require_nonempty_text(
        source.get("candidate_sql"), "source candidate_sql"
    )
    admission_sql = _require_nonempty_text(
        admission.get("gold_sql"), "admission gold_sql"
    )
    if source_sql != admission_sql:
        raise SchemaAwareMaterializationError(
            "source SQL target differs from admitted Gold SQL"
        )
    expected_hash = hashlib.sha256(source_sql.encode("utf-8")).hexdigest()
    if admission.get("gold_sql_sha256") != expected_hash:
        raise SchemaAwareMaterializationError(
            "admission Gold SQL hash differs from source target"
        )
    raw_spec = admission.get("query_spec")
    if not isinstance(raw_spec, Mapping):
        raise SchemaAwareMaterializationError("admission row has no QuerySpec")
    try:
        spec = validate_query_spec(QuerySpec.from_mapping(raw_spec), catalog)
    except Exception as exc:
        raise SchemaAwareMaterializationError(
            "admission QuerySpec does not validate"
        ) from exc
    if source.get("query_spec_id") != spec.query_spec_id:
        raise SchemaAwareMaterializationError(
            "source/admission QuerySpec identity drift"
        )
    return spec


def build_events(
    source_by_split: Mapping[str, list[dict[str, Any]]],
    admissions_by_seed: Mapping[str, dict[str, Any]],
    tokenizer: Any,
    max_seq_length: int,
    *,
    catalog: Catalog,
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Build paired events and record overlength items without truncation.

    The return is ``events[split][task]``, hash-only pairing rows, and external
    length exclusions.  An overlength record is absent from its task event
    output, but remains in pairing/exclusion evidence so a later Trainer audit
    will fail closed instead of silently training unpaired data.
    """

    if max_seq_length <= 0:
        raise SchemaAwareMaterializationError("max_seq_length must be positive")
    if set(source_by_split) != {"train", "validation"}:
        raise SchemaAwareMaterializationError(
            "only train and validation source splits are allowed"
        )
    events = {
        split: {TASK_A: [], TASK_B: [], "interleaved": []}
        for split in ("train", "validation")
    }
    pairing: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    global_sample_ids: set[str] = set()
    families_by_split: dict[str, set[str]] = {}
    query_specs_by_split: dict[str, set[str]] = {}
    for split in ("train", "validation"):
        families_by_split[split] = {
            _require_nonempty_text(row.get("family_id"), "source family_id")
            for row in source_by_split[split]
        }
        query_specs_by_split[split] = {
            _require_nonempty_text(row.get("query_spec_id"), "source query_spec_id")
            for row in source_by_split[split]
        }
    if families_by_split["train"] & families_by_split["validation"]:
        raise SchemaAwareMaterializationError(
            "source family IDs cross train and validation"
        )
    if query_specs_by_split["train"] & query_specs_by_split["validation"]:
        raise SchemaAwareMaterializationError(
            "source QuerySpec IDs cross train and validation"
        )
    for split in ("train", "validation"):
        for source in sorted(
            source_by_split[split], key=lambda row: str(row.get("sample_id"))
        ):
            sample_id, seed_id, query_spec_id, family_id, sql_program_id = (
                _source_identity(source)
            )
            if sample_id in global_sample_ids:
                raise SchemaAwareMaterializationError(
                    "source sample ID occurs in more than one split"
                )
            global_sample_ids.add(sample_id)
            admission = admissions_by_seed.get(seed_id)
            if admission is None:
                raise SchemaAwareMaterializationError(
                    "source seed has no admitted provenance row"
                )
            spec = _validate_source_admission_pair(source, admission, split, catalog)
            plan = derive_schema_link_plan(spec, catalog)
            validate_schema_link_plan(plan, spec, catalog)
            provenance = _source_provenance(source, admission)
            task_a_prompt = _require_nonempty_text(
                source.get("rendered_prompt"), "Task A prompt"
            )
            task_a_target = _require_nonempty_text(
                source.get("candidate_sql"), "Task A target"
            )
            task_b_prompt = render_program_prompt(task_a_prompt)
            task_b_target = serialize_schema_link_plan(plan)
            if "sql" in json.loads(task_b_target):
                raise SchemaAwareMaterializationError(
                    "SchemaLinkPlan target unexpectedly contains SQL"
                )
            event_specs = (
                (
                    TASK_A,
                    task_a_prompt,
                    task_a_target,
                    source.get("prompt_format_version"),
                ),
                (TASK_B, task_b_prompt, task_b_target, CONTRACT_VERSION),
            )
            pair_id = (
                "sap_"
                + hashlib.sha256(
                    f"{CONTRACT_VERSION}:{sample_id}".encode("utf-8")
                ).hexdigest()[:24]
            )
            pair_event_ids: dict[str, str] = {}
            pair_eligible: dict[str, bool] = {}
            for task_type, prompt, target, prompt_version in event_specs:
                length = _token_length(tokenizer, prompt, target)
                if task_type == TASK_A:
                    source_length = source.get("token_length")
                    if (
                        isinstance(source_length, Mapping)
                        and dict(source_length) != length
                    ):
                        raise SchemaAwareMaterializationError(
                            "source Task A token length evidence differs from exact runtime bytes"
                        )
                event_id = f"{pair_id}:{task_type}"
                pair_event_ids[task_type] = event_id
                eligible = length["sequence_tokens"] <= max_seq_length
                pair_eligible[task_type] = eligible
                if not eligible:
                    exclusions.append(
                        {
                            "event_id": event_id,
                            "pair_id": pair_id,
                            "task_type": task_type,
                            "split": split,
                            "source_sample_id": sample_id,
                            "source_seed_id": seed_id,
                            "sequence_tokens": length["sequence_tokens"],
                            "prompt_tokens": length["prompt_tokens"],
                            "target_plus_eos_tokens": length["target_plus_eos_tokens"],
                            "max_seq_length": max_seq_length,
                            "reason": "sequence_exceeds_frozen_contract",
                            "contains_question_or_sql": False,
                        }
                    )
                    continue
                event = {
                    "event_id": event_id,
                    "pair_id": pair_id,
                    "task_type": task_type,
                    "split": {"name": split},
                    "prompt_format_version": prompt_version,
                    "source": provenance,
                    "source_family_id": family_id,
                    "source_sql_program_id": sql_program_id,
                    "source_query_spec_id": query_spec_id,
                    "rendered_prompt": prompt,
                    "target_text": target,
                    "training_text": prompt + "\n" + target,
                    "token_length": length,
                }
                if task_type == TASK_A:
                    # Retain training-loader fields while preserving the exact
                    # original Prompt -> SQL bytes, rather than regenerating them.
                    event.update(
                        {
                            "sample_id": sample_id,
                            "seed_id": seed_id,
                            "family_id": family_id,
                            "sql_program_id": sql_program_id,
                            "query_spec_id": query_spec_id,
                            "candidate_sql": task_a_target,
                            "admission_status": "admitted",
                            "execution_outcome": {
                                "postgres_reader_result_contract": "pass"
                            },
                            "source_training_text_sha256": hashlib.sha256(
                                _require_nonempty_text(
                                    source.get("training_text"), "source training_text"
                                ).encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                else:
                    event.update(
                        {
                            "schema_link_plan_id": plan.schema_link_plan_id,
                            "schema_link_registry_version": plan.registry_version,
                            "schema_link_plan_sha256": hashlib.sha256(
                                task_b_target.encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                events[split][task_type].append(event)
            pairing.append(
                {
                    "pair_id": pair_id,
                    "split": split,
                    "source_sample_id": sample_id,
                    "source_seed_id": seed_id,
                    "source_query_spec_id": query_spec_id,
                    "source_family_id": family_id,
                    "task_a_event_id": pair_event_ids[TASK_A],
                    "task_b_event_id": pair_event_ids[TASK_B],
                    "task_a_eligible": pair_eligible[TASK_A],
                    "task_b_eligible": pair_eligible[TASK_B],
                    "schema_link_plan_id": plan.schema_link_plan_id,
                    "source_prompt_sha256": provenance["source_prompt_sha256"],
                    "source_gold_sql_sha256": provenance["source_gold_sql_sha256"],
                }
            )
        events[split]["interleaved"] = [
            event
            for pair_id in sorted({event["pair_id"] for event in events[split][TASK_A]})
            for event in sorted(
                (
                    item
                    for task in (TASK_A, TASK_B)
                    for item in events[split][task]
                    if item["pair_id"] == pair_id
                ),
                key=lambda item: 0 if item["task_type"] == TASK_A else 1,
            )
        ]
    return events, pairing, exclusions


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
            )


def materialize(
    source_audit_path: Path,
    source_train_path: Path,
    source_validation_path: Path,
    admission_dir: Path,
    tokenizer_dir: Path,
    output_dir: Path,
    *,
    expected_train: int,
    expected_validation: int,
    max_seq_length: int,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Materialize a fresh external pairing release; never trains or executes SQL."""

    if expected_train <= 0 or expected_validation <= 0:
        raise SchemaAwareMaterializationError(
            "expected train and validation counts must be positive"
        )
    output = _external_new_dir(output_dir)
    train_path = _require_olist_release_path(source_train_path, "source train JSONL")
    validation_path = _require_olist_release_path(
        source_validation_path, "source validation JSONL"
    )
    expected = {"train": expected_train, "validation": expected_validation}
    source_audit = _load_source_audit(
        source_audit_path, train_path, validation_path, expected
    )
    source_by_split = {
        "train": load_source_split(train_path, "train", expected_train),
        "validation": load_source_split(
            validation_path, "validation", expected_validation
        ),
    }
    admission_manifest_sha256 = source_audit.get("source", {}).get(
        "admission_assembly_manifest_sha256"
    )
    if (
        not isinstance(admission_manifest_sha256, str)
        or len(admission_manifest_sha256) != 64
    ):
        raise SchemaAwareMaterializationError(
            "source audit lacks admission manifest provenance"
        )
    admissions = _load_admission_records(admission_dir, admission_manifest_sha256)
    tokenizer = load_tokenizer(tokenizer_dir)
    catalog = CatalogLoader().load()
    events, pairing, exclusions = build_events(
        source_by_split, admissions, tokenizer, max_seq_length, catalog=catalog
    )
    generated_at = (
        generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        (staging / "exclusions").mkdir()
        staged_paths: dict[str, Path] = {}
        for split in ("train", "validation"):
            for task, name in (
                (TASK_A, "task_a"),
                (TASK_B, "task_b"),
                ("interleaved", "events"),
            ):
                path = staging / f"{split}_{name}.jsonl"
                _write_jsonl(path, events[split][task])
                staged_paths[f"{split}_{name}"] = path
        pairing_path = staging / "pairing.jsonl"
        exclusion_path = staging / "exclusions" / "length.jsonl"
        _write_jsonl(pairing_path, pairing)
        _write_jsonl(exclusion_path, exclusions)
        paired_count = sum(
            item["task_a_eligible"] and item["task_b_eligible"] for item in pairing
        )
        split_metadata: dict[str, Any] = {}
        for split in ("train", "validation"):
            task_a = events[split][TASK_A]
            task_b = events[split][TASK_B]
            split_metadata[split] = {
                "query_instances": len(source_by_split[split]),
                "families": len({row["family_id"] for row in source_by_split[split]}),
                "task_a_events": len(task_a),
                "task_b_events": len(task_b),
                "interleaved_events": len(events[split]["interleaved"]),
                "task_a_sha256": sha256_file(staged_paths[f"{split}_task_a"]),
                "task_b_sha256": sha256_file(staged_paths[f"{split}_task_b"]),
                "events_sha256": sha256_file(staged_paths[f"{split}_events"]),
                "max_task_a_tokens": max(
                    row["token_length"]["sequence_tokens"] for row in task_a
                ),
                "max_task_b_tokens": max(
                    row["token_length"]["sequence_tokens"] for row in task_b
                )
                if task_b
                else None,
            }
        audit = {
            "audit_version": CONTRACT_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current().as_dict(),
            "source": {
                "source_split_audit_sha256": sha256_file(
                    _require_olist_release_path(source_audit_path, "source split audit")
                ),
                "source_train_sha256": sha256_file(train_path),
                "source_validation_sha256": sha256_file(validation_path),
                "admission_manifest_sha256": admission_manifest_sha256,
                "admission_records_sha256": sha256_file(
                    _external_existing(
                        Path(admission_dir) / "admitted_records.jsonl",
                        "admitted records",
                    )
                ),
            },
            "tokenizer": {
                "dir": str(Path(tokenizer_dir).resolve()),
                "eos_token_id": tokenizer.eos_token_id,
            },
            "training_length_contract": {
                "max_seq_length": max_seq_length,
                "task_a_formula": "exact source runtime prompt + source canonical SQL + EOS",
                "task_b_formula": "training-only task selector + same source runtime context + canonical SchemaLinkPlan JSON + EOS",
                "silent_truncation": False,
            },
            "policy": {
                "source_release": "olist-domain-sft-release-v2",
                "source_splits": ["train", "validation"],
                "in_domain_test_materialized": False,
                "thelook_read": False,
                "task_a_runtime_prompt_unchanged": True,
                "task_b_is_training_only": True,
                "sql_and_plan_targets_separate": True,
                "training_event_order": "stable_pair_then_task_a_task_b",
            },
            "splits": split_metadata,
            "pairing": {
                "rows": len(pairing),
                "sha256": sha256_file(pairing_path),
                "fully_eligible_pairs": paired_count,
                "expected_pairs": expected_train + expected_validation,
            },
            "exclusions": {
                "rows": len(exclusions),
                "sha256": sha256_file(exclusion_path),
                "path": str(output / "exclusions" / "length.jsonl"),
                "contains_question_or_sql": False,
            },
            "checks": {
                "status": "pass"
                if not exclusions and paired_count == len(pairing)
                else "blocked_by_length_exclusions",
                "task_a_prompt_sql_bytes_preserved": True,
                "all_task_b_plans_rederived_and_validated": True,
                "all_task_a_task_b_pairs_present": not exclusions
                and paired_count == len(pairing),
                "in_domain_test_used_for_training": False,
                "thelook_read": False,
                "sql_executed": False,
                "model_called": False,
                "gpu_used": False,
            },
            "outputs": {
                name: str(output / path.name) for name, path in staged_paths.items()
            },
        }
        (staging / "materialization_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return audit


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = materialize(
        args.source_split_audit,
        args.source_train_jsonl,
        args.source_validation_jsonl,
        args.admission_dir,
        args.tokenizer_dir,
        args.output_dir,
        expected_train=args.expected_train,
        expected_validation=args.expected_validation,
        max_seq_length=args.max_seq_length,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
