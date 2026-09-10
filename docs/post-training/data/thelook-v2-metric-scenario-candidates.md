# TheLook v2 指标与业务场景候选矩阵

**状态：** 画像完成、候选待冻结；不是 Catalog、QuerySpec、Gold SQL 或评测集合同。  
**画像日期：** 2026-09-10  
**数据快照：** `thelook-kaggle-mirror-v1-20260908`  
**读取边界：** `thelook_analytics.analytics` 的七个脱敏视图，事务内 `SET LOCAL ROLE daa_thelook_reader`；仅执行聚合，不导出原始行。  
**关联版本：** 已冻结的 v1 资产见 [TheLook workspace v1](thelook-cross-schema-workspace-v1.md)、[首批指标合同](../../metric-contracts/thelook-metrics-v1.md) 与 [206 条 final test v1](thelook-cross-schema-final-test-v1.md)。

## 任务卡

| 项目 | 本轮决定 |
| --- | --- |
| 目标 | 让下一版 TheLook 零样本评测增加真实电商**事实域、指标、维度和 SQL 程序形态**，而不是复制 v1 的日期或改写同一句问题来凑条数。 |
| 非目标 | 本轮不改 `thelook-catalog-v1`、v1 QuerySpec/renderer、206 条历史评测；不构造自然语言问题、Gold SQL、模型候选或训练数据。 |
| 训练隔离 | TheLook 仍是 Olist LoRA 的 protected cross-schema holdout。任何 v2 SQL、问题、QuerySpec、数据画像结论都不得用于 Olist train/validation、few-shot、Prompt 调优或 Adapter 选择。 |
| 通过标准 | 每项候选都明确：事实粒度、分子/分母或时间差、统计时间字段、允许维度、可组合范围、行数预算和已知不确定性。不能定义清楚的常见指标明确排除。 |
| 下一步 | 用户审阅候选范围后，独立冻结 `thelook-catalog-v2` / QuerySpec v2 / renderer v2；先做小批准入，再构造 final evaluation-only v2。 |

## 为什么 v1 不够

v1 的 206 条评测已经是有效的第一道跨 schema 门，但它只覆盖六项以订单为中心的指标：完成成交额、完成订单数、客单价、平均履约天数、退货率、完成客户数。它主要使用订单、订单商品行及用户/商品维度，且维度分组以 `state`、`traffic_source`、`category`、`department` 为主。

TheLook 还提供库存、配送中心、匿名/已知用户行为事件、用户注册等独立事实。若只把 v1 的时间范围复制到 600 条，模型仍然只面对相同的表组合、时间字段和聚合套路，不能充分说明跨 schema 的泛化边界。因此 v2 的最小目标是覆盖五类业务分析工作：

1. 交易与购物篮结构；
2. 订单状态与履约阶段；
3. 库存生命周期与仓网分布；
4. 站内行为与会话；
5. 用户注册与获客结构。

## 只读数据画像：哪些事实可安全建模

### 订单、订单商品行与履约阶段

| 检查 | 聚合证据 | 对 v2 的含义 |
| --- | ---: | --- |
| 订单状态 | Cancelled `18,609`；Complete `31,354`；Processing `25,156`；Returned `12,530`；Shipped `37,577` | 状态是来源的精确英文值；取消、发货、完成、退货是可区分的业务阶段，不能映射为 Olist 状态。 |
| 订单创建时间 | `2019-01-06` 至 `2024-01-18`，缺失 `0` | 订单类时间序列仍以 `orders.created_at` 为唯一统计时间。 |
| 订单商品行数 | `125,226` 个订单均与 `orders.num_of_item` 一致；均值 `1.451`，最大 `4` | 可以安全定义完成商品行数、平均每完成订单商品行数；“商品行”不能偷换为 SKU 去重数或实际件数。 |
| 状态/阶段字段一致性 | `order_items.status`、`shipped_at`、`delivered_at`、`returned_at` 与所属订单零错配 | 履约阶段应统一从订单粒度计算，避免商品行重复放大订单分母。 |
| 商品行创建时间 | `181,748 / 181,759` 行与 `orders.created_at` 不同 | **禁止**以 `order_items.created_at` 代替订单/销售统计时间；金额和商品行仍以 `orders.created_at` 过滤。 |
| 时序有效性 | 所有已填充的 `shipped_at >= created_at`、`delivered_at >= shipped_at`、`returned_at >= delivered_at` | 可定义时间差指标，仍须按每项的非空字段资格计算分母。 |
| 阶段均值 | Complete：发货 `1.501` 天、运输 `2.516` 天；Returned：发货 `1.496` 天、运输 `2.495` 天、送达后退货 `1.503` 天 | 发货、运输、退货等待三段是可解释的不同指标，不能继续仅用总履约时长代替。 |

