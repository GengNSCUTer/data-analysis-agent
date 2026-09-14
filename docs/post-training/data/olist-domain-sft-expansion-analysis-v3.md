# Olist SQL-only 数据与训练集扩展说明 v3

## 1. 先回答最容易混淆的问题

### 1.1 十项指标是不是 Olist 原始数据自带的？

不是。Olist 原始数据只提供订单、商品行、支付、评价、客户、卖家和商品等字段；它没有
直接提供一列叫 `gmv`、`average_order_value` 或 `on_time_delivery_rate`。

这十项是本项目根据原始字段定义的**项目语义指标合同**。合同把四件事固定下来：

1. 指标的业务含义和中文/英文别名；
2. 事实粒度，例如订单、订单商品行或评价行；
3. 分子、分母、状态过滤和时间字段；
4. 哪些维度可以安全分组，哪些组合必须澄清或拒绝。

例如：

- `gmv` 不是数据库字段，而是有效订单商品行的 `SUM(fact_order_items.price)`，不含运费；
- `paid_order_count` 是排除 `canceled`/`unavailable` 后的 `COUNT(DISTINCT order_id)`；
- `average_order_value` 不是 `AVG(item.price)`，而是先按订单求商品金额，再对订单金额求平均；
- `positive_review_rate` 按评价行计算，不通过商品行 Join 复制评价；
- `on_time_delivery_rate` 只在已送达且购买、实际送达、预计送达时间齐全的订单中计算。

因此指标合同不是“数据集说明书里的现成答案”，而是我们为了让 Agent 不靠模型临时猜口径，
在项目中冻结的业务语义层。原始字段、指标合同、QuerySpec、Gold renderer 和运行时结果合同
必须保持版本一致。

权威文件：

- [`data/catalog/olist_catalog.yaml`](../../../data/catalog/olist_catalog.yaml)：机器可读 Catalog 和指标注册表；
- [`docs/metric-contracts/olist-metrics-v2.md`](../../../docs/metric-contracts/olist-metrics-v2.md)：十项指标的公式、粒度和边界；
- [`olist-sql-only-release-v2-audit-v1.md`](olist-sql-only-release-v2-audit-v1.md)：当前训练集覆盖审查。

## 2. Olist 原始数据包含什么

当前冻结的原始来源是 Kaggle Olist Brazilian E-Commerce Public Dataset，原始文件留在仓库外。
以下行数来自 `data/manifest/datasets.yaml` 的已核对快照：

| 原始文件 | 行数 | 主要列 | 业务含义 |
| --- | ---: | --- | --- |
| `olist_orders_dataset` | 99,441 | `order_id`, `customer_id`, `order_status`, `order_purchase_timestamp`, `order_approved_at`, `order_delivered_carrier_date`, `order_delivered_customer_date`, `order_estimated_delivery_date` | 一个订单及其状态、下单、确认、交承运商、实际送达和预计送达时间。 |
| `olist_order_items_dataset` | 112,650 | `order_id`, `order_item_id`, `product_id`, `seller_id`, `shipping_limit_date`, `price`, `freight_value` | 一个订单中的商品明细行；一单可以有多条商品行。 |
| `olist_order_payments_dataset` | 103,886 | `order_id`, `payment_sequential`, `payment_type`, `payment_installments`, `payment_value` | 一个订单的一次支付记录；一单可能对应多条支付记录。 |
| `olist_order_reviews_dataset` | 104,719 | `review_id`, `order_id`, `review_score`, `review_comment_title`, `review_comment_message`, `review_creation_date`, `review_answer_timestamp` | 订单评价、评分、评价文本和时间。当前分析层只暴露结构化评分字段，不暴露文本。 |
| `olist_customers_dataset` | 99,441 | `customer_id`, `customer_unique_id`, `customer_zip_code_prefix`, `customer_city`, `customer_state` | 订单对应的客户和客户地域；`customer_unique_id` 可用于去重客户。 |
| `olist_sellers_dataset` | 3,095 | `seller_id`, `seller_zip_code_prefix`, `seller_city`, `seller_state` | 卖家及其地域；卖家 ID 属于敏感关联标识。 |
| `olist_products_dataset` | 32,951 | `product_id`, `product_category_name`, `product_name_lenght`, `product_description_lenght`, `product_photos_qty`, `product_weight_g`, `product_length_cm`, `product_height_cm`, `product_width_cm` | 商品、品类和商品属性；原始列名 `lenght` 在分析层转换为 `length`。 |
| `product_category_name_translation` | 71 | `product_category_name`, `product_category_name_english` | 葡萄牙语品类名到英文展示名的映射。 |
| `olist_geolocation_dataset` | 1,000,163 | 邮编前缀、经纬度、城市、州 | 邮编级地理明细；当前不进入第一版 analyst schema，避免高基数和精确地理暴露。 |

