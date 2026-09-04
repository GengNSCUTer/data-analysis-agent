#!/usr/bin/env python3
"""Run a small, resumable bf16 LoRA (or legacy QLoRA) SFT experiment on external candidates.
This script deliberately trains only the LoRA adapter. Source rows, model
weights, checkpoints and logs must all live outside the Git working tree.
Evidence records counts, hashes and metrics but never copies questions or SQL.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import numpy as np
import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
    set_seed,
)
from data_analysis_agent.spider_sft_format import PROMPT_FORMAT_VERSION

# 分隔符：Prompt 和目标SQL之间的标记
SQL_MARKER = "\n\n### SQL\n"


def parse_args() -> argparse.Namespace:
    """解析训练脚本命令行参数"""
    parser = argparse.ArgumentParser(description=__doc__)
    # 基座模型本地目录
    parser.add_argument("--model-dir", type=Path, required=True)
    # 训练集jsonl文件（上一步split产出）
    parser.add_argument("--train-jsonl", type=Path, required=True)
    # 验证集jsonl文件
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    # 数据集切分审计文件，用于校验数据集合法性
    parser.add_argument("--split-audit", type=Path, required=True)
    # 输出目录：LoRA适配器、checkpoint、日志、证据清单，**必须放在git仓库外**
    parser.add_argument("--output-dir", type=Path, required=True)
    # 断点续训路径，同样禁止放在git仓库内
    parser.add_argument(
        "--resume-from-checkpoint",
        type=Path,
        default=None,
        help="External Trainer checkpoint directory; never resume from Git.",
    )
    # 序列最大token长度
    parser.add_argument("--max-seq-length", type=int, default=1536)
    # 最大训练步数，短实验优先按步数跑
    parser.add_argument("--max-steps", type=int, default=8)
    # 训练轮数，二选一：epochs / max‑steps
    parser.add_argument(
        "--num-train-epochs",
        type=float,
        default=None,
        help="Use complete dataset passes instead of --max-steps for a scaled experiment.",
    )
    # 全局随机种子，保证实验可复现
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    # 一次 forward/backward 实际并行处理的样本数；梯度累积另行控制 optimizer step 间隔
    parser.add_argument("--per-device-train-batch-size", type=int, default=4)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=1)
    parser.add_argument("--per-device-eval-batch-size", type=int, default=4)
    # 每多少步跑一次验证
    parser.add_argument("--evaluation-steps", type=int, default=4)
    # 每多少步保存一次checkpoint
    parser.add_argument("--save-steps", type=int, default=4)
    parser.add_argument("--logging-steps", type=int, default=1)
    # LoRA超参
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    # 基座权重加载模式：默认使用未量化 bf16 LoRA；qlora 仅保留为历史复现实验入口
    parser.add_argument(
        "--base-weight-mode",
        choices=("qlora_4bit", "bf16_lora"),
        default="bf16_lora",
        help=(
            "qlora_4bit stores the frozen base in 4-bit NF4; bf16_lora keeps "
            "the frozen base in bf16. Both modes train the same LoRA adapter."
        ),
    )
    # 物理显卡编号（nvidia‑smi看到的ID），用于绑定GPU，防止环境变量映射错乱
    parser.add_argument(
        "--physical-nvidia-smi-device",
        type=int,
        required=True,
        help="Physical nvidia-smi device ID recorded by the launcher.",
    )
    # 可选校验GPU‑UUID，防止任务跑错显卡
    parser.add_argument(
        "--expected-gpu-uuid",
        default=None,
        help="Optional physical GPU UUID guard; rejects an unexpected CUDA mapping.",
    )
    # 实验标签，写入最终审计清单，方便后续批量管理实验
    parser.add_argument(
        "--experiment-label",
        default="unnamed",
        help="Stable external experiment label recorded in the evidence manifest.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    """计算文件哈希，用于数据集溯源校验，保证实验可复现"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_rows(path: Path, expected_split: str) -> list[dict[str, Any]]:
    """加载数据集jsonl并做严格前置校验
    1. 文件非空
    2. sample_id无重复、无缺失
    3. split标签与预期(train/validation)一致
    4. 所有样本经过SQL执行校验通过，杜绝脏样本
    """
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        raise ValueError(f"{expected_split} split is empty")
    sample_ids = {row.get("sample_id") for row in rows}
    if len(sample_ids) != len(rows) or None in sample_ids:
        raise ValueError(f"{expected_split} split has duplicate or missing sample IDs")
    for row in rows:
        if row.get("split", {}).get("name") != expected_split:
            raise ValueError(f"{row['sample_id']} is not in expected {expected_split} split")
        outcome = row.get("execution_outcome", {})
        if not isinstance(outcome, dict):
            raise ValueError(f"{row['sample_id']} lacks execution evidence")
        if outcome.get("sqlite_readonly_explain") == "pass":
            continue
        if (
            row.get("prompt_format_version") == "olist-candidate-sql-v1"
            and outcome.get("postgres_reader_result_contract") == "pass"
        ):
            continue
        else:
            raise ValueError(f"{row['sample_id']} lacks execution evidence")
    return rows


