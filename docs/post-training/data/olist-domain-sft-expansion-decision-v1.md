# Olist SQL-only 扩展决策 v1

## 0. 本轮边界与确认状态

本文件先确认四件事：

1. 哪些新增指标进入 Olist metric v3 候选；
2. 哪些 SQL family/program 需要补，以及每类的 family 配额；
3. 中文 surface form 如何增加表达覆盖，同时不改变 QuerySpec 语义；
4. 以上三项之间的先后顺序、准入门槛和停止条件。

本轮不修改 `olist-metrics-v2.md`，不生成正式 SFT 训练行，不启动 GPU。v2 是已用于 Release v2 的
不可变实验快照；本文是下一版设计决策。v3 指标已落成隔离 Catalog、`METRIC_SQL_REGISTRY`、renderer
和回归测试；当前仅冻结不含自然语言、SQL 或结果的静态 family seed。

本轮设计结论已确认。v3 指标已经写入**隔离的离线** `olist-catalog-v3 / 0.3-proposal`，并通过标量
Gold 的 Policy、reader-role 和 ResultContract 回归；默认运行时仍固定在 v2。因此新增指标仍不能
被默认 Agent、正式训练集或生产查询使用，直到 v3 完成时间/分组 Gold 准入与 release manifest。

确认顺序固定为：

```text
新增指标合同 -> family/program 配额 -> 中文 surface contract -> 小批 Gold 准入 -> 正式数据集物化
```

任何一步失败，只回到当前一步修正，不越过准入门槛继续扩大样本。

## 1. 新增指标决策

### 1.1 进入 v3 候选的 9 个指标

这些指标都只使用当前 Olist 已有字段，且能形成与现有十指标不同的 SQL 结构。它们先作为
`olist-metrics-v3-proposal`，不是已经可用于训练的正式指标。

| 新指标 ID | 中文名 | 建议口径 | 默认时间 | 事实粒度 | 首批安全维度 |
| --- | --- | --- | --- | --- | --- |
| `unique_customer_count` | 去重客户数 | 有效订单通过 `customer_id` 关联 `dim_customers`，对 `customer_unique_id` 做 `COUNT(DISTINCT ...)`；排除 `canceled`、`unavailable`，不输出客户标识。 | 订单购买时间 | 客户/订单 | 客户州 |
| `review_count` | 有效评价数 | `review_score BETWEEN 1 AND 5` 的评价行数，不按 `review_id` 去重；空评分不计入。 | 评价创建时间 | 评价行 | 客户州 |
| `canceled_order_count` | 取消订单数 | `order_status='canceled'` 且购买时间非空的 `COUNT(DISTINCT order_id)`。 | 订单购买时间 | 订单 | 客户州 |
| `delivered_order_count` | 已送达订单数 | `order_status='delivered'` 且购买时间非空的 `COUNT(DISTINCT order_id)`。 | 订单购买时间 | 订单 | 客户州 |
| `unavailable_order_count` | 不可用订单数 | `order_status='unavailable'` 且购买时间非空的 `COUNT(DISTINCT order_id)`。 | 订单购买时间 | 订单 | 客户州 |
| `average_items_per_order` | 平均每单商品件数 | 先按有效订单统计商品行数，再对“至少有一条商品行”的订单级件数求平均；不直接对商品行求平均。 | 订单购买时间 | 订单（二层聚合） | 客户州 |
| `average_item_price` | 平均商品成交价 | 有效订单商品行 `AVG(price)`，不含运费。 | 订单购买时间 | 商品行 | 客户州、商品品类 |
| `approval_latency_days` | 平均订单确认时长 | `order_approved_at - order_purchase_timestamp` 的平均天数；两端非空且差值非负。 | 订单购买时间 | 订单 | 客户州 |
| `carrier_handoff_days` | 平均交承运商时长 | `order_delivered_carrier_date - order_purchase_timestamp` 的平均天数；两端非空且差值非负。 | 订单购买时间 | 订单 | 客户州 |

“有效订单”在前三个新状态指标之外，仍沿用 v2 的 `order_status NOT IN ('canceled', 'unavailable')`。
每个指标的正式合同还必须明确空值、负时长、日期边界、结果列别名、数值范围和零行结果。
正式实现时还要为每个指标登记唯一的中文别名集合；跨指标含义不唯一的词（例如单独的“订单数”）
不得用于 v3 变体。

### 1.2.1 v3 指标的实现准入门槛

每个新增指标都必须同时具备以下证据，才可进入 family seed：

- Catalog 中有唯一 `metric_id`、名称、受控别名、来源表/列、事实粒度、时间字段和允许维度；
- `METRIC_SQL_REGISTRY` 有确定性的标量表达式、分组表达式、过滤规则和空值行为；
- QuerySpec validator 能拒绝不允许的维度、混合时间域和未冻结归属；
- renderer 能生成稳定的 PostgreSQL Gold SQL，且结果列顺序、别名和 hash 可复现；
- Policy、只读角色和 ResultContract 回归测试通过，至少包含全量、空窗口、NULL 时间和边界状态样例。

