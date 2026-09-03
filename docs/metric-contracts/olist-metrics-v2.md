# Olist 十项业务指标合同 v2

**状态：** 已冻结；未实现 `QuerySpec`、确定性 Gold renderer、领域训练行或训练。
**运行时快照：** `olist-catalog-v2` / `metric_version=0.2-frozen` / `olist-kaggle-v2-2026-08-03` / PostgreSQL / `sql-policy-v1`。
**权威机器可读来源：** [`data/catalog/olist_catalog.yaml`](../../data/catalog/olist_catalog.yaml)。本文件解释公式、分母、维度边界和数据快照证据；不替代 Catalog。

## 1. 适用边界

指标 ID 是业务语义和最终结果列别名，不是物理字段。候选 SQL 必须从 Catalog 给出的真实表、列、时间字段、默认过滤和 Join 路径编译表达式；随后仍须经过 SQL Policy、PostgreSQL `daa_analytics_reader`、ResultContract 和 ResultValidator。当前候选结果必须且只能返回服务器声明的列；金额/时长/计数不得为负，计数必须为整数，比例必须落在 `[0, 1]`，评价分必须在 `[1, 5]`。这些是必要的结果合理性约束，不证明指标公式或任意 join 的业务语义正确。

本合同不授予如下能力：支付归因、订单/评价按品类或卖家的归属、自由过滤、排名/Top-N、同比环比、因果解释、QuerySpec 或 SQL 生成。历史 `text_to_sql_v2` 与其 60 条 protected holdout 固定在 `olist-catalog-v1` / `0.1-draft`，不能当作本快照的 v2 评测或训练输入。

## 2. 指标定义

| ID | 公式与粒度 | 默认时间字段 | 安全直接维度 | 不能自行推断的边界 |
| --- | --- | --- | --- | --- |
| `gmv` | 有效订单的 `SUM(fact_order_items.price)`；商品行粒度，不含 `freight_value` | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市、商品品类 | `seller_id` 是敏感展示/分组列；支付方式需要支付归属/分摊规则。 |
| `paid_order_count` | 有效订单的 `COUNT(DISTINCT fact_orders.order_id)`；订单粒度 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市 | 品类、卖家、支付方式都可能使同一订单跨组出现，须先冻结归属/分摊规则。 |
| `average_delivery_days` | `AVG(actual_delivery - purchase)`，仅两端时间齐全；订单粒度 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市 | 商品品类、卖家会通过订单商品行复制订单时长，须归属规则。 |
| `positive_review_rate` | 有效评分评价行中 `review_score >= 4` 的比例；评价行粒度 | `fact_reviews.review_creation_date` | 日期、客户州/城市 | 商品品类、卖家会复制评价行，须归属规则。 |
| `item_count` | 有效订单的 `COUNT(*)` 商品行；每个 `(order_id, order_item_id)` 是一件商品行 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市、商品品类 | `seller_id` 是敏感展示/分组列；支付方式需要商品行的支付归属/分摊规则。 |
| `average_order_value` | 有效订单商品金额之和 / 有效订单数。必须先按订单聚合 `SUM(price)`，再平均订单金额；订单粒度，不含运费且不是支付金额 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市 | 品类、卖家、支付方式下的分子/分母归属尚未冻结。 |
| `average_review_score` | 有效评分评价行的 `AVG(review_score)`；评价行粒度，不按 `review_id` 去重 | `fact_reviews.review_creation_date` | 日期、客户州/城市 | 品类、卖家、支付方式的归属尚未冻结。 |
| `on_time_delivery_rate` | `delivered_customer_date <= estimated_delivery_date` 的 eligible delivered 订单数 / eligible delivered 订单数；订单粒度 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市 | 分母仅含 `order_status='delivered'` 且购买、实际送达、预计送达时间都存在的订单；不能把未送达、取消或缺失预计时间当作迟到。 |
| `cancellation_rate` | `order_status='canceled'` 的订单数 / 购买时间存在的全部订单数；订单粒度 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市 | `unavailable` 不计入取消分子；不能把“未完成”或支付失败自行并入。 |
| `freight_amount` | 有效订单的 `SUM(fact_order_items.freight_value)`；商品行粒度，以 BRL 计 | `fact_orders.order_purchase_timestamp` | 日期、客户州/城市、商品品类 | `seller_id` 是敏感展示/分组列；不得和 GMV 或 `payment_value` 混称；支付方式需要运费归属/分摊规则。 |

