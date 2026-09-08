# TheLook 首批指标合同 v1

**Catalog：** `thelook-catalog-v1`
**Metric version：** `0.1-frozen`
**Dataset snapshot：** `thelook-kaggle-mirror-v1-20260908`
**Workspace：** `thelook_analytics` / PostgreSQL / `sql-policy-v1`

本文只冻结 TheLook 首批 Semantic Catalog 指标的业务口径，供后续 QuerySpec 和 Gold
SQL renderer 设计使用。当前不生成 SQL、不构造问题、不训练模型，也不代表跨 schema
评测已经运行。

## 共同边界

- 金额字段为 `sale_price`、`cost` 或 `retail_price` 的来源数值；CSV 没有币种字段，
  因此系统不把它们标记为 USD 或其他货币，也不自行换算。
- `Complete`、`Returned`、`Cancelled`、`Processing`、`Shipped` 是来源状态的精确值，
  保留英文原值，不能擅自改成 Olist 的 `delivered` / `canceled`。
- 订单指标以 `orders.order_id` 的订单粒度计算；成交额以 `order_items` 商品行粒度
  计算。跨粒度 Join 时必须先在各自事实粒度聚合，再 Join。
- 首批订单指标的统计时间统一为 `orders.created_at`。不使用 `order_items.created_at`
  或 `users.created_at` 替代。
- 用户 `id`、订单 `order_id`、商品/库存/会话 ID 只能内部关联或去重，不能成为分析员
  的最终投影、分组或排序字段。

## 指标定义

| metric_id | 定义 | 默认过滤 | 粒度 | 首批允许维度 |
| --- | --- | --- | --- | --- |
| `completed_sale_amount` | `Complete` 订单商品行 `SUM(order_items.sale_price)` | `orders.status = 'Complete'` | order item | 日期、用户 state/city、traffic source、商品 category/brand/department |
| `completed_order_count` | `Complete` 订单 `COUNT(orders.order_id)` | `orders.status = 'Complete'` | order | 日期、用户 state/city、traffic source |
| `average_order_value` | 先按订单汇总商品行金额，再对完成订单金额取平均 | `orders.status = 'Complete'` | order | 日期、用户 state/city、traffic source |
| `average_fulfillment_days` | `Complete` 订单 `delivered_at - created_at` 的平均天数 | `status = 'Complete'` 且两个时间非空 | order | 日期、用户 state/city、traffic source |
| `return_rate` | `Returned` 订单数 / (`Complete` + `Returned`) 订单数 | `orders.status IN ('Complete', 'Returned')` | order | 日期、用户 state/city、traffic source |
| `completed_customer_count` | 至少有一笔 `Complete` 订单的 `COUNT(DISTINCT orders.user_id)` | `orders.status = 'Complete'` | customer | 日期、用户 state/city、traffic source |

## 暂不冻结的口径

事件漏斗、会话转化、库存周转、毛利、配送中心绩效和退货商品金额暂不纳入首批指标。
它们需要额外定义事件去重、库存快照时间、成本/售价关系、退货分母和跨事实表归因，
不能因为物理列已经存在就直接放入 Catalog。后续增加指标必须提升 `metric_version`，
补充合同、Gold renderer 规则和分层审计。

## 与 Olist 的比较边界

这些指标是 TheLook 的 schema-specific 定义。它们在概念上可以与 Olist 的订单、成交额、
履约和客户指标做领域对照，但不保证公式、状态映射、币种或事实粒度相同。跨 schema 评测
应比较每个 workspace 自己的 Gold denotation，不把同名指标直接视为数值可比。
