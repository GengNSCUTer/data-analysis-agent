# 后训练实验台账

本台账只记录已完成实验和待评测的受控实验，不在这里讲通用概念。原始训练样本、SQL、预测、数据库、模型权重、checkpoint 和完整日志都留在仓库外；这里仅记录可复核的配置、哈希和聚合结果。

## 固定边界

- 研究模型：`Qwen/Qwen2.5-Coder-1.5B`，revision `df3ce67c0e24480f20468b6ef2894622d69eb73b`。
- 训练数据：历史 v1 smoke 使用 Spider train-only 128 条、102/26 schema-disjoint split；当前 v2 使用 3,600 条候选、3,048/552 schema-disjoint split。两者均不读取 dev gold SQL。
- 评测数据：当前 v2 使用固定的官方 Spider 1.0 release `dev.json`（1,034 prompts）；生成不读取其 gold SQL。历史 2020-01 mirror 结果单独保留，不与当前 release 混用。Test Suite 输出只作为固定资产组合的内部对照，不写为当前官方榜单成绩。
- 永久隔离：项目的 60 条 v2 golden 不进入训练、偏好数据、示例或改写。
- 运行时隔离：生产 Vanna/FastAPI/PostgreSQL 代码不被离线训练改写；模型候选仍需通过服务器安全和结果合同。

## 最近完成：官方 Spider release Base/Adapter 成对质量评测

2026-08-26，官方 Spider release 的 Base 与 26-step QLoRA Adapter 均完成 1,034/1,034 候选生成、只读 SQLite 诊断、固定 Test Suite bridge 和脱敏 paired analysis。Base 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090；Adapter 使用 logical CUDA `0` -> physical GPU `2` 的 RTX 4090。两条 `screen` 已正常退出。

SQLite executed 为 `829 -> 671`，execution error 为 `201 -> 360`，policy rejected 为 `4 -> 3`；Test Suite internal all 为 `0.433 -> 0.376`。状态迁移显示 240 条 Base 可执行候选在 Adapter 中退化，81 条错误候选恢复为可执行，净损失 158 条 executed。`no_such_column` 为 `182 -> 322`，其中限定列引用为 `15 -> 296`。Adapter 平均生成 token 从 `123.86` 降至 `36.9`，但规范化 SQL 中位长度从 `86` 增至 `121`，说明“更早停止”不是质量提升。

本轮结论是该 26-step Adapter 质量门失败。该结果只说明当前小数据、prompt 和超参组合回退，不归因于 QLoRA 本身；下一步转入官方 train-only 数据扩展与 Schema prompt v2，不进入 DPO/GRPO。完整报告见 [`post-training-official-base-adapter-analysis-v1.md`](post-training-official-base-adapter-analysis-v1.md)。

## 最近完成：Spider SFT v2 独立 Schema-Stratified 复验

官方 `train_spider.json` 已构造为 3,600 条可训练候选：全量覆盖 139 个可满足 token 预算的 Spider schema，使用 `spider-sft-schema-question-sql-v2`，每个列以 `table.column` 形式序列化，并保留列类型、PK 和 fully-qualified FK。构造阶段通过对应 SQLite 的只读 `EXPLAIN`，排除 3 条执行不兼容样本；随后用 Qwen tokenizer 的 1,536 token 硬预算排除 29 条过长样本，最大保留序列为 1,443 token，不发生静默截断。

最终 split 为 3,048 train / 552 validation，分别来自 118 / 21 个不重叠 schema。跨 schema 的通用 SQL text shape 有 6 个重叠，作为计数证据保留但不阻断，因为 `COUNT`、`GROUP BY` 等结构重叠不等于 schema 泄漏；所有表、列、外键 identity 仍被 schema-disjoint 边界隔离。2 epoch bf16 LoRA 已在物理 GPU 3 的 RTX 4090 完成并 fresh reload；前缀 100-case smoke 的 SQLite executed 为 `94 -> 89`，但 post-generation bounded denotation audit 为 `56 -> 69`。

