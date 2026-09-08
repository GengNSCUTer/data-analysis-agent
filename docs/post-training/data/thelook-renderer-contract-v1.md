# TheLook deterministic Gold SQL renderer v1

**Renderer：** `thelook-postgres-gold-renderer-v1`
**输入：** 已通过 `thelook-query-spec-v1` 的 `TheLookQuerySpec`
**输出：** 带 SQL SHA-256 和版本证据的 `TheLookGoldSqlArtifact`

## 设计边界

Renderer 只编译结构化 QuerySpec，不解析自然语言、不调用模型、不连接数据库，也不负责
权限裁决或结果合同校验。生成的 SQL 仍必须经过 `SqlPolicy`、TheLook reader role 和
后续 `ResultValidator`；renderer 的通过只表示“按冻结公式生成了结构合法的候选 Gold SQL”。

每个指标先生成独立 CTE，再按公共维度或时间键合并。跨事实粒度的
`average_order_value` 先按 `orders.order_id` 汇总商品行金额，再计算订单平均值，避免商品
行数量改变订单分母。所有首批指标使用 `orders.created_at` 做时间过滤；时间序列使用
`date_trunc`，时间范围保持 `[start, end_exclusive)`。

## 固定表达式

| 指标 | SQL 口径 |
| --- | --- |
| `completed_sale_amount` | `Complete` 订单的 `SUM(order_items.sale_price)` |
| `completed_order_count` | `Complete` 订单的 `COUNT(orders.order_id)` |
| `average_order_value` | 订单内先 `SUM(order_items.sale_price)`，再 `AVG(order_total)` |
| `average_fulfillment_days` | `Complete` 且时间非空订单的 `delivered_at - created_at` 平均天数 |
| `return_rate` | `Returned / (Complete + Returned)`，其他状态不进入分母 |
| `completed_customer_count` | `Complete` 订单的 `COUNT(DISTINCT orders.user_id)` |

分组维度只能使用 Catalog 已声明的 `state`、`city`、`traffic_source`、`category`、
`brand`、`department`，并由固定映射绑定到 `users` 或 `products` 视图；调用方不能注入
任意列表达式。多指标结果使用 `FULL OUTER JOIN` 合并公共键，单指标不产生不必要的 Join。

## 验证证据

实现位于 [`src/data_analysis_agent/thelook_renderer.py`](../../../src/data_analysis_agent/thelook_renderer.py)，
测试位于 [`tests/test_thelook_renderer.py`](../../../tests/test_thelook_renderer.py)。单元测试
覆盖 SQL 稳定性、五类结果形态、AOV 粒度边界、Policy 兼容性和证据脱敏；TheLook 专项回归
共 `55 passed`（包含 Catalog、QuerySpec、renderer 和 workspace 测试）。

另以本机 `thelook_analytics` 数据库的 `daa_thelook_reader` 权限执行标量、州分组、品类分组、
月度时间序列和 AOV 五个代表性 artifact，均通过 Policy 并成功返回聚合结果。执行只读取
reader-visible analytics views，没有读取 `thelook_raw` 或导出明细。

下一步才是基于 QuerySpec/Gold SQL 构造约 200 条 TheLook 测试问题，并在生成后复用相同的
Policy、reader 和结果合同做 Base/Adapter 对照；本轮不构造题目或启动模型。