### 1.3 为什么选这 9 个

它们正好补当前数据的四个结构缺口：

- `COUNT(DISTINCT ...)`：去重客户和状态订单；
- 评价事实：评价数量和评价时间序列；
- 二层聚合：平均每单商品件数；
- 时间差：确认、交承运商等运营时长。

这比继续加入 `gmv`、订单数和取消率的同类组合更能锻炼模型区分事实粒度、分母和 Join 路径。

### 1.4 暂不进入 v3 的指标

下列内容先不做普通 SQL 正例：

- 支付方式下的 GMV、订单数、运费或支付金额占比：一单可以有多条支付记录，归属规则未冻结；
- 品类下的订单数、履约、AOV 和评价指标：一单可以跨多个品类，需要订单归属或分摊规则；
- 卖家下的订单级指标：涉及敏感输出、权限和订单归属；
- 复购率、留存率、首购/复购周期：需要客户时间窗口和 cohort 合同；
- 评价回复时长：原始字段存在，但当前分析层没有暴露 `review_answer_timestamp`，需要单独扩 schema；
- 利润、毛利、退款、转化率：当前数据没有成本、退款或曝光分母。

## 2. 新增 family 类别与最终数据规模

### 2.1 配额原则

family 是结构语义单元，不是中文问法数量。每条 family 绑定一个完整的指标集合、结果形态、
维度、时间模式、Join 程序和聚合策略；同一个 family 的日期变化不能跨 split。风险标签（例如
`distinct`、`two_stage_aggregate`、`status_filter`、`null_boundary`）可以附加在 family 上，
但不另算 family 类别。

下面的配额是**新增 Coverage Repair v3 family**，不是把现有 2,400/600/600 行重新命名。
每个 family 只归入一个主结构桶；指标覆盖是独立的标签约束，可以覆盖多个桶但不重复计 family。

2026-09-14 对 Release v2 的实际 QuerySpec 重新统计后，原先设想的“八类各占 10%--15%”被否决。
原因不是数据量不足，而是合法 family 空间本来就不对称：在当前 19 项指标、无自由筛选、family
跨 split 隔离的合同下，单指标标量总共最多只有 `19 × (all_time + absolute_range) = 38` 个 family；
单指标品类分组更只有 4 个商品行指标 × 2 个时间模式 = 8 个 family。将它们强行扩成与多指标组合
同样多的行，只会反复复制同一语义，制造虚假的“均衡”。

因此本轮采用两个不同口径：

1. **family 覆盖配额**优先保证稀缺结构和新指标在 train/validation/test 都有代表；
2. **正式训练行曝光配额**在后续物化阶段限制多指标购买时间序列的占比，不把 v2 的历史偏置直接
   拼接进新 release。

本轮冻结 **300 个 v3 workspace family，8 个互斥主类别**。family seed 只是一条没有问题、Prompt、
SQL 或结果的结构化施工卡：它固定 split、主类别、风险标签和一个已验证的 QuerySpec 原型；之后才由
物化器在同 split 内派生最多 8 个合法日期实例。日期实例共享 family，不算 8 种独立能力；无法形成
合法窗口时必须减少实例并写入 manifest，不能复制 SQL 补齐数量。

### 2.2 新增 300 family 的主结构配额

| 主类别（互斥） | 占比 | 总 family | train | validation | test | 重点补什么 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 单指标标量 | 6.0% | 18 | 12 | 3 | 3 | 9 个新指标 × 全量/绝对时间两种有限合法 family |
| 多指标标量 | 18.0% | 54 | 38 | 9 | 7 | 2--4 指标独立 CTE 与标量合并 |
| 单指标维度分组 | 6.7% | 20 | 12 | 3 | 5 | 客户州；并保留 8 个有限商品品类 family，test 固定 5 个 |
| 多指标维度分组 | 14.0% | 42 | 28 | 7 | 7 | 同一客户州下多指标合并，不跨未冻结归属 |
| 单指标购买时间序列 | 13.3% | 40 | 27 | 6 | 7 | 8 个新增购买域指标 × 5 种时间粒度 |
| 单指标评价时间序列 | 1.7% | 5 | 3 | 1 | 1 | `review_count` × 5 种时间粒度；空间天然有限 |
| 多指标时间序列 | 25.0% | 75 | 50 | 13 | 12 | 同时间域的购买/评价多指标组合；评价组合单独保留 |
| 结构难例 | 15.3% | 46 | 30 | 8 | 8 | DISTINCT、二层聚合、状态过滤、NULL/非负时长边界 |
| **合计** | **100%** | **300** | **200** | **50** | **50** | — |

