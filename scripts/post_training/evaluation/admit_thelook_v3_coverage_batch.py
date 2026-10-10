#!/usr/bin/env python3
"""Run a deterministic per-family TheLook v3 Gold admission batch.

This is a pre-materialization release gate, not a model evaluation.  It selects
representative static coverage seeds and runs exactly:

    QuerySpec -> Gold renderer -> SqlPolicy -> reader role
    -> ResultContract / ResultValidator -> 200-row display budget

Detailed SQL and result evidence are written only outside the Git worktree.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v2_queryspec import (  # noqa: E402
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)
from data_analysis_agent.thelook_v2_renderer import render_thelook_v2_gold_sql  # noqa: E402
from data_analysis_agent.thelook_v3_coverage import (  # noqa: E402
    TheLookV3CoverageSeed,
    load_thelook_v3_coverage_contract,
    validate_thelook_v3_coverage_seed,
)
from data_analysis_agent.thelook_v3_release import (  # noqa: E402
    assert_thelook_v3_gold_uses_analytics_views_only,
    build_thelook_v3_result_contract,
    validate_thelook_v3_result,
)


ADMISSION_VERSION = "thelook-v3-coverage-admission-batch-v1"
DEFAULT_SEEDS = ROOT / "data" / "fixtures" / "thelook_v3_coverage_seeds_v2.jsonl"


class TheLookV3AdmissionError(RuntimeError):
    """The admission batch cannot produce trustworthy release evidence."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds-jsonl", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-family", type=int, default=2)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_seeds(path: Path) -> list[TheLookV3CoverageSeed]:
    """Read and validate every static seed before selecting the pilot slice."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TheLookV3AdmissionError(f"seed fixture is unavailable: {path}") from exc
    contract = load_thelook_v3_coverage_contract()
    seeds: list[TheLookV3CoverageSeed] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TheLookV3AdmissionError(
                f"seed fixture has invalid JSON at line {line_number}"
            ) from exc
        seeds.append(validate_thelook_v3_coverage_seed(raw, contract))
    if len(seeds) != int(contract["release"]["target_cases"]):
        raise TheLookV3AdmissionError("seed fixture row count does not match v3 contract")
    return seeds


def select_per_family(
    seeds: Iterable[TheLookV3CoverageSeed], *, per_family: int
) -> list[TheLookV3CoverageSeed]:
    """Select a reproducible, structure-diverse admission slice per family.

    The approved snapshot family has exactly four legal QuerySpecs, so all four
    are selected.  Other families use the smallest stable hashes, while the
    selection key first spreads across result shape and metric multiplicity.
    """

    if per_family <= 0:
        raise ValueError("per_family must be positive")
    by_family: dict[str, list[TheLookV3CoverageSeed]] = defaultdict(list)
    for seed in seeds:
        by_family[seed.scenario_family_id].append(seed)
    selected: list[TheLookV3CoverageSeed] = []
    for family_id in sorted(by_family):
        family = by_family[family_id]
        target = len(family) if family_id == "inventory_unsold_snapshot" else min(per_family, len(family))
        ranked = sorted(
            family,
            key=lambda seed: (
                seed.query_spec.result_shape,
                len(seed.query_spec.metric_ids),
                hashlib.sha256(seed.seed_id.encode("utf-8")).hexdigest(),
            ),
        )
        chosen: list[TheLookV3CoverageSeed] = []
        seen_shape: set[str] = set()
        for seed in ranked:
            if seed.query_spec.result_shape not in seen_shape and len(chosen) < target:
                chosen.append(seed)
                seen_shape.add(seed.query_spec.result_shape)
        for seed in ranked:
            if len(chosen) >= target:
                break
            if seed not in chosen:
                chosen.append(seed)
        if len(chosen) != target:
            raise TheLookV3AdmissionError(f"cannot select {target} rows for {family_id}")
        selected.extend(chosen)
    return sorted(selected, key=lambda seed: seed.seed_id)


def execute_as_reader(sql: str) -> pd.DataFrame:
    """Execute a policy-normalized query under the isolated TheLook reader role."""

    connection = psycopg2.connect(
        host="/tmp", port=35434, database="thelook_analytics", user="postgres"
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(f"SET LOCAL ROLE {THELOOK_V2_WORKSPACE.reader_role}")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(sql)
            rows = [dict(row) for row in cursor.fetchmany(200)]
    finally:
        connection.close()
    return pd.DataFrame(rows)


def admit_seed(
    seed: TheLookV3CoverageSeed,
    *,
    catalog: Catalog,
    policy: SqlPolicy,
    execute: Callable[[str], pd.DataFrame],
) -> dict[str, Any]:
    """Run one seed through all deterministic pre-materialization gates."""

    spec = validate_thelook_v2_query_spec(seed.query_spec, catalog)
    record: dict[str, Any] = {
        "seed_id": seed.seed_id,
        "scenario_family_id": seed.scenario_family_id,
        "program_signature": seed.program_signature,
        "risk_tags": list(seed.risk_tags),
        "window_candidate": seed.window_candidate,
        "query_spec": spec.as_dict(),
        "admission_status": "rejected",
    }
    try:
        artifact = render_thelook_v2_gold_sql(spec, catalog)
        if artifact.sql_sha256 != sha256_text(artifact.sql):
            raise TheLookV3AdmissionError("renderer SQL hash does not match SQL bytes")
        assert_thelook_v3_gold_uses_analytics_views_only(artifact.sql)
        decision = policy.evaluate(artifact.sql, role="analyst")
        frame = execute(decision.final_sql)
        result_contract = build_thelook_v3_result_contract(spec, catalog)
        validation = validate_thelook_v3_result(
            frame,
            result_contract,
            time_bucket_grain=spec.time.grain,
        )
        record.update(
            {
                "gold_sql": artifact.sql,
                "gold_sql_sha256": artifact.sql_sha256,
                "gold_renderer_version": artifact.renderer_version,
                "policy_status": decision.status,
                "policy_final_sql_sha256": sha256_text(decision.final_sql),
                "policy_limit_applied": decision.policy_limit_applied,
                "policy_row_limit": decision.row_limit,
                "reader_role": THELOOK_V2_WORKSPACE.reader_role,
                "result_contract": result_contract.as_evidence(),
                "result_validation": validation.as_dict(),
                "admission_status": "admitted" if validation.safe_to_answer else "rejected",
            }
        )
        if not validation.safe_to_answer:
            record["failure_type"] = "ResultValidation"
    except (OSError, ValueError, PolicyViolation, psycopg2.Error) as exc:
        record["failure_type"] = type(exc).__name__
        record["failure_reason"] = str(exc)[:500]
    return record


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise TheLookV3AdmissionError("admission output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def admit_batch(
    seeds_path: Path,
    output_dir: Path,
    *,
    per_family: int = 2,
    generated_at: str | None = None,
    execute: Callable[[str], pd.DataFrame] = execute_as_reader,
) -> dict[str, Any]:
    """Admit a representative seed batch and preserve all release evidence."""

    output_path = _external_new_dir(output_dir)
    seeds = load_seeds(seeds_path)
    selected = select_per_family(seeds, per_family=per_family)
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    records = [admit_seed(seed, catalog=catalog, policy=policy, execute=execute) for seed in selected]
    selected_by_family = Counter(row["scenario_family_id"] for row in records)
    admitted_by_family = Counter(
        row["scenario_family_id"]
        for row in records
        if row["admission_status"] == "admitted"
    )
    failure_types = Counter(
        str(row.get("failure_type", "unknown"))
        for row in records
        if row["admission_status"] != "admitted"
    )
    all_families_passed = selected_by_family == admitted_by_family
    generated = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        records_path = staging / "admission_records.jsonl"
        records_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        aggregate = {
            "admission_version": ADMISSION_VERSION,
            "generated_at": generated,
            "workspace": {
                "workspace_id": THELOOK_V2_WORKSPACE.workspace_id,
                "catalog_version": THELOOK_V2_WORKSPACE.catalog_version,
                "dataset_version": THELOOK_V2_WORKSPACE.dataset_version,
                "metric_version": THELOOK_V2_WORKSPACE.metric_version,
                "policy_version": THELOOK_V2_WORKSPACE.policy_version,
            },
            "source": {
                "seed_fixture": str(seeds_path.resolve()),
                "seed_fixture_sha256": sha256_file(seeds_path),
                "per_family": per_family,
            },
            "output": {
                "admission_records_jsonl": {
                    "rows": len(records),
                    "sha256": sha256_file(records_path),
                }
            },
            "counts": {
                "selected": len(records),
                "admitted": sum(row["admission_status"] == "admitted" for row in records),
                "rejected": sum(row["admission_status"] != "admitted" for row in records),
                "selected_by_family": dict(sorted(selected_by_family.items())),
                "admitted_by_family": dict(sorted(admitted_by_family.items())),
                "failure_types": dict(sorted(failure_types.items())),
            },
            "checks": {
                "status": "pass" if all_families_passed else "failed",
                "every_scenario_family_selected": len(selected_by_family) == 21,
                "all_selected_rows_admitted": all_families_passed,
                "gold_sql_hashes_recorded": all(
                    row.get("gold_sql_sha256") for row in records if row["admission_status"] == "admitted"
                ),
                "reader_role_execution_evidence_recorded": all(
                    row.get("reader_role") == THELOOK_V2_WORKSPACE.reader_role
                    for row in records
                    if row["admission_status"] == "admitted"
                ),
                "result_contract_and_validator_recorded": all(
                    row.get("result_contract") and row.get("result_validation")
                    for row in records
                    if row["admission_status"] == "admitted"
                ),
                "max_display_rows": 200,
                "questions_or_model_output_materialized": False,
            },
        }
        (staging / "admission_aggregate.json").write_text(
            json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return aggregate


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = admit_batch(
        args.seeds_jsonl,
        args.output_dir,
        per_family=args.per_family,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["checks"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
