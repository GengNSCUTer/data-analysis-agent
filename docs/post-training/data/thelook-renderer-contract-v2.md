# TheLook v2 deterministic PostgreSQL Gold renderer 合同

**状态：** 已冻结；仅将已经通过 `TheLookV2QuerySpec` 验证的施工图编译为 canonical PostgreSQL Gold SQL。
**实现：** [`src/data_analysis_agent/thelook_v2_renderer.py`](../../../src/data_analysis_agent/thelook_v2_renderer.py)。
**版本：** `thelook-postgres-gold-renderer-v2`。

## 1. 职责与非职责

renderer 的输入只有已验证的 `TheLookV2QuerySpec` 和版本匹配的 Catalog。它：

- 不解析中文问题；
- 不调用 Base、Adapter 或外部 LLM；
- 不连接 PostgreSQL；
- 不访问 Olist 的训练/验证资产；
- 不用 SQL 字符串猜测业务含义；
- 不绕过后续 `SqlPolicy`、只读角色或 `ResultValidator`。

输出 `TheLookV2GoldSqlArtifact`，包含确定性 SQL、SQL SHA-256、版本、指标、结果列合同与不含问题/结果行的证据对象。相同 QuerySpec 必须得到字节一致 SQL 与 SHA-256。

## 2. 编译规则

1. 每个业务指标从只读 `_METRICS` 注册表取得固定公式、过滤、事实域和时间表达式；没有注册表定义或事实域不一致即 fail-closed。
2. 每个指标产生一个顶层 CTE。标量多指标用 `CROSS JOIN` 合并；分组和时间序列多指标按唯一维度键或 `time` 使用 `FULL OUTER JOIN` 合并，结果列顺序始终服从 QuerySpec。
3. 订单/销售使用 `orders.created_at`；库存入库使用 `inventory_items.created_at`；库存售出与售出周期使用 `sold_at`；行为/会话使用 `events.created_at`；用户注册使用 `users.created_at`。
4. 订单级平均值不允许在商品行粒度直接平均。`average_order_value` 和 `average_items_per_completed_order` 先产生按 `order_id` 的顶层 inner CTE，再对订单值/商品行数做 outer `AVG`。inner CTE 显式关联 `analytics.order_items AS oi`，即使这两个指标的公开事实域、时间和维度仍是订单域。
5. 库存按配送中心时使用 `inventory_items.product_distribution_center_id`；不把 `products.distribution_center_id` 的默认归属说成某订单的实际发货中心。

## 3. 为什么订单均值是两层顶层 CTE

一层 `AVG(oi.sale_price)` 会把商品行数更多的订单赋予更高权重，计算的不是平均订单金额；同理，直接计算商品行可能失去“每订单”的明确粒度。v2 使用：

```sql
WITH tlv2_order_value_01 AS (
  SELECT o.order_id, SUM(oi.sale_price) AS order_value
  FROM analytics.orders AS o
  JOIN analytics.order_items AS oi ON o.order_id = oi.order_id
  WHERE o.status = 'Complete'
  GROUP BY o.order_id
),
m01_average_order_value AS (
  SELECT AVG(order_values.order_value) AS average_order_value
  FROM tlv2_order_value_01 AS order_values
)
SELECT m01_average_order_value.average_order_value AS average_order_value
FROM m01_average_order_value;
```

内层 CTE 保持顶层而不嵌套在 `m01` 内，是因为 `SqlPolicy` 可以证明顶层 CTE 导出的别名，继续以白名单方式校验外层引用；不需要为 renderer 放宽 AST 策略。

## 4. 后续准入不是 renderer 自己完成的

renderer 成功只说明“施工图可被稳定编译”。每一条正式评测记录仍依序要求：

```text
validated QuerySpec
  -> Gold renderer
  -> SqlPolicy（单语句、AST、对象/列白名单、最大行数）
  -> SET LOCAL ROLE daa_thelook_reader + statement_timeout
  -> ResultValidator（精确列、非空、数值范围、时间边界、200 行预算）
  -> protected external case artifact
```

若其中任一阶段失败，构建器停止且不会写出看似完整的正式评测集。特别地，当分组结果达到 200 行时，不能通过静默 `LIMIT` 截断并称为完整结果；需要缩小窗口或调整经过审查的覆盖设计。

## 5. 测试承诺

v2 回归测试至少覆盖：20 个指标的标量 renderer 与 SQL Policy、订单均值的订单粒度、事件/库存/订单多指标编译、未见原始 `thelook_raw` schema、证据对象不含问题与结果行，以及 600 个 QuerySpec 的全量 renderer/Policy 预检。真实 PostgreSQL 准入另以 `daa_thelook_reader` 执行代表性跨事实域样本，并由正式物化流程对所有 case 复验。