上表的 train/validation/test 是新增 family 的分配，三者比例约为 `2:0.5:0.5`。类别互斥的
判定优先级是：如果一个 QuerySpec 的主要学习目标是二层聚合、去重/分母或空值边界，就归入
“结构难例”；否则按结果形态、指标数量和时间域归入前七类。`distinct`、`status_filter`、
`review_fact` 等是标签，不会再把同一个 family 重复计入其他类别。为修复 Release v2 的品类
测试空白，`single_dimension` 中允许 8 个位于 **v3 workspace** 的商品行品类 family：其中三个是
继承 v2 公式，`average_item_price` 是新增公式；它们不是新的指标合同，但必须进入新的结构测试边界。

300 个 family 的理论日期实例上限为 2,400，但这是容量上限而不是必须凑满的目标。最终 v3 release
不能做 `Release v2 + 新行` 的字节级拼接：v2 train 的多指标购买时间序列已经是 1,049/2,400（43.7%），
追加 1,600 行后仍不可能得到相对均衡的训练分布。v2 是不可变历史实验；新 release 从 v2 与 v3 的
合法候选中重新按 family 选择、物化和审计。

冻结的**正式发布目标**为 `train=3,000 / validation=750 / final test=750`，总计 4,500 行。这个规模
满足中等规模 LoRA SFT 的需求，也避免为了达到 6,000 行而无意义重复稀缺单指标 family。后续物化器
必须遵循以下行曝光上限/目标；这是一种可行的相对均衡，而不是要求数学上八等分：

| 主类别 | train | validation | test | 总行数 | 总占比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 单指标标量 | 110 | 25 | 25 | 160 | 3.6% |
| 多指标标量 | 600 | 150 | 150 | 900 | 20.0% |
| 单指标维度分组 | 134 | 33 | 33 | 200 | 4.4% |
| 多指标维度分组 | 466 | 117 | 117 | 700 | 15.6% |
| 单指标购买时间序列 | 400 | 100 | 100 | 600 | 13.3% |
| 单指标评价时间序列 | 72 | 19 | 19 | 110 | 2.4% |
| 多指标时间序列 | 750 | 188 | 187 | 1,125 | 25.0% |
| 结构难例 | 468 | 118 | 119 | 805 | 17.9% |
| **合计** | **3,000** | **750** | **750** | **4,500** | **100%** |

单指标类占比低并不是缺口被忽略，而是受有限合法 family 数限制；其覆盖强度应由“每种指标/粒度/
维度 family 是否存在”衡量，而不能由复制次数衡量。多指标购买时间序列在新 release 中最多 25%，不再
允许重现 v2 的 43.7% 偏置。每行只选择一条中文主问法，其他受控问法仅作为 overlay/robustness
评测；最终 manifest 同时报告行数、family 数、QuerySpec 数、SQL skeleton、类别占比、指标覆盖和
每个 split 的 hash。

### 2.3 指标覆盖最低门槛

指标出现次数不作为唯一指标，必须报告“包含该指标的 family 数”。新指标最低要求：

- 每个新指标至少进入 10 个 train family、3 个 validation family、3 个 test family；指标
  覆盖按 family 去重计数，不按 8 个日期 instance 或中文 overlay 重复计数；
- `unique_customer_count`、`average_items_per_order`、`approval_latency_days`、`carrier_handoff_days`
  至少各有 2 个粒度难例 family；
- `review_count` 至少进入 3 个评价时间 family；
- 受 v3 合同限制，`average_item_price` 只有全量/绝对时间两个独立品类 family；不得人为伪造第 3 个。
  它与继承商品行指标共同保证 8 个 v3 workspace 品类 family，其中 test 固定 5 个；
- 现有 `positive_review_rate` 和 `average_review_score` 各新增至少 5 个 validation/test family，
  不再只靠订单指标的多指标组合带出它们；
- `category_grouped` test 固定包含 5 个独立 family；它们来自“单指标维度分组”，而不是同一 family
  的日期改写；
- 任何没有 test family 的新指标/新程序，都只能写成“训练覆盖”，不能写成“已评测覆盖”。

## 3. 中文 Query surface contract

### 3.1 八类受控问法

每个通过 Gold 准入的 QuerySpec 生成 8 类候选表达，但正式 SFT 仍只选择一条主表述；其余是
同一 QuerySpec 的 overlay，不计为新的 family。