def prompt_format_version(rows: list[dict[str, Any]]) -> str:
    """校验整份数据集使用统一的Prompt模板版本，防止Prompt格式混杂引发训练异常"""
    versions = {str(row.get("prompt_format_version", PROMPT_FORMAT_VERSION)) for row in rows}
    if len(versions) != 1:
        raise ValueError("training split mixes prompt format versions")
    return versions.pop()


def validate_split_audit(
    audit: dict[str, Any], train_path: Path, validation_path: Path
) -> None:
    """校验已知数据协议的切分审计与当前训练输入一致。"""

    checks = audit.get("checks")
    if not isinstance(checks, dict) or checks.get("status") != "pass":
        raise ValueError("split audit did not pass")

    policy = audit.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("split audit has no policy")
    if train_path.resolve() == validation_path.resolve():
        raise ValueError("train and validation paths must be distinct")

    split_strategy = policy.get("split_strategy")
    if split_strategy is None:
        validate_spider_candidate_audit(checks, policy)
    elif split_strategy == "official_cspider_train_dev_test":
        validate_cspider_official_audit(audit, checks, policy, train_path, validation_path)
    elif split_strategy == "olist_pilot_v1_family_isolated":
        validate_olist_pilot_audit(audit, checks, policy, train_path, validation_path)
    else:
        raise ValueError(f"unsupported split audit strategy: {split_strategy}")

    split_metadata = audit.get("splits")
    if not isinstance(split_metadata, dict):
        raise ValueError("split audit has no split metadata")
    for split_name, path in (("train", train_path), ("validation", validation_path)):
        metadata = split_metadata.get(split_name)
        if not isinstance(metadata, dict):
            raise ValueError(f"split audit has no {split_name} metadata")
        expected_hash = metadata.get("sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            raise ValueError(f"split audit has no {split_name} hash")
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"{split_name} file does not match split audit: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        expected_rows = metadata.get("rows")
        actual_rows = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)
        if expected_rows != actual_rows:
            raise ValueError(
                f"{split_name} row count does not match split audit: "
                f"expected {expected_rows}, got {actual_rows}"
            )
    validate_materialized_length_contract(audit)


def validate_spider_candidate_audit(checks: dict[str, Any], policy: dict[str, Any]) -> None:
    """Keep the original Spider candidate audit contract explicit and stable."""
    if checks.get("v2_holdout_used") is not False:
        raise ValueError("split audit does not prove v2 holdout isolation")
    if policy.get("primary_group") != "spider_db_id":
        raise ValueError("split audit does not prove database-grouped validation")


