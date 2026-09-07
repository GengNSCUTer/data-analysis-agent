# Olist Medium v1 Adapter 失败归因

## 结论

本次分析针对 2026-09-06 Olist Medium v1 Adapter final test 的 150 条未通过样本。
原始评测共有 240 条样本：Adapter 生成成功 `240/240`，SqlPolicy accepted `101/240`，
PostgreSQL executed `101/240`，ResultContract valid `90/240`。未通过的 150 条由
`139` 条 `policy_rejected` 和 `11` 条 `result_contract_rejected` 构成。

这 150 条不能直接称为 150 条错误 SQL。诊断性重放表明：

| 诊断类别 | 数量 | 重放结果 | 当前归因 |
| --- | ---: | --- | --- |
| 首行是 `Query` | 99 | 删除首行后 Policy `99/99`，补合同别名后 Gold 有序匹配 `99/99` | 输出边界/展示包装问题 |
| 首行是 `Query Plan` | 30 | 删除首行后 Policy `30/30`，补合同别名后 Gold 有序匹配 `30/30` | 输出边界/展示包装问题 |
| 首行是 `Code` | 10 | 删除首行后 Policy `10/10`，补合同别名后 Gold 有序匹配 `10/10` | 输出边界/展示包装问题 |
| 时间序列合同缺少 `time` 别名 | 11 | 内存补 `time` 别名后 ResultContract `11/11`，Gold 有序匹配 `11/11` | runtime ResultContract 元数据问题 |

因此，在本次受控反事实条件下，139 条格式失败和 11 条时间列合同失败合计
`150/150` 都能完成可信执行并与 deterministic Gold SQL 的结果逐行匹配。这个结论
说明它们被评测边界或运行时合同挡住，不说明原始生产链路已经通过，也不改变原始
`90/240` ResultContract 通过率。

## 重放方法

- 输入只来自仓库外的 Adapter `safe-report.json`、`raw-candidates.jsonl`、冻结 runtime
  candidates 和 final-test Gold JSONL；输入指纹保存在外部报告中。
- 139 条样本仅在首行**精确等于** `Query`、`Query Plan` 或 `Code` 时删除这一行。
  没有做 SQL 修复、改写标识符、改变谓词或改变查询结构。
- 每条候选依次通过同一个 `SqlPolicy`、PostgreSQL reader role 和 `ResultValidator`。
- 对时间序列重放只在内存合同中把 `time` 加入
  `result_time_column_aliases`；没有修改冻结 runtime 文件或生产代码。
- 只有候选通过 ResultContract 后才读取同一 final-test Gold SQL，并以相同 reader role
  和合同执行；比较列名、行数、数值容差和有序结果。
- 重放产物保存在仓库外：
  `/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-medium-base-adapter-finaltest-v1-20260906/analysis/adapter-format-replay-v1.json`
  。产物只包含聚合状态、source ID、前缀类型和输入哈希，不包含问题、SQL 或结果行。

## 对模型质量的含义

这批 Adapter 输出的 SQL 主体在当前 Olist PostgreSQL 快照、Catalog/QueryPlan 和
deterministic Gold 合同下表现出很强的结果一致性：格式归一化与合同别名补齐后，
`150/150` 与 Gold 有序 denotation match。结合原始已通过的 `90/90`，可以说本次
240 条 final test 在这两个确定性缺口被隔离后得到 `240/240` 的诊断性 Gold match。

但这个数字不是原始运行时通过率，也不是可直接发布的业务准确率，原因是：

1. 原始运行时确实没有去掉 `Query`/`Query Plan`/`Code`，SqlPolicy 因此按 fail-closed
   规则拒绝了这些候选。
2. 原始 runtime prompt 的 `result_time_column_aliases` 为 `null`，时间列检查确实会误判；
   当前源代码虽已支持别名，冻结 prompt 资产尚未用新代码重建。
3. 重放使用了已知的确定性修正，不能证明模型在未知数据、不同指标口径或其他数据库上
   泛化正确。

## 对后续领域训练数据的建议

本批失败不应全部作为“错误 SQL”扩充训练集。优先级应是：

1. 增加 SQL-only 边界样本：正例以 `### SQL` 后直接开始 `WITH`/`SELECT` 并以 EOS 结束；
   负例覆盖 `Query`、`Query Plan`、`Code`、Markdown fence、解释文字和多语句输出。
2. 保留多指标 CTE、订单/商品行/评论三种事实粒度和 `customer_state` 分组，继续用 Gold
   renderer 生成标签，防止只学到表面格式。
3. 把 `time` 展示别名作为所有时间序列训练样本的显式结果列合同，并增加时间列回归样本。
4. 修复并重建 runtime ResultContract 别名传播后，再做一次不带反事实修正的 matching
   Base/Adapter 评测，分别报告格式通过率、合同通过率和 Gold denotation。
5. 不把本次 `150/150` 诊断性匹配直接写成生产质量门，也不在未修复运行时合同前启动更大
   训练或 DPO/GRPO。

## 资产与边界

- 原始评测结论仍以仓库外 Adapter `safe-report.json` 和已有
  `gold-denotation-audit.json` 为准。
- 本文只记录脱敏聚合和方法；问题、候选 SQL、Gold SQL、结果行和完整数据库审计不进入 Git。
- 生产默认模型、Vanna/FastAPI 运行时和数据库权限未被本次诊断修改。
