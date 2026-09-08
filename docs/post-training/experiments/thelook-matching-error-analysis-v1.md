# TheLook Base/Adapter 错误分析 v1

**评测版本**：`thelook-cross-schema-final-test-v1`  
**样本数**：206 条  
**运行产物**：`/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-thelook-matching-v1-20260908/`  
**目的**：区分评测/格式问题、跨 schema 结构问题和业务指标语义问题，为下一批 Olist 领域 SFT 数据设计提供证据。

## 1. 总体结果

| 阶段 | Base | Olist LoRA Adapter |
| --- | ---: | ---: |
| 生成覆盖 | 206/206 | 206/206 |
| 通过 Policy 并执行 | 54/206 | 63/206 |
| ResultContract 通过 | 23/206 | 63/206 |
| ordered denotation match | 6/206 | 37/206 |
| bag denotation match | 6/206 | 41/206 |

Adapter 的主要收益是真正进入数据库并满足结果列/值合同的数量增加；它不是只表现为字符串更像 Gold。跨 schema 仍未达到可泛化水平。

## 2. 错误分层

### A. 输出格式与评测接口不一致：优先修复，不能用扩充数据掩盖

Adapter 有 132 条 Policy 拒绝，其中 129 条在 SQL 开头输出了 `Selection`、`Solution` 或 `Query` 展示标签，然后才是 `WITH ...`。当前 TheLook runner 的清洗逻辑没有复用项目已有 `unwrap_sql_completion()`，因此合法候选在 AST 之前被拒绝。

这部分首先是评测/运行时契约问题，其次才是训练格式问题。下一步应：

1. 让 TheLook runner 复用统一的候选清洗函数，并为 `Selection`、`Solution`、`Query` 增加精确单行前缀回归测试；只剥离展示标签，不修 SQL 语义、不放宽 Policy。
2. 重新评测一次，记录“原始候选拒绝”和“规范化后拒绝”两个指标，避免把格式错误误报成跨 schema 能力。
3. Olist 领域 SFT 的 target 继续保持 canonical SQL + EOS，训练样本中不加入 `Selection`/`Solution` 等展示前缀。

### B. 表/列/连接幻觉：真正的跨 schema 泛化问题

Base 大量生成不存在的中间表，如 `order_items_products`、`inventory_distribution_centers`；也经常引用 `event_id`、`orders.id` 等不存在列，并加入与问题无关的 `events`、`distribution_centers`、`inventory_items` 连接。Adapter 在已加载的规范 Prompt 下大幅减少了 PostgreSQL 执行错误（58 -> 11），但仍会输出额外连接和错误关联路径。

对训练的含义：不能只增加“正确 SQL 正例”，还需要加入**近似但错误的 schema 选择对比**：同一问题明确展示只允许 `orders -> order_items -> products`，而不是模型凭名字拼接潜在表。每个连接路径都应提供最小必要 join 和禁止的常见幻觉连接。

### C. 指标公式和粒度错误：需要领域数据覆盖

在已经执行且 ResultContract 通过、但 Gold 不匹配的 26 条 Adapter 样本中，主要模式是：

- AOV 直接 `AVG(order_items.sale_price)`，没有先按订单汇总再求订单均值。该错误集中在 scalar、traffic_source、state 和 time_series。
- 少数订单数使用 `COUNT(o.order_id)` 而不是合同要求的订单去重计数。
- 少数退货率按分组后错误地缺少 `Complete`/`Returned` 的状态集合约束。
- 履约天数时间序列少量按 `delivered_at` 分桶，而合同要求按 `orders.created_at` 作为订单时间。
- 多指标查询中某一个 CTE 的公式或粒度错误会使整条结果不匹配，即使其他指标正确。

这类问题不是 SQL 语法问题。ResultContract 只能检查列、时间范围和数值边界，不能替代 Gold denotation 或指标公式验证；训练集应为每个指标提供 scalar、dimension_grouped、month/quarter time_series、多指标组合和错误对照案例。

### D. 维度归因问题：traffic_source 是最弱场景

Adapter 的 `traffic_source` 分组只有 12 条执行，0 条 ordered match；`state` 分组 6 条执行、5 条 ordered match；category/department 的少量样本可以匹配。原因是 `traffic_source` 需要从 `users.traffic_source` 归因到订单，而模型倾向从 `events.traffic_source` 连接，或同时连接 events 与 users，造成重复计数/错误归因。

