# CSpider 两 Epoch 训练与成对评测合同 v1

## 目标

这是一个版本冻结但尚未启动的离线实验合同。它训练 `Qwen/Qwen2.5-Coder-1.5B` 的一个
未量化 bf16 LoRA adapter，然后在 CSpider 官方 validation 上比较同一 bf16 基座与该
adapter 的候选 SQL。它不修改可信 PostgreSQL/Vanna 运行时，不将模型接入产品。

## 冻结训练变量

训练只读取 `official-splits-length1536-v1/train.jsonl` 的 8,574 行；validation 只读取
同目录的 1,034 行；final test 2,147 行不传给训练入口。`max_seq_length=1536`，超长
截断被禁止。使用 bf16 冻结基座、LoRA `r=16/alpha=32/dropout=0.05`、普通
`adamw_torch`、学习率 `1e-4`、`weight_decay=0.01`、`batch=4`、`accumulation=1`。

训练恰好运行两轮。8,574 条样本按 batch 4 对应每 epoch 2,144 次 optimizer update，
因此预期共 4,288 global steps。每 536 step 对 validation 计算 loss 并保存 checkpoint；
这些 loss 只用于训练稳定性观察，不以中途最佳 checkpoint 替换末尾 adapter，也不以它
推断 SQL 语义正确。

任务使用 logical CUDA `1`（本机 physical GPU `3` 的 RTX 4090），并强制校验
`GPU-10863af0-8588-7625-5609-640ba794f64b`。任何 UUID、split audit、长度清单、有限
loss 或 adapter reload 失败都是停止条件，而不是继续训练或改超参的理由。

## Matching Base/Adapter 评测

训练完成并通过 fresh reload 后，才实现并运行 CSpider 专用的成对生成入口。双方只看到
同一顺序的 validation schema 与中文问题，不能读取 validation gold SQL、数据库行、final test
或 Olist/PostgreSQL 运行时上下文。唯一变量是 Adapter 是否加载；基座 revision、bf16 加载、
tokenizer、prompt、greedy decode、seed、token 上限和 SQLite 诊断策略完全相同。

生成结束并冻结两侧候选后，才能读取 validation gold SQL 做只读的 bounded denotation audit。
评测必须覆盖 SQLite 执行/策略状态、Base 到 Adapter 的状态迁移、受限结果等价性和固定数量的
changed-case 人工复核。当前通用 Spider 生成器不能直接冒充 CSpider 评测入口，必须先为
CSpider 输入和 case identity 完成适配与回归测试。

该合同不包含官方榜单成绩、CSpider final test、Olist 业务迁移或生产替换结论。完整 machine-readable
版本为 [`cspider_bf16_lora_2epoch_v1.yaml`](../../../evals/manifests/cspider_bf16_lora_2epoch_v1.yaml)。