原始文件是事实来源，但模型并不会直接看到这些 CSV。我们先把它们转换为有明确粒度的
`analytics` schema，再由 Catalog 决定哪些表和列可以出现在候选 SQL Prompt 中。

## 3. 当前分析层的表、列和 Join

### 3.1 事实表

| 分析层表 | 每行代表什么 | 暴露列 | 最重要的陷阱 |
| --- | --- | --- | --- |
| `fact_orders` | 一个订单 | `order_id`, `customer_id`, `order_status`, `order_purchase_timestamp`, `order_approved_at`, `order_delivered_carrier_date`, `order_delivered_customer_date`, `order_estimated_delivery_date` | 订单粒度；与商品行、支付、评价都是一对多，不能直接裸 Join 后重复计算订单指标。 |
| `fact_order_items` | 一个订单商品行 | `order_id`, `order_item_id`, `product_id`, `seller_id`, `shipping_limit_date`, `price`, `freight_value` | 商品行粒度；`COUNT(order_id)` 不是订单数，商品行金额与订单指标不能混用。 |
| `fact_payments` | 一次支付序列 | `order_id`, `payment_sequential`, `payment_type`, `payment_installments`, `payment_value` | 支付一对多；支付金额不能直接替代 GMV，支付方式也不能未经规则分摊到商品行。 |
| `fact_reviews` | 一条评价记录 | `review_id`, `order_id`, `review_score`, `review_creation_date` | 评价粒度；评价与订单是一对多，不能通过商品行 Join 计算品类好评率。 |

### 3.2 维度表和治理表

| 分析层表 | 暴露列 | 用途与限制 |
| --- | --- | --- |
| `dim_customers` | `customer_id`, `customer_unique_id`, `customer_zip_code_prefix`, `customer_city`, `customer_state` | 客户地域分组和客户去重。标识列仅用于关联/聚合，不作为默认输出列；城市目前因高基数输出合同尚未物化。 |
| `dim_sellers` | `seller_id`, `seller_zip_code_prefix`, `seller_city`, `seller_state` | 卖家关联维度。当前十项指标没有冻结 seller 分组合同，不能因为 SQL 写得出来就进入训练。 |
| `dim_products` | `product_id`, `product_category_name`, `product_name_length`, `product_description_length`, `product_photos_qty`, `product_weight_g`, `product_length_cm`, `product_height_cm`, `product_width_cm` | 商品品类和属性。商品品类天然属于商品行，当前只允许商品行指标按品类分组。 |
| `dim_category_translation` | `product_category_name`, `product_category_name_english` | 仅用于品类展示名映射，不改变商品行聚合粒度。 |
| `dataset_versions` | `dataset_version_id`, `dataset_id`, `source_url`, `source_license`, `source_version`, `archive_sha256`, `transform_version`, `loaded_at` | 仅管理员治理使用，不进入普通 analyst Prompt。 |

当前稳定 Join 路径是：

```text
fact_orders -> dim_customers       多对一，得到客户州/城市
fact_orders -> fact_order_items    一对多，订单到商品行
fact_orders -> fact_payments       一对多，订单到支付序列
fact_orders -> fact_reviews        一对多，订单到评价
fact_order_items -> dim_products   多对一，得到商品品类
fact_order_items -> dim_sellers    多对一，得到卖家
dim_products -> dim_category_translation 多对一，得到英文品类名
```

关键原则是：跨事实表查询时，先在各自事实粒度聚合，再在顶层合并。不能把订单、商品行、
支付和评价四张事实表直接连接后再 `SUM` 或 `COUNT`。

## 4. 当前十项指标合同

