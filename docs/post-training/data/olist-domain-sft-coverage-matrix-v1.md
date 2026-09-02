# Olist 领域 Candidate SQL SFT 覆盖矩阵 v1

## 1. 任务卡

**状态：** 覆盖范围已冻结；未创建 `QuerySpec`、未实现 renderer、未物化训练行、未加载 tokenizer、未启动 GPU。<br>
**上游合同：** [`olist-domain-sft-data-contract-v1.md`](olist-domain-sft-data-contract-v1.md)。<br>
**Catalog 快照：** `olist-catalog-v1` / `metric_version=0.1-draft` / `olist-kaggle-v2-2026-08-03` / `sql-policy-v1`。

| 项目 | 本轮冻结内容 |
| --- | --- |
| 目标 | 盘点当前 Olist Catalog 和运行时能够形成确定性、可审查 PostgreSQL Gold 的指标、维度、时间和多指标查询形状。 |
| 输入 | 当前 `olist_catalog.yaml`、`QuestionRouter`、`QueryPlan`、`ResultContract`、数据模型、既有确定性路由/计划测试。 |
| 输出 | 首版领域 SFT 的可训练覆盖单元、Join 程序、split 目标分布、排除清单和 renderer 前置问题。 |
| 非目标 | 不新增指标/维度/归因规则；不修改 Catalog、Router、QueryPlan、Policy 或数据库；不生成样本、SQL、Prompt、审计或训练配置。 |
| 不变量 | 真实 Prompt 仍为 `olist-candidate-sql-v1`；候选模型只生成 SQL；60 条 protected holdout 不读取原文/Gold；所有产物保持仓库外；运行时安全链路不变。 |
| 验收证据 | Catalog 的 4 个已实现指标、7 条 Join、归因三态和 Router/QueryPlan 测试均能解释每个纳入或排除决定。 |

## 2. 事实基线和裁决原则

当前 Catalog 中只有以下 4 个指标是运行时已声明、可检索、可进入本矩阵的对象：

| 指标 | 事实粒度 | 时间字段 | 默认过滤 | 关键物理表达式 |
| --- | --- | --- | --- | --- |
| `gmv` | `order_item` | `fact_orders.order_purchase_timestamp` | 排除 `canceled`、`unavailable` | `SUM(fact_order_items.price)`，不含运费。 |
| `paid_order_count` | `order` | `fact_orders.order_purchase_timestamp` | 排除 `canceled`、`unavailable` | `COUNT(DISTINCT fact_orders.order_id)`。 |
| `average_delivery_days` | `order` | `fact_orders.order_purchase_timestamp` | 实际送达时间非空 | 购买至实际送达时长的平均值。 |
| `positive_review_rate` | `review` | `fact_reviews.review_creation_date` | 评分在 1--5 | `review_score >= 4` 的评价行占有效评分评价行比例。 |

`docs/metric-catalog.md` 中的 `average_order_value`、`item_count`、`on_time_delivery_rate` 和
`average_review_score` 是早期草案，不属于 `olist_catalog.yaml` 的当前 4 项运行时指标，因而不进入本轮数据范围。

每个矩阵单元按如下优先级裁决，而不是只看 `allowed_dimensions`：

```text
当前运行时是否能路由
  + 是否存在不改变业务含义的事实粒度 / Join 程序
  + 是否没有未实现的归因或重复计数风险
  + 是否能由当前 Prompt 暴露足够的 QueryPlan/ResultContract 信息
  = 是否可进入 v1 Gold renderer 设计
```

`allowed_dimensions` 是 Catalog 检索范围，不是“所有指标 × 维度组合已冻结业务口径”的证明。尤其当订单或评价粒度需要经过 `fact_order_items` 时，不能因为 SQL 可写或 Router 当前未拦截，就假定不存在归属、重复计数或展示语义问题。

## 3. Join 程序目录

下面的 `join_program_id` 是未来 `QuerySpec` 的稳定语义引用，不是当前代码中已经实现的 renderer。每个程序都必须在下一任务中实现为确定性 PostgreSQL SQL，并接受 Policy、reader role、ResultContract 和人工审查。

