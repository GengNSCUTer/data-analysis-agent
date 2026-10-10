#!/usr/bin/env python3
"""Materialize deterministic Gold SQL for TheLook SFT seeds.

This stage renders and statically audits Gold SQL only.  It does not execute
SQL, call an LLM, or apply the reader-role admission gate; those evidences are
written by ``admit_thelook_sft_batch.py`` in the following stage.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import uuid

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v2_renderer import render_thelook_v2_gold_sql  # noqa: E402
from data_analysis_agent.thelook_sft_coverage import (  # noqa: E402
    THELOOK_SFT_SEED_VERSION,
    load_thelook_sft_contract,
    validate_thelook_sft_seed,
)
from data_analysis_agent.thelook_v3_release import assert_thelook_v3_gold_uses_analytics_views_only  # noqa: E402


MATERIALIZATION_VERSION = "thelook-sql-only-sft-gold-v1"
DEFAULT_SEEDS = Path("/disk2/gengnan/data-analysis-agent-data/evals/thelook-sql-only-sft-v1-r4/seeds.jsonl")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_records(path: Path, contract: Mapping[str, Any], catalog: Any) -> list[Any]:
    rows: list[Any] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            rows.append(validate_thelook_sft_seed(raw, contract, catalog))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid TheLook SFT seed at line {line_no}: {exc}") from exc
    return rows


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("SFT Gold output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def materialize(
    seeds_path: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    output = _external_new_dir(output_dir)
    contract = load_thelook_sft_contract()
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    seeds = _load_records(seeds_path, contract, catalog)
    target = int(contract["targets"]["train_samples"]) + int(contract["targets"]["validation_samples"])
    if len(seeds) != target:
        raise ValueError(f"expected {target} seeds, found {len(seeds)}")
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    records_by_split: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}
    failures: list[dict[str, Any]] = []
    for seed in seeds:
        try:
            artifact = render_thelook_v2_gold_sql(seed.query_spec, catalog)
            if artifact.sql_sha256 != sha256_text(artifact.sql):
                raise ValueError("renderer SQL hash mismatch")
            assert_thelook_v3_gold_uses_analytics_views_only(artifact.sql)
            decision = policy.evaluate(artifact.sql, role="analyst")
            row = {
                "seed_id": seed.seed_id,
                "split": seed.split,
                "program_id": seed.program_id,
                "variant_index": seed.variant_index,
                "scenario_family_id": seed.scenario_family_id,
                "program_signature": seed.program_signature,
                "risk_tags": list(seed.risk_tags),
                "window_candidate": seed.window_candidate,
                "query_spec": seed.query_spec.as_dict(),
                "gold_sql": artifact.sql,
                "gold_sql_sha256": artifact.sql_sha256,
                "gold_renderer_version": artifact.renderer_version,
                "gold_evidence": dict(artifact.evidence),
                "static_policy_status": decision.status,
                "static_policy_final_sql_sha256": sha256_text(decision.final_sql),
                "static_policy_row_limit": decision.row_limit,
                "static_policy_limit_applied": decision.policy_limit_applied,
                "database_executed": False,
                "admission_status": "pending",
            }
            records_by_split[seed.split].append(row)
        except Exception as exc:  # preserve an auditable exclusion, never substitute a seed
            failures.append({"seed_id": seed.seed_id, "split": seed.split, "failure_type": type(exc).__name__, "failure_reason": str(exc)[:500]})
    if failures:
        raise ValueError(f"Gold materialization rejected {len(failures)} seeds; see failures: {failures[:2]}")

    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        file_info: dict[str, Any] = {}
        for split, rows in records_by_split.items():
            path = staging / f"{split}_gold.jsonl"
            path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
            file_info[f"{split}_gold.jsonl"] = {"rows": len(rows), "sha256": sha256_file(path)}
        manifest = {
            "materialization_version": MATERIALIZATION_VERSION,
            "generated_at": generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source": {"seed_fixture": str(seeds_path.resolve()), "seed_fixture_sha256": sha256_file(seeds_path), "seed_schema_version": THELOOK_SFT_SEED_VERSION},
            "workspace": {"workspace_id": THELOOK_V2_WORKSPACE.workspace_id, "catalog_version": THELOOK_V2_WORKSPACE.catalog_version, "dataset_version": THELOOK_V2_WORKSPACE.dataset_version, "metric_version": THELOOK_V2_WORKSPACE.metric_version, "policy_version": THELOOK_V2_WORKSPACE.policy_version},
            "counts": {"seeds": len(seeds), "train": len(records_by_split["train"]), "validation": len(records_by_split["validation"]), "failures": 0},
            "output": file_info,
            "checks": {"sql_rendered": True, "database_accessed": False, "model_called": False, "all_static_policy_checks_passed": True},
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        file_info["manifest.json"] = {"rows": 1, "sha256": sha256_file(manifest_path)}
        staging.rename(output)
    except Exception:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds-jsonl", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(materialize(args.seeds_jsonl, args.output_dir, generated_at=args.generated_at), ensure_ascii=False, sort_keys=True, indent=2))