| 指标 | 事实粒度 | 默认时间字段 | 业务口径 |
| --- | --- | --- | --- |
| `gmv` | 商品行 | 订单购买时间 | 有效订单商品价之和，不含运费。 |
| `paid_order_count` | 订单 | 订单购买时间 | 有效订单去重计数。 |
| `average_delivery_days` | 订单 | 订单购买时间 | 实际送达减购买时间的平均值，两端非空。 |
| `positive_review_rate` | 评价行 | 评价创建时间 | 评分 >= 4 的评价行 / 有效评分评价行。 |
| `item_count` | 商品行 | 订单购买时间 | 有效订单商品行数，不按订单去重。 |
| `average_order_value` | 订单 | 订单购买时间 | 先按订单求商品价格总和，再对订单金额求平均；不含运费。 |
| `average_review_score` | 评价行 | 评价创建时间 | 有效评分评价行的平均评分，范围 1--5。 |
| `on_time_delivery_rate` | 订单 | 订单购买时间 | 已送达且时间齐全的订单中，实际送达不晚于预计送达的比例。 |
| `cancellation_rate` | 订单 | 订单购买时间 | `canceled` 订单 / 购买时间存在的全部订单；`unavailable` 不计入分子。 |
| `freight_amount` | 商品行 | 订单购买时间 | 有效订单运费之和，不是 GMV，也不是支付金额。 |

十项指标覆盖了用户最常见的四类问题：

1. 销售规模：GMV、订单数、商品件数、客单价、运费；
2. 履约质量：平均履约天数、准时送达率、取消率；
3. 用户口碑：好评率、平均评价分；
4. 分析维度：时间、客户州、商品品类，以及受限的客户城市、支付方式和卖家关联。

## 5. 电商用户通常关心什么，Olist 能不能回答

| 用户问题方向 | Olist 是否有数据 | 当前状态 |
| --- | --- | --- |
| 销售额、订单量、销量、客单价 | 有商品价、订单和商品行 | 十项指标已覆盖，但训练结构偏向多指标。 |
| 运费和履约速度 | 有运费、交承运商、实际/预计送达时间 | 已有运费、平均履约天数、准时率；需要补更多单指标和时间序列。 |
| 取消和订单状态 | 有 `order_status` | 已有取消率，但订单状态分布、取消订单数等可继续补。 |
| 品类表现 | 有商品品类和翻译表 | 当前只安全支持商品行指标按品类，订单/评价按品类需要归属规则；test 目前没有 category family。 |
| 客户地域 | 有客户州、城市 | 州已覆盖；城市需要高基数、排序、Top-N 和行数合同后再开放。 |
| 评价口碑 | 有评分和评价时间 | 两项评价指标已定义，但评价专属程序稀疏；需要补评价时间序列和评价标量。 |
| 支付方式、分期、支付金额 | 有支付事实表 | 可回答，但一单多支付记录带来归属问题；当前不作为普通 SQL SFT。 |
| 卖家经营表现 | 有卖家和卖家地域 | 有原始数据，但 seller 分组涉及权限和归属/敏感输出合同，当前不开放。 |
| 复购、客户留存 | 有 `customer_unique_id` 和订单时间 | 可以定义，但还没有冻结客户行为指标合同。 |
| 退款、利润、毛利 | 没有成本和退款明细 | 不能从 Olist 现有字段推断，不能为了训练集凑能力。 |

## 6. 当前训练集是怎样构建出来的

实际流水线不是“让 LLM 写 3,600 条问题和 SQL”，而是：

```text
覆盖 seed
  -> QuerySpec validator
  -> deterministic PostgreSQL Gold renderer
  -> SqlPolicy
  -> daa_analytics_reader 只读执行
  -> ResultContract / ResultValidator
  -> 生成 5 种受控中文 surface form
  -> Router/Catalog/QueryPlan 重建检查
  -> Prompt/SQL 长度审计
  -> family / QuerySpec / holdout 切分
  -> 选择一条主问法物化为 SFT 行
```

`QuerySpec` 是离线结构化施工图，主要包含：

- `metric_ids`：要查哪些指标；
- `result_shape`：标量、按州、按品类或时间序列；
- `dimension`：是否按客户州或商品品类分组；
- `time`：全量、绝对时间范围或时间粒度；
- `join_program_id`：允许的最小 Join 路径；
- `workspace`：Catalog、指标、数据集和策略版本快照；
- `required_result_columns`：结果合同必须返回哪些列。

当前 Olist v2 的主要 family/program 是：

| 程序族 | 语义 |
| --- | --- |
| `JP01_item_scalar` | GMV、商品件数、运费等商品行指标的标量/时间查询。 |
| `JP02_order_scalar` | 订单指标和 AOV 的订单级查询；AOV 使用二层聚合。 |
| `JP03_review_scalar` | 评价指标按评价行统计。 |
| `JP04_customer_geo_item` | 商品行指标按客户州。 |
| `JP05_customer_geo_order` | 订单指标按客户州。 |
| `JP06_customer_geo_review` | 评价指标按客户州，不连接商品行。 |
| `JP07_category_item` | GMV、商品件数、运费按商品品类。 |
| `JP09_scalar_multi_metric` | 多指标标量，各指标独立聚合后合并。 |
| `JP10_state_multi_metric` | 多指标按客户州聚合后合并。 |
| `JP11_purchase_time_multi_metric` | 使用订单购买时间的多指标时间序列。 |
| `JP12_review_time_multi_metric` | 使用评价创建时间的评价指标时间序列。 |