| ID | 指标/维度使用 | 受控事实路径 | v1 状态 | 关键不变量 |
| --- | --- | --- | --- | --- |
| `JP01_gmv_scalar` | `gmv` 标量/时间 | `fact_orders -> fact_order_items` | 纳入 | 订单状态过滤作用于订单；只求 `price`，不含 `freight_value`。 |
| `JP02_order_scalar` | `paid_order_count` 标量/时间 | `fact_orders` | 纳入 | 订单粒度去重、状态过滤。 |
| `JP03_delivery_scalar` | `average_delivery_days` 标量/时间 | `fact_orders` | 纳入 | 两端时间齐全；时长单位与 PostgreSQL 表达式需固定。 |
| `JP04_review_scalar` | `positive_review_rate` 标量/时间 | `fact_reviews` | 纳入 | 评价行粒度；有效评分为 1--5。 |
| `JP05_customer_geo_order` | 订单粒度指标按州/城市 | `fact_orders -> dim_customers` | 纳入 | `orders_customers` 为 many-to-one；保持订单粒度。 |
| `JP06_customer_geo_gmv` | `gmv` 按州/城市 | `fact_orders -> fact_order_items`，再经 `orders_customers` | 纳入 | 以订单商品行聚合 GMV；客户维度不放大商品行。 |
| `JP07_customer_geo_review` | `positive_review_rate` 按州/城市 | `fact_reviews -> fact_orders -> dim_customers` | 纳入 | 评价行只经订单关联客户地理维度；不连接商品行。 |
| `JP08_category_gmv` | `gmv` 按品类 | `fact_orders -> fact_order_items -> dim_products` | 纳入 | 商品品类天然属于商品行，GMV 的事实粒度与分组粒度一致。 |
| `JP09_seller_gmv` | `gmv` 按卖家 | `fact_orders -> fact_order_items -> dim_sellers` | 纳入 | 卖家天然属于商品行，GMV 的事实粒度与分组粒度一致。 |
| `JP10_metric_cte_cross_join` | 多指标标量 | 每指标独立使用 `JP01`--`JP04` 后 `CROSS JOIN` | 纳入 | 任何跨事实表组合都不得先裸 Join 明细。 |
| `JP11_metric_cte_customer_geo` | 多指标按州 | 每指标独立聚合州级结果后按 `customer_state` Join | 纳入 | 各 CTE 保持自己的事实粒度；最终只保留州和 metric aliases。 |
| `JP12_metric_cte_purchase_time` | 多指标按时间 | 仅 `gmv`、`paid_order_count`、`average_delivery_days` 独立按购买时间聚合后按 `time` Join | 纳入 | 三项共用订单购买时间；不把该字段替代好评率的评价时间。 |

`JP08`、`JP09` 只对 `gmv` 进入 v1。品类或卖家不是订单/评价事实的天然唯一归属，因此不能把同一 Join 程序泛化给履约天数、好评率或订单数。

## 4. 指标 × 维度 × 时间可训练矩阵

“纳入”表示允许进入下一阶段的 QuerySpec/renderer 设计，不表示已经有样本或 Gold SQL。

