# Olist 领域 Candidate SQL SFT 覆盖矩阵 v2

## 1. 任务卡

**状态：** 覆盖范围与 `QuerySpec`/renderer 职责设计均已冻结；尚未实现 renderer、未物化训练行、未加载 tokenizer、未启动 GPU。
**上游合同：** [`olist-domain-sft-data-contract-v1.md`](olist-domain-sft-data-contract-v1.md) 与 [`../../metric-contracts/olist-metrics-v2.md`](../../metric-contracts/olist-metrics-v2.md)。
**Catalog 快照：** `olist-catalog-v2` / `metric_version=0.2-frozen` / `olist-kaggle-v2-2026-08-03` / PostgreSQL / `sql-policy-v1`。

| 项目 | 本轮冻结内容 |
| --- | --- |
| 目标 | 依据十项业务指标合同，定义后续领域 Candidate SQL SFT 可安全覆盖的指标、维度、时间、多指标形状与明确排除项。 |
| 输入 | v2 Catalog、十指标合同、QuestionRouter、QueryPlan、ResultContract、数据模型、既有路由和计划测试。 |
| 输出 | 十指标覆盖矩阵、未来 renderer 可引用的逻辑 Join 程序、split 分布目标、排除清单与进入下一任务的条件。 |
| 非目标 | 不新增指标、维度、归属/分摊规则；不修改 Catalog、Router、QueryPlan、Policy 或数据库；不生成 QuerySpec、Prompt、SQL、审计或训练配置。 |
| 不变量 | 训练输入仍是 `olist-candidate-sql-v1` 的真实运行时 Prompt；候选模型只生成 SQL；60 条 protected holdout 不读取原文或 Gold；产物保持仓库外；运行时安全链路不变。 |
| 验收证据 | Catalog 的 10 个指标、粒度、时间字段、`dimension_policies`、12 个逻辑 Join 程序和 QueryPlan 时间字段 warning 均能解释每一项纳入、澄清或排除决定。 |

[`olist-domain-sft-coverage-matrix-v1.md`](olist-domain-sft-coverage-matrix-v1.md) 保留为
`olist-catalog-v1` / 四指标历史快照，不再可用于数据物化或新评测。

## 2. 裁决原则

一个单元必须同时满足以下条件，才能标记为“纳入”；不能因为 SQL 可写或 `allowed_dimensions`
包含某个维度，就把它视为可训练的业务语义。

```text
当前运行时可路由
  + 指标事实粒度与分组维度天然一致，或存在不改变粒度的受控 Join
  + 不依赖未冻结的订单、评价或支付归属/分摊规则
  + 可由真实 Prompt 中的 Catalog、QueryPlan 和 ResultContract 完整表达
  = 可进入后续 QuerySpec / deterministic renderer 设计
```

这里的“纳入”只表示允许下一阶段设计确定性 Gold SQL，不代表已有样本、Gold、训练数据、
模型效果或在线可用性。任何未来 Gold 仍须通过 SQL Policy、PostgreSQL reader role、
ResultContract、ResultValidator 和人工口径审查。

## 3. 十项指标事实分组

| 事实组 | 指标 | 粒度与时间字段 | 安全维度边界 |
| --- | --- | --- | --- |
| 商品行 | `gmv`、`item_count`、`freight_amount` | `order_item`；`fact_orders.order_purchase_timestamp` | 日期、客户州、商品品类。客户城市关联粒度安全但受高基数输出合同限制，暂不物化；卖家 ID 是敏感展示列，支付方式需要归属/分摊。 |
| 订单 | `paid_order_count`、`average_delivery_days`、`average_order_value`、`on_time_delivery_rate`、`cancellation_rate` | `order`；`fact_orders.order_purchase_timestamp` | 日期、客户州。客户城市暂缓；品类、卖家或支付方式会使订单跨组或引入重复计数，不能自行推断。 |
| 评价行 | `positive_review_rate`、`average_review_score` | `review`；`fact_reviews.review_creation_date` | 日期、客户州。客户城市暂缓；品类、卖家会复制评价行，支付方式没有冻结的关联/归属语义。 |

所有金额使用 BRL；`average_order_value` 是先按订单聚合 `price` 后的平均商品金额，不含运费且不等同于
`payment_value`。评价指标按有效评价行而不是 `review_id` 去重；准时率和取消率必须沿用十指标合同中的分子、分母和状态过滤。

## 4. 逻辑 Join 程序目录

