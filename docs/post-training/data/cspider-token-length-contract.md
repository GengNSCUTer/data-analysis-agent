# CSpider SFT 训练长度合同 v1

## 目标与范围

本合同把 CSpider 官方 `train` 与 `validation` 的单样本 token 上限冻结为可复核的训练输入边界。它只审计仓库外官方派生资产：

- `train.jsonl`：8,656 条；
- `validation.jsonl`：1,034 条；
- `split_audit.json`：`official_cspider_train_dev_test` 协议。

最终 `final_evaluation_only/test.jsonl` 没有被读取、分词、统计或物化；它继续禁止用于训练、验证、模型选择、长度决策和数据合成。本次工作没有加载模型权重、使用 GPU、启动训练或生成过滤后的数据集。

## 冻结合同

| 项 | 规则 |
| --- | --- |
| 合同版本 | `cspider-token-length-v1` |
| `max_seq_length` | `1536` |
| tokenizer | 本地 Qwen2.5-Coder-1.5B revision `df3ce67...` 的 `Qwen2TokenizerFast`；仅对 tokenizer 资产（`tokenizer.json`、`tokenizer_config.json`、`vocab.json`、`merges.txt`）计算指纹，不读取 `model.safetensors` |
| 单样本计数 | `tokenize(prompt + SQL marker, add_special_tokens=False) + tokenize(candidate SQL, add_special_tokens=False) + 1 EOS` |
| special token | 除末尾一个真实 EOS 外不额外添加 special token |
| padding | 不计入单样本长度；它只由 collator 在 batch 内右侧补齐，并使用 `attention_mask=0`、`labels=-100` |
| 超长行为 | 禁止静默截断。长度大于 1,536 的样本不得进入后续 SFT 物化输入；`CausalSqlDataset` 仍应 fail closed，拒绝任何绕过物化门的超长行。 |

这里的计数与 [`CausalSqlDataset`](../../../scripts/post_training/training/run_post_training_sft_smoke.py) 完全一致。它不衡量 SQL 的业务语义、可执行性以外的正确性，或模型生成质量；SQLite `EXPLAIN` 和 split audit 分别由独立的来源质量/隔离合同负责。

## 审计结果

审计脚本为 [`audit_cspider_sft_token_lengths.py`](../../../scripts/post_training/data/audit_cspider_sft_token_lengths.py)，报告位于仓库外的 `prepared/official-splits-v1/token-length-audit-v1.json`。报告固定生成时间为 `2026-09-02T00:00:00Z`，并记录输入 JSONL 与 split audit 的 SHA-256。

| split | 样本数 | sequence p50 / p90 / p95 / p99 / max | 超过 1,024 | 超过 1,536 | 1,536 可入选 |
| --- | ---: | --- | ---: | ---: | ---: |
| train | 8,656 | 352 / 713 / 961 / 1,438 / 3,228 | 267 | 82 | 8,574 (99.05%) |
| validation | 1,034 | 275 / 576 / 760 / 824 / 850 | 0 | 0 | 1,034 (100.00%) |

在 `2,048` 和 `3,072` 两个比较上限下，train 仍各有 82 条超长、validation 仍为 0 条。因此提高预算不会增加 CSpider v1 的可入选样本数；`1,536` 在当前证据下保留相同样本覆盖，同时避免无收益地增大训练显存与动态 padding 成本。

超长样本主要由 prompt/schema 长度导致：train prompt 最大为 3,095 token，目标 SQL 加 EOS 最大仅为 199 token；该观察不包含具体问题、schema 或 SQL 内容。

## 后续物化门

当前官方 `train.jsonl` 与 `validation.jsonl` 保持不变，本合同也没有删除或改写它们。开始训练前必须由一个单独批准的物化步骤完成以下事项：

1. 以本合同的 tokenizer、长度公式和 `1,536` 上限重新核验每行；
2. 为 train 与 validation 分别生成新的、仓库外的派生 JSONL 和 exclusion manifest；
3. manifest 记录源文件 hash、tokenizer hash、合同版本、保留/排除计数及每条排除的稳定 sample ID 与长度，但不复制问题、schema 或 SQL；
4. 保持官方 split 角色、`cspider_db_id` 无交集和 `final_evaluation_only` test 隔离；
5. 训练入口继续先验证 split audit，再验证派生输入与该 manifest，任何 hash、角色、长度或计数不一致均拒绝训练。

这一步尚未获准执行。因此当前状态是“训练长度已定义且有审计证据”，不是“CSpider 训练集已经物化”或“训练已经开始”。

## 验证与空白

- 真实审计成功读取且只读取 train/validation；报告显式记录 `final_test_read=false`、`model_weights_loaded=false`、`gpu_used=false` 和 `truncation_performed=false`。
- `tests/test_audit_cspider_sft_token_lengths.py` 覆盖独立 prompt/SQL/EOS 计数、超长统计、无静默截断字段和小样本 percentile。
- 与 CSpider split audit、SFT Dataset/collator 契约和 CSpider 构造器的组合专项测试共 `12 passed`。

本合同尚未覆盖未来物化器的 exclusion manifest、Trainer 对该 manifest 的输入校验、训练稳定性、验证集指标或最终 test 评测；它们必须按单一任务分别实现和验证。
