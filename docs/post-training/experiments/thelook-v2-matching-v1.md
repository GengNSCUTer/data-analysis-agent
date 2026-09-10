# TheLook v2 跨 Schema matching 评测 v1

## 结论

冻结的 Olist Release v2 LoRA Adapter 在 **未见的 TheLook 电商 PostgreSQL schema** 上，显著优于同一 bf16 Qwen2.5-Coder-1.5B Base：在 600 条 protected case 中，服务器结果合同通过从 `127` 提升至 `313`，Gold 结果有序匹配或仅排序不同的集合匹配从 `67` 提升至 `225`。

这是一份受冻结 TheLook 快照、Catalog、Prompt 和 greedy 解码约束的离线候选 SQL 证据。它支持“当前 Olist 领域 Adapter 对这个未见电商 schema 存在正向迁移信号”，不支持将 Adapter 接入产品默认路径，更不等价于开放式 Text-to-SQL、生产安全或任意业务指标的泛化结论。

## 对照条件与边界

- final test：`thelook-cross-schema-final-test-v2`，600 个唯一 protected QuerySpec/family；问题、Gold SQL、raw completion、结果行和失败日志均留在仓库外。
- Base / Adapter：同一 `Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b`、同一完整 v2 Catalog 与 Prompt、greedy decode、`max_input=4096`、`max_new=768`、seed `20260910`；唯一质量变量是是否加载冻结 Olist Adapter。
- 三阶段隔离：生成阶段不读 Gold 或数据库行；配对 marker 校验 600 条 case 顺序、hash、Prompt、模型 revision、decode 和 Adapter 状态；仅 marker 通过后才以 `SqlPolicy -> daa_thelook_reader -> ResultValidator` 执行候选并读取 Gold 作 denotation 对照。
- 原始产物及修复后评测报告：`/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-thelook-v2-matching-v1-20260910/`。本文件不复制题目、SQL、行数据或模型原始输出。

## 评测器修复

首轮后置报告错误地把 `SqlPolicy` 给所有未写 `LIMIT` 的候选自动补上的 `LIMIT 200`，直接传为 `ResultValidator.limit_applied=True`。因此即使标量查询只返回一行，也会被判为可能截断，造成 `0/600` ResultContract valid 的假阴性，不能用于模型结论。

修复后，只有候选显式请求超过 200 行且被 Policy 压缩时才将其视作可能截断；无显式 LIMIT 的安全默认 cap 继续受实际 `len(frame) >= 200` 保护。修复与生产 runner 的既有语义一致。回归测试覆盖默认 cap、显式超预算 cap 和显式小 LIMIT；未重新调用 GPU、未改写任何候选生成文本，只对冻结 raw completion 重跑阶段 C。

## 聚合结果

| 门 / 结果 | Base | Adapter | 变化 |
| --- | ---: | ---: | ---: |
| 生成成功 | 600 / 600 (100.0%) | 600 / 600 (100.0%) | 0 |
| SqlPolicy 通过 | 259 / 600 (43.2%) | 467 / 600 (77.8%) | +208 |
| PostgreSQL 执行成功 | 173 / 600 (28.8%) | 335 / 600 (55.8%) | +162 |
| ResultContract valid | 127 / 600 (21.2%) | 313 / 600 (52.2%) | +186 |
| Gold ordered match | 59 / 600 (9.8%) | 207 / 600 (34.5%) | +148 |
| Gold bag match（仅排序不同） | 8 / 600 (1.3%) | 18 / 600 (3.0%) | +10 |
| ordered-or-bag match | 67 / 600 (11.2%) | 225 / 600 (37.5%) | +158 |

Adapter 相对 Base 的 ResultContract valid 转移为：`207` 条从未通过变为通过、`106` 条两侧均通过、`21` 条从通过变为未通过。Gold 状态中，`128` 条从“未通过合同”变为 Adapter 有序匹配，另有 `28` 条从 Base 的值不匹配变为 Adapter 有序匹配；但也有 `6` 条 Base 有序匹配在 Adapter 侧未通过合同，以及 `12` 条退化为值不匹配。

在已通过 ResultContract 的候选中，Base 的 ordered-or-bag match 为 `67/127 (52.8%)`，Adapter 为 `225/313 (71.9%)`。因此 Adapter 的改善不只是放宽了可执行性，也提高了合同有效候选中的结果一致性；但这两个门仍必须同时报告，不能只用 execution 或 Policy 通过率替代语义正确性。

## 按结果形态的执行合同表现

| 形态 | case 数 | Base valid | Adapter valid |
| --- | ---: | ---: | ---: |
| scalar | 163 | 74 (45.4%) | 130 (79.8%) |
| dimension_grouped | 303 | 23 (7.6%) | 69 (22.8%) |
| time_series | 134 | 30 (22.4%) | 114 (85.1%) |

分组查询仍是主要难点：Adapter 虽然显著减少 Policy 拒绝，但在 303 条中仍有 133 条拒绝、100 条 PostgreSQL 执行错误，仅 69 条通过结果合同。Adapter 的执行错误绝对数（132）高于 Base（86），是因为它尝试通过 Policy 的候选更多；条件执行成功率反而从 `173/259 (66.8%)` 升至 `335/467 (71.7%)`。这提示下一轮扩大训练数据时，应优先补足多表连接、分组键、结果别名和复杂维度场景，而非简单增加标量模板。

## 停止条件与下一步

- 本轮不修改 Olist 训练集、Prompt 或生产默认 SQL 生成链路；TheLook v2 保持 protected，不能被用于错误驱动训练、few-shot 或 Adapter 选择。
- 评测器现在具备可信的聚合门，但当前安全报告未持久化逐 case 的 denotation state，因此不能仅据本报告做按指标的失败归因。若下一轮需要错误族分析，应先单独扩展安全报告为仅含 case ID 和状态的受控汇总，不导出 SQL/问题/结果。
- 下一项应基于已冻结 Olist train/validation 与独立 protected TheLook 评测边界，设计领域训练扩展的**程序覆盖**：重点为多表 join、维度分组、时间序列、计数去重、订单级二层聚合和 Catalog/结果别名合同；不以 TheLook case 或错误文本为训练种子。