下列 ID 是未来 `QuerySpec` 的稳定设计引用，不是现有 renderer 或已经生成的 SQL。

| ID | 使用范围 | 受控事实路径 | 关键不变量 |
| --- | --- | --- | --- |
| `JP01_item_scalar` | `gmv`、`item_count`、`freight_amount` 的标量/时间 | `fact_orders -> fact_order_items` | 订单状态过滤作用于订单；金额分别只使用 `price` 或 `freight_value`。 |
| `JP02_order_scalar` | 五个订单指标的标量/时间 | `fact_orders`，AOV 再关联并先聚合 `fact_order_items` | 订单去重、状态过滤和每项分母固定；AOV 不能按商品行直接平均。 |
| `JP03_review_scalar` | 两个评价指标的标量/时间 | `fact_reviews` | 仅 `review_score BETWEEN 1 AND 5`；按评价行计算。 |
| `JP04_customer_geo_item` | 商品行指标按客户州 | `fact_orders -> fact_order_items -> dim_customers` | 客户是订单的多对一维度，不放大商品行；客户城市等待输出合同。 |
| `JP05_customer_geo_order` | 订单指标按客户州 | `fact_orders -> dim_customers`，AOV 保持订单内商品聚合 | 每个 CTE 先保持订单粒度；客户城市等待输出合同。 |
| `JP06_customer_geo_review` | 评价指标按客户州 | `fact_reviews -> fact_orders -> dim_customers` | 不连接商品行，避免复制评价；客户城市等待输出合同。 |
| `JP07_category_item` | 商品行指标按品类 | `fact_orders -> fact_order_items -> dim_products` | 品类天然属于商品行；翻译表只作展示映射，不能改写聚合粒度。 |
| `JP09_scalar_multi_metric` | 任意安全多指标标量 | 每项独立使用 `JP01`--`JP03`，顶层 `CROSS JOIN` | 跨事实表不得裸 Join 明细。 |
| `JP10_state_multi_metric` | 任意安全多指标按客户州 | 每项独立聚合州级结果后按 `customer_state` Join | 每个 CTE 保持自身事实粒度和时间字段。 |
| `JP11_purchase_time_multi_metric` | 八个购买时间指标的多指标时间序列 | 每项独立按购买时间截断后按 `time` Join | 只允许同一购买时间字段的指标组合。 |
| `JP12_review_time_multi_metric` | 两个评价指标的多指标时间序列 | 每项独立按评价创建时间截断后按 `time` Join | 只允许 `positive_review_rate` 与 `average_review_score` 的组合。 |

## 5. 指标 × 维度 × 时间矩阵

“需澄清”表示运行时应该在 SQL 生成前返回归属问题；“排除”表示本轮不进入 renderer
设计，也不以训练数据诱导模型绕过该边界。

| 指标 | 标量 / 确认时窗 / 自身时间序列 | 客户州 | 客户城市 | 商品品类 | 卖家 | 支付方式 |
| --- | --- | --- | --- | --- | --- | --- |
| `gmv` | 纳入 `JP01` | 纳入 `JP04` | 暂缓：高基数输出/稳定顺序未入合同。 | 纳入 `JP07` | 排除：`seller_id` 是敏感结果/分组列。 | 需澄清：支付归属/分摊未冻结。 |
| `item_count` | 纳入 `JP01` | 纳入 `JP04` | 暂缓：高基数输出/稳定顺序未入合同。 | 纳入 `JP07` | 排除：`seller_id` 是敏感结果/分组列。 | 需澄清：商品行支付归属/分摊未冻结。 |
| `freight_amount` | 纳入 `JP01` | 纳入 `JP04` | 暂缓：高基数输出/稳定顺序未入合同。 | 纳入 `JP07` | 排除：`seller_id` 是敏感结果/分组列。 | 需澄清：运费支付归属/分摊未冻结。 |
| `paid_order_count` | 纳入 `JP02` | 纳入 `JP05` | 暂缓：高基数输出/稳定顺序未入合同。 | 需澄清：一单可跨品类。 | 需澄清：一单可跨卖家。 | 需澄清：一单可有多条支付记录。 |
| `average_delivery_days` | 纳入 `JP02` | 纳入 `JP05` | 暂缓：高基数输出/稳定顺序未入合同。 | 需澄清：订单履约被商品行复制。 | 需澄清：订单履约被卖家商品行复制。 | 排除：Catalog 未允许且无归属合同。 |
| `average_order_value` | 纳入 `JP02` | 纳入 `JP05` | 暂缓：高基数输出/稳定顺序未入合同。 | 排除：分子/分母按品类归属未冻结。 | 排除：分子/分母按卖家归属未冻结。 | 排除：支付不等于商品金额，且归属未冻结。 |
| `on_time_delivery_rate` | 纳入 `JP02` | 纳入 `JP05` | 暂缓：高基数输出/稳定顺序未入合同。 | 排除：准时订单按品类归属未冻结。 | 排除：准时订单按卖家归属未冻结。 | 排除：Catalog 未允许且无归属合同。 |
| `cancellation_rate` | 纳入 `JP02` | 纳入 `JP05` | 暂缓：高基数输出/稳定顺序未入合同。 | 排除：取消订单按品类归属未冻结。 | 排除：取消订单按卖家归属未冻结。 | 排除：Catalog 未允许且无归属合同。 |
| `positive_review_rate` | 纳入 `JP03` | 纳入 `JP06` | 暂缓：高基数输出/稳定顺序未入合同。 | 需澄清：评价会被订单商品行复制。 | 需澄清：评价会被卖家商品行复制。 | 排除：Catalog 未允许且无归属合同。 |
| `average_review_score` | 纳入 `JP03` | 纳入 `JP06` | 暂缓：高基数输出/稳定顺序未入合同。 | 排除：评价按品类归属未冻结。 | 排除：评价按卖家归属未冻结。 | 排除：Catalog 未允许且无归属合同。 |