### 库存与配送中心

| 检查 | 聚合证据 | 对 v2 的含义 |
| --- | ---: | --- |
| 库存事实 | `490,705` 个库存单元；已售 `181,759`、未售 `308,946` | `inventory_items` 是独立的库存单元粒度，可覆盖入库、售出和当前未售快照。 |
| 库存时间 | `created_at` 缺失 `0`；`sold_at < created_at` 为 `0`；已售单元平均售出周期 `29.934` 天 | 可定义“入库库存单元数”“按售出时间统计的已售库存单元数”“平均售出周期”。当前未售库存只能作为快照指标，不能伪装成期间周转率。 |
| 仓网关联 | 10 个配送中心，中心名称无缺失；`490,705` 条库存记录与商品默认配送中心零错配 | 可按配送中心、品类、部门分析库存单元和售出周期；这是新的 `inventory_items -> distribution_centers` join 程序。 |
| 商品维度基数 | 品类 `26`、部门 `2`、品牌 `2,756` | 品类/部门适合全时段展示；品牌只能通过经运行时验证的短窗口进入，不能默认全量分组。 |

### 行为事件与会话

| 检查 | 聚合证据 | 对 v2 的含义 |
| --- | ---: | --- |
| 行为规模 | `2,431,963` 事件、`681,759` 去重会话、`80,044` 已知用户 | `events` 可提供独立的事件量和会话量指标，检验不同于订单表的 `COUNT(DISTINCT)` 程序。 |
| 事件时间 | `2019-01-02` 至 `2024-01-22` | 行为指标必须按 `events.created_at` 过滤/分桶，不能复用订单创建时间。 |
| 事件类型 | product `845,607`、cart `595,994`、department `595,323`、purchase `181,759`、cancel `125,568`、home `87,712` | `event_type` 是低基数、可展示维度，适合覆盖事件分组与事件类型差异。 |
| 匿名边界 | 匿名事件 `1,125,671`（约 `46.3%`）；已知 user ID 事件零孤儿 | 事件量/会话量可按事件自己的流量来源、浏览器、类型和州分析；不能把 `events` 流量来源混作订单或用户获客归因，也不能要求事件必须关联用户。 |
| 低基数维度 | 流量来源 `5`、浏览器 `5`、事件类型 `6` | 这些维度适合全时间范围的分组与多指标组合。 |

### 用户注册与行数预算

| 检查 | 聚合证据 | 对 v2 的含义 |
| --- | ---: | --- |
| 用户事实 | `100,000` 用户；国家 `16`、流量来源 `5`、州 `229`、城市 `7,884`；注册时间 `2019-01-02` 至 `2024-01-17` | 可定义注册用户数并按国家/注册流量来源做用户获得结构；城市不是默认展示维度。 |
| 订单结果维度 | Complete 订单对应州 `221`、城市 `5,465`、流量来源 `5`；完成商品对应品类 `26`、品牌 `2,512`、部门 `2` | `state`、`city`、`brand` 的完整范围可能超过图表 `200` 行预算，不能默认全时间范围分组。 |
| 每日高基数上界 | Complete 订单单日：城市最多 `263`、品牌最多 `319`，但分别有 `1,726/1,727` 和 `1,724/1,727` 个活跃日不超过 200 行 | v2 可使用**物化前验证通过的单日安全窗口**覆盖 city/brand，而不是永久排除；仍不得把“单日”当作静态安全保证。 |
| 事件维度上界 | 单日州最多 `177`、城市最多 `998`；仅 `21/1,847` 天的事件城市不超过 200 行 | 行为 `state` 可纳入；事件城市仍排除，避免脆弱的少数日期特例。 |

这里的 200 行是当前图表合同的上限；SQL runner/ResultValidator 的一般返回上限更高，但 v2 要保证结果既可验证又可展示，故以后者作为更严格门。

## v2 候选指标：按事实域扩展，而非仅扩行

下表中的“候选”尚未写入 Catalog。其 SQL、别名、版本、允许组合和 Gold 公式要等 v2 合同冻结后才成立。

