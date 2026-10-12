# Runtime Candidate v2 Base / Adapter Matching：r3 评测结果

## 运行范围

本次评测使用已经封存的 runtime-aligned 输入和已经完成的 Base / Adapter
生成物，没有重新生成模型输出：

- Olist：750 条，其中 736 条进入候选生成，14 条因 Router 合同拒绝；
- TheLook：750 条，其中 717 条进入候选生成，33 条因 Router 合同拒绝；
- 合计：1,500 条固定分母，1,453 条可生成输入。

输入、模型生成物和原始问题没有被修改。r3 只修复评测阶段对 PostgreSQL
statement timeout 的处理，并通过 `generation_contract` 明确复用了 r2 的
prepare/generation marker 和 raw completion。r3 合同 SHA-256 为
`6602344363eec518089045a868c8f9d9cc56ca5427347d87f754ecc0f01b0513`，复用的
r2 生成合同 SHA-256 为
`c639a1fd71368e7b2e10ce0af199052a72bb69cb36f6b3ec83ea6e0eea46408c`。

## 评测修复

原评测在一条候选 SQL 超过 5 秒 `statement_timeout` 后，把 PostgreSQL
`QueryCanceled` 当作数据库连接基础设施故障，导致整轮中止。现在
`execute_sql()` 对 `QueryCanceled` 或 SQLSTATE `57014` 返回：

```text
state=postgres_execution_error, sqlstate=57014
```

当前连接随后关闭，下一条候选使用新连接继续执行。权限错误、普通连接/接口
错误以及 Gold SQL 执行失败仍然会阻断评测，避免把数据库故障伪装成模型失败。

新增回归用例覆盖该分类；runtime matching 定向测试为 46 passed，Ruff 通过。

## 结果

| workspace | 可生成输入 | Base ordered Gold match | Adapter ordered Gold match | Adapter valid execution | Adapter EOS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Olist | 736 | 0（另有 1 条 bag match） | 430（58.4%） | 736（100%） | 736（100%） |
| TheLook | 717 | 41（另有 12 条 bag match） | 717（100%） | 717（100%） | 717（100%） |
| 合计 | 1,453 | 41（另有 13 条 bag match） | 1,147（78.9%） | 1,453（100%） | 1,453（100%） |

这里的 `ordered_denotation_match` 才是严格的结果顺序与值均匹配；`bag` 只表示
忽略行顺序后匹配，不能和 ordered 结果混写成同一个数字。路由拒绝样本保留在
1,500 条总分母中，没有被当作模型成功。

### Base 暴露的问题

在 1,453 条可生成输入中，Base 只有 1,041 条观察到 EOS，另有 412 条达到
1,024 token 上限后停止。其最终状态包括：

- `postgres_execution_error`：404；
- `policy_rejected`：380；
- `result_contract_rejected`：112；
- `length_stopped`：412；
- `valid`：140。

因此 Base 的低匹配率同时受到输出边界、SQL 安全/可执行性和结果合同失败的
影响，不能只把它解释成语义能力不足。

### 第 1 项审计：时间端点先于模型错误

Adapter 的所有可生成输出都能结束、通过 Policy、使用 reader-only 事务执行并
通过 ResultContract，但对 Olist 结果的第一轮解释必须先扣除输入合同偏差。
704 条带时间范围的 Olist 输入全部存在 `time_range_mismatch`：运行时
QueryPlan 的 `end` 比原 QuerySpec/Gold 的 `end_exclusive` 晚一天。原问题使用
“至”但没有明确写“ 不含结束日 ”；`extract_absolute_range()` 因此按运行时默认
语义把结束日加一天，而 Gold 仍按原 QuerySpec 的 exclusive 上界执行。

这 704 条的 Adapter 结果按实际边界分为：

- 266 条生成了 Gold 的边界，全部 `ordered_denotation_match`；
- 438 条生成了运行时 QueryPlan 的边界，其中 132 条因额外一天没有数据而仍然
  匹配，177 条为 `denotation_mismatch`，129 条为 `row_count_mismatch`。

因此目前不能把这 306 条直接归因于模型的时间分桶、Join 或业务语义错误。Olist
端点与来源一致的 32 条可生成样本全部匹配，TheLook 端点一致的 717 条也全部
匹配；`source_semantics_consistent` 子集的 Adapter 是 749 / 749。该子集只作
诊断，不能替代固定 1,500 条主分母。

