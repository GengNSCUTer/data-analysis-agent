# TheLook v3｜750 条跨 Schema 评测覆盖合同 v2

本合同是 TheLook v3 的当前机器合同说明。机器可读来源为
[`thelook_v3_coverage_contract_v2.json`](../../../data/fixtures/thelook_v3_coverage_contract_v2.json)，静态 seed 为
[`thelook_v3_coverage_seeds_v2.jsonl`](../../../data/fixtures/thelook_v3_coverage_seeds_v2.jsonl)。

## 目标与边界

v3 目标是 750 条中文、跨 Schema 的 final evaluation-only 样本。它复用已经审计的
TheLook v2 QuerySpec、指标目录和 deterministic PostgreSQL renderer，但不进入训练、few-shot、
Prompt 调优、错误回流或模型选择。v2 历史 600 条资产保持不可变。

本轮保持冻结的 20 个指标不变。v3 的新增价值来自事实域、聚合程序、结果形态、时间粒度、
维度和风险覆盖，不通过新增或改写指标口径凑数量。

## 复用策略

v3 不要求与 v2 完全不同。v2 中已经证明合法、且 v3 语义覆盖确实需要的 QuerySpec 可以复用，
但复用必须显式记录并经过 release audit：

- 允许复用的 family 只有 `inventory_unsold_snapshot`；
- 预期复用 4 个 QuerySpec ID 和 4 个 canonical Gold SQL SHA-256；
- 这 4 条对应 all-time 快照指标的标量、配送中心、商品类目和商品部门分组；
- 除这 4 条外，不允许其他 QuerySpec 或 Gold hash 重合；
- v3 的规范化主问题与 v2 仍要求零重合；
- v2/v3 重合是审计结果，不是失败本身。未经批准的重合才是 release blocker。

因此，v1 文档中“所有身份零重合”的规则仅作为历史设计记录，不再作为当前 v3 的执行规则。

## 当前配额

| 维度 | 配额 |
| --- | --- |
| 事实域 | `order_items/orders/inventory_received/inventory_sold/inventory_snapshot/events/users = 135/250/60/100/4/135/66` |
| result shape | `scalar/dimension_grouped/time_series = 210/285/255` |
| 时序粒度 | `day/week/month/quarter/year = 35/45/65/60/50` |
| 指标数 | `1/2/3/4 = 500/210/35/5` |
| scenario family | 21 类，目标数合计 750 |

高基数维度 `customer_city/product_brand/user_state` 至少覆盖 36 条，并且必须使用预先冻结的 7 天
绝对时间窗口（不是运行时失败后的重试选日期）。这些候选仍必须通过真实 reader-role 准入；展示预算
固定为最多 200 行。每条 seed 都独立记录 `scenario_family_id`、程序签名、风险标签
和时间窗口候选，不能把 `family_id` 当作 QuerySpec 身份。

## 八类中文问法

每个通过准入的 QuerySpec 生成 8 条语义等价、无 SQL 泄露的中文 surface overlay。overlay 不增加
case 数，正式 750 条每个 QuerySpec 只选一条主问法；主问法按 family × shape 分层、可复现地满足
全局 `v1-v6=94`、`v7-v8=93` 配额。英文列名、SQL 关键字、`analytics.` 路径和未冻结的排名、因果、
自由筛选含义不得进入主问题。

## 准入与 release audit

静态 seed 不是最终评测集。物化前每条候选必须通过：

```text
QuerySpec validation
-> deterministic Gold renderer
-> SqlPolicy
-> SET LOCAL ROLE daa_thelook_reader
-> ResultContract / ResultValidator
-> <= 200 行展示预算
```

小批准入按每个 family 选代表样本并保留 SQL hash、reader-role、结果合同和失败原因。全量物化后，
 release audit 还要重算所有 case / QuerySpec / Gold hash，核对精确配额、8 类问法分层配额、v2 复用
 策略、无 SQL 泄露以及 Gold SQL 只访问 `analytics` schema。任何 family 达不到质量门时，必须记录
 实际减少数和原因，不能通过只改日期、措辞或复制结构填数。

## 已执行结果（2026-09-15）

本合同已经完成从静态设计到受保护 release 的执行：

| 阶段 | 结果 | 外部证据 |
| --- | --- | --- |
| 按 scenario family 小批准入 | 44/44 通过，21/21 family 覆盖 | `thelook-v3-coverage-admission-v1-r3/` |
| 全量物化 | 750/750 通过，0 exclusions，21/21 family 配额满足 | `thelook-cross-schema-final-test-v3-20260915-r3/manifest.json` |
| release audit | `status=pass`，`release_eligible=true` | `release_audit.json` |

最终 `cases.jsonl` SHA-256 为
`bd1195c00dd68a884fc55eb8301184ef1492dd070f01fa26f96293ac13acef7c`。每条 case 均保留
QuerySpec、Gold SQL 及其 SHA-256、renderer/Policy 结果、reader-role 执行证据、
ResultContract/ResultValidator 结果和展示行数预算。该 release 是 TheLook 的
`final evaluation-only` protected holdout，不得回流 Olist 训练集、Prompt、错误样本或模型选择。

### 关于 v2/v3 “重合”

“零重合”只针对未经批准的身份重合，而不是要求两个版本完全不能复用已经正确的语义。
本版本明确批准 `inventory_unsold_snapshot` family 的 4 个 QuerySpec/Gold 复用；审计结果为
QuerySpec overlap=4、canonical Gold overlap=4、规范化 primary question overlap=0。除该批准范围外，
任何 QuerySpec、Gold hash 或主问题重合都会阻断 release。v2 的安全/语义约束继续作为 renderer、
Policy 和 ResultContract 的基础，v3 只在覆盖配额、family 元数据和受控复用规则上扩展。