为避免按前缀选择的 3 个 schema 造成误判，独立复验以固定 seed 排除前 100 条及其出现的全部 schema，从 17 个未观察 schema 选择 164 条。生成输入不含 gold SQL，gold 仅在 Base/Adapter 输出冻结后用于仓库外 bounded denotation audit。复验结果为 denotation match `97 -> 122`、SQLite executed `153 -> 155`、`no_such_column` `9 -> 8`，所有预冻结质量门通过。它允许进入完整 1,034-case 对照，但不是官方 Spider 分数，也不允许 production 接入。完整合同、哈希与限制见 [`post-training-spider-sft-v2-plan.md`](post-training-spider-sft-v2-plan.md)。

## 当前运行

2026-08-26 12:12 CST 启动的 Spider SFT v2 训练、前缀 Base/Adapter smoke 与独立 164-case 复验均已完成。主训练使用 logic CUDA `1` -> physical GPU `3` 的 RTX 4090；前缀 Base 使用 logic CUDA `0` -> physical GPU `2`，独立复验使用两张空闲 4090，均通过进程内 UUID guard。前缀对照的执行护栏失败，但独立复验的语义主判据及两项执行护栏均通过。下一项待启动任务是完整官方 dev 1,034-case 的 bf16 Base/Adapter 对照与其后的固定 Test Suite bridge。

2026-08-25 18:06 CST 启动的两条独立完整质量评测已经完成；以下历史启动表保留用于追溯，不再表示任务仍在运行。

| ID | `screen` 会话 | 当前阶段 | 对照合同 | 状态 |
| --- | --- | --- | --- | --- |
| `spider_sft_v2_bf16_lora` | `daa-qwen15b-spider-sft-v2-train` | 2 epoch SFT + fresh reload | official train-only、v2 prompt、schema-disjoint split | 已完成；训练/reload 工程通过，尚无独立质量结论。 |
| `spider_sft_v2_prefix_smoke100` | `daa-qwen15b-spider-sft-v2-base-smoke` / `daa-qwen15b-spider-sft-v2-adapter-smoke` | 100-case Base/Adapter diagnostics + denotation audit | official dev 前缀、v2 prompt、greedy decode、跳过 Test Suite | 已完成；SQLite `94 -> 89`、denotation `56 -> 69`，不作全量放行结论。 |
| `spider_sft_v2_independent_smoke164` | 已完成 | 164-case Base/Adapter diagnostics + denotation audit | 排除前缀 schema、schema-stratified、v2 prompt、greedy decode、跳过 Test Suite | 通过；denotation `97 -> 122`、SQLite `153 -> 155`、`no_such_column` `9 -> 8`。 |
| `official_base_adapter_pair_v1` | `daa-qwen15b-base-official-v1` / `daa-qwen15b-adapter-official-v1` | 官方 release Base 与 26-step QLoRA Adapter | 同 prompt、greedy decode、normalizer、SQLite diagnostics、pinned Test Suite | 已完成；质量门失败，详见官方专属分析。 |

本轮的两个 screen 已正常退出。脚本在进程内核验 UUID，未抢占或停止其他用户 GPU 进程；原始证据与聚合报告均保留在仓库外。

## 已完成