| 指标 | 无维度标量 | 明确时间窗口 | 时间序列 | 客户州 | 客户城市 | 商品品类 | 卖家 | 支付方式 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gmv` | 纳入 `JP01` | 纳入，购买时间 | 纳入，购买时间 | 纳入 `JP06` | 纳入 `JP06` | 纳入 `JP08` | 纳入 `JP09` | 排除，需要支付归因/分摊规则。 |
| `paid_order_count` | 纳入 `JP02` | 纳入，购买时间 | 纳入，购买时间 | 纳入 `JP05` | 纳入 `JP05` | 暂缓，订单可跨品类，需明确“含该品类的订单”是否可重复计入多个品类。 | 暂缓，订单可跨卖家，需明确归属/重复计数语义。 | 排除，需要订单支付方式归属规则。 |
| `average_delivery_days` | 纳入 `JP03` | 纳入，购买时间 | 纳入，购买时间 | 纳入 `JP05` | 纳入 `JP05` | 排除，Catalog 已声明 `requires_attribution`。 | 排除，需穿过订单商品行，卖家并非订单的唯一归属。 | 不在允许维度中。 |
| `positive_review_rate` | 纳入 `JP04` | 纳入，评价创建时间 | 纳入，评价创建时间 | 纳入 `JP07` | 纳入 `JP07` | 排除，Catalog 已声明 `requires_attribution`。 | 排除，需穿过订单商品行，卖家并非评价的唯一归属。 | 不在允许维度中。 |

### 时间范围和粒度

v1 可训练时间语义只有以下三类：

1. 无显式时间范围的全量标量或分组查询；
2. 已由 `WorkingMemory` 确认的绝对日期范围；
3. 以各指标自身 `time_field` 分组的日、月、季度或年度时间序列。

不纳入“本月”“最近”“去年同期”等未确认相对时间。Router 已要求这类问题先澄清，不能用训练集把其改造成模型自行猜测日历范围。`QuerySpec -> renderer` 设计时必须单独冻结时间端点约定、时区/日期截断和 PostgreSQL `INTERVAL` 转天数的 canonical form；在该约定写入 renderer 合同前，不物化任何涉及时窗或时间序列的 Gold。

## 5. 多指标查询形状

多指标是当前小模型迁移失配的主要来源之一，但第一版不应因此完全放弃。只纳入服务器现有 `QueryPlan` 已明确表达、且能以独立 CTE 保持事实粒度的形状。

| 查询形状 | 指标集合 | 维度/时间 | Join 程序 | v1 状态 | 原因 |
| --- | --- | --- | --- | --- | --- |
| 标量概览 | 4 个核心指标的任意 2--4 项 | 无 | `JP10` | 纳入 | `scalar_multi_metric_overview` 已要求每指标 CTE 后 `CROSS JOIN`。 |
| 标量概览 + 明确时间范围 | 4 个核心指标的任意 2--4 项 | 无 | `JP10` | 纳入 | 各 CTE 使用各自时间字段和相同确认窗口。 |
| 按州多指标 | 4 个核心指标的任意 2--4 项 | `customer_state` | `JP11` | 纳入 | 州是四项都能经多对一客户维度安全关联的共同键。 |
| 按州多指标 + 明确时间范围 | 4 个核心指标的任意 2--4 项 | `customer_state` | `JP11` | 纳入 | 每个 CTE 独立按州和自身时间字段聚合。 |
| 按时间多指标 | `gmv`、`paid_order_count`、`average_delivery_days` 的任意 2--3 项 | 日/月/季度/年 | `JP12` | 纳入 | 三项均使用订单购买时间。 |
| 含好评率的按时间多指标 | 任意含 `positive_review_rate` 的组合 | 时间 | 排除 | 好评率使用评价创建时间，当前 QueryPlan 会产生 `selected_metrics_use_different_time_fields` warning；v1 合同不允许带 warning 的 SQL SFT 样本。 |
| 按城市多指标 | 任意多指标 | `customer_city` | 暂缓 | SQL 语义可沿用州级 CTE 形状，但高基数展示、行数、排序和训练覆盖应另行设计；不与首版州级多指标混入。 |
| 按品类/卖家多指标 | 任意多指标 | 品类/卖家 | 排除 | 存在跨订单/评价事实的多对多归属或重计数风险。 |
| 支付方式多指标 | 任意 | `payment_type` | 排除 | 没有服务器归因规则。 |

## 6. 首版分布目标

下表是物化前的上限/配额规划，避免数据只堆在最简单标量 SQL 上。它不是现有数据行数，也不是必须机械凑满的配额；若可用语义 family 不足，必须减少规模，不得复制模板。

| 覆盖桶 | 允许内容 | Train 行目标 | Validation 行目标 | In-domain test 行目标 | 最低 family 目标 |
| --- | --- | ---: | ---: | ---: | ---: |
| A. 单指标标量 | 4 指标，无时间或确认时窗 | 520 | 80 | 80 | 24 |
| B. 单指标时间序列 | 4 指标，日/月/季度/年 | 450 | 70 | 70 | 24 |
| C. 单指标客户地域 | 4 指标，州/城市，无时间或确认时窗 | 520 | 80 | 80 | 28 |
| D. 单指标商品行地域 | `gmv` 按品类或卖家 | 220 | 35 | 35 | 12 |
| E. 多指标标量 | 2--4 项，无时间或确认时窗 | 500 | 75 | 75 | 28 |
| F. 多指标按州 | 2--4 项，`customer_state`，可有确认时窗 | 500 | 75 | 75 | 28 |
| G. 多指标同时间字段序列 | `gmv`/订单数/履约天数中 2--3 项 | 290 | 45 | 45 | 18 |
| **总计** | 仅上述纳入范围 | **3,000** | **460** | **460** | **162** |

同一 `family_id` 只能属于一个 split。为了让 validation/test 仍能检验组合泛化，三者可共享原子指标、物理表和已有的单指标 Join 程序，但不得共享完整 `metric_set + result_shape + dimension + time_mode + join_program + aggregation strategy`。in-domain test 预留以下未见组合：

- 训练中单独见过的指标，组成未在 train 出现过的安全多指标州级组合；
- 训练中单独见过的时间粒度，组成未在 train 出现过的同时间字段多指标时间序列；
- 不同的中文表达风格，但绝不把同一 QuerySpec 的轻微改写跨 split。

每个 bucket 还需报告：QuerySpec 数、family 数、每 family 的语言改写数、指标/维度/时间/Join 覆盖、Prompt/SQL token 分位数和人工抽检结果。单靠 `3,000` 行不足以说明覆盖或泛化。

## 7. 明确排除项与发现的运行时空白

| 类别 | v1 决定 | 原因与后续前提 |
| --- | --- | --- |
| `payment_type` | 排除 | `gmv` 和订单数已有 `requires_attribution`；当前没有已注册、可执行的服务器归因规则。 |
| 履约/好评按品类 | 排除 | Catalog 已明确 `requires_attribution`，不能通过商品行复制订单/评价事实。 |
| 订单数按品类或卖家 | 暂缓 | Catalog 当前允许检索，但订单可跨品类/卖家；需要冻结“每组订单数”的重叠展示语义或归属/分摊规则。 |
| 履约/好评按卖家 | 排除 | 需经过订单商品行，卖家不是订单/评价的唯一维度；当前无 Catalog policy 或服务器规则。 |
| Top-N、排序、排名 | 排除 | Router 可识别部分排名措辞，但 `QueryPlan` 尚未把排序列、方向、limit 作为结构化字段暴露给真实 Prompt；不能为训练另造隐藏 QuerySpec 字段。 |
| 自由业务筛选 | 排除 | 当前 QueryPlan/WorkingMemory 未有通用、受控的过滤解析与 Prompt 合同；仅允许默认过滤和已确认时间范围。 |
| 同比、环比、增减、因果归因 | 排除 | 需要比较基线、时间对齐和证据/解释合同；当前 candidate SQL SFT 只负责查询，不训练模型凭结果写因果结论。 |
| 相对时间 | 排除 | Router 对缺少确认范围的相对时间 fail closed。 |
| 帮助、指标定义、通用建议、澄清、拒答、结果追问、多轮记忆 | 排除 | 它们不是 SQL candidate target，必须单独建 Router/对话训练与评测合同。 |
| 数据集/敏感元数据、原始标识投影 | 排除 | 不属于 analyst 的候选 SQL 训练范围，且会破坏敏感字段和结果列边界。 |

这份矩阵记录了两个应在后续独立处理的产品/数据治理空白：

1. 对 `seller_id`、`product_category_name` 的 Catalog `allowed_dimensions` 需要继续补充“事实粒度是否天然归属”的明确政策，不能只靠 Router 的显式 `requires_attribution` 拦截；
2. `QueryPlan` 还没有结构化承载 `ORDER BY`、`LIMIT`、自由筛选或相对时间解析，因此这些能力不能作为“真实 Prompt 对齐”领域 SFT 的监督范围。

本轮不修改这些运行时模块或 Catalog。将它们直接混入训练数据，会把“模型看到了额外隐藏计划”误当成小模型能力提升。

## 8. 进入下一任务的条件

下一项只能设计并审阅 `QuerySpec` schema 和确定性 PostgreSQL Gold renderer 的职责边界。开始前必须先固定：

1. `QuerySpec` 如何引用上述 `join_program_id`、指标、维度、确认时间范围和结果列；
2. 每项指标的 canonical PostgreSQL 表达式、默认过滤、日期边界、时间截断和 `average_delivery_days` 的单位；
3. 多指标 CTE 的稳定命名、Join 键和顶层 alias 顺序；
4. 对 `metric_version=0.1-draft` 是否以当前快照物化，或先单独升级/冻结 Catalog 版本；
5. renderer 不允许实现、推断或绕过的归因、排序、自由过滤和解释行为。

在该任务完成前，不实现代码、不生成任何 QuerySpec、Gold SQL 或领域训练行。
