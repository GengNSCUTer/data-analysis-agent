# 官方 Spider Release Base/Adapter 分析 v1

## 结论摘要

本报告分析官方 Spider 1.0 release 上的同合同 Qwen2.5-Coder-1.5B Base 与 26-step QLoRA Adapter。两者均完成官方 `dev.json` 的 1,034/1,034 条候选生成，并使用同一 prompt、greedy decode、共享输出规范化器、只读 SQLite 诊断和固定 Test Suite evaluator。结果显示 Adapter 在本轮配置下回退，不能进入生产链路，也不能据此进入 DPO/GRPO。

这是一组离线候选生成实验，不是 Vanna/PostgreSQL 运行时评测。SQLite 执行成功不等于 SQL 语义正确，Test Suite 原始输出也只能作为当前固定资产组合的内部对照，不能包装为公开 Spider leaderboard 分数。

## 数据与证据边界

| 项目 | 官方 release 事实 |
| --- | --- |
| 数据目录 | `/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-official-v1-20260825/spider_data` |
| `dev.json` | 1,034 条，SHA-256 `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| `train_spider.json` | 7,000 条 |
| `tables.json` | 166 个 schema，SHA-256 `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed` |
| SQLite 数据库 | 166 个，完整 tree SHA-256 `67d29c2285095e39c15d08632605bb0b94945aac5aef38fca2e15540548a5aba` |
| Base 外部目录 | `qwen25coder15b-base-spider-official-v1-20260826` |
| Adapter 外部目录 | `qwen25coder15b-adapter-spider-official-v1-20260826` |
| 成对分析报告 | `qwen25coder15b-official-base-adapter-pair-v1-20260826/analysis-v1/safe-comparison.json` |
| 分析报告 SHA-256 | `0ccc1ab065557b77bac8dee875b35391c6ae6a6c274bcd1ddffcc10299e1f190` |

原始 prediction、SQL、gold SQL、数据库行、完整错误信息和评测日志均留在仓库外。本仓库只记录可复核的 release 指纹、聚合统计和有限 case ID。

## 运行配置

Base 与 Adapter 使用同一 `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67c0e24480f20468b6ef2894622d69eb73b`、4-bit NF4 + double quant、bf16 compute、相同 schema/question prompt、greedy decode、`max_new_tokens=256` 和相同的安全诊断/官方 bridge。Base 使用逻辑 CUDA `1` -> 物理 GPU `3` 的 RTX 4090；Adapter 使用逻辑 CUDA `0` -> 物理 GPU `2` 的 RTX 4090。两条任务均已正常退出，screen 不再运行。

Adapter 训练数据仍是 102 条 train / 26 条 validation 的 schema-disjoint smoke split，训练 26 optimizer steps，LoRA `r=16`、`alpha=32`、`dropout=0.05`、learning rate `2e-4`。官方 `train_spider.json` 的 7,000 条样本尚未用于下一轮扩展训练。

## 评测结果

### 只读 SQLite 执行诊断

| 指标 | Base | Adapter | 变化 |
| --- | ---: | ---: | ---: |
| executed | 829 | 671 | -158 |
| execution error | 201 | 360 | +159 |
| policy rejected | 4 | 3 | -1 |
| timeout | 0 | 0 | 0 |
| 执行率 | 80.2% | 64.9% | -15.3 个百分点 |

这是“候选能否在受限 SQLite 上执行”的诊断，不是 exact match、execution accuracy 或业务指标准确率。

### 固定 Test Suite evaluator

| 难度 | Base | Adapter | 变化 |
| --- | ---: | ---: | ---: |
| easy | 0.758 | 0.653 | -0.105 |
| medium | 0.451 | 0.381 | -0.070 |
| hard | 0.253 | 0.201 | -0.052 |
| extra | 0.090 | 0.133 | +0.043 |
| all | 0.433 | 0.376 | -0.057 |

这些数值来自当前官方数据包与 pinned Test Suite 资产的内部组合；条款和排行榜 release 组合尚未单独核验，因此不能对外称为官方榜单分数。

## 回退模式

配对状态迁移中，`executed -> execution_error` 为 240 条，`executed -> policy_rejected` 为 1 条；反向恢复为 `execution_error -> executed` 81 条、`policy_rejected -> executed` 2 条。净损失 158 条 executed。20 个开发数据库中，17 个回退、2 个改善、1 个不变，说明不是单一数据库偶发现象。

错误类别的主要变化如下：

| 错误类别 | Base | Adapter |
| --- | ---: | ---: |
| no such column | 182 | 322 |
| 其中限定列引用 | 15 | 296 |
| no such table | 4 | 24 |
| ambiguous column | 11 | 2 |
| aggregate misuse | 4 | 12 |

Adapter 平均生成 token 从 123.86 降到 36.9，触及 256 token 上限从 338 条降到 1 条；但规范化 SQL 平均长度从 109.59 增至 118.59，中位数从 86 增至 121。也就是说，它更早停止并不代表 SQL 更好，主要问题转移成了错误的表别名、限定列和聚合结构。

## 解释边界与学习结论

本轮只能确认“26-step Adapter 在该小数据、该 prompt 和该配置下回退”。不能把原因简单归结为 QLoRA：训练覆盖度、样本规模、schema 序列化、别名模式、学习率和监督目标仍然混合在一起。训练 loss 或输出更短也不能替代生成时质量门。

面试时可以把它描述为一次严格的负向 ablation：保持模型 revision、数据 release、输入顺序、prompt、解码和评测器不变，只比较是否加载 Adapter；结果拒绝了“不回退”的质量门，并通过错误迁移定位到 schema linking 是下一轮首要问题。

## 下一步质量门

下一轮不进入 DPO/GRPO，也不把 Adapter 接入生产 Vanna。先使用官方 `train_spider.json` 构造约 1,000--3,000 条 schema-stratified、train-only 的候选集，永久隔离项目 v2 60 条 holdout；训练前审计 schema 数量、SQL shape、JOIN/聚合/子查询、限定列比例和外键序列化。优先把共享 Schema serializer 升级为 fully-qualified table-column identity，补充 PK、列类型和完整 FK，并做 prompt v2 静态审计与小规模 forward smoke。

只有新的 SFT Adapter 在相同 release、相同精度 Base 上至少不回退，并完成 changed-case 人工语义核验，才考虑更大训练 horizon；只有 SFT 质量门通过且具备可信 chosen/rejected 或执行反馈数据，才讨论 DPO/GRPO。