正式合同仍然保留了“原问题不修改、不能调用训练物化器的
`repair_source_question()`”规则，因此 r3 的 76.5% 固定分母结果必须标注为
包含 704 条已知 source/Gold 端点不一致的历史评测，而不是干净的模型语义准确率。
在端点对齐之前，不应据这 306 条失败样本制作 DPO 负例。不能把诊断子集的
749 / 749 扩写成整个测试集 100% 正确。

还需区分「匹配旧 Gold」与「遵循当前计划」。上述 266 条虽然匹配旧 Gold，却
没有采用输入中 QueryPlan 的排除结束日；438 条采用了运行时计划的上界。
带时间范围样本的端点计划遵循率是 438 / 704（62.2%），其余 266 / 704
（37.8%）回到了原标签的上界。SQL 可执行、ResultContract valid 不会自动证明
SQL 忠实编译了 QueryPlan；这也是本次审计发现的独立风险。
[`sql_plan_mismatch()`](../../../src/data_analysis_agent/plan_sql_guard.py) 当前检查
受控州筛选、top-N 和归属规则，没有精确校验每个指标 CTE 的时间谓词。
ResultValidator 检查返回结果与范围/粒度约束，但不能从返回行证明底层过滤
没有漏一天。本次只记录该边界，不改运行时代码或冻结评测输入。

### 第 2 项审计：时间粒度没有观测到混淆

全量检查可生成 Adapter SQL 的 `DATE_TRUNC`：Olist 的 388 条时间序列都包含
所要求的日/周/月/季度/年粒度，TheLook 的 222 条也都一致；后者全部有 ordered
Gold match。Olist 按粒度的原始成绩如下，不能据此把端点差异误判为粒度错误。

| 粒度 | Olist 时间序列样本 | 原始 ordered match | row_count mismatch |
| --- | ---: | ---: | ---: |
| day | 77 | 34 | 43 |
| week | 74 | 22 | 14 |
| month | 82 | 46 | 36 |
| quarter | 78 | 51 | 27 |
| year | 77 | 41 | 9 |
| 合计 | 388 | 194 | 129 |

仅发现相同的粒度关键字还不够，因此第 3 项继续检查完整 SQL 结构。

### 第 3 项审计：过滤、Join 和聚合结构

对 736 条 Olist 可生成 SQL 做只读 SQLGlot 审计：使用统一
`unwrap_sql_completion()`；把 Gold 最终投影调整为运行时规定的列序；解析
PostgreSQL AST；在内存里把日期字面量替换为占位符，再比较完整 AST 的规范化
SQL。结果是 736 / 736 完全一致，分组为 430 条 ordered match、177 条
denotation mismatch、129 条 row_count mismatch。没有解析失败或结构差异。

该检查覆盖完整 SQL 中的表、JOIN 类型和条件、CTE/子查询、状态过滤、非空/
非负边界、DISTINCT、聚合表达式、GROUP BY、时间分桶和 ORDER BY。因此这批
失败没有出现遗漏过滤、重复 JOIN 或错误聚合层级的证据。日期审计进一步确认
704 条候选都使用原 Gold 或运行时计划两套边界之一，没有混用或其他日期。
规范化只用于诊断，没有写回 SQL、生成文件或重新执行候选。

### 第 4 项审计：129 条行数差异全部是多一个桶

从现有执行报告读取候选与 Gold 的行数，不另跑数据库。129 条
`row_count_mismatch` 中，候选行数全部恰好等于 Gold 行数加 1：日 43 条、周
14 条、月 36 条、季度 27 条、年 9 条。月粒度全部是 4 对 3 行，周全部是
15 对 14 行，季度和年全部是 2 对 1 行。

结合第 1 项的额外一天与第 3 项的日期之外完整结构一致，可以归因到跨桶边界
纳入了额外时间桶；没有证据表明它们来自 JOIN 扩行或粒度写错。另 177 条是
结果值不同：额外一天即使仍处于同一时间桶，也可能改变聚合数值。

### 第 5 项审计：训练覆盖与问法合同