def validate_cspider_official_audit(
    audit: dict[str, Any],
    checks: dict[str, Any],
    policy: dict[str, Any],
    train_path: Path,
    validation_path: Path,
) -> None:
    """Require the published CSpider train/dev/test isolation proof before SFT."""
    if policy.get("primary_group") != "cspider_db_id":
        raise ValueError("CSpider audit does not prove database-grouped validation")
    if policy.get("test_storage") != "final_evaluation_only":
        raise ValueError("CSpider audit does not isolate final evaluation storage")
    if policy.get("test_forbidden_for_training") is not True:
        raise ValueError("CSpider audit does not forbid test training use")
    if checks.get("raw_data_in_git") is not False:
        raise ValueError("CSpider audit does not prove raw data stays outside Git")
    for check_name in (
        "train_validation_database_overlap",
        "train_test_database_overlap",
        "validation_test_database_overlap",
    ):
        if checks.get(check_name) != []:
            raise ValueError(f"CSpider audit has non-empty {check_name}")

    source = audit.get("source")
    dataset = source.get("dataset") if isinstance(source, dict) else None
    if not isinstance(dataset, dict) or dataset.get("id") != "cspider":
        raise ValueError("CSpider audit has no CSpider source identity")

    splits = audit.get("splits")
    if not isinstance(splits, dict):
        raise ValueError("CSpider audit has no split metadata")
    expected_roles = {
        "train": ("train", "parameter_updates"),
        "validation": ("dev", "validation_only"),
        "test": ("test", "final_evaluation_only"),
    }
    for split_name, (official_name, role) in expected_roles.items():
        metadata = splits.get(split_name)
        if not isinstance(metadata, dict):
            raise ValueError(f"CSpider audit has no {split_name} metadata")
        if metadata.get("official_split") != official_name or metadata.get("role") != role:
            raise ValueError(f"CSpider audit has invalid {split_name} role")
    if splits["test"].get("forbidden_for_training") is not True:
        raise ValueError("CSpider test metadata does not forbid training use")

    outputs = audit.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("CSpider audit has no output paths")
    expected_outputs = {
        "train_jsonl": train_path,
        "validation_jsonl": validation_path,
    }
    for name, expected_path in expected_outputs.items():
        recorded = outputs.get(name)
        if not isinstance(recorded, str) or Path(recorded).resolve() != expected_path.resolve():
            raise ValueError(f"CSpider audit {name} does not match the requested input")
    test_output = outputs.get("test_jsonl")
    if not isinstance(test_output, str):
        raise ValueError("CSpider audit has no final test output path")
    test_path = Path(test_output).resolve()
    if test_path.parent.name != "final_evaluation_only":
        raise ValueError("CSpider test output is not under final_evaluation_only")
    if test_path in {train_path.resolve(), validation_path.resolve()}:
        raise ValueError("CSpider final test cannot be a training input")

    explain = checks.get("sqlite_readonly_explain")
    if not isinstance(explain, dict):
        raise ValueError("CSpider audit has no SQLite explain summary")
    for source_split, audit_split in (("train", "train"), ("dev", "validation"), ("test", "test")):
        summary = explain.get(source_split)
        metadata = splits[audit_split]
        if not isinstance(summary, dict) or summary.get("pass") != metadata.get("rows"):
            raise ValueError(f"CSpider audit has inconsistent {source_split} execution evidence")


def validate_olist_pilot_audit(
    audit: dict[str, Any],
    checks: dict[str, Any],
    policy: dict[str, Any],
    train_path: Path,
    validation_path: Path,
) -> None:
    """Accept only the explicit Olist PostgreSQL/runtime-Prompt SFT protocol."""
    if policy.get("primary_group") != "family_id":
        raise ValueError("Olist audit does not prove family-isolated splits")
    if policy.get("test_storage") != "final_evaluation_only" or policy.get("test_forbidden_for_training") is not True:
        raise ValueError("Olist audit does not isolate the in-domain test split")
    if checks.get("family_split_overlap") != [] or checks.get("query_spec_split_overlap") != []:
        raise ValueError("Olist audit has cross-split semantic identity overlap")
    if checks.get("all_gold_admitted") is not True or checks.get("runtime_contract_rebuilt") is not True:
        raise ValueError("Olist audit lacks Gold or runtime-contract evidence")
    if checks.get("in_domain_test_forbidden_for_training") is not True:
        raise ValueError("Olist audit does not forbid in-domain test training")
    outputs = audit.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("Olist audit has no output paths")
    expected = {"train_jsonl": train_path, "validation_jsonl": validation_path}
    for field, path in expected.items():
        if Path(str(outputs.get(field, ""))).resolve() != path.resolve():
            raise ValueError(f"Olist audit {field} does not match requested input")
    test_path = Path(str(outputs.get("in_domain_test_jsonl", ""))).resolve()
    if test_path.parent.name != "final_evaluation_only" or test_path in {train_path.resolve(), validation_path.resolve()}:
        raise ValueError("Olist in-domain test output is not isolated")
    contract = audit.get("training_length_contract")
    if not isinstance(contract, dict):
        raise ValueError("Olist audit has no training length contract")
    if contract.get("formula") != "exact rendered runtime prompt + canonical SQL + EOS":
        raise ValueError("Olist audit has an unsupported training length formula")
    if contract.get("silent_truncation") is not False:
        raise ValueError("Olist audit does not forbid silent truncation")
    max_seq_length = contract.get("max_seq_length")
    if not isinstance(max_seq_length, int) or max_seq_length <= 0:
        raise ValueError("Olist audit has an invalid training max_seq_length")