当前 release 的 2,400/600/600 行主要集中在 `JP09`、`JP10`、`JP11`，合计超过 97%。
这就是“行数不少，但结构单一”的根本原因。

## 7. 这次 Olist 扩展应该怎么做

### 7.1 第一优先级：补程序结构，而不是先补数量

先构造一个 coverage repair pilot，不直接生成几千条训练行。优先补：

1. `category_grouped` 的独立 validation/test family；
2. `JP12_review_time_multi_metric` 的评价时间序列；
3. 每项指标的单指标 scalar、单指标 time series；
4. 2 指标组合，减少对 3--4 指标固定模板的依赖；
5. AOV 二层聚合、订单 `COUNT(DISTINCT)`、准时率 eligible 分母和取消率分母难例；
6. 订单/商品行/评价三种事实粒度之间的最小 Join 对照；
7. 受控拒答或澄清样本单独存放，不把错误 SQL 当成正向标签。

建议第一批只做约 60--100 个新 family，每个 family 生成 3--5 条中文问法，先完成 Gold 准入和
人工抽检，再决定是否扩大到正式 release。新 family 必须在 test 中留下代表，不能只把稀缺结构
塞进 train。

### 7.2 第二优先级：补项目指标，但先冻结合同

基于当前 Olist 字段，以下是值得评审的新指标候选，不代表已经批准进入 Catalog：

当前 `olist-metrics-v2.md` 是已用于 Release v2 的冻结快照，不应直接改写。新增指标应先进入
`olist-metrics-v3-proposal`，完成公式、粒度、空值、时间字段、维度和结果约束审查后，再作为
新的指标版本物化训练数据；否则旧实验无法复现。

**可优先评审的低歧义指标**

- `unique_customer_count`：按 `customer_unique_id` 去重的客户数；
- `review_count`：有效评分评价行数；
- `canceled_order_count`：取消订单数；
- `unavailable_order_count`：不可用订单数；
- `average_items_per_order`：先按订单统计商品行数，再取平均；
- `average_item_price`：有效商品行的平均商品价；
- `approval_latency_days`：订单确认时间减购买时间；
- `carrier_handoff_days`：交承运商时间减购买时间。

这些指标仍需分别冻结：事实粒度、空值规则、状态过滤、时间字段、允许维度和结果约束。

**需要先解决业务归属的指标**

- 按支付方式统计 GMV、订单数或运费；
- 按品类统计订单履约、AOV、评价指标；
- 按卖家统计订单级指标；
- 支付金额占比、分期分析；
- 客户复购率、留存率、首购/复购周期。

这些不是 SQL 写不出来，而是存在一单多支付、一单多品类、一条评价关联多个商品等口径问题。
必须先定义归属/分摊规则，再进入 Catalog、renderer 和训练数据；不能由模型自行猜测。

**当前不应构造的指标**

- 利润、毛利、退款金额：数据中没有成本和退款事实；
- 真实转化率：没有完整曝光/访问分母；
- 因果型促销效果：没有实验设计或对照合同。

### 7.3 第三优先级：增加中文表达多样性

每个通过结构准入的 family 保留 3--5 种受控表达：

1. 正式统计：“请统计 2017 年各州的有效订单数。”
2. 业务口语：“看一下 2017 年各州完成了多少单。”
3. 管理者表达：“我想比较各州的成交订单规模。”
4. 省略表达：“2017 各州订单量。”
5. 结果导向：“按州列出订单数，方便做表格比较。”

改写只能改变语言形式，不能偷偷加入同比、Top-N、因果、自由筛选或新的归属规则。每条变体
都必须重新经过 Router、QueryPlan、ResultContract 身份一致性检查。

## 8. 推荐的下一步

当前只推进一个小任务：冻结 **Olist Coverage Repair Pilot v1** 的 family 配额和新指标候选，
然后先生成 60--100 个结构 family 的 seed 清单；不立即生成大规模中文问题、不启动训练。

验收标准是：每个新增 family 都能解释指标、事实粒度、Join、时间字段、结果列和失败边界；
`category_grouped`、评价时间、单指标、AOV/去重难例在 validation/test 中都有代表；全部 Gold
通过现有 deterministic 准入链路后，才进入下一轮训练集物化。
