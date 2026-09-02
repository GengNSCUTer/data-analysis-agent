# CSpider bf16 LoRA 两 Epoch v1

## 目标与边界

本实验验证长度物化的 CSpider 官方 train/validation 能否完成两 epoch 的未量化 bf16
LoRA SFT，并产出可独立重载的 adapter；随后在相同 validation 输入上与匹配 Base 做离线
候选生成、SQLite 诊断和 bounded denotation 对照。它不使用 CSpider final test，不修改
PostgreSQL/Vanna 运行时，也不把本地 SQLite 结果写成官方榜单或生产业务准确率。

## 冻结配置

| 项目 | 值 |
| --- | --- |
| 数据 | `official-splits-length1536-v1`，train / validation 为 8,574 / 1,034 |
| final test | 2,147，未传给训练 runner |
| 基座 | `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 权重方式 | `bf16_lora`，无 4-bit quantization |
| SFT | 2 epoch、4,288 optimizer steps、batch 4、accumulation 1 |
| 优化 | `adamw_torch`、learning rate `1e-4`、weight decay `0.01` |
| LoRA | `r=16`、`alpha=32`、dropout `0.05` |
| 长度 | 1,536，禁止截断 |
| GPU | logical CUDA `1` -> physical GPU `3`，RTX 4090，UUID guard 通过 |

## 工程结果

训练正常退出，`global_step=4,288`、`epoch=2.0`，总用时 `2,330.47` 秒。完整 validation
在训练过程中每 536 step 运行，共 8 个独立 checkpoint 事件；训练结束后又执行一次同配置的
完整 validation。末尾汇总 train loss 为 `0.117384`、末尾 logged train loss 为 `0.0608`、
末尾 full-validation loss 为 `0.318318`。训练峰值 allocated/reserved 为
`16,481,064,448 / 24,865,931,264` bytes，约 `15.35 / 23.16 GiB`，未发生 OOM。

最终 LoRA-only adapter 已保存，`adapter_model.safetensors` 为 `73,911,112` bytes，SHA-256 为
`35eb45c0ebccaaeaf2cefb742473788031eb01bb3948412b2f35c5840c974983`。新的独立进程已重新加载
同一 bf16 基座和 adapter；PEFT 加载成功、GPU UUID guard 通过，并在 validation `cspider_dev:00000`
上得到有限 forward loss `0.018230`。这验证 artifact 不依赖原训练进程。

## 验证曲线与解读

| step / epoch | full validation loss |
| --- | ---: |
| 536 / 0.25 | 0.289461 |
| 1,072 / 0.50 | **0.278697** |
| 1,608 / 0.75 | 0.287931 |
| 2,144 / 1.00 | 0.282592 |
| 2,680 / 1.25 | 0.317112 |
| 3,216 / 1.50 | 0.332047 |
| 3,752 / 1.75 | 0.300258 |
| 4,288 / 2.00 | 0.318318 |

最小 validation loss 出现在 step 1,072；末尾值较它高 `14.22%`，较首个 validation 高
`9.97%`。这表明在当前训练数据、常数学习率和 schema-disjoint validation 下，末尾 adapter
并非 validation-loss 最优。它是后续实验需要控制的现象，不足以证明过拟合的业务影响，也不能
据此直接挑选或宣称 `checkpoint-1072` 更好的 SQL 生成质量。

## Matching Base/Adapter 生成与评测结果

两侧均在生成阶段完整覆盖 CSpider official validation 的 1,034 条记录，并通过 matching
verifier。Base 与 Adapter 的模型 revision、bf16 加载、prompt v2、输入/输出 token 上限、
greedy decode、seed、数据文件 hash、source order case ID 均一致，唯一变量是 adapter 是否加载。
Base 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090；Adapter 使用 logical CUDA `2` ->
physical GPU `0` 的 RTX 3090。硬件不同，因此生成耗时不能用于 Base/Adapter 质量或延迟比较。

| 指标 | Base | Adapter | 变化 |
| --- | ---: | ---: | ---: |
| 完整生成覆盖 | 1,034 / 1,034 | 1,034 / 1,034 | 相同 |
| SQLite executed | 911（88.10%） | 932（90.14%） | +21 |
| SQLite execution error | 116 | 102 | -14 |
| SQLite policy rejected | 7 | 0 | -7 |
| bounded denotation exact-or-bag match | 525（50.77%） | 743（71.86%） | +218 |
| strict ordered match | 507（49.03%） | 725（70.12%） | +218 |
| not executable in denotation audit | 123 | 102 | -21 |

结果迁移显示：289 条 Base 中不匹配或不可执行的 case 在 Adapter 中变为结果匹配，71 条原本
匹配的 case 退化为不匹配或不可执行，674 条保持同一匹配状态。SQLite status 迁移为
`execution_error -> executed` 93 条、`policy_rejected -> executed` 7 条，但也有 79 条
`executed -> execution_error`；因此不能只用执行率解释 denotation 提升。

Adapter 的生成形态也发生明显变化：Base 平均生成 108.29 tokens，257 条达到 256 token
上限，只有 406 条是直接 SQL 开头；Adapter 平均 29.61 tokens，没有命中上限，1,034 条
均为直接 SQL 开头。更短和更规整的输出与 denotation 正向变化同时出现，但不单独构成
生产延迟或吞吐结论。

本轮 bounded denotation 使用 validation gold SQL 仅在两侧生成冻结之后的只读阶段；报告只
保存状态和聚合数字，不保存问题、SQL、数据库标识或结果行。该结果说明在当前 CSpider
validation SQLite 快照、当前 prompt 和最终 adapter 下存在明显的离线候选质量提升，同时保留
71 条匹配退化和各类列/聚合错误。它不证明跨数据集泛化、中文 Olist/PostgreSQL 业务正确性、
安全策略充分性或生产可替换性。

## 产物位置与下一步

所有权重、checkpoint、日志和 JSON evidence 均在仓库外：

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
qwen25coder15b-cspider-bf16-lora-length1536-full2epoch-v1-20260902/
```

其中 `sft_smoke.json` 是训练汇总，`adapter_final/` 是最终 adapter，
`adapter_checkpoints/checkpoint-3752/` 和 `checkpoint-4288/` 是保留 checkpoint，
`reload-validation/adapter_validation.json` 是独立重载证据，`screen-run.log` 是完整运行日志。

完整成对评测产物位于仓库外：

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
qwen25coder15b-cspider-bf16-lora-full2epoch-pair-v1-20260902/
```

其中包括两侧的 `predictions.jsonl`、`generation_evidence.json`、SQLite diagnostics，以及
`matching-generation-verification.json`、`sqlite-paired-analysis.json` 和
`bounded-denotation-audit.json`。下一步不是读取 final test 或直接接入运行时，而是针对 71 条
denotation 回退与 Adapter 的 `no_such_column`/聚合错误做受限 changed-case 人工审核。当前自动
评测已给出正向净变化，但人工审核尚未完成，因此质量合同不能标记为无回退通过。确认错误模式后再
决定是否需要新训练实验；任何新实验都必须重新冻结 matching Base、唯一变量和停止条件。
