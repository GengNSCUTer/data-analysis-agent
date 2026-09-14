# Olist 业务指标合同 v3 Proposal

**状态：** 已实现离线 Catalog、QuerySpec、deterministic Gold renderer、SchemaLinkPlan、
reader-role 与 ResultContract 回归，并已冻结静态 coverage family seed；尚未成为默认运行时
Catalog，尚未进入正式训练集。

**隔离快照：** `olist-catalog-v3` / `metric_version=0.3-proposal` /
`olist-kaggle-v2-2026-08-03` / PostgreSQL / `sql-policy-v1`。

**机器可读定义：** [`data/catalog/olist_catalog_v3.yaml`](../../data/catalog/olist_catalog_v3.yaml)。

## 1. 版本边界

v2 的十项指标、已经完成的 Olist Release v2、历史训练 checkpoint 和既有评测都继续固定在
`olist-catalog-v2 / 0.2-frozen`。v3 使用独立的 `OLIST_V3_WORKSPACE` 进行离线构造，不能静默
替换线上 `OLIST_WORKSPACE`。只有 v3 Gold、数据库、ResultContract 和训练数据准入都完成后，才
能单独讨论运行时接入。

## 2. 新增九项指标

| ID | 公式与粒度 | 默认时间字段 | 首批安全直接维度 | 关键边界 |
| --- | --- | --- | --- | --- |
| `unique_customer_count` | 有效订单关联客户维度后 `COUNT(DISTINCT customer_unique_id)`；客户/订单粒度。 | 购买时间 | 日期、客户州/城市 | 排除 canceled/unavailable；客户标识只用于去重和 Join，不能输出。 |
| `review_count` | `review_score BETWEEN 1 AND 5` 的 `COUNT(*)`；评价行粒度。 | 评价创建时间 | 日期、客户州/城市 | 不按 `review_id` 去重；空评分不计入。 |
| `canceled_order_count` | 状态为 `canceled` 且购买时间非空的 `COUNT(DISTINCT order_id)`。 | 购买时间 | 日期、客户州/城市 | 不把 unavailable 算作取消。 |
| `delivered_order_count` | 状态为 `delivered` 且购买时间非空的 `COUNT(DISTINCT order_id)`。 | 购买时间 | 日期、客户州/城市 | “已送达”只按订单状态定义，不等同有效订单。 |
| `unavailable_order_count` | 状态为 `unavailable` 且购买时间非空的 `COUNT(DISTINCT order_id)`。 | 购买时间 | 日期、客户州/城市 | 不使用“异常订单”等含混别名。 |
| `average_items_per_order` | 先对每个有效且含商品行订单 `COUNT(order_item_id)`，再取 `AVG`；订单级二层聚合。 | 购买时间 | 日期、客户州/城市 | 不能直接对商品行做平均或把 `COUNT(order_id)` 误作订单指标。 |
| `average_item_price` | 有效商品行 `AVG(price)`；商品行粒度，不含运费。 | 购买时间 | 日期、客户州/城市、商品品类 | 不是 AOV/客单价，品类直接分组安全。 |
| `approval_latency_days` | 非空且 `approved_at >= purchase_at` 的 `AVG(approved_at - purchase_at)`，以天计。 | 购买时间 | 日期、客户州/城市 | 负时长和缺失时间都排除。 |
| `carrier_handoff_days` | 非空且 `carrier_at >= purchase_at` 的 `AVG(carrier_at - purchase_at)`，以天计。 | 购买时间 | 日期、客户州/城市 | “交承运商”不是最终送达。 |

除明确列出的维度外，品类下订单级/评价级指标、支付方式、卖家维度仍需单独的归属合同；不得由
renderer 或中文问法隐式放开。

## 3. 已实现的离线构造组件

- `OLIST_V3_WORKSPACE` 指向独立 v3 Catalog，不改变默认运行时 v2 workspace；
- `METRIC_SQL_REGISTRY` 增加 9 个确定性公式、时间域和过滤边界；
- `QuerySpec` 根据 pin 自动加载相符 v2/v3 Catalog，并继续拒绝混合购买/评价时间序列、未冻结
  支付归属和不安全品类分组；
- renderer 对 `average_items_per_order` 使用命名的订单级中间 CTE；
- SchemaLinkPlan 为 9 个指标分别记录关系、Join、字段、去重和聚合规则；
- `tests/test_olist_v3_metrics.py` 覆盖全部 v3 scalar Gold、受控别名检索、客户去重、二层聚合、
  时长非负边界和商品品类分组；在 `RUN_PROJECT_DB=1` 时，9 项标量 Gold，以及客户州/商品品类分组、
  购买/评价时间序列和空时间窗口，均经 `SqlPolicy`、`daa_analytics_reader` 和
  ResultContract/ResultValidator 验证通过（2026-09-14 共 37 项）；
- [`evals/sql/verify_olist_metrics_v3_proposal.sql`](../../evals/sql/verify_olist_metrics_v3_proposal.sql)
  和 [`evals/results/olist-metrics-v3-proposal-golden.yaml`](../../evals/results/olist-metrics-v3-proposal-golden.yaml)
  固定当前数据快照的聚合基线；2026-09-14 对本机 PostgreSQL 执行返回 `DO`。

## 4. 当前准入状态与剩余门

2026-09-14 已完成 v3 指标的确定性准入回归：全量标量、客户州/商品品类分组、购买/评价时间序列、
二层聚合和空时间窗口均实际由 reader role 执行。空窗口行为也被固定：`COUNT` 标量返回一行 `0`
并可展示；`AVG` 标量返回 `NULL` 时必须拒绝，不能伪装为 `0`；空分组/时间序列必须
`needs_clarification`，不能凭空补 bucket。

这允许进入静态 family seed 阶段，但不等于完整 v3 release 已准入。后续仍必须：

1. 将 seed 小批物化为具体 QuerySpec/Gold SQL，逐条通过 Policy、reader role 和结果合同；
2. 对客户去重、二层聚合、状态计数和时长指标做分层人工口径审核（可使用 LLM 作为 advisory，不替代签字）；
3. 为通过的具体 artifact 记录 Catalog、SQL hash、数据快照与执行证据，形成 release manifest；
4. 完成中文 surface、Prompt/长度和 split 审计后，才可物化正式 SFT JSONL。

无论上述哪一步完成，v3 都不得静默替换默认 Agent 的 v2 workspace；运行时接入是独立决策。
