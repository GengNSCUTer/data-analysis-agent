# 指标目录（Phase 2 草案）

指标目录是 Agent 的业务约束来源。下列定义是 v0.1 草案，等数据加载和人工核验完成后
才冻结为 v1。所有指标都必须带 `metric_id`、版本、默认时间字段、粒度、允许维度和
推荐图表；模型不能只根据字段名猜口径。

| metric_id | 中文名称 | 定义 | 默认时间字段 | 允许维度 |
| --- | --- | --- | --- | --- |
| `gmv` | 商品成交额 | `SUM(fact_order_items.price)`，订单状态排除 `canceled`、`unavailable` | `order_purchase_timestamp` | 日期、州/城市、品类、卖家、支付类型 |
| `paid_order_count` | 有效订单数 | `COUNT(DISTINCT fact_orders.order_id)`，排除 `canceled`、`unavailable` | `order_purchase_timestamp` | 日期、州/城市、品类、卖家、支付类型 |
| `average_order_value` | 平均订单金额 | `gmv / paid_order_count`，分母为有效订单数 | `order_purchase_timestamp` | 日期、州/城市、品类、支付类型 |
| `item_count` | 商品件数 | `COUNT(*)`，按订单商品行计数 | `order_purchase_timestamp` | 日期、州/城市、品类、卖家 |
| `average_delivery_days` | 平均履约天数 | `AVG(delivered_customer - order_purchase)`，仅统计两端时间齐全订单 | `order_purchase_timestamp` | 日期、州/城市、卖家、品类 |
| `on_time_delivery_rate` | 准时送达率 | 实际送达时间 `<=` 预计送达时间的订单数 / 两个时间均存在的已送达订单数 | `order_purchase_timestamp` | 日期、州/城市、卖家、品类 |
| `positive_review_rate` | 好评率 | `review_score >= 4` 的评价数 / 有效评分评价数 | `review_creation_date` | 日期、州/城市、品类、卖家 |
| `average_review_score` | 平均评价分 | `AVG(review_score)`，仅统计 1–5 分评价 | `review_creation_date` | 日期、州/城市、品类、卖家 |

## 共同约束

- 默认不把运费并入 `gmv`；用户明确询问含运费金额时必须切换到单独口径。
- 多表关联时先按事实表粒度聚合，避免订单项与支付/评价一对多连接造成金额放大。
- 没有明确时间范围时，Agent 应先询问或明确展示数据覆盖范围，不能默认为“本月”。
- 比率指标展示分子、分母和缺失值排除规则；分母为 0 时返回无数据而不是 0%。
- 所有结果必须携带 `metric_id`、目录版本和使用的时间字段。

## 待人工核验的问题

1. Olist 的业务展示是否把 `approved`/`created` 状态纳入 GMV，还是只纳入已发票/已发货订单。
2. `payment_value` 与商品 `price` 的差异是否需要独立展示为“支付金额”。
3. 州/城市的展示是否只允许聚合到州，避免邮编前缀和城市稀疏数据造成误读。
4. 评价关联是否按订单级去重，防止多条评价记录改变订单口径。

## 当前技术基线

基于 `olist-kaggle-v2-2026-08-03`、转换版本 `olist-analytics-v1` 和本目录 v0.1 草案，
PostgreSQL 实测基线为：GMV `13,494,400.74`，有效订单数 `98,207`，平均履约天数
`12.558702`，评价行粒度好评率 `0.770680`。版本化结果见
`evals/results/olist-v2-golden.yaml`，可执行回归断言见
`evals/sql/verify_olist_golden.sql`。这些数值用于发现转换或 SQL 语义漂移，不表示上述
待核验业务问题已经获得业务方批准。