“有效订单”统一为 `order_status NOT IN ('canceled', 'unavailable')`。所有金额均为 `BRL`，展示符号 `R$`；不做汇率换算。

## 3. 质量与数据快照证据

2026-09-02 使用项目 PostgreSQL `daa_analytics_reader` 做只读聚合核验。只记录聚合值，不导出订单、客户或评价原始行：

| 证据项 | 值 | 合同含义 |
| --- | ---: | --- |
| 全部订单 / 有效订单 | 99,441 / 98,207 | 取消率分母与有效订单过滤可区分。 |
| 有效商品行 / 不重复 `(order_id, order_item_id)` | 112,101 / 112,101 | `item_count` 可严格解释为商品行数。 |
| GMV / 运费金额 | 13,494,400.74 / 2,241,126.29 BRL | 两种金额口径可独立回归。 |
| 平均订单商品金额 | 137.42 BRL | 分母为有效订单，不是支付记录数。 |
| 有效评分评价行 / 不同 `review_id` | 99,224 / 98,410 | 有 814 条额外 `review_id` 行；平均评分和好评率明确按评价行，不能暗中改为 review-ID 去重。 |
| 平均评价分 | 4.0864 | 只纳入 1--5 分。 |
| 准时率 eligible 分母 / 准时订单 | 96,470 / 88,644 | 对应准时送达率 0.918876。 |
| 取消订单 / 取消率分母 | 625 / 99,441 | 对应取消率 0.006285。 |

`evals/sql/verify_olist_metrics_v2.sql` 和 `evals/results/olist-v3-golden.yaml` 固定这十项的聚合回归基线。它们能发现数据或公式漂移，不能证明在线模型 SQL 语义正确，也不替代逐条领域 Gold 的人工审查。

## 4. 版本、接口与后续门

- `CatalogLoader`、`OLIST_WORKSPACE`、Prompt、ResultContract、运行 trace 和审计默认携带 `olist-catalog-v2` / `0.2-frozen`，避免新旧口径混用。
- `seller_id` 已从所有 metric 的 `allowed_dimensions` 删除；CatalogLoader 会拒绝任何将敏感物理列重新声明为 analyst 可展示维度的配置。它仍可按 SQL Policy 在内部关联或受控聚合中使用，不能成为最终分组或结果列。
- `customer_city`、`payment_type` 以及卖家展示在当前运行时由 QueryPlan 预检拒绝，直到单独冻结稳定排序、Top-N、行数预算、截断解释或归属合同。维度识别仍只接受列名或列别名的直接命中，不再因商品行表带有 `GMV` 标签而误把 GMV 问题判成卖家维度。
- 旧的“按品类有效订单数前十”starter 已移除，因为它需要未冻结的订单归属规则且 Top-N 尚未进入 QueryPlan 合同；替换为无排名的“按商品品类统计 GMV”。受保护的 `text_to_sql_v2` 保留旧快照预期，因此当前 Catalog 下确定性回放为 `57/60`：`data_014` 与 `multi_006` 因 `dimension_attribution_requires_clarification` 被拒绝，`multi_005` 因州 × 月的保守结果形状超过 200 行预算而以 `result_row_budget_exceeded` 被拒绝。三者都是历史到 v2 合同的迁移差异，不是 v2 回归通过率。
- 十指标 coverage matrix 与 QuerySpec/renderer 职责设计均已冻结。下一项仅可实现并共同审阅 QuerySpec schema、验证器、只读指标表达式注册表、deterministic renderer 及其单元测试；在此之前不得物化训练行或启动 split/token 审计、GPU 训练和评测。