时间语义只覆盖：无显式范围、由 WorkingMemory 已确认的绝对日期范围，以及按照指标自身
`time_field` 的日/月/季度/年度序列。相对时间、同比环比和自由日期解析保持排除；未来 renderer
必须单独固定闭区间/半开区间、时区、日期截断和履约天数的 canonical PostgreSQL 形式。

## 6. 多指标查询形状

| 查询形状 | 指标集合 | 维度/时间 | 程序 | 决定 |
| --- | --- | --- | --- | --- |
| 标量概览 | 十项中任意 2--4 项 | 无维度，可带确认时窗 | `JP09` | 纳入。上限必须与当前 `CatalogRetriever.max_metrics=4` 一致；每项以独立 CTE 聚合，禁止跨事实表明细 Join。 |
| 按客户州多指标 | 十项中任意 2--4 项 | `customer_state`，可带确认时窗 | `JP10` | 纳入。上限为 4；所有指标均有安全州级路径。 |
| 按客户城市多指标 | 任意安全多指标 | `customer_city` | 暂缓。语义路径安全，但高基数结果、行数上限、排序和样本长度尚未进入 QuerySpec 合同。 |
| 购买时间多指标序列 | `gmv`、`item_count`、`freight_amount`、`paid_order_count`、`average_delivery_days`、`average_order_value`、`on_time_delivery_rate`、`cancellation_rate` 中任意 2--4 项 | 日/月/季度/年 | `JP11` | 纳入。四项上限与运行时一致；按日必须有确认绝对范围，且任何形状都不得超过 analyst 200 行结果预算。 |
| 评价时间多指标序列 | `positive_review_rate`、`average_review_score` | 日/月/季度/年 | `JP12` | 纳入。按日必须有确认绝对范围，且受 200 行结果预算约束。 |
| 跨购买/评价时间字段的序列 | 任意同时含订单/商品行与评价行指标 | 时间序列 | 无 | 排除。`QueryPlan` 会产生 `selected_metrics_use_different_time_fields` warning，不能把有 warning 的形状写入 SQL SFT。 |
| 按品类/卖家多指标 | 任意混合粒度组合 | 品类或卖家 | 无 | 排除。混入订单或评价指标会产生归属歧义；卖家 ID 还属于敏感结果/分组列。 |
| 支付方式多指标 | 任意 | `payment_type` | 无 | 排除。没有服务器归属/分摊规则。 |

## 7. 物化前的分布目标

下表是后续人工审核后可使用的最大规划配额，而不是现有样本数量或必须凑满的训练规模。
任何桶若无法提供足够多的独立 `QuerySpec` / `family_id`，必须缩小规模，不能用同义改写填数。

