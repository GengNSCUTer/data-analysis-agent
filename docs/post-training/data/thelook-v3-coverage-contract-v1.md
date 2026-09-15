# TheLook v3｜750 条跨 Schema 评测覆盖合同

## 1. 决策、目标与冻结边界

本合同冻结新的 `thelook-cross-schema-final-test-v3` 的**设计配额**。目标是在不改变
TheLook 已审计业务语义的前提下，建立约 `750` 个唯一 QuerySpec / canonical Gold SQL 的
中文跨 Schema final evaluation-only 资产，用于在 Olist 模型与 Adapter 已冻结后比较 matching
Base/Adapter 的 SQL 生成质量。

本合同不授权修改 Olist 训练、validation、final test、Prompt、checkpoint 或模型参数；也不授权
启动 TheLook Base/Adapter 生成。TheLook v3 一旦物化，即为 protected holdout，不能被用于训练、
few-shot、错误回流、Prompt/超参调优或模型选择。

TheLook v2 的 600 条历史资产和其历史结果保持不可变。v3 不是在 v2 目录上扩写，也不覆盖 v2。
所有 v3 QuerySpec ID、canonical Gold SQL hash 和规范化主问题都必须与 v2 零重合。

机器可读的唯一配额来源是
[`thelook_v3_coverage_contract_v1.json`](../../../data/fixtures/thelook_v3_coverage_contract_v1.json)。
本 Markdown 只解释设计理由，不能与 JSON 冲突。

## 2. 已冻结的语义底座：保持 20 项指标不变

本轮显式决定保持 `thelook-catalog-v2`、指标版本 `0.2-frozen`、TheLook v2 QuerySpec 和
deterministic PostgreSQL renderer 的 20 项指标不变。750 条的覆盖扩展优先来自事实域、聚合程序、
维度、时间粒度和安全多指标组合，不通过新增指标名称凑数量。

若后续静态 seed 与真实 reader-role 准入共同证明：在不复制 v2、不重复 QuerySpec/Gold、不违反
200 行展示预算且不伪造新业务语义的条件下，无法达到 750 条，才允许单独新建“指标合同变更”任务。
在那以前，任何构造器不得添加指标或放宽现有指标口径。

## 3. 目标规模与配额

v3 的正式目标是 750 条，且每条都有唯一 `case_id`、`query_spec_id`、canonical Gold SQL SHA-256。
所有多指标查询仍必须保持单一事实域、相同时间归属和同一结果形态。

| 维度 | 配额 |
| --- | --- |
| 事实域 | `order_items/orders/inventory_received/inventory_sold/inventory_snapshot/events/users = 135/250/60/100/0/135/70` |
| result shape | `scalar/dimension_grouped/time_series = 210/285/255` |
| 时序粒度 | `day/week/month/quarter/year = 35/45/65/60/50` |
| 指标数 | `1/2/3/4 = 500/210/35/5` |
| 高基数维度 | `customer_city/product_brand/user_state` 各在真实 reader-role 准入的短窗口中覆盖；合计至少 36 条 |

指标暴露数按事实域约束，而不使用所有指标共用的一条错误上限：商品行和事件域各只有两个指标，
会自然在 135 个 case 中各出现约 90 次；收到库存和注册用户各只有一个指标，分别必须出现 60 和
70 次。订单域的 11 个指标则各出现 25–70 次，以保证取消、退货、二层订单均值、时长和去重指标
都实际进入评测，而不是只由销售额、事件量等高频指标凑满总行数。机器合同中的
`case_exposure_by_fact_domain` 是这项规则的唯一来源。

未售库存快照仍保留在 20 项 Catalog 指标中，但 v3 的配额为 0。原因是当前 QuerySpec 只允许
all-time 标量或三个低基数分组，四个合法 QuerySpec 已全部存在于 v2；在 v2/v3 QuerySpec 和 Gold SQL
零重合的规则下不能复用。不得为平衡数字把它伪造为时间序列、复制旧题或在本轮新增未冻结过滤语义。

## 4. Scenario family、程序签名与风险标签

v2 的 `family_id` 曾等于 `query_spec_id`，只代表身份唯一，无法表达业务或 SQL 程序覆盖。v3 不能延续
这一定义。每个静态 seed 都必须附带：

- `scenario_family_id`：20 个重复可统计的业务/程序类别之一；
- `program_signature`：由事实域、指标组合、结果形态、维度、时间模式/粒度、join program 确定性导出，
  **不含**日期边界；
- `risk_tags`：标注 `distinct`、`two_stage_aggregate`、`status_filter`、`duration_eligibility`、
  `snapshot`、`multi_metric_cte`、`high_cardinality_window`、`time_filter` 或 `time_bucket` 等风险。

其中 `time_filter` 只说明普通标量/分组查询使用了冻结的绝对期间，不是额外业务语义，也没有单独的
最低配额；`time_bucket` 则是所有时间序列的必备标签。

它们是覆盖 metadata，不是 QuerySpec 的执行语义，故不进入 QuerySpec canonical hash，也不改变
renderer 的编译职责。`case_id` / `query_spec_id` / Gold SQL hash 仍是唯一性与重合审计的来源。

合同要求的 20 类 scenario family、每类事实域和精确目标行数全部在机器合同中定义；其总和恰好为 750。

## 5. 八类纯中文问法

每个已准入 QuerySpec 必须生成 8 条语义等价、SQL-free、纯中文 surface form：正式请求、业务口语、
管理者表达、简洁表达、结果导向、时间前置、分析切面前置和 Catalog 中文别名。

8 条问法只是同一 QuerySpec 的 overlay，不增加测试 case 数。正式 750 条中，每个 QuerySpec 只选一条
主问法；选择策略为 `sha256_ranked_family_shape_eight_way_quota_v1`，全局配额为 `v1-v6=94`、
`v7-v8=93`，并在 `scenario_family_id × result_shape` 分层桶内保持差值最多为 1。

主问题只能使用 Catalog 中文名称或中文受控别名；不得含 SQL、`analytics.`、英文指标缩写或未冻结的
趋势、排名、因果、相对时间和自由筛选含义。

## 6. 静态 seed 与后续准入

本合同之后的静态 coverage seed 必须是确定性的，不由 LLM 生成 QuerySpec 或 Gold SQL。每条 seed 固定：

```text
seed_id
scenario_family_id
query_spec（metric_ids / shape / dimension / time / join program）
program_signature
risk_tags
window_candidate / admission requirement
```

静态 seed 还不是 final test。最终物化前，所有候选都必须逐条通过：

```text
QuerySpec validation
-> deterministic Gold renderer
-> SqlPolicy
-> SET LOCAL ROLE daa_thelook_reader
-> ResultContract / ResultValidator
-> <= 200 行展示预算
-> v2 overlap audit
```

任何失败候选应进入外部 exclusion manifest。若某 family 不能用安全替补达到合同配额，必须报告并停止
release，而不是复制日期、问题或 SQL 填满 750。

本轮的 seed 窗口候选固定从 2019-02-01 起构造，并排除 January-aligned 起点，主动避开 v2 的已知年度
窗口模式；但这只是减少碰撞的候选策略，最终仍以受保护 v2 QuerySpec ID / Gold SQL hash 的零重合审计为准。
