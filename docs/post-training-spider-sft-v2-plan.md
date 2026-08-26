# Spider SFT v2 规模化实验计划

## 目标

本实验服务于可信数据分析 Agent 的候选 SQL 生成组件，不复现单篇论文，也不替代运行时的 Vanna、SQL Policy、PostgreSQL reader role、ResultValidator 或 ChartContract。目标是验证经过规模化 SFT 的 `Qwen/Qwen2.5-Coder-1.5B` LoRA Adapter 是否能够在官方 Spider dev 的固定合同下，至少不劣于同精度 Base，并减少 Schema linking 的限定列/别名错误。

## 与历史实验的隔离

- 历史 v1 使用 `spider-sft-schema-question-sql-v1`，外键没有完整表列限定；其 prediction 和结果不可改写。
- 新实验使用 `spider-sft-schema-question-sql-v2`：每个列以 `table.column` 表达，保留列类型、主键标记和 fully-qualified 外键；不包含数据库行或值。
- 训练只读取官方 `train_spider.json`；官方 dev 的 SQL 不参与生成或训练；项目 v2 的 60 条 golden 永久隔离。
- 原始问题、SQL、训练 JSONL、预测、数据库行、模型、adapter、checkpoint 和完整日志都留在仓库外。

## 数据与训练合同

| 项目 | v2 约束 |
| --- | --- |
| 候选规模 | 已构造 3,600 条 train-only 候选，固定切分为 3,048 train 与 552 schema-disjoint validation |
| 选择 | `schema_stratified_v2`：先覆盖每个 Spider schema，再在 schema 内轮询 SQL shape；同时审计 JOIN、聚合、子查询、集合操作和限定列引用覆盖 |
| 执行过滤 | 每条 gold target 都必须通过对应 SQLite 的只读 `EXPLAIN QUERY PLAN`；失败样本不训练 |
| 基座 | `Qwen/Qwen2.5-Coder-1.5B`，已冻结 revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 主训练模式 | bf16 LoRA，单张 RTX 4090；LoRA `r=16`、`alpha=32`、dropout `0.05` |
| 训练长度 | 2 个数据 epoch，effective batch 4，learning rate `1e-4`；每 375 step 评估/保存一次 |
| 监督 | 只监督 `### SQL` 之后的 SQL target 和 EOS，不监督 schema 或问题 token |

bf16 LoRA 是本轮主路径：此前 1.5B 的 bf16 LoRA 在 24 GB RTX 4090 上已完成工程 smoke，峰值显存约 5.19 GiB。QLoRA 仍保留为显存受限路径，但不作为本轮唯一变量。

## 分层质量门

1. 数据门：3,600 条候选的只读 EXPLAIN、holdout 隔离、schema-disjoint split、SQL feature coverage 和 token 长度预检全部通过。
2. 工程门：训练、adapter 保存、fresh PEFT reload 和有限 validation forward 均完成且无 OOM。
3. 100-case smoke：官方 dev 前 100 条在相同 v2 prompt/greedy decode 下比较 bf16 Base 与 Adapter；只执行 SQLite 诊断，不运行 Test Suite。若 Adapter executed 明显下降或限定列错误再次显著增加，停止全量评测并做错误审核。
4. 完整评测：仅在 smoke 通过后，运行官方 dev 1,034 条、只读 SQLite、固定 Test Suite、状态迁移和有限 changed-case 语义审核。

成功不等于训练 loss 下降。最低要求是 Adapter 不相对 matching bf16 Base 出现明显 SQLite/Test Suite 回退；SQLite executed 也不能被表述为语义准确率。只有通过完整质量门，Adapter 才能作为 Agent 的可选候选 SQL 生成器接入，且仍必须经过服务器拥有的安全、权限和结果合同。

## 明确不做

- 不直接进入 DPO、GRPO、多候选自一致性或执行反馈 RL；
- 不为了通过 Spider 改动生产 PostgreSQL/Vanna 链路；
- 不把本实验输出包装为公开 Spider leaderboard 成绩；
- 不因一次 100-case smoke 通过就声称业务语义正确或生产可用。