| 类别 | 示例风格 | 约束 |
| --- | --- | --- |
| 正式请求 | “请统计 2018 年各州的去重客户数。” | 业务名词完整，适合基础覆盖。 |
| 口语请求 | “帮我看下 2018 年各州有多少个客户。” | 只能使用已登记别名，不能省略关键时间/维度。 |
| 管理者表达 | “我想了解各州的客户规模。” | 不使用“表现/增长/趋势”等额外语义词。 |
| 简洁短句 | “2018 各州客户数。” | 仅在时间、维度、指标都明确时使用。 |
| 结果导向 | “按州列出 2018 年客户数，方便做表格比较。” | “比较”只表示并列展示，不引入 baseline。 |
| 时间前置 | “2018 年 1 月到 4 月，按月看订单确认时长。” | 时间放在句首，边界必须与 QuerySpec 相同。 |
| 维度前置 | “各商品品类分别统计商品成交价均值。” | 仅用于允许该维度的指标。 |
| 指标别名 | “看一下各州的平均每单件数。” | 别名必须在 Catalog 中注册并可反向还原。 |

### 3.2 受控别名与变体生成规则

每个 family 的 8 类问法从受控槽位组合生成。新增指标先使用以下不重叠的别名；实现时以 Catalog
注册表为唯一来源，不能在脚本中另写一份别名表：

| 指标 | 允许的示例别名 | 禁止的含混简称 |
| --- | --- | --- |
| `unique_customer_count` | 去重客户数、独立客户数、客户人数 | 客户数（若未出现“去重/独立”） |
| `review_count` | 有效评价数、评价条数、有效评论数 | 评价量（可能被理解为评价率） |
| `canceled_order_count` | 取消订单数、已取消订单量 | 取消情况 |
| `delivered_order_count` | 已送达订单数、完成送达订单量 | 完成订单数 |
| `unavailable_order_count` | 不可用订单数、状态为 unavailable 的订单数 | 异常订单数 |
| `average_items_per_order` | 平均每单商品件数、每单平均件数 | 平均销量 |
| `average_item_price` | 平均商品成交价、商品均价 | 平均订单金额、客单价 |
| `approval_latency_days` | 平均订单确认时长、下单到确认平均天数 | 平均处理时长 |
| `carrier_handoff_days` | 平均交承运商时长、下单到交运平均天数 | 平均发货速度 |

每个 family 的 8 类问法从以下受控槽位组合生成：

```text
开头方式 × 时间表达 × 维度表达 × 指标别名 × 结果导向短语
```

可变化的内容：

- “请统计 / 帮我看下 / 想了解 / 给我列出 / 看一下”；
- 时间前置、时间后置、中文日期和 ISO 日期格式；
- “按州分组 / 各州分别 / 各个州 / 按商品品类”；
- 已登记的指标别名；
- “列成表 / 返回明细汇总 / 方便比较”这类不改变计算的结果描述。

禁止自动加入：

- “同比、环比、增长、趋势、变化”；
- “Top-N、排名、最好、最差”；
- “原因、影响、导致”；
- “本月、最近、截至目前”等未经 WorkingMemory 确认的相对时间；
- 任意自由筛选、支付归属、品类订单归属和卖家归属。

这些词会改变任务类型或需要额外合同，不能作为普通 SQL-only 正例的表面润色。

### 3.3 语言分布与质量门

- 8 类问法在每个 split 都必须出现；单一开头短语不超过该 split 的 25%；
- 同一 family 至少保留 5 个不同归一化模板；
- exact duplicate 必须为 0；
- 每条变体都重新经过 Router、Catalog、QueryPlan、ResultContract 身份检查；
- `query_spec_id`、Gold SQL hash、结果列和 workspace 版本必须完全一致；
- 正式 train/validation/test 仍是一条 query instance 一行，不能因为 8 个问法把行数乘 8；
- 额外的 surface robustness 评测可以保留全部 8 个变体，但应单独存放，不能污染主测试集。

## 4. 完成顺序和停止条件

实际实施顺序固定为：

```text
v3 指标合同 proposal
  -> METRIC_SQL_REGISTRY / Catalog / renderer 单测
  -> 300 family coverage seed
  -> QuerySpec / Gold admission
  -> 8 类中文 surface overlay
  -> Router / ResultContract / 长度审计
  -> family 配额与 split 审计
  -> 正式 train/validation/test 物化
```

在新指标公式、归属、时间和结果列尚未冻结之前，不生成大批中文问题；在 family 配额和 test
覆盖门槛未通过之前，不增加训练行数；在 Gold admission、长度和 split 审计未通过之前，不启动
GPU 训练。

本文件的“指标合同 → family 配额 → 中文 surface contract”设计已经完成。v3 指标的 Gold 回归已覆盖
时间序列、分组、二层聚合和空窗口，静态 coverage seed 也已冻结为
[`olist_v3_coverage_family_seeds_v1.jsonl`](../../../data/fixtures/olist_v3_coverage_family_seeds_v1.jsonl)：
300 个跨 split 隔离的 family（train/validation/test=`200/50/50`）。下一项只将其中小批 seed 物化为
具体 QuerySpec/Gold SQL，验证完整执行与结果合同；不同时生成正式 JSONL 或启动训练。
