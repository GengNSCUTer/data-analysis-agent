# 数据字典（Olist 运行时 v2）

本字典定义 Olist 展示工作区的最小分析范围和字段语义。原始 CSV 与派生资产保存在仓库外；分析层已加载到项目本地 PostgreSQL，并使用稳定的英文表名。

## 分析表和粒度

| 分析表 | 一行代表 | 主键 | 来源 |
| --- | --- | --- | --- |
| `fact_orders` | 一个订单 | `order_id` | `olist_orders_dataset` |
| `fact_order_items` | 一个订单中的一个商品行 | `order_id`, `order_item_id` | `olist_order_items_dataset` |
| `fact_payments` | 一个订单的一次支付序列 | `order_id`, `payment_sequential` | `olist_order_payments_dataset` |
| `fact_reviews` | 一条评价记录 | 无已验证的唯一单列主键；`review_id` 可重复 | `olist_order_reviews_dataset` |
| `dim_customers` | 一个客户地址记录 | `customer_id` | `olist_customers_dataset` |
| `dim_sellers` | 一个卖家 | `seller_id` | `olist_sellers_dataset` |
| `dim_products` | 一个商品 | `product_id` | `olist_products_dataset` |
| `dim_category_translation` | 一个葡萄牙语品类 | `product_category_name` | `product_category_name_translation` |

## 核心字段

### `fact_orders`

| 字段 | 类型 | 含义 | 口径/限制 |
| --- | --- | --- | --- |
| `order_id` | string | 订单标识 | 不向用户展示原始 ID 集合；用于关联和审计 |
| `customer_id` | string | 订单对应的客户地址记录 | 关联 `dim_customers` |
| `order_status` | enum | `created`、`approved`、`invoiced`、`processing`、`shipped`、`delivered`、`canceled`、`unavailable` | GMV 默认排除 `canceled` 与 `unavailable` |
| `order_purchase_timestamp` | timestamp | 下单时间 | 默认业务日期字段 |
| `order_approved_at` | timestamp | 付款确认时间 | 可能为空 |
| `order_delivered_carrier_date` | timestamp | 交给承运商时间 | 可能为空，不作为默认履约终点 |
| `order_delivered_customer_date` | timestamp | 实际送达时间 | 未送达订单为空 |
| `order_estimated_delivery_date` | timestamp | 预计送达时间 | 用于准时率 |

### `fact_order_items`

| 字段 | 类型 | 含义 | 口径/限制 |
| --- | --- | --- | --- |
| `order_id` | string | 订单标识 | 关联 `fact_orders` |
| `order_item_id` | integer | 订单内商品行序号 | 与 `order_id` 组成主键 |
| `product_id` | string | 商品标识 | 关联 `dim_products` |
| `seller_id` | string | 卖家标识 | 关联 `dim_sellers` |
| `shipping_limit_date` | timestamp | 卖家最晚发货时间 | 不作为 GMV 时间字段 |
| `price` | decimal | 商品成交价 | GMV 默认只汇总此字段，不把运费重复计入 |
| `freight_value` | decimal | 运费 | 单独分析时明确标注，不并入默认 GMV |

### `fact_payments`

| 字段 | 类型 | 含义 | 口径/限制 |
| --- | --- | --- | --- |
| `payment_type` | enum | `credit_card`、`boleto`、`voucher`、`debit_card`、`not_defined` | 允许作为维度 |
| `payment_installments` | integer | 分期数 | 非金额字段 |
| `payment_value` | decimal | 支付金额 | 与订单商品金额存在不同口径，不能替代 GMV |

### `fact_reviews`

| 字段 | 类型 | 含义 | 口径/限制 |
| --- | --- | --- | --- |
| `review_id` | string | 评价标识 | 当前快照存在重复值；评价指标按有效评价行而非该字段去重计算 |
| `order_id` | string | 订单标识 | 关联订单 |
| `review_score` | integer | 1–5 分评价 | 好评默认定义为 `review_score >= 4` |
| `review_creation_date` | timestamp | 评价创建时间 | 评价趋势的默认日期字段 |

### 维表和展示边界

`dim_customers`、`dim_sellers` 保留城市、省份和邮编前缀用于聚合；不在 UI 中展示客户级
明细，也不暴露完整订单/卖家 ID 列表。`dim_products` 的 `product_category_name`
通过 `dim_category_translation` 映射为英文展示名；原始品类名仍保留用于回溯。

## 数据质量规则（待加载时执行）

1. 订单、订单项和支付复合键保持不重复；评价记录的外键必须能关联到订单，但 `review_id` 重复需要显式保留并在评价指标口径中说明，不可静默去重。
2. `price`、`freight_value`、`payment_value` 非负；`review_score` 在 1–5。
3. 送达时间不早于下单时间；预计送达时间存在时才能参与准时率分母。
4. 订单状态、支付类型和品类映射不出现未记录的新枚举；新增枚举必须更新字典版本。
5. 缺失时间和缺失评价不填充为 0；指标需明确分母和排除原因。