| ID | 假设 / 目的 | 配置 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| `forward-smoke-v1` | 验证 1.5B 4-bit QLoRA 加载、mask 与显存 | QLoRA, LoRA r=16, 单 train-only 样本 | finite loss 0.884486，peak allocated 约 1.94 GiB | 工程链路可用；没有 backward 或质量结论。 |
| `sft-smoke-v1` | 验证训练、checkpoint、adapter reload | QLoRA, 8 steps, effective batch 4 | train/eval loss 0.556203/0.466989，adapter 74 MB，peak allocated 约 3.31 GiB | 只说明工程可跑；8 steps 约 0.31 epoch。 |
| `base-adapter-pair-v1` | 验证首轮 adapter 是否改善生成行为 | 相同 4-bit base、prompt、greedy decode、1,034 dev | SQLite executed 831 -> 666；Test Suite internal all 0.427 -> 0.215 | 负向 ablation；不支持“QLoRA 提升 Text-to-SQL”的说法。 |
| `analysis-v1` | 确认回退模式 | 上述 pair 的聚合诊断与受限人工 changed-case 审核 | 253 条从 executed 变失败，88 条反向恢复；20 个库中 17 回退、3 不变；抽检的 4 条表面恢复均不满足语义 | 不能用执行 recovery 代替正确率；下一步先控制变量。 |
| `qlora_coverage26_v1` | 验证约一轮训练覆盖后的 QLoRA 工程路径 | 4-bit NF4，26 steps，effective batch 4，LoRA r16/alpha32/dropout0.05 | train/eval loss 0.427482/0.290193；peak allocated 4.35 GiB；fresh reload finite loss 0.249638 | 训练和 reload 通过；尚未与对应 4-bit base 做全量质量对照。 |
| `bf16_lora_coverage26_v1` | 在同配置下验证直接 bf16 LoRA 是否可行 | bf16 base，26 steps，其余与 QLoRA 相同 | train/eval loss 0.426504/0.309192；peak allocated 5.19 GiB；fresh reload finite loss 0.245578 | 1.5B 可在 24GB 卡做 bf16 LoRA；尚未与对应 bf16 base 做全量质量对照。 |
| `spider_sft_v2_independent_smoke164` | 检验 v2 SFT 的独立非回退资格 | 164 条/17 未观察 schema，bf16 Base/Adapter、v2 prompt、greedy decode | denotation `97 -> 122`，SQLite `153 -> 155`，`no_such_column` `9 -> 8` | 通过预冻结 bounded smoke 门，进入完整 1,034-case 对照；不是官方分数。 |

本轮的外部训练/reload evidence SHA-256：`qlora_coverage26_v1` 为 `c00413757f24c3bfc338b5eec3dfe689720ec03be9e55be8a415d6d0c96f587e` / `c83fa84ab2b845bbb8dc94a2a92d8b67aa786d59fccfa4d60f295ffb23cec36a`；`bf16_lora_coverage26_v1` 为 `266a6f9a4471ca9734ebd7a01829ef8a601e7628023570e08b441369bc039afd` / `1e68125d350364c8a6d8a1734cae6c4a9622576ae945e24810f936be2a877cdb`。每对依次对应 `sft_smoke.json` 和 `adapter_validation.json`。

## 下一道质量门

| ID | 已冻结的训练变量 | 必须新增的对照 | 质量门 |
| --- | --- | --- | --- |
| `qlora_coverage26_v1` | 102/26 split、26 steps、seed 20260825、lr 2e-4、effective batch 4、LoRA r16/alpha32/dropout0.05、4-bit NF4 | 使用同一 4-bit base 对 1,034 dev 做 base/adapter 成对生成、SQLite diagnostics 与 Test Suite bridge | 不能比对应 base 退化；随后做状态迁移和人工 changed-case 审核。 |
| `bf16_lora_coverage26_v1` | 其余完全相同，唯一改为 bf16 frozen base | 先建立 bf16 base，再与 bf16 adapter 使用相同 1,034 dev/greedy/evaluator 合同 | 不能把 bf16 adapter 与 4-bit base 直接比较；必须先消除加载精度这一混杂变量。 |

这两个任务并行只节省墙钟时间，不改变统计含义。它们分别在 logic 0 -> physical 2 和 logic 1 -> physical 3 的 4090 上运行，脚本在进程内校验 UUID，避免把 `CUDA_VISIBLE_DEVICES` 的逻辑编号误当成 `nvidia-smi` 物理编号。

## 结果写入规则

一个训练任务完成后，依次记录：

1. `sft_smoke.json` 中的模型 revision、数据 SHA-256、steps、loss、GPU UUID、peak memory 和 adapter hash。
2. 同精度 base 与 adapter 的完整 1,034-case 生成、SQLite diagnostics 和 Test Suite bridge。
3. 状态迁移、错误类别和有限人工 changed-case 审核。
4. 是否满足“不比对应 base 回退”的门槛。未满足则记录失败假设，不能跳到 DPO/GRPO。

只有这四层都有证据，才更新“模型质量”结论；训练完成、loss 变小或 SQL 外观更像 SQL 都不够。