def required_max_seq_length(audit: dict[str, Any]) -> int | None:
    """Return a frozen materialization cap when this audit defines one."""
    checks = audit.get("checks")
    cspider_contract = checks.get("length_contract") if isinstance(checks, dict) else None
    if isinstance(cspider_contract, dict):
        value = cspider_contract.get("max_seq_length")
        if not isinstance(value, int) or value <= 0:
            raise ValueError("materialized token-length contract has invalid max_seq_length")
        return value
    olist_contract = audit.get("training_length_contract")
    if isinstance(olist_contract, dict):
        value = olist_contract.get("max_seq_length")
        if not isinstance(value, int) or value <= 0:
            raise ValueError("Olist training length contract has invalid max_seq_length")
        return value
    return None


def validate_materialized_length_contract(audit: dict[str, Any]) -> None:
    """Validate the external exclusion evidence for a materialized length split."""
    checks = audit.get("checks")
    contract = checks.get("length_contract") if isinstance(checks, dict) else None
    if not isinstance(contract, dict):
        return
    if contract.get("version") != "cspider-token-length-v1":
        raise ValueError("unsupported materialized token-length contract")
    max_seq_length = contract.get("max_seq_length")
    if not isinstance(max_seq_length, int) or max_seq_length <= 0:
        raise ValueError("materialized token-length contract has invalid max_seq_length")
    exclusions = audit.get("exclusions")
    if not isinstance(exclusions, dict):
        raise ValueError("materialized length audit has no exclusion manifest")
    exclusion_path = Path(str(exclusions.get("path", ""))).resolve()
    repository_root = Path(__file__).resolve().parents[3]
    if exclusion_path.is_relative_to(repository_root):
        raise ValueError("length exclusion manifest must stay outside the Git worktree")
    expected_hash = exclusions.get("sha256")
    if not exclusion_path.is_file() or not isinstance(expected_hash, str):
        raise ValueError("length exclusion manifest is unavailable")
    if sha256_file(exclusion_path) != expected_hash:
        raise ValueError("length exclusion manifest hash mismatch")
    if exclusions.get("contains_question_or_sql") is not False:
        raise ValueError("length exclusion manifest must not contain question or SQL text")
    expected_rows = int(contract.get("train_excluded", 0)) + int(
        contract.get("validation_excluded", 0)
    )
    if exclusions.get("rows") != expected_rows:
        raise ValueError("length exclusion count does not match contract")
    split_metadata = audit.get("splits")
    if not isinstance(split_metadata, dict):
        raise ValueError("materialized length audit has no split metadata")
    for split_name in ("train", "validation", "test"):
        metadata = split_metadata.get(split_name)
        if not isinstance(metadata, dict):
            raise ValueError(f"materialized length audit has no {split_name} metadata")
        if metadata.get("excluded_rows") != (contract.get(f"{split_name}_excluded", 0)):
            raise ValueError(f"materialized {split_name} exclusion count mismatch")
        if metadata.get("max_sequence_tokens", 0) > max_seq_length:
            raise ValueError(f"materialized {split_name} exceeds max_seq_length")


def split_prompt_and_target(row: dict[str, Any]) -> tuple[str, str]:
    """切分样本：取出输入Prompt 和目标输出SQL
    依据标记符 SQL_MARKER 分割；并且校验分割出来的SQL与字段candidate_sql完全一致
    返回：(prompt部分(带分隔符), target_sql)
    """
    training_text = row.get("training_text")
    candidate_sql = row.get("candidate_sql")
    if not isinstance(training_text, str) or not isinstance(candidate_sql, str):
        raise ValueError(f"{row['sample_id']} lacks training text or target SQL")
    rendered_prompt = row.get("rendered_prompt")
    if rendered_prompt is not None:
        if not isinstance(rendered_prompt, str) or not rendered_prompt.strip():
            raise ValueError(f"{row['sample_id']} has an invalid rendered runtime prompt")
        expected_text = rendered_prompt.rstrip() + "\n" + candidate_sql.strip()
        if training_text != expected_text:
            raise ValueError(f"{row['sample_id']} runtime prompt and target SQL mismatch")
        return rendered_prompt.rstrip() + "\n", candidate_sql.strip()
    if SQL_MARKER not in training_text:
        raise ValueError(f"{row['sample_id']} has no SQL target marker")
    prompt, embedded_sql = training_text.rsplit(SQL_MARKER, 1)
    if embedded_sql.strip() != candidate_sql.strip():
        raise ValueError(f"{row['sample_id']} target SQL mismatch")
    return prompt + SQL_MARKER, candidate_sql.strip()