| 覆盖桶 | 允许内容 | Train 上限 | Validation 上限 | In-domain test 上限 | 最低 family 目标 |
| --- | --- | ---: | ---: | ---: | ---: |
| A. 单指标标量 | 十指标，无时间或确认时窗 | 760 | 115 | 115 | 50 |
| B. 单指标时间序列 | 十指标，各自时间字段 | 760 | 115 | 115 | 60 |
| C. 单指标客户州 | 十指标，州，无时间或确认时窗 | 720 | 105 | 105 | 50 |
| D. 商品行品类维度 | `gmv`/`item_count`/`freight_amount` 按品类 | 300 | 45 | 45 | 20 |
| E. 多指标标量 | 任意安全 2--4 项 | 600 | 90 | 90 | 50 |
| F. 多指标按州 | 任意安全 2--4 项 | `customer_state`，可带确认时窗 | 720 | 105 | 105 | 60 |
| G. 同时间字段多指标序列 | `JP11` 或 `JP12`；2--4 项 | 日/月/季度/年，按日需绝对范围和行数预算 | 600 | 90 | 90 | 40 |
| **总计** | 仅本矩阵的纳入范围 | **4,460** | **665** | **665** | **330** |

同一 `family_id`、`sql_program_id` 或仅日期/阈值/措辞/别名不同的表面改写只能属于一个 split。
Train、validation 和 in-domain test 可以共享原子指标、表和单指标 Join，但不得共享完整的
`metric_set + result_shape + dimension + time_mode + join_program + aggregation_strategy`。
未来每次物化必须同时报告 QuerySpec 数、family 数、行数、指标/维度/时间/Join 覆盖、token
分位数和人工抽检；行数本身不构成覆盖或泛化证据。

## 8. 排除项与运行时空白

| 类别 | 决定 | 原因与解除前提 |
| --- | --- | --- |
| 品类/卖家下的订单、履约、AOV、准时率、取消率或评价指标 | 需澄清或排除 | 需要显式订单/评价归属、去重或分摊合同；不得以商品行 Join 隐式复制事实。 |
| `payment_type` | 排除 | 需要按订单、商品金额和运费分别定义支付归属/分摊规则。 |
| 客户城市分组 | 运行时与物化均拒绝 | 关联粒度本身安全，但当前没有 Top-N、稳定排序、行数上限和截断解释合同；`QueryPlan` 在 SQL 前返回澄清。 |
| 卖家分组 | 排除 | 虽然卖家天然关联商品行，但 `seller_id` 是 analyst 的敏感投影/分组列；当前 SQL Policy 明确拒绝，不能用训练样本绕过。 |
| Top-N、排序、排名 | 排除 | QueryPlan 尚未将排序字段、方向、limit 作为真实 Prompt 的结构化合同。 |
| 自由业务筛选 | 排除 | 当前没有受控过滤解析和 Prompt 合同；只允许默认过滤和确认时间范围。 |
| 相对时间、同比环比、趋势比较、因果解释 | 排除 | 需要日历解析、比较基线或结果解释证据；Candidate SQL SFT 只训练查询候选。 |
| 帮助、指标解释、通用建议、澄清、拒答、结果追问与多轮记忆 | 排除 | 它们不是 SQL candidate target，应由 Router 或对话合同独立处理。 |
| 敏感标识、原始行导出和数据集治理元数据 | 排除 | 不属于 analyst 候选 SQL 范围，并会破坏列白名单和敏感数据边界。 |

## 9. 进入下一任务的条件

本矩阵已经由 [`olist-queryspec-renderer-design-v1.md`](olist-queryspec-renderer-design-v1.md)
落实为 `QuerySpec` schema 与 deterministic PostgreSQL Gold SQL renderer 的职责边界。该设计冻结：

1. `QuerySpec` 如何引用本矩阵的 `join_program_id`、指标、维度、确认时间范围和结果列；
2. 十项 canonical PostgreSQL 表达式、默认过滤、日期边界、时间截断与 AOV/履约天数/比例分母；
3. 多指标 CTE 的稳定命名、Join 键、空值策略和顶层 alias 顺序；
4. renderer 对“需澄清”和“排除”单元必须拒绝，而不是尝试猜测业务语义；
5. 品类分组是 `category_grouped`，仅限 `gmv`、`item_count`、`freight_amount` 的单指标 `JP07_category_item`；卖家分组保持排除，因为 `seller_id` 是 analyst 敏感投影/分组列。多指标上限为四项；按日序列必须具有明确绝对范围，并受 200 行 analyst 结果预算约束。

下一项可以且只能实现并审阅 QuerySpec schema、验证器、只读指标表达式注册表、renderer 和确定性单元测试。
在代码与测试经审阅且用户确认前，不生成正式 QuerySpec、Gold SQL、训练行或启动 split/token 审计和 GPU 任务。
