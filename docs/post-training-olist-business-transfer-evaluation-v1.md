# Olist 业务迁移候选 SQL 评测 v1

## 结论

Spider SFT v2 的 bf16 LoRA Adapter 已通过其原始 SQLite 离线候选质量门，但未通过当前 Olist PostgreSQL 业务工作区的迁移质量门。固定 12 条永久 holdout 业务查询上，Adapter 与同精度 Base 均完成 12/12 生成；Adapter 没有产生一条从无效变为 ResultContract 有效的候选，反而使两条 Base 已通过结果合同的候选变为无效。

这说明本次差异不能被解释为“微调没有价值”，但足以拒绝以下做法：直接替换 Vanna/SiliconFlow 默认模型、只因 Spider 正向结果继续盲目扩大通用 Spider 样本、或将 SQL 可执行/结果合同通过写为业务语义准确率。

下一步应优先建立**领域对齐、PostgreSQL 方言、与当前 Catalog/QueryPlan 输出形态一致**的训练/验证资产，并与项目的 60 条 v2 holdout 永久隔离；在这个较小而受控的实验得到正向业务迁移证据前，不再扩大通用 Spider 训练规模。

## 问题与边界

本评测只回答一个问题：已经在 Spider SQLite 训练和评测过的候选 SQL 模型，放进当前 Olist 数据分析工作区时，能否比相同 bf16 Base 更容易提出可通过可信查询链路的 PostgreSQL SQL 候选？

它不是网页 Agent 端到端评测，未调用 SiliconFlow、Vanna 规划循环、图表生成或一次 SQL repair；repair 被明确关闭，避免在线模型把候选模型的错误掩盖掉。生产默认模型保持不变。

```text
受保护 Olist 业务问题
-> QuestionRouter
-> Semantic Catalog
-> QueryPlan + ResultContract
-> bf16 Base / bf16 LoRA Adapter（各生成一个 SQL 候选）
-> SqlPolicy
-> PostgreSQL readonly role
-> ResultValidator
-> 脱敏安全聚合报告
```

任一来源的 SQL 仍必须经过现有服务器边界。`PostgreSQL executed` 和 `ResultContract valid` 只说明可受控执行和结果列/时间等确定性合同满足，仍不能独自证明 GMV 过滤、Join 粒度、归因规则等业务语义正确。

## 冻结合同