这提示训练数据需要明确区分：

- 用户注册来源：`orders.user_id -> users.id -> users.traffic_source`；
- 站内事件来源：`events.traffic_source` 是另一种业务问题，不能替代订单归因；
- 如果问题未明确事件漏斗，不允许为了找到“流量来源”而引入 events。

### E. 时间窗口和结果形状

Adapter 的 time_series 结果最好：month ordered match 13/50，quarter 9/30；但大量失败仍来自展示前缀，清洗后应重新测量。剩余语义错误主要是时间桶字段、状态过滤和多指标 CTE 组合，而不是中文日期理解本身。

## 3. 对下一批训练/验证/测试数据的建议

不要简单把 206 条 TheLook 题目复制到 Olist 领域数据。下一版 Olist 数据应按**程序族**扩展，而不是只按自然语言数量扩展：

| 程序族 | 建议占比 | 必须覆盖 |
| --- | ---: | --- |
| 单指标 scalar | 20% | 六个指标、all-time 和绝对日期窗口 |
| 低基数维度分组 | 20% | state、category、department、traffic_source |
| 月/季度时间序列 | 20% | 每个指标至少一种粒度，订单创建时间统一 |
| 多指标同粒度 | 15% | 2--4 指标、相同时间和维度合同 |
| 指标粒度陷阱 | 15% | AOV 二层聚合、订单 DISTINCT、退货率分母、履约时间字段 |
| 连接/安全负例 | 10% | 不存在表列、events 误连、敏感 ID 投影、额外 join |

每个 family 生成多种中文措辞，但 train/validation/test 按 family 隔离。验证和测试不能只随机切行，否则相同 QuerySpec/Join 程序会泄露。第一版可以先构建约 1,000 条（约 700/150/150），通过错误分层和人工抽样后再扩至 3,000 条。

## 4. 推荐执行顺序

1. 修复并测试统一输出前缀清洗，重新跑 TheLook matching，不改模型。
2. 对重跑结果重新统计 Policy、执行、ResultContract 和 Gold match，确认真正的语义基线。
3. 以 Olist 的真实 `olist-candidate-sql-v1` Prompt 构建约 1,000 条领域 SFT，重点加入 AOV、traffic_source/客户归因、时间序列和多指标 CTE。
4. 先做一轮 LoRA 训练和 Olist holdout 评测，再用 TheLook 作为跨 schema 泛化回归集；不能只依据训练 loss 或 Spider/CSpider 分数判断成功。

## 5. 统一前缀清洗后的重评结果

没有重新调用模型，只对原始 Adapter 候选复用统一 `unwrap_sql_completion()` 后重新执行后续链路。Base 也用同一 evaluator 重算，便于保留对照：

| 指标 | Base | Adapter（清洗后） |
| --- | ---: | ---: |
| 通过 Policy 并执行 | 54/206 | 174/206 |
| ResultContract 通过 | 23/206 | 171/206 |
| ordered denotation match | 6/206 | 100/206 |
| bag denotation match | 6/206 | 111/206 |

Adapter 清洗后剩余失败为：`policy_rejected=7`（6 条 `event_id` 不存在、1 条 `orders_users` 不存在）、`postgres_execution_error=25`（错误 Join 键或把 events 连接到订单/用户主链路）、ResultContract 失败 3 条。原始 132 条 Policy 拒绝中的 129 条展示前缀问题已经消除，说明第一版数字严重混入了 runner 清洗缺口。

剩余 25 条执行错误主要集中在 traffic_source、category/department/state 分组和少数客户/退货指标：模型仍会把 `events` 当成订单归因表，或使用 `o.id`、`event_id` 等不存在连接键；这才是下一轮领域数据需要重点解决的 schema 泛化问题。

## 6. 当前结论

TheLook 评测证明 Adapter 相对 Base 有明显工程质量提升。统一清洗后的结果表明，格式问题不应再作为训练数据扩充的主要依据；下一轮应围绕剩余的真实 schema/连接错误、AOV 粒度、traffic_source 归因和少量结果合同失败设计领域样本。重评报告位于 `.../evaluation-normalized/evaluation-report.json`，规范化候选位于同目录的 `base-normalized-candidates.jsonl` 和 `adapter-normalized-candidates.jsonl`。
