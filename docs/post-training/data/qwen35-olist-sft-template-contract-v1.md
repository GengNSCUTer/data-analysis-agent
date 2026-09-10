# Qwen3.5 Olist Instruct SFT 模板合同 v1

**合同 ID：** `qwen35-olist-instruct-sft-v1`

**状态：** 训练前 token/label 准入；尚未启动 Qwen3.5 LoRA。

**范围：** 将既有冻结 Olist Release v2 的 `train` / `validation` 行包装为 Qwen3.5-4B Instruct 的 SQL-only SFT 输入；不重新生成问题、QuerySpec、Gold SQL 或 split。

## 目标与非目标

目标是让 Qwen3.5 的后训练权重保留原生中文指令与稳定停止能力，并学习当前服务器的业务 Prompt 如何编译为 canonical PostgreSQL candidate SQL。TheLook v2 是受保护跨 schema final test，不能在本合同的训练、验证、样本选择、few-shot 或模板调整中出现。

这不是把 Qwen3.5 接入产品默认路径，也不训练 QuestionRouter、Semantic Catalog、QuerySpec renderer、SqlPolicy、数据库角色或 ResultValidator。模型输出仍只是候选 SQL，运行时治理链路保持不变。

## 精确输入与监督边界

每一行继续复用既有的真实运行时输入：

```text
row.rendered_prompt
= Candidate contract + Semantic Catalog + QueryPlan + ResultContract + 中文问题 + ### SQL
```

它作为**唯一 user message 的 text content**，不改写为 SQLite schema-only Prompt，也不向内容注入 Gold SQL、结果行或额外 few-shot。assistant message 的唯一 text content 是 `row.candidate_sql.strip()`。

使用本地冻结的 Qwen3.5-4B Instruct `AutoProcessor.apply_chat_template()`：

```python
prefix = apply_chat_template([user], add_generation_prompt=True,
                             enable_thinking=False)
full   = apply_chat_template([user, assistant], add_generation_prompt=False,
                             enable_thinking=False)
```

`full` 必须以 `prefix` 为逐 token 前缀，否则拒绝该模型模板。Qwen3.5 non-thinking 模板仍会在 assistant 开始位置序列化空的 `<think>...</think>`；它属于 `prefix`，和 user message、角色标记、assistant 起始标记一起全部设为 `labels=-100`。

监督部分仅为：

```text
canonical PostgreSQL SQL + assistant end-of-turn token (<|im_end|>)
```

模板在 EOT 后序列化的换行不参与监督，因为推理会在 EOT 停止；这样训练目标与 `generate(..., eos_token_id=<|im_end|>)` 一致。batch 右侧 padding 使用 `attention_mask=0`、`labels=-100`，不参与 loss。任何超过冻结上限的 Prompt 或 SQL 都拒绝，不做静默截断。

## 训练与推理一致性

| 项目 | 固定值 |
| --- | --- |
| 权重 | `Qwen/Qwen3.5-4B`（后训练 Instruct，不是 `-Base`） |
| 消息模板 | 模型目录中官方 chat template |
| thinking | `enable_thinking=False` |
| user 内容 | exact `rendered_prompt` |
| assistant 内容 | SQL-only canonical SQL |
| 结束条件 | assistant `<|im_end|>` / model EOS |
| 推理解码 | 后续 matching 继续使用 greedy，禁止把 TheLook 用于模板调参 |

## 准入证据

训练前必须在仓库外生成 `qwen35-olist-sft-layout-audit-v1`，并证明：

1. 原始 Olist family-isolated split audit 与 train/validation 文件哈希一致；
2. 训练 / 验证行数、Prompt token、SQL+EOT token 和总 token 的聚合范围；
3. 每一行均满足完整 template prefix、目标 EOT、`labels` mask 与长度上限；
4. 不保存问题、SQL、completion 或 TheLook 内容；
5. token audit 通过后，才允许审阅 Qwen3.5 专用 Trainer/LoRA 配置与 GPU smoke。
