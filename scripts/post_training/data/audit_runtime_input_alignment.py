#!/usr/bin/env python3
"""Replay frozen train/validation questions; never consume protected tests.

Outputs contain sample identifiers and disagreement categories only. This is
not a training-data materializer, and a zero mismatch count would not prove
Gold SQL correctness, date-end semantics or production routing equivalence.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE  # noqa: E402
from data_analysis_agent.runtime_alignment_audit import (  # noqa: E402
    AUDIT_VERSION,
    compare_source_semantics,
    replay_single_turn,
)
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-prompt-v2", action="store_true",
                        help="Use the shared runtime request builder; this does not admit training labels.")
    args = parser.parse_args()
    output = ensure_path_outside_repository(args.output_dir, ROOT)
    output.mkdir(parents=True, exist_ok=False)
    workspaces = {"olist": OLIST_RUNTIME_WORKSPACE, "thelook": THELOOK_V2_WORKSPACE}
    retrievers = {key: CatalogRetriever(CatalogLoader(profile).load())
                  for key, profile in workspaces.items()}
    summary = {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "train/validation; single turn; no LLM grounder; no Gold execution",
        "shared_candidate_v2": args.candidate_prompt_v2,
        "source_dir": str(args.source_dir.resolve()),
        "source_files": {},
        "groups": {},
        "limits": [
            "TheLook replay is a hypothetical use of existing runtime rules, not an online workspace.",
            "Explicit endpoint/shape agreement does not prove Gold SQL semantics or Catalog completeness.",
            "No final test, new split, training, GPU or default provider change.",
        ],
    }
    groups = defaultdict(lambda: {"total": 0, "structural_match": 0,
                                  "semantic_structure_match": 0, "reasons": Counter(), "routes": Counter()})
    with (output / "records.jsonl").open("x", encoding="utf-8") as records:
        for split in ("train", "validation"):
            path = args.source_dir / f"{split}.jsonl"
            content = path.read_bytes()
            summary["source_files"][split] = {
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)
            }
            for line in content.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row["split"] != {"name": split}:
                    raise ValueError("source split must match its train/validation filename")
                workspace = row["workspace_key"]
                # Deliberately build the model input before consulting QuerySpec.
                replay = replay_single_turn(
                    row["question"], retrievers[workspace],
                    workspace=workspaces[workspace] if args.candidate_prompt_v2 else None,
                )
                reasons = compare_source_semantics(replay, row["query_spec"])
                group = groups[f"{workspace}/{split}"]
                group["total"] += 1
                group["structural_match"] += not reasons
                group["semantic_structure_match"] += set(reasons) <= {"result_column_order_mismatch"}
                group["reasons"].update(reasons)
                group["routes"].update([replay["route_state"]])
                records.write(json.dumps({
                    "sample_id": row["sample_id"], "split": split,
                    "workspace_key": workspace, "reasons": list(reasons),
                    "route_state": replay["route_state"], "route_reason": replay["route_reason"],
                    "candidate_request": replay.get("candidate_request"),
                }, ensure_ascii=False) + "\n")
            if hashlib.sha256(path.read_bytes()).hexdigest() != summary["source_files"][split]["sha256"]:
                raise ValueError("source file changed during alignment audit")
    summary["groups"] = dict(sorted(groups.items()))
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
