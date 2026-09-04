#!/usr/bin/env python3
"""Generate one deterministic Chinese business question per admitted Olist Pilot v1 Gold row.

Question text is an external overlay, not Git data. The templates deliberately
express only already-frozen QuerySpec intent; runtime prompt materialization
must still rebuild and validate the actual Router/Catalog/QueryPlan contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import OLIST_CANDIDATE_SQL_PROMPT_VERSION  # noqa: E402
from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402


OVERLAY_VERSION = "1"
EXPECTED_ROWS = 40
_METRIC_LABELS = {
    "gmv": "GMV",
    "paid_order_count": "有效订单数",
    "average_delivery_days": "平均履约天数",
    "positive_review_rate": "好评率",
    "item_count": "商品件数",
    "average_order_value": "平均订单商品金额",
    "average_review_score": "平均评价分",
    "on_time_delivery_rate": "准时送达率",
    "cancellation_rate": "取消率",
    "freight_amount": "运费金额",
}
_DIMENSION_LABELS = {"customer_state": "各客户州", "product_category_name": "各商品品类"}
_GRAIN_LABELS = {"day": "按日", "month": "按月", "quarter": "按季度", "year": "按年"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-assembly-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _external_existing(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _external_new(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("question overlay must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def load_admitted_rows(directory: Path) -> list[dict[str, Any]]:
    directory = directory.resolve()
    if directory.is_relative_to(ROOT) or not directory.is_dir():
        raise ValueError("admission assembly directory must exist outside the Git worktree")
    manifest_path = directory / "admission_assembly_manifest.json"
    records_path = directory / "admitted_records.jsonl"
    manifest = json.loads(_external_existing(manifest_path, "assembly manifest").read_text(encoding="utf-8"))
    if manifest.get("workspace") != WorkspacePin.current().as_dict():
        raise ValueError("assembly workspace differs from current pin")
    if manifest.get("checks", {}).get("status") != "pass":
        raise ValueError("assembly did not pass")
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    if output.get("sha256") != sha256_file(_external_existing(records_path, "admitted records")):
        raise ValueError("assembly admitted records hash mismatch")
    rows = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line]
    if len(rows) != EXPECTED_ROWS or output.get("rows") != EXPECTED_ROWS:
        raise ValueError("Pilot v1 requires exactly 40 admitted records")
    if len({row.get("seed_id") for row in rows}) != len(rows):
        raise ValueError("admitted records have duplicate seed IDs")
    return rows


def _date(value: str) -> str:
    # WorkingMemory accepts ISO endpoints as a production contract. Keep the
    # training question in that exact user-facing form instead of asking a
    # second parser to infer Chinese calendar expressions.
    return value


def render_question(spec: dict[str, Any]) -> str:
    metrics = "、".join(_METRIC_LABELS[metric] for metric in spec["metric_ids"])
    shape = spec["result_shape"]
    time = spec["time"]
    if time["mode"] == "all_time":
        time_text = "全部可用数据范围内"
    else:
        time_text = f"{_date(time['start'])}至{_date(time['end_exclusive'])}"
    if shape == "scalar":
        return f"请统计 {time_text} 的{metrics}。"
    if shape == "time_series":
        return f"请{_GRAIN_LABELS[time['grain']]}统计 {time_text} 的{metrics}。"
    dimension = _DIMENSION_LABELS[spec["dimension"]]
    return f"请统计 {time_text} {dimension}的{metrics}。"


def generate(admission_assembly_dir: Path, output_json: Path) -> dict[str, Any]:
    rows = load_admitted_rows(admission_assembly_dir)
    output_json = _external_new(output_json)
    cases = []
    for row in rows:
        spec = row.get("query_spec")
        if not isinstance(spec, dict):
            raise ValueError("admitted record has no QuerySpec")
        cases.append({"seed_id": row["seed_id"], "question": render_question(spec)})
    payload = {
        "schema_version": OVERLAY_VERSION,
        "language": "zh",
        "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
        "cases": cases,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    staging = output_json.parent / f".{output_json.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(output_json)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return {"rows": len(cases), "sha256": sha256_file(output_json), "output": str(output_json)}


def main() -> int:
    args = parse_args()
    print(json.dumps(generate(args.admission_assembly_dir, args.output_json), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
