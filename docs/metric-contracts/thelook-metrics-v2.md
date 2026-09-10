# TheLook 二十项业务指标合同 v2

**状态：** 已冻结为 `thelook-catalog-v2` / `metric_version=0.2-frozen`；仅用于受保护的 TheLook 跨 Schema 评测，不进入 Olist 的训练、验证、Prompt、few-shot 或 Adapter 选择。

**机器可读来源：** [`data/catalog/thelook_catalog_v2.yaml`](../../data/catalog/thelook_catalog_v2.yaml)。
**数据快照：** `thelook-kaggle-mirror-v1-20260908`，PostgreSQL `thelook_analytics` 的 `analytics` 脱敏视图。
**执行防线：** `SqlPolicy` → `daa_thelook_reader` → `ResultValidator`；本合同不是直接授予任意 SQL 或数据库权限。

## 1. 设计目的与统一边界

TheLook v2 是 Olist 领域 LoRA 的 **cross-schema holdout**。它把 v1 的订单指标扩展到订单商品、订单/履约、库存/配送中心、行为/会话和用户注册五类真实电商分析工作，目的是检验未见过的 Schema 与事实表组合，而不是通过复制日期范围凑评测数量。

所有金额均保留来源数据的数值，不声明币种；因此不能称为某个币种的收入、利润或 GMV。所有时间范围都是左闭右开：`[start, end_exclusive)`。订单相关指标始终按 `orders.created_at` 归属，不能拿 `order_items.created_at` 替代；此前画像已验证二者在 `181,748 / 181,759` 个商品行上不同。

下列能力不在 v2 范围内：

- 未冻结事件到订单归因桥接下的漏斗、转化率或渠道 ROI；
- 缺少期间平均库存分母的库存周转率；
- 缺少成本时点、币种和税费口径的毛利、利润及利润率；
- 把商品默认配送中心误称为订单实际发货中心的履约比较；
- 事件城市、用户 ID、订单 ID、会话 ID、SKU、精确坐标等敏感或高基数展示；
- 排名、Top-N、同比环比、因果解释与自由过滤。

## 2. 指标定义

| 事实域 | 指标 ID | 冻结公式与分母边界 | 时间归属 | 可安全分组 |
| --- | --- | --- | --- | --- |
| 订单商品 | `completed_sale_amount` | `Complete` 订单商品行的 `SUM(order_items.sale_price)`；不声明币种 | `orders.created_at` | 客户州/城市/注册流量来源；商品品类/品牌/部门 |
| 订单商品 | `completed_item_count` | `Complete` 订单的 `COUNT(order_items.id)`；是商品行数，不是 SKU 去重数或物理件数 | `orders.created_at` | 同上 |
| 订单 | `completed_order_count` | `COUNT(orders.order_id)`，仅 `status='Complete'` | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单 | `average_order_value` | 先在每个 Complete `order_id` 内 `SUM(sale_price)`，再 `AVG(order_value)`；不能直接对商品行价格平均 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单 | `average_items_per_completed_order` | 先在每个 Complete `order_id` 内 `COUNT(order_items.id)`，再平均；不能把所有商品行总数直接除以不匹配分母 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单 | `cancelled_order_count` | `COUNT(order_id)`，仅 `status='Cancelled'` | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单 | `returned_order_count` | `COUNT(order_id)`，仅 `status='Returned'` | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单/履约 | `average_fulfillment_days` | Complete 订单的 `AVG(delivered_at - created_at)`；两端非空且时间顺序合法 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单/履约 | `average_dispatch_days` | `AVG(shipped_at - created_at)`；只纳入有有效发货时间的订单，缺失不按 0 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单/履约 | `average_transit_days` | `AVG(delivered_at - shipped_at)`；只纳入有有效发货和送达时间的订单 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单/退货 | `average_post_delivery_return_days` | `AVG(returned_at - delivered_at)`；只纳入有有效送达和退货时间的订单 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单/退货 | `return_rate` | `Returned / (Complete + Returned)`；不把取消、处理中和已发货状态放入分母 | `orders.created_at` | 客户州/城市/注册流量来源 |
| 订单 | `completed_customer_count` | 至少有一笔 Complete 订单的 `COUNT(DISTINCT user_id)` | `orders.created_at` | 客户州/城市/注册流量来源 |
| 库存入库 | `received_inventory_unit_count` | 在观察期 `inventory_items.created_at` 创建的库存单元 `COUNT(id)` | `inventory_items.created_at` | 配送中心；商品品类/品牌/部门 |
| 库存售出 | `sold_inventory_unit_count` | 在观察期 `inventory_items.sold_at` 售出的库存单元 `COUNT(id)` | `inventory_items.sold_at` | 配送中心；商品品类/品牌/部门 |
| 库存快照 | `current_unsold_inventory_unit_count` | 当前快照 `sold_at IS NULL` 的库存单元 `COUNT(id)`；只能 all-time，禁止虚构期间趋势 | 无期间归属 | 配送中心；商品品类/品牌/部门 |
| 库存售出 | `average_days_to_sale` | 已售库存单元的 `AVG(sold_at - created_at)`；按售出时点观察，且售出不得早于创建 | `inventory_items.sold_at` | 配送中心；商品品类/品牌/部门 |
| 行为/会话 | `event_count` | `COUNT(events.id)`；保留匿名事件，不可作为订单归因分子或分母 | `events.created_at` | 事件类型、浏览器、事件流量来源 |
| 行为/会话 | `unique_session_count` | `COUNT(DISTINCT events.session_id)`；会话 ID 只供内部去重，不展示 | `events.created_at` | 事件类型、浏览器、事件流量来源 |
| 用户 | `registered_user_count` | 观察期内 `COUNT(users.id)`；不等同于活跃、下单或成交客户 | `users.created_at` | 用户国家、注册流量来源、注册州/地区 |

