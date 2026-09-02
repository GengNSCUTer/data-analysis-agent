# Olist 指标目录

当前运行时 Catalog 为 `olist-catalog-v2` / `metric_version=0.2-frozen`，包含 10 个已经冻结的业务指标。机器可读定义是 [`data/catalog/olist_catalog.yaml`](../data/catalog/olist_catalog.yaml)；公式、分母、维度归属和数据快照证据见 [十项业务指标合同 v2](metric-contracts/olist-metrics-v2.md)。

| metric_id | 中文名称 | 事实粒度 | 默认时间字段 |
| --- | --- | --- | --- |
| `gmv` | 商品成交额 | 商品行 | 订单购买时间 |
| `paid_order_count` | 有效订单数 | 订单 | 订单购买时间 |
| `average_delivery_days` | 平均履约天数 | 订单 | 订单购买时间 |
| `positive_review_rate` | 好评率 | 评价行 | 评价创建时间 |
| `item_count` | 商品件数 | 商品行 | 订单购买时间 |
| `average_order_value` | 平均订单商品金额 | 订单 | 订单购买时间 |
| `average_review_score` | 平均评价分 | 评价行 | 评价创建时间 |
| `on_time_delivery_rate` | 准时送达率 | 订单 | 订单购买时间 |
| `cancellation_rate` | 取消率 | 订单 | 订单购买时间 |
| `freight_amount` | 运费金额 | 商品行 | 订单购买时间 |

共同不变量：金额为 BRL；业务指标 ID 不是物理列；模型 SQL 必须通过服务器 Catalog、SQL Policy、reader role 和结果合同；支付归因以及订单/评价按品类或卖家归属默认 fail closed。`olist-v2-golden` 保留为旧 `0.1-draft` 历史快照，当前十指标回归资产为 `evals/results/olist-v3-golden.yaml` 与 `evals/sql/verify_olist_metrics_v2.sql`。