| 项目 | 固定值 |
| --- | --- |
| 模型 | `Qwen/Qwen2.5-Coder-1.5B`，revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 对照变量 | 不加载 Adapter 的 bf16 Base；加载 Spider SFT v2 bf16 LoRA Adapter |
| 业务输入 | 当前服务器生成的中文问题、Catalog slice、QueryPlan 和 ResultContract；模型不读取数据库行 |
| 方言与生成 | PostgreSQL、`olist-candidate-sql-v1`、greedy、seed `42`、`max_new_tokens=256` |
| 执行边界 | QuestionRouter、Catalog、QueryPlan、SqlPolicy、PostgreSQL reader role、ResultValidator、ResultContract |
| SQL 修复 | 禁用 |
| 评测案例 | 12 个 `text_to_sql_v2.yaml` 的 `answerable` 数据库 case；均在 `post_training_holdout_v1.yaml` 中标记 `forbidden_for_training: true` |
| 设备 | logical CUDA `1` -> physical GPU `3`，RTX 4090，UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` |

评测 manifest 为 [`evals/manifests/post_training_olist_business_adapter_evaluation_v1.yaml`](../evals/manifests/post_training_olist_business_adapter_evaluation_v1.yaml)。Base/Adapter 输出保存在仓库外的 `qwen25coder15b-bf16-lora-olist-transfer-v2-20260826`；仓库只保留 manifest、代码、测试和本脱敏结论，不提交候选 SQL、问题、数据库行、模型、Adapter、checkpoint 或完整日志。

## 安全聚合结果

| 指标 | Base | Adapter | Adapter - Base |
| --- | ---: | ---: | ---: |
| 生成成功 | 12 | 12 | 0 |
| 通过 SqlPolicy | 6 | 6 | 0 |
| PostgreSQL 执行完成 | 4 | 2 | -2 |
| ResultContract 有效 | 2 | 0 | -2 |
| 无效 -> 有效 | - | 0 | - |
| 有效 -> 无效 | - | 2 | - |

Base 总生成 2,481 token，其中 5 条触达 256 token 上限；Adapter 总生成 801 token，未触达上限。Adapter 更短的输出和更低的生成耗时不构成质量提升，因为其结果合同有效数为零。

状态迁移由三类现象组成：三条 `policy_rejected -> policy_rejected`，两条 `policy_rejected -> postgres_execution_error`，一条 `policy_rejected -> result_contract_rejected`，两条 `postgres_execution_error -> postgres_execution_error`，两条 `result_contract_rejected -> policy_rejected`，以及两条 `result_contract_valid ->` 非有效状态。不存在 `non_valid -> valid`。

## 有限人工语义复核与失败模式

人工复核仅在仓库外进行，不在本文保存问题原文、候选 SQL 或查询结果。两条 Base 的合同有效候选均是平均履约天数指标：一条整体聚合、一条按客户州聚合；它们使用 Catalog 声明的订单事实、实际送达时间减购买时间和客户维度 Join，符合当前已冻结口径。它们仍只代表两个受控样本，不是整体业务准确率。

失败模式具有可泛化性，而非某个 Olist case 的特判：

- Base 在复杂任务上倾向输出较长的中文注释和重复 CTE；5 条触达生成上限，截断后的 SQL 由 AST Policy 拒绝。另有按月查询遗漏用户明确的年份过滤，结果时间范围由 ResultValidator 拒绝。
- Adapter 输出更短，但常遗漏 ResultContract 要求的 metric alias、把派生指标名当作物理列、使用不存在的列，或没有沿 Catalog 提供的 Join 图完成维度关联。
- 多指标任务中，Adapter 没有稳定遵守 QueryPlan 的“按各自事实粒度先聚合，再按受控键组合”约束，因而仍出现跨事实表混合、缺少分组键或错误结果列形状。
- 这些问题同时涉及中文问题、Catalog/QueryPlan prompt、PostgreSQL 方言和业务 schema linking；不能单独归因给数据量或 LoRA 算法。

评测还暴露一条通用安全健壮性问题：截断 SQL 可触发 `sqlglot.TokenError`，旧策略只归一 `ParseError`。现已将两类异常统一为 `PolicyViolation`，使不完整 SQL 被 fail closed 并记录为 policy rejection；这不改变已冻结模型输出，只修正后续审计分类完整性。

## 决策与下一实验

当前 Adapter 的状态为 `offline_candidate_generator_quality_gate_passed_runtime_integration_deferred`，但业务迁移子门状态为 `not_passed`。因此生产运行时继续使用现有 Vanna/SiliconFlow 路径，不挂载该 Adapter。

下一轮不是直接把 Spider 从 3,600 条扩到更大规模，而是建立一份新的、与本次 holdout 不重叠的 Olist-domain train/validation 候选集。它应至少覆盖四个当前失败类别：指标 alias 与结果合同、PostgreSQL 日期/聚合、Catalog Join 路径、单指标/多指标 QueryPlan 形状。每条训练候选必须由服务器拥有的 Catalog 口径构造、只读执行和人工审查，保留数据来源/SQL/执行证据于仓库外；60 条 v2 holdout、它们的改写和本次 12 条 case 均不得进入训练、提示样例或偏好数据。

完成小规模领域对齐数据后，使用同一 Base、LoRA 设置和本 manifest 的 12 条 holdout 重新做 Base/Adapter 对照；只有出现可复核的 `non_valid -> valid` 净增、没有新的安全边界回退，并完成合同有效样本的人工业务语义核验，才讨论扩大该领域数据或增加 runtime shadow candidate generator。