## 3. 组合、结果与安全规则

- 一个 `QuerySpec` 最多 4 个不同指标；多指标只能来自**同一事实域**。这避免把不同时间字段、粒度与事实表的数值在一条 SQL 中伪装成可直接比较的统一指标。
- `scalar` 返回指标列；`dimension_grouped` 的第一列必须是维度；`time_series` 的最后一列必须是 `time`。这些列顺序是 ResultContract，不由模型自行决定。
- `current_unsold_inventory_unit_count` 只允许 all-time 标量或分组，禁止绝对时间范围和时间序列。
- 金额、计数、时长不得为负；计数必须是整数；`return_rate` 必须在 `[0, 1]`。这是结果合理性检查，不足以独立证明任意模型 SQL 的业务语义。
- `average_order_value` 和 `average_items_per_completed_order` 虽然属于订单域，计算时必须内部关联 `order_items` 并先保留订单粒度；这一依赖是 renderer 的强制实现，不允许模型或问题措辞改写。

## 4. 数据快照画像证据

v2 合同建立前只做了只读聚合画像，未把原始用户、订单或事件行提交到仓库：

| 画像项 | 聚合事实 | 对合同的影响 |
| --- | ---: | --- |
| `orders.num_of_item` 与真实订单商品行数一致 | `125,226` 个订单全部一致 | 可以将“平均每完成订单商品行数”严格定义为订单粒度的商品行计数。 |
| Complete 平均发货/运输时长 | `1.501 / 2.516` 天 | 履约阶段字段存在且填充值未发现逆序；仍不将缺失阶段当 0。 |
| 库存总量 / 已售 / 未售 | `490,705 / 181,759 / 308,946` | 支持库存单元和 all-time 未售快照，不支持没有期间平均存量的周转率。 |
| 平均售出周期 / 时间逆序 | `29.934` 天 / `0` 行逆序 | 支持 `average_days_to_sale` 的非负结果合同。 |
| 行为事件 / 去重会话 / 匿名事件比例 | `2,431,963 / 681,759 / 约 46.3%` | 支持事件、会话指标，明确禁止直接推导订单转化。 |

正式评测集的逐条执行结果和 manifest 在仓库外受保护目录中冻结；不要将该目录的自然语言问题、Gold SQL 或结果行复制进 Olist 训练资产。
