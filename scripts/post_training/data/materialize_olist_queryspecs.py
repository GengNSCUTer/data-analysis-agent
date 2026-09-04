#!/usr/bin/env python3
"""Materialize audited Olist QuerySpec and Gold-SQL intermediate artifacts.
This is not an SFT data builder. It accepts only structural coverage seeds,
never reads questions or protected holdout cases, and never executes SQL.
All artifacts must be written outside the Git worktree.
"""
# 开启延迟注解，用于类型提示
from __future__ import annotations
# 命令行参数解析库
import argparse
# 计数器，用于统计各类样本数量
from collections import Counter
# 日期时间，生成产物的时间戳
from datetime import datetime, timezone
# sha256哈希，用来生成指纹、校验文件防篡改
import hashlib
# json序列化与反序列化
import json
# 面向对象风格的文件路径操作
from pathlib import Path
# 文件、文件夹删除操作，用于失败时清理临时目录
import shutil
# 修改python模块搜索路径
import sys
# uuid生成唯一标识，用于临时文件夹命名，防止并发冲突
import uuid
# 类型注解
from typing import Any, Mapping, Sequence

# 获取当前文件向上3层的项目根目录
ROOT = Path(__file__).resolve().parents[3]
# 如果根目录不在模块搜索列表，则插入到最前面，保证可以导入项目内部模块
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 导入项目内部自定义模块：查询规格模型、校验、SQL渲染函数
from data_analysis_agent.olist_queryspec import (  # noqa: E402
    QUERY_SPEC_SCHEMA_VERSION,
    RENDERER_VERSION,
    QuerySpec,
    QuerySpecValidationError,
    QueryTime,
    WorkspacePin,
    render_gold_sql,
    validate_query_spec,
)

# 当前物化程序版本号，写入产物清单用于后期溯源
MATERIALIZER_VERSION = "olist-queryspec-materializer-v1"
# 查询家族指纹计算使用的协议版本
FAMILY_SCHEMA_VERSION = "olist-query-family-v1"
# 受保护家族黑名单摘要文件的协议版本
PROTECTED_SUMMARY_VERSION = "olist-protected-family-summary-v1"
# 与 protected summary 同目录的证据文件协议版本。物化器必须验证它，不能只相信摘要本身。
PROTECTED_EVIDENCE_VERSION = "olist-protected-family-summary-evidence-v1"
# 允许的数据集切分集合：训练集、验证集、域内测试集
_SPLITS = frozenset({"train", "validation", "in_domain_test"})
_SPLIT_POLICIES = frozenset({"strict_v1", "family_scoped_v2"})
# 种子输入文件允许的全部字段
_SEED_FIELDS = frozenset(
    {
        "seed_id",
        "split",
        "metric_ids",
        "result_shape",
        "dimension",
        "time",
        "join_program_id",
        "attribution_rule_id",
    }
)
# 必填字段集合，attribution_rule_id为可选字段
_REQUIRED_SEED_FIELDS = _SEED_FIELDS - {"attribution_rule_id"}