class CausalSqlDataset(Dataset[dict[str, torch.Tensor]]):
    """因果语言模型微调数据集
    标签掩码策略：Prompt部分labels=-100（损失屏蔽，不计算loss），仅目标SQL+EOS参与损失计算
    ⚠️ 拒绝截断超长样本，超长直接报错，避免SQL目标被截断损坏训练
    """
    def __init__(
        self,
        rows: list[dict[str, Any]],
        tokenizer: Any,
        max_seq_length: int,
    ) -> None:
        self.examples: list[dict[str, torch.Tensor]] = []
        # 数据集统计信息，后续写入审计报告
        self.stats = {"samples": 0, "max_sequence_tokens": 0, "max_target_tokens": 0}
        for row in rows:
            prompt, target = split_prompt_and_target(row)
            prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            target_ids = tokenizer(target, add_special_tokens=False)["input_ids"]
            # 完整输入序列 = Prompt + TargetSQL + EOS
            input_ids = prompt_ids + target_ids + [tokenizer.eos_token_id]
            # label：Prompt部分‑100屏蔽，SQL和EOS计算损失
            labels = [-100] * len(prompt_ids) + target_ids + [tokenizer.eos_token_id]
            if len(input_ids) != len(labels) or not target_ids:
                raise ValueError(f"invalid label layout for {row['sample_id']}")
            if len(input_ids) > max_seq_length:
                raise ValueError(
                    f"{row['sample_id']} has {len(input_ids)} tokens above max_seq_length={max_seq_length}; "
                    "refuse to truncate target SQL"
                )
            self.examples.append(
                {
                    "input_ids": torch.tensor(input_ids, dtype=torch.long),
                    "attention_mask": torch.ones(len(input_ids), dtype=torch.long),
                    "labels": torch.tensor(labels, dtype=torch.long),
                }
            )
            self.stats["samples"] += 1
            self.stats["max_sequence_tokens"] = max(
                self.stats["max_sequence_tokens"], len(input_ids)
            )
            self.stats["max_target_tokens"] = max(
                self.stats["max_target_tokens"], len(target_ids) + 1
            )

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return self.examples[index]


