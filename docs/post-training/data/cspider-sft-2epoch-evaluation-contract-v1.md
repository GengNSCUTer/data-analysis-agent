# CSpider 两 Epoch 训练与成对评测合同 v1

## 目标

这是一个已完成训练、但尚未运行完整生成质量评测的离线实验合同。它训练
`Qwen/Qwen2.5-Coder-1.5B` 的一个未量化 bf16 LoRA adapter，然后在 CSpider 官方 validation
上比较同一 bf16 基座与该 adapter 的候选 SQL。它不修改可信 PostgreSQL/Vanna 运行时，不将模型
接入产品。

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

训练完成并通过 fresh reload 后，CSpider 专用的成对生成入口已经实现并通过本地回归，但尚未运行。
双方只看到
同一顺序的 validation schema 与中文问题，不能读取 validation gold SQL、数据库行、final test
或 Olist/PostgreSQL 运行时上下文。唯一变量是 Adapter 是否加载；基座 revision、bf16 加载、
tokenizer、prompt、greedy decode、seed、token 上限和 SQLite 诊断策略完全相同。

生成结束并冻结两侧候选后，才能读取 validation gold SQL 做只读的 bounded denotation audit。
评测必须覆盖 SQLite 执行/策略状态、Base 到 Adapter 的状态迁移、受限结果等价性和固定数量的
changed-case 人工复核。当前通用 Spider 生成器不能直接冒充 CSpider 评测入口，必须先为
CSpider 输入和 case identity 完成适配与回归测试。

## 已实现的评测入口

规范生成器 `scripts/post_training/inference/generate_post_training_text_to_sql.py` 新增
`--dataset cspider_validation`。该模式要求外部 acquisition manifest，且在加载模型前核验
`dev.json`/`tables.json` 的 SHA-256、`dev=validation_only` 角色与 1,034 条记录数；生成只保留
`db_id`、问题和 schema，不读取 gold SQL、SQLite 数据行、final test 或业务运行时上下文。

两个候选分别写入仓库外目录后，`verify_cspider_matching_generation.py` 必须先读取两份 generation
evidence 与 prediction JSONL，只检查证据、哈希、完整 source-order case ID 覆盖和唯一变量。它要求
同一模型 revision、bf16、prompt、input/new token 上限、greedy decode、源文件/manifest hash；Base
必须只声明 adapter disabled，Adapter 必须声明已加载并提供 adapter 哈希。验证报告不写问题、SQL、
库标识或结果行。随后才允许只读 SQLite diagnostics、paired 状态分析和 bounded denotation audit。

`start_post_training_cspider_base_adapter_evaluation_screen.sh` 将 Base 和 Adapter 在同一受 UUID
守卫的 GPU 上顺序生成，先运行上述 verifier，再执行 CSpider SQLite、paired analysis 与生成后
denotation。它不引用 Spider Test Suite，也不引用或读取 CSpider final test。本轮对入口的回归为 15
项，覆盖 source hash/role 漂移、gold SQL 非读取、case namespace、报告脱敏、证据/配置漂移和 case
顺序缺失；没有启动 1,034 条生成，也没有产生任何 SQL 质量指标。

该合同不包含官方榜单成绩、CSpider final test、Olist 业务迁移或生产替换结论。完整 machine-readable
版本为 [`cspider_bf16_lora_2epoch_v1.yaml`](../../../evals/manifests/cspider_bf16_lora_2epoch_v1.yaml)。
