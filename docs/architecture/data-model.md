# 分析数据模型（Phase 2 草案）

## 目标

将 Olist 原始 CSV 组织成可解释的分析层。第一版只支持一个数据集和一套 SQL 方言，
先保证事实表粒度、指标口径和关联路径清楚，再考虑多数据集和多租户。

## 关系和粒度

```text
dim_customers 1 ─── * fact_orders 1 ─── * fact_order_items * ─── 1 dim_products
                                  │                         └── * ─── 1 dim_sellers
                                  ├── * fact_payments
                                  └── * fact_reviews

dim_products * ─── 1 dim_category_translation
```

`fact_orders` 是订单级事实，`fact_order_items` 是订单商品行事实，`fact_payments` 是
支付序列事实，`fact_reviews` 是评价事实。任何跨事实表指标必须先在各自粒度聚合，再
通过 `order_id` 合并，禁止直接把支付和商品行裸连接后求和。

## 初版 schema

### `analytics`

- `dim_customers(customer_id, customer_unique_id, customer_zip_code_prefix, customer_city, customer_state)`
- `dim_sellers(seller_id, seller_zip_code_prefix, seller_city, seller_state)`
- `dim_products(product_id, product_category_name, product_name_length, product_description_length, product_photos_qty, product_weight_g, product_length_cm, product_height_cm, product_width_cm)`
- `dim_category_translation(product_category_name, product_category_name_english)`
- `fact_orders(order_id, customer_id, order_status, order_purchase_timestamp, order_approved_at, order_delivered_customer_date, order_estimated_delivery_date)`
- `fact_order_items(order_id, order_item_id, product_id, seller_id, shipping_limit_date, price, freight_value)`
- `fact_payments(order_id, payment_sequential, payment_type, payment_installments, payment_value)`
- `fact_reviews(review_id, order_id, review_score, review_creation_date)`，复合主键为 `(review_id, order_id)`

### `app`（后续 Phase 3）

`users`、`conversations`、`messages`、`metric_definitions`、`dataset_versions`、
`query_audits`、`policy_decisions`、`evaluation_cases` 和 `evaluation_runs` 暂不创建，
这里只预留职责边界。

## 权限和展示边界

分析查询使用独立只读角色，应用元数据使用独立读写角色。分析层默认只提供聚合结果和
允许的维度；客户/订单/卖家 ID 只用于关联、审计和受控钻取，不作为默认展示字段。城市、
州和邮编前缀应先完成聚合后展示，暂不暴露精确地理坐标。

## 版本要求

- `dataset_version`：来源、许可证、下载日期、checksum 和转换版本。
- `schema_version`：DDL 与字段字典版本。
- `metric_version`：指标目录版本。
- `policy_version`：SQL 安全规则版本。

数据真正加载前，以上版本只记录设计草案，不能在项目介绍中描述为已完成能力。