@dataclass
class CausalSqlCollator:
    """自定义batch收集器：动态右padding
    labels填充值‑100，padding部分不计入损失
    """
    pad_token_id: int

    def __call__(self, features: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
        max_length = max(feature["input_ids"].size(0) for feature in features)
        batch: dict[str, list[torch.Tensor]] = {"input_ids": [], "attention_mask": [], "labels": []}
        for feature in features:
            padding = max_length - feature["input_ids"].size(0)
            batch["input_ids"].append(
                torch.nn.functional.pad(feature["input_ids"], (0, padding), value=self.pad_token_id)
            )
            batch["attention_mask"].append(
                torch.nn.functional.pad(feature["attention_mask"], (0, padding), value=0)
            )
            batch["labels"].append(
                torch.nn.functional.pad(feature["labels"], (0, padding), value=-100)
            )
        return {name: torch.stack(values) for name, values in batch.items()}


def count_parameters(model: torch.nn.Module) -> tuple[int, int]:
    """统计可训练参数 / 模型全部参数，用来输出LoRA占比"""
    total = sum(parameter.numel() for parameter in model.parameters())
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    return trainable, total


def latest_metric(log_history: list[dict[str, Any]], key: str) -> float | None:
    """从Trainer日志历史取出最新一条指标，用于审计报告"""
    values = [item[key] for item in log_history if key in item]
    return float(values[-1]) if values else None


def main() -> int:
    """主训练流水线"""
    args = parse_args()

    # 关闭NCCL P2P、IB，单卡训练规避RTX40系显卡的通信报错
    os.environ.setdefault("NCCL_P2P_DISABLE", "1")
    os.environ.setdefault("NCCL_IB_DISABLE", "1")

    # 环境合法性校验
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for SFT smoke")
    if args.max_seq_length <= 0:
        raise ValueError("max sequence length must be positive")
    # max‑steps / epochs 二选一配置校验
    if args.num_train_epochs is None and args.max_steps <= 0:
        raise ValueError("max steps must be positive when epochs are not configured")
    if args.num_train_epochs is not None and args.num_train_epochs <= 0:
        raise ValueError("num train epochs must be positive")
    if min(
        args.gradient_accumulation_steps,
        args.per_device_train_batch_size,
        args.per_device_eval_batch_size,
        args.evaluation_steps,
        args.save_steps,
        args.logging_steps,
    ) <= 0:
        raise ValueError("batch and step interval arguments must be positive")
    if args.weight_decay < 0:
        raise ValueError("weight decay must be non-negative")

    # 强约束：输出目录、断点目录禁止放在Git仓库内，避免大文件提交污染版本库
    repository_root = Path(__file__).resolve().parents[3]
    if args.output_dir.resolve().is_relative_to(repository_root):
        raise ValueError("output directory must be outside the Git working tree")
    if args.resume_from_checkpoint is not None:
        if args.resume_from_checkpoint.resolve().is_relative_to(repository_root):
            raise ValueError("resume checkpoint must be outside the Git working tree")
        if not args.resume_from_checkpoint.is_dir():
            raise FileNotFoundError(args.resume_from_checkpoint)

    # 加载训练、验证数据集
    train_rows = load_rows(args.train_jsonl, "train")
    validation_rows = load_rows(args.validation_jsonl, "validation")

    # 校验训练集验证集Prompt版本一致
    train_prompt_format = prompt_format_version(train_rows)
    validation_prompt_format = prompt_format_version(validation_rows)
    if train_prompt_format != validation_prompt_format:
        raise ValueError("train and validation splits use different prompt formats")

    # 读取切分审计文件，校验数据集切分流程已经通过所有安全检查
    split_audit = json.loads(args.split_audit.read_text(encoding="utf-8"))
    validate_split_audit(split_audit, args.train_jsonl, args.validation_jsonl)
    frozen_max_seq_length = required_max_seq_length(split_audit)
    if frozen_max_seq_length is not None and frozen_max_seq_length != args.max_seq_length:
        raise ValueError("max_seq_length does not match the materialized length contract")

    # 固定全部随机种子，保证实验可复现
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)

    # 加载分词器；如果没有pad_token，则使用eos_token补齐；右padding
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # 构建PyTorch数据集
    train_dataset = CausalSqlDataset(train_rows, tokenizer, args.max_seq_length)
    validation_dataset = CausalSqlDataset(validation_rows, tokenizer, args.max_seq_length)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = args.output_dir / "adapter_checkpoints"

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    # 模型加载参数配置
    model_load_kwargs: dict[str, Any] = {
        "local_files_only": True,
        "torch_dtype": torch.bfloat16,
        "device_map": {"": 0},
    }
    # 开启4bit‑QLoRA量化配置
    if args.base_weight_mode == "qlora_4bit":
        model_load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )

    # 加载基座大模型
    model = AutoModelForCausalLM.from_pretrained(args.model_dir, **model_load_kwargs)
    model.config.use_cache = False

    # 开启梯度检查点，节省显存
    if args.base_weight_mode == "qlora_4bit":
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    elif hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()

    # 注入LoRA适配器；目标模块覆盖Llama/Mistral系列全部注意力+MLP层
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            bias="none",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            task_type="CAUSAL_LM",
        ),
    )

    trainable, total = count_parameters(model)
    planned_max_steps = -1 if args.num_train_epochs is not None else args.max_steps

    optimizer_name = "adamw_torch" if args.base_weight_mode == "bf16_lora" else "paged_adamw_8bit"
    # HuggingFace Trainer训练参数
    training_args = TrainingArguments(
        output_dir=str(checkpoint_dir),
        overwrite_output_dir=args.resume_from_checkpoint is None,
        do_train=True,
        do_eval=True,
        eval_strategy="steps",
        eval_steps=args.evaluation_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        max_steps=planned_max_steps,
        num_train_epochs=args.num_train_epochs if args.num_train_epochs is not None else 3.0,
        lr_scheduler_type="constant",
        logging_strategy="steps",
        logging_steps=args.logging_steps,
        logging_first_step=True,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=2,  # 最多保存2个checkpoint，防止磁盘爆满
        bf16=True,
        tf32=True,
        # bf16 LoRA 的默认优化器不量化 optimizer state；QLoRA 历史模式仍使用 paged AdamW。
        optim=optimizer_name,
        weight_decay=args.weight_decay,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[], # 关闭wandb等云端日志上报
        remove_unused_columns=False,
        seed=args.seed,
        data_seed=args.seed,
        dataloader_num_workers=0,
    )

    # 初始化训练器
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        data_collator=CausalSqlCollator(tokenizer.pad_token_id),
    )

    started = datetime.now(timezone.utc)
    # 启动训练（支持断点续训）
    train_result = trainer.train(
        resume_from_checkpoint=(str(args.resume_from_checkpoint) if args.resume_from_checkpoint else None)
    )
    # 训练结束后跑一次验证集评估
    evaluation = trainer.evaluate()

    # 保存最终LoRA适配器权重
    adapter_dir = args.output_dir / "adapter_final"
    trainer.save_model(str(adapter_dir))
    trainer.save_state()

    elapsed_seconds = (datetime.now(timezone.utc) - started).total_seconds()

    # 读取模型下载清单，写入审计报告溯源基座版本
    model_manifest = json.loads((args.model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    if not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    # GPU‑UUID校验，如果指定了预期显卡，则必须匹配，防止任务跑错设备
    if args.expected_gpu_uuid is not None and gpu_uuid != args.expected_gpu_uuid:
        raise RuntimeError(
            "CUDA device UUID does not match launcher guard: "
            f"expected {args.expected_gpu_uuid}, got {gpu_uuid}"
        )

    # 生成完整实验证据清单 sft_smoke.json，记录全部超参、数据集哈希、硬件、指标，实现全链路可复现
    evidence = {
        "experiment_type": "adapter_sft_coverage_ablation",
        "experiment_label": args.experiment_label,
        "started_at": started.replace(microsecond=0).isoformat(),
        "model": {
            "id": model_manifest["model_id"],
            "revision": model_manifest["revision"],
            "download_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
        },
        "data": {
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "split_audit_sha256": sha256_file(args.split_audit),
            "train": train_dataset.stats,
            "validation": validation_dataset.stats,
            "prompt_format_version": train_prompt_format,
            "raw_question_or_sql_saved": False,
            "v2_holdout_used": False,
        },
        "training": {
            "seed": args.seed,
            "max_seq_length": args.max_seq_length,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "per_device_eval_batch_size": args.per_device_eval_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "global_batch_size": args.per_device_train_batch_size * args.gradient_accumulation_steps,
            "max_steps": planned_max_steps,
            "num_train_epochs": args.num_train_epochs,
            "evaluation_steps": args.evaluation_steps,
            "save_steps": args.save_steps,
            "logging_steps": args.logging_steps,
            "resumed_from_checkpoint": (
                str(args.resume_from_checkpoint) if args.resume_from_checkpoint else None
            ),
            "learning_rate": args.learning_rate,
            "optimizer": optimizer_name,
            "weight_decay": args.weight_decay,
            "gradient_checkpointing": True,
            "base_weight_mode": args.base_weight_mode,
            "load_in_4bit": args.base_weight_mode == "qlora_4bit",
            "quant_type": "nf4" if args.base_weight_mode == "qlora_4bit" else None,
            "double_quant": args.base_weight_mode == "qlora_4bit",
            "compute_dtype": "bfloat16",
            "lora_r": args.lora_r,
            "lora_alpha": args.lora_alpha,
            "lora_dropout": args.lora_dropout,
            "trainable_parameters": trainable,
            "total_parameters": total,
            "trainable_percent": round(trainable / total * 100, 6),
            "nccl_p2p_disable": os.environ["NCCL_P2P_DISABLE"],
            "nccl_ib_disable": os.environ["NCCL_IB_DISABLE"],
        },
        "results": {
            "global_step": int(trainer.state.global_step),
            "train_loss": float(train_result.training_loss),
            "last_logged_train_loss": latest_metric(trainer.state.log_history, "loss"),
            "evaluation_loss": float(evaluation["eval_loss"]),
            "elapsed_seconds": elapsed_seconds,
        },
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "process_local_device": 0,
            "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
            "name": gpu.name,
            "uuid": gpu_uuid,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        },
        "versions": {
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "peft": __import__("peft").__version__,
            "bitsandbytes": __import__("bitsandbytes").__version__,
        },
        "outputs": {
            "adapter_dir": str(adapter_dir),
            "checkpoint_dir": str(checkpoint_dir),
            "adapter_is_lora_only": True,
            "production_postgres_or_vanna_modified": False,
        },
    }
    evidence_path = args.output_dir / "sft_smoke.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
