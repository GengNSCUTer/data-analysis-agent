"""Summarize an aligned runtime Base/Adapter matching report safely.

The summary contains hashes, states, family/risk aggregates and source IDs for
diagnostic rows only. It never copies raw SQL, prompts or result rows.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any


FORBIDDEN = {"raw_completion", "sql", "prompt", "result_rows", "candidate_sql", "gold_sql"}


class SummaryError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise SummaryError(f"invalid JSONL: {path}")
    return rows


def ensure_safe(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = FORBIDDEN.intersection(value)
        if forbidden:
            raise SummaryError(f"forbidden diagnostic field(s): {sorted(forbidden)}")
        for nested in value.values():
            ensure_safe(nested)
    elif isinstance(value, list):
        for nested in value:
            ensure_safe(nested)


def state_counts(rows: list[dict[str, Any]], role: str) -> dict[str, Any]:
    return {
        "states": dict(sorted(Counter(row[role]["state"] for row in rows).items())),
        "denotations": dict(sorted(Counter(row[role]["denotation"] for row in rows).items())),
        "plan_adherent": sum(row[role].get("plan_adherent") is True for row in rows),
        "ordered_matches": sum(row[role]["denotation"] == "ordered_denotation_match" for row in rows),
        "ordered_or_bag_matches": sum(
            row[role]["denotation"] in {"ordered_denotation_match", "bag_denotation_match"}
            for row in rows
        ),
    }


def grouped(rows: list[dict[str, Any]], key_fn, role: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for key in key_fn(row):
            groups[str(key)].append(row)
    return {
        key: {"total": len(group), **state_counts(group, role)}
        for key, group in sorted(groups.items())
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise SummaryError(f"refusing to overwrite {args.output}")
    records = read_jsonl(args.records)
    source_ids = [row.get("source_id") for row in records]
    if any(not isinstance(source_id, str) for source_id in source_ids):
        raise SummaryError("missing source_id")
    if len(set(source_ids)) != len(source_ids):
        raise SummaryError("duplicate source_id")
    by_workspace = {
        workspace: [row for row in records if row.get("workspace") == workspace]
        for workspace in ("olist", "thelook")
    }
    result: dict[str, Any] = {
        "version": "runtime-queryplan-aligned-diagnostic-v1",
        "scope": "validation_only",
        "source": {
            "records": {"path": str(args.records), "sha256": sha256_file(args.records), "rows": len(records)},
            "inputs": {"path": str(args.inputs), "sha256": sha256_file(args.inputs)},
            "gold": {"path": str(args.gold), "sha256": sha256_file(args.gold)},
        },
        "overall": {role: state_counts(records, role) for role in ("base", "adapter")},
        "by_workspace": {
            workspace: {role: state_counts(rows, role) for role in ("base", "adapter")}
            for workspace, rows in by_workspace.items()
        },
        "by_risk_tag": {
            role: grouped(records, lambda row: row.get("risk_tags") or ["<none>"], role)
            for role in ("base", "adapter")
        },
        "by_family": {
            role: grouped(records, lambda row: [row.get("scenario_family_id", "<none>")], role)
            for role in ("base", "adapter")
        },
        "transitions": dict(
            sorted(
                Counter(
                    f"{row['base']['denotation']} -> {row['adapter']['denotation']}" for row in records
                ).items()
            )
        ),
        "adapter_non_ordered": [
            {
                "source_id": row["source_id"],
                "workspace": row["workspace"],
                "scenario_family_id": row.get("scenario_family_id"),
                "risk_tags": row.get("risk_tags", []),
                "state": row["adapter"]["state"],
                "denotation": row["adapter"]["denotation"],
                "candidate_sql_sha256": row["adapter"].get("candidate_sql_sha256"),
            }
            for row in records
            if row["adapter"]["denotation"] != "ordered_denotation_match"
        ],
    }
    ensure_safe(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["overall"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SummaryError as exc:
        print(f"summary error: {exc}")
        raise SystemExit(2) from exc
