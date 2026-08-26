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

## 2026-08-26：训练完成与前缀 Smoke 诊断

主训练已完成 2 epoch / 1,524 optimizer steps；训练 loss 为 `0.141485`、validation loss 为 `0.222317`，74 MB LoRA adapter 的 fresh PEFT reload 得到有限 validation loss `0.235857`。这些只闭合训练工程和 artifact reload，不构成 Text-to-SQL 效果结论。

第一轮官方 dev 前 100 条 smoke 使用相同 bf16 base、v2 prompt、greedy decode 和 SQLite policy。SQLite diagnostics 为 Base `94 executed / 5 execution_error / 1 policy_rejected`，Adapter `89 / 11 / 0`；限定列 `no_such_column` 从 3 增至 4，因此按原始“执行不回退”护栏不通过，不能直接进入完整 Test Suite。配对诊断中 Adapter 从 Base 的 42/100 direct-query 输出提升到 100/100，生成 token 均值从 114.93 降至 36.86，但格式稳定性不能代替 schema linking。

生成结束后，另做了不回流训练的 bounded denotation audit：gold SQL 仅在模型输出冻结后于本地只读执行，输出报告不保存问题、SQL、数据库标识或结果行。该审计在相同 100 条上得到 Base 56、Adapter 69 条 exact-or-bag denotation match；16 个状态变化 case 中，Adapter 新增 2 条精确匹配但使 6 条 Base 精确匹配变为不可执行。该结果说明 SQLite 可执行性不足以单独判断候选质量，但前 100 条只覆盖 3 个 dev schema，不能据此宣布 v2 通过。

## 独立 Schema-Stratified 复验合同

在看到前缀 smoke 的结果前，不再选择或剔除样本。复验集由固定 seed `20260826`、`per_schema=10`、排除前 100 个 source index 及其出现的全部 schema 的确定性选择器构造；得到 164 条、17 个此前未观察的 Spider dev schema。模型输入文件只含 `db_id` 和 question；gold query 只位于仓库外 audit 文件，并且只允许在 Base/Adapter 生成完毕后读取。

复验在运行前冻结以下判据：

1. 主判据：Adapter 的 exact-or-bag denotation match 必须高于 Base，并报告所有配对语义状态迁移；这不是官方 Spider 分数。
2. 护栏：Adapter 的 SQLite executed 不能比 Base 低超过总 case 的 5%，且 `no_such_column` 数量不能超过 Base 的两倍。
3. 决策：主判据与两项护栏都通过，才有资格讨论完整 1,034-case Base/Adapter 评测；任一护栏失败则停止，不跑完整 Test Suite，优先评测通用 schema-identifier repair 或调整训练数据/目标，绝不针对某个 schema 硬编码。

## 2026-08-26：独立复验结果与放行决策

Base 与 Adapter 均已在冻结的 164 条、17 个此前未观察 schema 上完成生成和只读 SQLite diagnostics；两条 `screen` 正常退出。Base 使用 logical CUDA `0` -> physical GPU `2` 的 RTX 4090，Adapter 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090。两者的模型 revision、bf16 基座、v2 prompt、greedy decode、case 顺序与 SQLite policy 相同，模型侧均未读取 dev gold SQL。

| 指标 | Base | Adapter | 解读 |
| --- | ---: | ---: | --- |
| exact-or-bag denotation match | 97 | 122 | bounded audit 的主判据，提升 25 条 |
| mismatch | 56 | 33 | 与 gold 的执行结果不一致；不是官方 Test Suite 分类 |
| not executable | 11 | 9 | 生成或 gold-side bounded audit 无法比较 |
| SQLite executed | 153 | 155 | 没有发生执行回退 |
| SQLite execution error | 10 | 9 | 仅是单库诊断错误 |
| SQLite policy rejected | 1 | 0 | 只读 policy 拒绝数 |
| `no_such_column` | 9 | 8 | 未超过 Base 的两倍护栏 |

三个预先冻结的条件均满足：Adapter denotation match 高于 Base，SQLite executed 未超过 5% 的允许回退，并且 `no_such_column` 未恶化。因此 v2 SFT 获得进入完整 1,034-case Base/Adapter 对照的资格。这个结论只适用于独立的 bounded smoke，既不是 Spider 官方分数，也不说明可以接入生产 Vanna/PostgreSQL；完整评测仍需运行只读 SQLite diagnostics、固定 Test Suite bridge、状态迁移和有限 changed-case 审核。

复验 generation cases SHA-256 为 `23ad1816c724f6d11628b0b5d8e5f2a7bba8630c8732560f12615240ed961b08`，仅在生成完成后读取的 audit cases SHA-256 为 `8b3e973c82296409d4a30b0b49c9142e11448a9f9e7657f16f103f5dc0782f8e`；聚合 denotation audit SHA-256 为 `76f3e0db946dc57ada7d30e8a9ff94bd649ca121f0cbb6602942499eae0d06e7`。所有预测、gold SQL、数据库、结果行、模型与原始日志均保持在 Git 外。

## 完整评测合同

完整评测固定使用官方 Spider 1.0 release 的 1,034 条 dev case、`spider-sft-schema-question-sql-v2`、bf16 Base/Adapter、greedy decode（seed 42，`max_input_tokens=1536`，`max_new_tokens=256`）和相同的 case 顺序。Base 在 logical CUDA `0` -> physical GPU `2` 的 RTX 4090 上运行，Adapter 在 logical CUDA `1` -> physical GPU `3` 的 RTX 4090 上运行；启动器在进程内核验 UUID，且使用互不重叠的仓库外目录。

两条生成均完成后，各自运行只读 SQLite diagnostics 和固定 commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c` 的未修改 Test Suite bridge；随后才生成配对状态迁移、错误类别、bounded denotation 和 changed-case 审核证据。完整合同记录在 [`evals/manifests/post_training_spider_sft_v2_full_evaluation_v1.yaml`](../evals/manifests/post_training_spider_sft_v2_full_evaluation_v1.yaml)。在两条完整生成、两层评测和人工语义审核均完成前，不对 Test Suite、业务语义、生产接入或候选模型提升作结论。

## 明确不做

- 不直接进入 DPO、GRPO、多候选自一致性或执行反馈 RL；
- 不为了通过 Spider 改动生产 PostgreSQL/Vanna 链路；
- 不把本实验输出包装为公开 Spider leaderboard 成绩；
- 不因一次 100-case smoke 通过就声称业务语义正确或生产可用。
