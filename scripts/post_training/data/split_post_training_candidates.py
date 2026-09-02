#!/usr/bin/env python3
"""Create a deterministic, schema-disjoint train/validation split externally.
This is the canonical post-training data split implementation. The source
candidate JSONL must already be train-only and execution-checked.
This script groups all samples from a Spider ``db_id`` together, so a database
schema never appears in both train and validation. It only writes derived
artifacts to the explicitly supplied external output directory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    """解析命令行入参"""
    parser = argparse.ArgumentParser(description=__doc__)
    # 上一步脚本产出的候选数据集文件 candidates.jsonl
    parser.add_argument("--candidates-jsonl", type=Path, required=True)
    # 留出集清单，校验不能混入验证集/训练集
    parser.add_argument("--holdout-manifest", type=Path, required=True)
    # 拆分后数据集输出目录
    parser.add_argument("--output-dir", type=Path, required=True)
    # 验证集占比（样本数目标比例，不是数据库数量比例）
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    # 确定性随机种子，保证每次拆分结果完全一致、可复现
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument(
        "--allow-sql-shape-overlap",
        action="store_true",
        help=(
            "Keep schema‑disjoint validation while recording, rather than rejecting, "
            "generic SQL‑shape overlap across different databases."
        ),
    )
    # 开关：是否允许训练集、验证集出现相同SQL骨架；默认禁止，开启后仅记录重叠不报错终止
    parser.add_argument("--generated-at", default=None)
    # 数据集生成UTC时间戳；固定值可保证输出文件哈希复现；不传则脚本自动生成
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    """计算文件sha256哈希值，用于审计溯源校验，确认文件未被篡改"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def holdout_ids(path: Path) -> set[str]:
    """读取yaml留出清单，提取禁止出现在数据集内的样本ID；无pyyaml依赖，采用简单行解析"""
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("- case_id:"):
            ids.add(line.split(":", maxsplit=1)[1].strip())
    if not ids:
        raise ValueError(f"no case IDs found in holdout manifest: {path}")
    return ids


def load_rows(path: Path) -> list[dict[str, Any]]:
    """加载候选jsonl样本，并做前置合法性校验
    校验项：
    1. 文件不能为空
    2. 每条样本必须携带必要字段
    3. 输入样本必须标记为train，不能是dev/test
    4. 所有样本必须经过SQL‑EXPLAIN校验通过
    """
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        raise ValueError("candidate input is empty")
    required = {"sample_id", "split", "query_plan", "execution_outcome", "workspace_id"}
    for row in rows:
        missing = required - row.keys()
        if missing:
            raise ValueError(f"candidate {row.get('sample_id')} missing {sorted(missing)}")
        if row["split"].get("name") != "train":
            raise ValueError(f"candidate is not train‑only: {row['sample_id']}")
        if row["execution_outcome"].get("sqlite_readonly_explain") != "pass":
            raise ValueError(f"candidate lacks read‑only execution evidence: {row['sample_id']}")
    return rows


def db_id(row: dict[str, Any]) -> str:
    """从样本split.group字段还原出Spider数据库db_id
    group字段格式：db_id:sha1摘要，以冒号分割，前面部分即为数据库ID
    """
    group = row["split"].get("group", "")
    database, separator, _ = group.partition(":")
    if not separator or not database:
        raise ValueError(f"candidate has invalid database group: {row['sample_id']}")
    return database


def rank_group(group: str, seed: int) -> str:
    """利用seed+数据库名生成哈希字符串，用于确定性排序，实现无随机数、可复现的分组抽取"""
    return hashlib.sha256(f"{seed}:{group}".encode()).hexdigest()