本项输入是已冻结 `runtime-candidate-sql-v2-r1` train/validation 与 Olist v3.1
test Spec sidecar。目标是检查本轮错误是否足以支持扩充某类训练数据；输出为
聚合分布与来源 hash，不生成训练样本、不改 split、不选择新 checkpoint。
按 `query_spec_id` 回连源覆盖桶，按实际运行时 JSONL 统计 shape、粒度、风险和
问题端点，避免把丢失的顶层 metadata 当作零覆盖。

| 结果形态 | Olist train | Olist validation | Olist test（固定来源） |
| --- | ---: | ---: | ---: |
| scalar | 792 | 183 | 192 |
| state_grouped | 680 | 142 | 159 |
| category_grouped | 17 | 8 | 11 |
| time_series | 1,511 | 417 | 388 |
| 总行数 | 3,000 | 750 | 750 |

| 时间粒度 | train | validation | test |
| --- | ---: | ---: | ---: |
| day | 264 | 87 | 77 |
| week | 324 | 83 | 74 |
| month | 283 | 72 | 82 |
| quarter | 362 | 98 | 78 |
| year | 278 | 77 | 77 |

时间序列占 train 的 50.4%，validation 的 55.6%，test 的 51.7%；五种粒度
均有数百条训练样本。原 `structural_hard` 主桶分别为 468 / 118 / 119 条，
占比约 15.6% / 15.7% / 15.9%。因此本轮没有证据支持「时间序列或结构难例
整体训练不足」的判断。

训练集风险标签覆盖：多指标 CTE 合并 2,296 条、状态过滤与订单去重 648 条、
非空/非负时长边界 463 条、二层订单聚合 268 条、评论事实粒度 257 条、客户
去重 241 条。风险标签可重叠，不能相加作为样本数。19 个 Olist 指标全部
进入 train/validation/test。family 数分别为 455 / 113 / 285，三对 split
的 family 交集均为零；同一数据库和同一程序类别仍然已见，不能称为未见
Schema 泛化。

时间端点在训练/验证物化时已经显式修复：2,902 条带时间范围的 train 和
728 条 validation 都补入「含开始日，不含结束日」，且 QueryPlan 与来源
QuerySpec 的边界一致；其余 98 / 22 条是 all-time。评测的 704 条时间样本却
保留旧的未标端点问法。代码来源是
[`repair_source_question()`](../../../src/data_analysis_agent/runtime_candidate_materializer.py)，
它只允许 train/validation；protected matching 合同明确禁止调用它。问题是
训练/评测问题语义合同不一致，而不是一个可以不留记录就改的标签错误。

仍有独立覆盖限制值得保留：品类分组训练仅 17 / 3,000（0.57%），单评论
scalar 程序仅 8 条、商品 scalar 程序仅 23 条，程序曝光明显倾向多指标时间
序列。Olist train 49 条、validation 16 条风险 metadata 为空，不能推导为无
风险语义；本轮只记录原状态。TheLook validation 750 条只覆盖事件/会话/注册
三个指标，不能代表其订单、履约和库存事实域的模型选择质量。

### 五项排查后的下一步

先单独确认原问题、QueryPlan 和参考 SQL 的时间语义，并冻结端点一致的诊断
合同，在 train/validation 复现「含结束日」与「不含结束日」两种输入。需要
明确是评估原标签任务还是编译实际运行时计划，不能挑更高分的一套端点。
若对当前生成做新的运行时语义对照，应另存独立参考与报告，保持 r2/r3 来源、
输入、生成、Gold 和原始成绩不变，并标注为事后诊断，不作为新 headline。

这一步完成后，才判断是否需要补端点遵循样本或精确时间谓词 Guard。未来扩大
覆盖可优先考虑品类分组、低曝光程序和 validation 事实域平衡；不能把本次
受保护失败题直接回灌训练/DPO，也没有必要仅凭当前 306 条失败再跑一轮 SFT。

## 证据位置与限制

安全报告位于仓库外：

`/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-final-20261011-r3/evaluation/safe-report.json`

报告只保存 source identity、模型/Gold hash、状态、reader 和 denotation，
不保存问题、原始 SQL、结果行或数据库错误原文。评测结果证明这轮固定输入上
Adapter 的生成边界和受控执行能力明显改善，也证明了 TheLook 这组输入上的
结果匹配；它不能推出所有电商 schema、自由问题或线上模型调用的泛化准确率。