def parse_args() -> argparse.Namespace:
    """解析命令行传入参数"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds-jsonl", type=Path, required=True)
    parser.add_argument("--protected-summary-json", type=Path, required=True)
    parser.add_argument("--protected-evidence-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--split-policy",
        choices=sorted(_SPLIT_POLICIES),
        default="strict_v1",
        help="v1 forbids join programs crossing splits; v2 isolates families/programs without banning shared join paths.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp; set this for byte-reproducible output.",
    )
    return parser.parse_args()


def sha256_bytes(value: bytes) -> str:
    """对一段字节内容计算sha256哈希摘要"""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """分块读取文件，计算整个文件的sha256哈希，用于校验文件完整性"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    """
    生成标准化JSON字符串
    sort_keys=True强制字典key排序；保证相同语义内容得到完全一致的字符串，用于生成稳定指纹
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def family_fingerprint(family_id: str) -> str:
    """生成家族的保护指纹，用于和黑名单进行比对，检测是否发生数据泄露"""
    """Return the only protected-family representation accepted by v1."""
    return sha256_bytes(f"{PROTECTED_SUMMARY_VERSION}:{family_id}".encode("utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取jsonl文件，一行对应一条json样本记录"""
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"seed at {path}:{line_number} must be an object")
        rows.append(value)
    if not rows:
        raise ValueError("seed input is empty")
    return rows


def load_protected_family_fingerprints(path: Path) -> frozenset[str]:
    """加载受保护家族指纹黑名单，黑名单中的家族禁止生成到训练/验证数据中"""
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("protected summary must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("protected summary must be an object")
    if set(value) != {"summary_version", "family_fingerprints"}:
        raise ValueError("protected summary fields do not match the v1 contract")
    if value["summary_version"] != PROTECTED_SUMMARY_VERSION:
        raise ValueError("unsupported protected summary version")
    fingerprints = value["family_fingerprints"]
    if not isinstance(fingerprints, list) or any(
        not isinstance(item, str) or len(item) != 64 for item in fingerprints
    ):
        raise ValueError("protected family fingerprints must be SHA-256 strings")
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("protected family fingerprints must be unique")
    return frozenset(fingerprints)


def _require_external_file(path: Path, label: str) -> Path:
    """Keep protected inputs outside the Git worktree before reading them."""
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def load_protected_summary_evidence(
    protected_summary_json: Path,
    protected_evidence_json: Path,
    *,
    family_count: int,
) -> dict[str, Any]:
    """Verify that the externally exported summary has matching provenance evidence."""
    summary_path = _require_external_file(protected_summary_json, "protected summary")
    evidence_path = _require_external_file(protected_evidence_json, "protected summary evidence")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("protected summary evidence must be valid JSON") from exc
    required_fields = {
        "evidence_version",
        "exporter_version",
        "generated_at",
        "approved_input_version",
        "approved_input_sha256",
        "protected_source_manifest_sha256",
        "review_reference",
        "workspace",
        "family_count",
        "protected_summary_version",
        "protected_summary_sha256",
    }
    if not isinstance(evidence, dict) or set(evidence) != required_fields:
        raise ValueError("protected summary evidence fields do not match the v1 contract")
    if evidence["evidence_version"] != PROTECTED_EVIDENCE_VERSION:
        raise ValueError("unsupported protected summary evidence version")
    if evidence["protected_summary_version"] != PROTECTED_SUMMARY_VERSION:
        raise ValueError("protected summary evidence version does not match the materializer")
    if evidence["workspace"] != WorkspacePin.current().as_dict():
        raise ValueError("protected summary evidence workspace does not match the current pin")
    if evidence["family_count"] != family_count:
        raise ValueError("protected summary evidence family count does not match the summary")
    if evidence["protected_summary_sha256"] != sha256_file(summary_path):
        raise ValueError("protected summary evidence hash does not match the summary")
    return evidence


def canonicalize_seed(raw: Mapping[str, Any]) -> dict[str, Any]:
    """
    对原始种子数据进行标准化与合法性校验
    检查多余字段、缺失字段、每个字段的数据类型，转换为内部标准结构体
    """
    unknown = set(raw) - _SEED_FIELDS
    missing = _REQUIRED_SEED_FIELDS - set(raw)
    if unknown or missing:
        raise QuerySpecValidationError(
            "unsupported_query_feature",
            f"seed fields are invalid; unknown={sorted(unknown)}, missing={sorted(missing)}",
        )
    seed_id = raw["seed_id"]
    if not isinstance(seed_id, str) or not seed_id or len(seed_id) > 120:
        raise QuerySpecValidationError("invalid_query_spec", "seed_id must be a non-empty short string")
    split = raw["split"]
    if not isinstance(split, str) or split not in _SPLITS:
        raise QuerySpecValidationError("invalid_query_spec", "seed split is not supported")
    metric_ids = raw["metric_ids"]
    if not isinstance(metric_ids, list):
        raise QuerySpecValidationError("invalid_metric_ids", "metric_ids must be an ordered list")
    if not isinstance(raw["result_shape"], str):
        raise QuerySpecValidationError("coverage_shape_not_permitted", "result_shape must be a string")
    if raw["dimension"] is not None and not isinstance(raw["dimension"], str):
        raise QuerySpecValidationError("coverage_shape_not_permitted", "dimension must be a string or null")
    if not isinstance(raw["join_program_id"], str):
        raise QuerySpecValidationError("coverage_shape_not_permitted", "join_program_id must be a string")
    if raw.get("attribution_rule_id") is not None and not isinstance(raw.get("attribution_rule_id"), str):
        raise QuerySpecValidationError("attribution_not_frozen", "attribution_rule_id must be a string or null")
    time = raw["time"]
    if not isinstance(time, dict):
        raise QuerySpecValidationError("invalid_time_contract", "time must be an object")
    allowed_time_fields = {"mode", "start", "end_exclusive", "grain"}
    if set(time) - allowed_time_fields:
        raise QuerySpecValidationError("unsupported_query_feature", "time has unsupported fields")
    if not isinstance(time.get("mode"), str):
        raise QuerySpecValidationError("invalid_time_contract", "time.mode must be a string")
    if any(not isinstance(time.get(name), str) for name in ("start", "end_exclusive") if time.get(name) is not None):
        raise QuerySpecValidationError("invalid_time_contract", "time endpoints must be ISO date strings or null")
    if time.get("grain") is not None and not isinstance(time.get("grain"), str):
        raise QuerySpecValidationError("invalid_time_contract", "time.grain must be a string or null")
    return {
        "seed_id": seed_id,
        "split": split,
        "metric_ids": tuple(metric_ids),
        "result_shape": raw["result_shape"],
        "dimension": raw["dimension"],
        "time": QueryTime(
            mode=time.get("mode"),
            start=time.get("start"),
            end_exclusive=time.get("end_exclusive"),
            grain=time.get("grain"),
        ),
        "join_program_id": raw["join_program_id"],
        "attribution_rule_id": raw.get("attribution_rule_id"),
    }


def family_payload(spec: QuerySpec) -> dict[str, Any]:
    """
    生成用于计算家族id的载荷字典
    注意：故意剔除起止日期；业务逻辑相同、仅时间范围不同的查询属于同一个查询家族
    """
    """Semantic identity intentionally excludes date endpoints and language."""
    return {
        "family_schema_version": FAMILY_SCHEMA_VERSION,
        "workspace": spec.workspace.as_dict(),
        # Result-column order is canonical SQL presentation, not a distinct
        # business program. Keep family isolation invariant under a caller
        # changing the requested metric order.
        "metric_ids": sorted(spec.metric_ids),
        "result_shape": spec.result_shape,
        "dimension": spec.dimension,
        "time_mode": spec.time.mode,
        "time_grain": spec.time.grain,
        "join_program_id": spec.join_program_id,
        "aggregation_contract": "olist-metrics-v2",
    }


def family_id(spec: QuerySpec) -> str:
    """基于查询语义载荷生成唯一的查询家族编号family_id"""
    return "family_" + sha256_bytes(_canonical_json(family_payload(spec)).encode("utf-8"))[:24]


def _rejection(seed_id: str, split: str | None, reason_code: str) -> dict[str, Any]:
    """生成一条被拒绝样本的日志记录，用于写入rejections文件，记录失败原因"""
    return {
        "seed_id": seed_id,
        "split": split,
        "reason_code": reason_code,
        "materializer_version": MATERIALIZER_VERSION,
    }


def _validate_artifact(spec: QuerySpec) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    调用渲染器生成黄金标准答案SQL，并做一致性校验
    校验SQL哈希、返回字段，防止渲染器内部逻辑漂移
    """
    # Keep the explicit validator even though create_validated and renderer validate too.
    validate_query_spec(spec)
    artifact = render_gold_sql(spec)
    actual_hash = sha256_bytes(artifact.sql.encode("utf-8"))
    if actual_hash != artifact.sql_sha256 or actual_hash != artifact.evidence["sql_sha256"]:
        raise QuerySpecValidationError("renderer_hash_mismatch", "renderer SQL hash is inconsistent")
    if artifact.required_result_columns != spec.required_result_columns:
        raise QuerySpecValidationError("result_columns_do_not_match_contract", "renderer output contract drifted")
    return spec.as_dict(), {
        "query_spec_id": artifact.query_spec_id,
        "renderer_version": artifact.renderer_version,
        "sql": artifact.sql,
        "sql_sha256": artifact.sql_sha256,
        "metric_ids": list(artifact.metric_ids),
        "join_program_id": artifact.join_program_id,
        "required_result_columns": list(artifact.required_result_columns),
        "evidence": dict(artifact.evidence),
    }


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """将样本列表写入jsonl文件；'x'模式文件已存在时报错，禁止覆盖旧文件"""
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _check_output_dir(output_dir: Path) -> None:
    """输出目录前置校验：产物不能放在Git仓库内；输出目录不能已经存在"""
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(ROOT):
        raise ValueError("materialized artifacts must stay outside the Git worktree")
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")


def materialize(
    seeds_jsonl: Path,
    protected_summary_json: Path,
    protected_evidence_json: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
    split_policy: str = "strict_v1",
) -> dict[str, Any]:
    """
    主物化流程函数
    读取种子文件、校验、生成黄金SQL、查重防泄露、原子写入所有产物和审计清单
    """
    """Write structural QuerySpec/Gold records and an audit manifest atomically."""
    output_dir = output_dir.resolve()
    if split_policy not in _SPLIT_POLICIES:
        raise ValueError(f"unsupported split policy: {split_policy}")
    _check_output_dir(output_dir)
    raw_seeds = _read_jsonl(seeds_jsonl.resolve())
    protected_summary_path = _require_external_file(protected_summary_json, "protected summary")
    protected_fingerprints = load_protected_family_fingerprints(protected_summary_path)
    protected_evidence_path = _require_external_file(
        protected_evidence_json, "protected summary evidence"
    )
    protected_evidence = load_protected_summary_evidence(
        protected_summary_path,
        protected_evidence_path,
        family_count=len(protected_fingerprints),
    )
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    # 保存校验通过的样本
    accepted: list[dict[str, Any]] = []
    # 保存校验失败被丢弃的样本日志
    rejections: list[dict[str, Any]] = []
    # 集合，用来检测各类重复项
    seen_seed_ids: set[str] = set()
    seen_query_specs: set[str] = set()
    seen_families: set[str] = set()
    seen_sql_hashes: set[str] = set()
    # 循环逐条处理每一条种子
    for raw_seed in raw_seeds:
        seed_id = str(raw_seed.get("seed_id", "unknown"))
        split = raw_seed.get("split") if isinstance(raw_seed.get("split"), str) else None
        # 重复种子id，直接拒绝
        if seed_id in seen_seed_ids:
            rejections.append(_rejection(seed_id, split, "duplicate_seed_id"))
            continue
        seen_seed_ids.add(seed_id)
        try:
            # 1.清洗校验原始种子字段
            seed = canonicalize_seed(raw_seed)
            # 2.由种子生成QuerySpec查询规格对象
            spec = QuerySpec.create_validated(
                metric_ids=seed["metric_ids"],
                result_shape=seed["result_shape"],
                dimension=seed["dimension"],
                time=seed["time"],
                join_program_id=seed["join_program_id"],
                attribution_rule_id=seed["attribution_rule_id"],
            )
            # 3.计算该查询所属家族id
            family = family_id(spec)
            # 安全校验：家族命中黑名单，则拒绝，防止测试集泄露进训练集
            if family_fingerprint(family) in protected_fingerprints:
                raise QuerySpecValidationError("protected_family_collision", "family collides with protected summary")
            # 禁止同一个家族多次出现
            if family in seen_families:
                raise QuerySpecValidationError("duplicate_family", "family appears more than once")
            # 4.渲染生成黄金SQL并且校验产物一致性
            query_spec, artifact = _validate_artifact(spec)
            if spec.query_spec_id in seen_query_specs:
                raise QuerySpecValidationError("duplicate_query_spec", "QuerySpec appears more than once")
            if artifact["sql_sha256"] in seen_sql_hashes:
                raise QuerySpecValidationError("duplicate_sql_artifact", "canonical SQL appears more than once")

        except QuerySpecValidationError as exc:
            # 捕获校验异常，记录拒绝日志，跳过本条样本
            rejections.append(_rejection(seed_id, split, exc.reason_code))
            continue
        # 全部校验通过，加入成功列表
        seen_families.add(family)
        seen_query_specs.add(spec.query_spec_id)
        seen_sql_hashes.add(artifact["sql_sha256"])
        accepted.append(
            {
                "seed_id": seed["seed_id"],
                "split": seed["split"],
                "family_id": family,
                "sql_program_id": spec.join_program_id,
                "query_spec": query_spec,
                "gold_artifact": artifact,
            }
        )

    # v1 的历史合同禁止 join path 跨 split；v2 将泄露边界收紧到完整 family/
    # canonical SQL，而允许同一底层 join path 服务不同业务组合。
    program_splits: dict[str, set[str]] = {}
    for row in accepted:
        program_splits.setdefault(row["sql_program_id"], set()).add(row["split"])
    crossing_programs = sorted(program for program, splits in program_splits.items() if len(splits) > 1)
    if split_policy == "strict_v1" and crossing_programs:
        raise ValueError(f"SQL programs cross splits: {crossing_programs}")

    # 原子写入策略：先写入临时目录，全部成功后再重命名成最终目录；崩溃不会留下残缺文件
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        # 拆分两份输出：查询规格文件、黄金SQL文件
        query_spec_rows = [
            {key: row[key] for key in ("seed_id", "split", "family_id", "sql_program_id", "query_spec")}
            for row in accepted
        ]
        gold_rows = [
            {key: row[key] for key in ("seed_id", "split", "family_id", "sql_program_id", "gold_artifact")}
            for row in accepted
        ]
        query_specs_path = staging / "query_specs.jsonl"
        gold_sql_path = staging / "gold_sql.jsonl"
        rejection_path = staging / "materialization_rejections.jsonl"
        # 写出3个结果文件
        _write_jsonl(query_specs_path, query_spec_rows)
        _write_jsonl(gold_sql_path, gold_rows)
        _write_jsonl(rejection_path, rejections)

        split_rows = Counter(row["split"] for row in accepted)
        split_families = {
            split: sorted(row["family_id"] for row in accepted if row["split"] == split)
            for split in _SPLITS
        }
        # 生成审计清单manifest，记录版本、哈希、样本统计，用于溯源与实验复现
        manifest = {
            "materializer_version": MATERIALIZER_VERSION,
            "split_policy": split_policy,
            "generated_at": generated_at,
            "query_spec_schema_version": QUERY_SPEC_SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "workspace": WorkspacePin.current().as_dict(),
            "source": {
                "seeds_jsonl": str(seeds_jsonl.resolve()),
                "seeds_sha256": sha256_file(seeds_jsonl.resolve()),
                "protected_summary_json": str(protected_summary_json.resolve()),
                "protected_summary_sha256": sha256_file(protected_summary_json.resolve()),
                "protected_summary_version": PROTECTED_SUMMARY_VERSION,
                "protected_evidence_json": str(protected_evidence_json.resolve()),
                "protected_evidence_sha256": sha256_file(protected_evidence_json.resolve()),
                "protected_review_reference": protected_evidence["review_reference"],
            },
            "outputs": {
                "query_specs_jsonl": {"rows": len(query_spec_rows), "sha256": sha256_file(query_specs_path)},
                "gold_sql_jsonl": {"rows": len(gold_rows), "sha256": sha256_file(gold_sql_path)},
                "rejections_jsonl": {"rows": len(rejections), "sha256": sha256_file(rejection_path)},
            },
            "counts": {
                "input_seeds": len(raw_seeds),
                "accepted_rows": len(accepted),
                "query_specs": len(seen_query_specs),
                "families": len(seen_families),
                "sql_programs": len(program_splits),
                "canonical_sql_hashes": len(seen_sql_hashes),
                "rejections_by_reason": dict(sorted(Counter(row["reason_code"] for row in rejections).items())),
                "protected_family_collisions": sum(
                    row["reason_code"] == "protected_family_collision" for row in rejections
                ),
            },
            "splits": {
                split: {"rows": split_rows.get(split, 0), "family_ids": split_families[split]}
                for split in sorted(_SPLITS)
            },
            "checks": {
                "family_split_overlap": [],
                "query_spec_split_overlap": [],
                "sql_program_split_overlap": crossing_programs if split_policy == "family_scoped_v2" else [],
                "protected_holdout_raw_read": False,
                "sql_executed": False,
                "prompt_or_question_materialized": False,
                "status": "pass" if accepted else "rejected_all",
            },
        }
        # 将审计清单写入磁盘
        (staging / "materialization_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # 临时文件夹重命名为最终输出目录，原子提交完成
        staging.replace(output_dir)
        return manifest
    except Exception:
        # 出现任意异常，删除临时目录，清理垃圾文件
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    """程序入口函数，调用物化主流程并打印结果清单"""
    args = parse_args()
    result = materialize(
        args.seeds_jsonl,
        args.protected_summary_json,
        args.protected_evidence_json,
        args.output_dir,
        generated_at=args.generated_at,
        split_policy=args.split_policy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