| 事实域 | 保留/新增候选指标 | 精确定义草案 | 统计时间 | 推荐展示维度 |
| --- | --- | --- | --- | --- |
| 交易与购物篮 | 保留 `completed_sale_amount`、`completed_order_count`、`average_order_value`、`completed_customer_count` | 沿用 v1：Complete 订单金额/订单/订单级 AOV/去重成交客户 | `orders.created_at` | state、traffic_source；金额和商品行另可按 category/department，品牌仅安全日窗口 |
| 交易与购物篮 | 新增 `completed_item_count` | Complete 订单的 `COUNT(order_items.id)`；是商品行数，不是 SKU 去重数 | `orders.created_at` | category、department、品牌安全日窗口 |
| 交易与购物篮 | 新增 `average_items_per_completed_order` | 先按 Complete `order_id` 统计商品行数，再 `AVG`；必须使用两层 order-grain CTE | `orders.created_at` | state、traffic_source；city 仅安全日窗口 |
| 订单结果 | 新增 `cancelled_order_count` | `COUNT(orders.order_id)`，`status = 'Cancelled'` | `orders.created_at` | state、traffic_source；city 仅安全日窗口 |
| 订单结果 | 新增 `returned_order_count` | `COUNT(orders.order_id)`，`status = 'Returned'` | `orders.created_at` | state、traffic_source；city 仅安全日窗口 |
| 履约阶段 | 保留 `average_fulfillment_days`；新增 `average_dispatch_days` | 前者为 delivered - created；后者为 shipped - created。各自只纳入起止时间非空且顺序合法的订单 | `orders.created_at` | state、traffic_source；city 仅安全日窗口 |
| 履约阶段 | 新增 `average_transit_days` | `AVG(delivered_at - shipped_at)`；只纳入两字段非空且顺序合法的订单 | `orders.created_at` | state、traffic_source；city 仅安全日窗口 |
| 退货阶段 | 保留 `return_rate`；新增 `average_post_delivery_return_days` | 退货率仍为 Returned / (Complete + Returned)；新指标为 Returned 的 `AVG(returned_at - delivered_at)` | `orders.created_at` | state、traffic_source；city 仅安全日窗口 |
| 库存/仓网 | 新增 `received_inventory_unit_count` | `COUNT(inventory_items.id)`；表示期间入库/创建的库存单元，不称“库存周转” | `inventory_items.created_at` | distribution_center、category、department；品牌仅安全日窗口 |
| 库存/仓网 | 新增 `sold_inventory_unit_count` | `COUNT(inventory_items.id)`，`sold_at IS NOT NULL`，时间按 `sold_at` | `inventory_items.sold_at` | distribution_center、category、department；品牌仅安全日窗口 |
| 库存/仓网 | 新增 `current_unsold_inventory_unit_count` | 快照中 `sold_at IS NULL` 的库存单元数；仅允许 all-time / 无时间序列 | 无期间字段（快照） | distribution_center、category、department |
| 库存/仓网 | 新增 `average_days_to_sale` | 已售库存单元的 `AVG(sold_at - created_at)`；按 `sold_at` 归属观察期 | `inventory_items.sold_at` | distribution_center、category、department |
| 站内行为 | 新增 `event_count` | `COUNT(events.id)`；事件源不强制关联用户 | `events.created_at` | event_type、browser、events.traffic_source、state |
| 站内行为 | 新增 `unique_session_count` | `COUNT(DISTINCT events.session_id)`；会话 ID 只用于去重 | `events.created_at` | event_type、browser、events.traffic_source、state |
| 用户获得 | 新增 `registered_user_count` | `COUNT(users.id)`；表示期间新注册用户，不等同于活跃或成交用户 | `users.created_at` | country、users.traffic_source；state 仅安全日窗口 |

该候选集会使可评测指标从 v1 的 **6 项增加到 20 项**，但质量门不是“20 个名字都要出现”。每一项都必须先由 v2 renderer 通过 Policy、只读角色、ResultContract、行数和人工口径审核，才会进入 final test。

## 场景、SQL 程序形态与组合规则

新增指标必须带来实际不同的编译任务，而不只是名称不同。

