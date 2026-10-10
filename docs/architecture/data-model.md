# 分析数据模型与运行时持久化边界

> 本文描述当前 Olist 演示工作区的分析层及实际已落地的 `app` 元数据表。
> 数据指标、PostgreSQL Gold SQL 与后训练数据合同以 `docs/metric-contracts/`、`docs/post-training/data/` 为准；当前请求/执行链的已验证事实见 [project-v1-baseline-review.md](project-v1-baseline-review.md)。

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

### `app`（当前已落地的运行时元数据）

`app` 与 `analytics` 使用不同数据库角色：前者由应用 writer role 写入，后者由
analytics reader role 只读查询。当前实现已实际创建并使用以下表：

- `conversations`：用户所有的会话记录，含标题、workspace、数据/指标版本、状态、消息数、乐观并发 `state_version` 与受控的 `working_memory` JSONB；
- `messages`：会话按 `message_index` 追加的 user / assistant / tool message、受控 tool call metadata；
- `agent_runs`：一次用户请求的模型、预算、token、Catalog trace、结果合同、repair evidence 与终止状态；
- `query_audits`：每次 SQL 尝试的原始/最终 SQL、Policy 状态、行数、耗时、脱敏错误与 repair evidence。

这些不是业务事实表，不能通过 analytics reader role 查询。会话加载和删除均按 `user_id`
做所有权范围约束；前端历史详情会把 tool message 显示为“工具结果已记录”，不直接回传原始工具内容。

当前 `app.conversations` 保存 `workspace_id`、`dataset_version_id`、`metric_version`、`state_version`
与 `working_memory`。历史单工作区行由迁移回填为 `olist-demo`；旧数据不会猜测迁移到 TheLook。Store
在 get/list/delete/update 中按 workspace 过滤，并在 update 时要求客户端对象携带当前 `state_version`，
成功后递增版本；过期对象抛出 `ConversationStateConflict`，不会静默覆盖新状态。M5 已提供可见的「清空分析上下文」。隐形 TTL 仍未实现。

## 权限和展示边界

分析查询使用独立只读角色，应用元数据使用独立读写角色。分析层默认只提供聚合结果和
允许的维度；客户/订单/卖家 ID 只用于关联、审计和受控钻取，不作为默认展示字段。城市、
州和邮编前缀应先完成聚合后展示，暂不暴露精确地理坐标。

## 版本要求

- `dataset_version`：来源、许可证、下载日期、checksum 和转换版本。
- `schema_version`：DDL 与字段字典版本。
- `metric_version`：指标目录版本。
- `policy_version`：SQL 安全规则版本。

当前 Olist 演示已使用数据、指标与 Policy 版本写入会话和运行审计；不同历史报告中的版本数字只对各自
冻结资产负责，不能混为“当前默认版本”。