def choose_validation_groups(
    groups: dict[str, list[dict[str, Any]]], validation_ratio: float, seed: int
) -> set[str]:
    """选取划入验证集的数据库schema分组
    逻辑：
    1. 先算出目标验证集样本总数 = 全部样本 × validation_ratio
    2. 按哈希确定性顺序逐个把整个数据库加入验证集
    3. 累加样本数量直到达到目标样本量
    4. 返回所有选中的数据库id集合
    """
    total_rows = sum(len(rows) for rows in groups.values())
    target_rows = max(1, round(total_rows * validation_ratio))
    selected: set[str] = set()
    selected_rows = 0
    for group in sorted(groups, key=lambda item: rank_group(item, seed)):
        if selected_rows >= target_rows:
            break
        selected.add(group)
        selected_rows += len(groups[group])
    if len(selected) == len(groups):
        raise ValueError("validation split selected every group")
    return selected


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """把样本列表写入jsonl文件，一行一条json"""
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    """主流水线：加载候选集 → 按数据库分组 → schema‑互斥切分训练/验证集 → 重叠校验 → 输出文件与审计报告"""
    args = parse_args()
    # 验证集比例限制，不能超过0.5，防止训练集样本过少
    if not 0 < args.validation_ratio < 0.5:
        raise ValueError("--validation‑ratio must be between 0 and 0.5")
    rows = load_rows(args.candidates_jsonl)
    forbidden_ids = holdout_ids(args.holdout_manifest)
    # 检查样本id和留出集有无冲突，防止数据泄露
    collisions = sorted({row["sample_id"] for row in rows}.intersection(forbidden_ids))
    if collisions:
        raise ValueError(f"holdout collision: {collisions[:5]}")

    # 以数据库db_id为key，把所有样本按schema分组
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[db_id(row)].append(row)

    # 选出验证集数据库分组
    validation_groups = choose_validation_groups(groups, args.validation_ratio, args.seed)

    # 根据数据库归属划分训练样本、验证样本
    train_rows = [row for row in rows if db_id(row) not in validation_groups]
    validation_rows = [row for row in rows if db_id(row) in validation_groups]

    if not train_rows or not validation_rows:
        raise AssertionError("both splits must be non‑empty")

    # 修改每条样本split字段，打上train / validation标签
    for row in train_rows:
        row["split"] = {**row["split"], "name": "train"}
    for row in validation_rows:
        row["split"] = {**row["split"], "name": "validation"}

    train_db_ids = {db_id(row) for row in train_rows}
    validation_db_ids = {db_id(row) for row in validation_rows}
    train_shapes = {row["query_plan"].get("sql_shape") for row in train_rows}
    validation_shapes = {row["query_plan"].get("sql_shape") for row in validation_rows}

    # 强校验：数据库schema绝对不能在训练、验证两边同时出现，杜绝schema泄露
    if train_db_ids.intersection(validation_db_ids):
        raise AssertionError("schema/db groups overlap between train and validation")

    shape_overlap = train_shapes.intersection(validation_shapes)
    # 默认禁止SQL骨架重叠；开启allow‑sql‑shape‑overlap则只记录重叠、不报错
    if shape_overlap and not args.allow_sql_shape_overlap:
        raise AssertionError("SQL shapes overlap between train and validation")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    train_path = args.output_dir / "train.jsonl"
    validation_path = args.output_dir / "validation.jsonl"
    audit_path = args.output_dir / "split_audit.json"

    # 输出拆分后的两个数据集
    write_jsonl(train_path, train_rows)
    write_jsonl(validation_path, validation_rows)

    generated_at = args.generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # 生成审计报告，完整记录拆分参数、统计数据、文件哈希，保证数据集可溯源复现
    audit = {
        "generated_at": generated_at,
        "generator": "scripts/split_post_training_candidates.py",
        "generator_version": "1",
        "source": {
            "candidates_jsonl": str(args.candidates_jsonl),
            "candidates_sha256": sha256_file(args.candidates_jsonl),
            "holdout_manifest": str(args.holdout_manifest),
        },
        "policy": {
            "seed": args.seed,
            "validation_ratio_requested": args.validation_ratio,
            "primary_group": "spider_db_id",
            "secondary_overlap_check": (
                "normalized_sql_shape_recorded_not_blocking"
                if args.allow_sql_shape_overlap
                else "normalized_sql_shape_required_disjoint"
            ),
            "v2_holdout_case_collisions": collisions,
        },
        "splits": {
            "train": {
                "rows": len(train_rows),
                "database_groups": len(train_db_ids),
                "sha256": sha256_file(train_path),
            },
            "validation": {
                "rows": len(validation_rows),
                "database_groups": len(validation_db_ids),
                "sha256": sha256_file(validation_path),
            },
        },
        "checks": {
            "train_validation_database_overlap": [],
            "train_validation_sql_shape_overlap": [] if not shape_overlap else None,
            "train_validation_sql_shape_overlap_count": len(shape_overlap),
            "sql_shape_overlap_allowed": args.allow_sql_shape_overlap,
            "v2_holdout_used": False,
            "raw_data_in_git": False,
            "status": "pass",
        },
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf‑8")
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