| 场景簇 | 必须覆盖的程序形态 | 允许的同簇组合 | 明确禁止 |
| --- | --- | --- | --- |
| 销售与购物篮 | 订单直聚合；订单商品行聚合；`order_id` 内层聚合再外层 `AVG`；`COUNT(DISTINCT user_id)` | 同一订单时间、同一维度、粒度已明确的 2--4 指标；如完成成交额 + 完成商品行数、完成订单数 + 完成客户数 | 将订单数按商品 category 直接 join 后重复计数；把商品行时间当订单时间 |
| 订单状态与履约 | 订单状态过滤；时间差资格过滤；多指标 CTE 以公共维度/时间键合并 | 退货数 + 退货率；履约、发货、运输时长的同维度对照 | 把不同阶段缺失值当 0；用 `delivered_at` 替代订单创建时间做趋势分桶 |
| 库存与仓网 | `inventory_items` 单元聚合；库存到商品/配送中心的 many-to-one join；售出周期时间差 | 同一库存时间归属/同一维度的入库或售出相关指标 | 将当前未售快照与期间指标混成时间序列；将配送中心说成订单实际履约中心 |
| 行为与会话 | `COUNT(*)`、`COUNT(DISTINCT session_id)`；事件类型/浏览器/事件流量来源分组 | event_count + unique_session_count | 自行定义访问到成交的 conversion rate；用事件流量来源解释订单/用户来源 |
| 用户获得 | `users` 单表计数、注册时间序列、国家/注册流量来源分组 | 仅同一用户注册事实内组合 | 将注册用户与订单客户/事件用户直接相除或拼成“转化率” |

时间序列覆盖也要分层：日序列使用经长度和行数审核的短范围；周序列不超过约 180 个桶；月/季度/年序列可以用更长范围。对每条 materialized case，renderer 后的真实结果行数仍是最终准入依据，而不是靠模板推测。

## 行数与安全边界

1. 所有 v2 查询继续只访问 `analytics` 脱敏视图，并使用 `daa_thelook_reader`；不得读取 `thelook_raw`。
2. 城市、品牌、部分州分组需要由候选构造器维护安全窗口，并在 `SqlPolicy -> reader role -> ResultValidator/ChartContract` 中逐条验证；超过 200 行就排除或缩小窗口，不以 `LIMIT` 截断后冒充完整分析。
3. 多指标的 `time_series` 只能合并**相同事实域、相同统计时间字段、相同时间归属**的指标。订单、库存售出、行为和注册绝不跨域拼接。
4. 本轮不开放自由过滤、排名、Top-N、用户/订单/会话标识展示、事件城市，也不放宽 SQL AST Policy。
5. 每个 v2 case 仍保留五种受控中文表达，但稳定选择一种用于一次 Base/Adapter 评测；五种问法是表面鲁棒性覆盖，不是五条独立语义样本。

## 有意不纳入的“常见指标”

| 未纳入项 | 原因 |
| --- | --- |
| 漏斗转化率、购买转化率 | 事件存在大量匿名流量；事件与订单没有经过冻结归因规则的一对一业务桥接。即使 `purchase` 事件数量与订单商品行数量恰好相同，也不能据此宣称可计算转化。 |
| 库存周转率 | 当前数据没有可冻结的期间平均库存分母；未售库存是快照，不能替代周转率分母。 |
| 毛利、利润率 | 虽存在成本列，但需要进一步冻结成本时点、币种/税费和订单商品行到库存成本的经营口径；不为扩大题目数而提前引入。 |
| 配送中心订单履约表现 | 目前配送中心是商品/库存的默认关联，不能证明订单实际从该中心发货。v2 仅用于库存/仓网场景。 |
| 事件城市全量分析 | 数据基数高，绝大多数日期也会超出可展示行数预算；不采用脆弱的小样本日期特例。 |

## 后续 v2 的规模原则（尚未构造样本）

后续可把 final evaluation-only v2 的目标设在约 600 个**唯一 QuerySpec / Gold SQL 语义实例**，但数量不是验收条件。建议的上限分配是：交易/购物篮约 140、订单结果与履约约 130、库存与仓网约 125、事件与会话约 115、用户获得约 60、同事实域多指标或边界形态约 30。若某簇不能通过口径、Policy、reader role、行数或 ResultContract 门，应减少总量而不是用日期/同义改写补足。

实现顺序保持最小化：先冻结 v2 Catalog 与指标合同，再设计独立 v2 QuerySpec / deterministic renderer，做 20--40 条跨事实小批准入，最后才物化新的 holdout 评测集和 matching Base/Adapter 生成。v1 的 Catalog、206 cases、历史输出和结果始终保留不变。
