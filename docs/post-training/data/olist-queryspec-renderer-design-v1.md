# Olist QuerySpec 与 Deterministic Gold SQL Renderer 设计 v1

## 1. 任务卡

**状态：** 设计已冻结；尚未实现 `QuerySpec` 数据类、验证器、SQL renderer、样本构造器或训练。
**上游事实来源：** [`olist-domain-sft-data-contract-v1.md`](olist-domain-sft-data-contract-v1.md)、[`olist-domain-sft-coverage-matrix-v2.md`](olist-domain-sft-coverage-matrix-v2.md)、[`../../metric-contracts/olist-metrics-v2.md`](../../metric-contracts/olist-metrics-v2.md) 与 [`../../../data/catalog/olist_catalog.yaml`](../../../data/catalog/olist_catalog.yaml)。
**目标快照：** `olist-catalog-v2` / `metric_version=0.2-frozen` / `olist-kaggle-v2-2026-08-03` / PostgreSQL / `sql-policy-v1` / `olist-candidate-sql-v1`。

| 项目 | 本轮设计 |
| --- | --- |
| 目标 | 定义可审查的业务查询施工图，以及由它生成唯一 canonical PostgreSQL Gold SQL 的职责边界。 |
| 输入 | 已通过 Router 的业务意图、v2 Catalog、十指标合同、coverage v2 中标记为“纳入”的查询形状。 |
| 输出 | `QuerySpec` 机器可读 schema、renderer 输入/输出、拒绝规则、canonical SQL 约定和后续实现测试清单。 |
| 非目标 | 不从自然语言解析 QuerySpec；不改 Router、Catalog、QueryPlan、Policy 或数据库；不生成训练 JSONL、不运行 SQL、不进行 token 审计或训练。 |
| 核心不变量 | QuerySpec 不是 SQL；业务指标 ID 不是物理列；Gold 必须由固定规则生成且对相同输入字节稳定；模型仍只学习从真实运行时 Prompt 生成 SQL。 |

## 2. 先建立直觉：指标、底层表与训练目标

Olist 的数据并不是一张“经营指标表”。它保存的是订单、订单商品行、评价和客户维度等原始业务事实。
十个指标是把这些底层事实按固定业务口径聚合后的名称。

| 指标 | 用户想知道什么 | Gold SQL 实际依赖的底层事实 |
| --- | --- | --- |
| `gmv` | 卖出了多少商品金额 | 有效订单关联商品行，求 `SUM(price)`；不把运费或支付金额混进来。 |
| `paid_order_count` | 有多少有效订单 | 在订单表上排除 `canceled`、`unavailable`，按 `order_id` 去重。 |
| `average_delivery_days` | 平均多久送达 | 订单的实际送达时间减购买时间，只纳入两端时间齐全的订单。 |
| `positive_review_rate` | 好评占比 | 评价行中 `review_score >= 4` 的比例，只纳入 1--5 分。 |
| `item_count` | 卖出多少商品件 | 有效订单对应的 `(order_id, order_item_id)` 商品行数。 |
| `average_order_value` | 每笔订单平均买多少商品金额 | 先在每笔订单内 `SUM(price)`，再对订单金额取平均；不能直接平均商品行价格。 |
| `average_review_score` | 平均评分 | 有效评价行的 `AVG(review_score)`，按评价行而非 `review_id` 去重。 |
| `on_time_delivery_rate` | 准时送达的比例 | eligible 已送达订单中，实际送达时间不晚于预计送达时间的比例。 |
| `cancellation_rate` | 下单后取消的比例 | 所有购买时间存在的订单中，`order_status='canceled'` 的比例；`unavailable` 不进入分子。 |
| `freight_amount` | 收取了多少运费 | 有效订单商品行的 `SUM(freight_value)`，与 GMV、支付金额分开。 |

因此训练行不是简单的“中文问题 -> 背答案 SQL”。它的构造过程是：

```text
业务语义施工图 QuerySpec
-> 固定 renderer 查 Catalog 和指标表达式
-> canonical PostgreSQL Gold SQL
-> Policy / reader role / ResultContract / 人工口径审核
-> 使用真实运行时 Prompt 训练小模型生成该 SQL
```

例如“统计 2017 年各州 GMV 和取消率”里的 `gmv`、`cancellation_rate` 都不是表字段。
Gold 会分别在各自事实粒度聚合，再按 `customer_state` 合并；模型需要从 Prompt 看到的业务指标、
真实 Schema、QueryPlan 和结果列合同中学会这件事。QuerySpec 是构造 Gold 的内部施工图，不会
作为模型输入，也不能取代运行时安全链路。

## 3. QuerySpec 的职责与边界

### 3.1 它回答什么

QuerySpec 只回答下列确定性问题：

```text
要哪些指标？结果是标量、客户州分组，还是时间序列？
若有时间，使用已确认的哪个绝对范围和哪个时间粒度？
这个语义组合对应 coverage v2 的哪个 join_program_id？
最终结果列应当是什么、按什么顺序出现？
```

它不包含自然语言问题、SQL 字符串、数据库结果、模型输出、训练 split、中文改写、自由过滤、
排序、LIMIT、支付归因或任何未冻结的业务规则。这些内容若混入 QuerySpec，会让“固定语义
施工图”退化成隐藏的 SQL 模板或另一个模型输入接口。

### 3.2 JSON 形状

下列是 v1 的逻辑 schema。实际实现可使用冻结 dataclass/Pydantic model，但字段含义、默认值和
拒绝规则不得改变。

```json
{
  "schema_version": "olist-query-spec-v1",
  "query_spec_id": "qs_...",
  "workspace": {
    "workspace_id": "olist",
    "catalog_version": "olist-catalog-v2",
    "dataset_version": "olist-kaggle-v2-2026-08-03",
    "metric_version": "0.2-frozen",
    "policy_version": "sql-policy-v1",
    "prompt_version": "olist-candidate-sql-v1",
    "dialect": "postgres"
  },
  "metric_ids": ["gmv", "cancellation_rate"],
  "result_shape": "state_grouped",
  "dimension": "customer_state",
  "time": {
    "mode": "absolute_range",
    "start": "2017-01-01",
    "end_exclusive": "2018-01-01",
    "grain": null
  },
  "join_program_id": "JP10_state_multi_metric",
  "required_result_columns": ["customer_state", "gmv", "cancellation_rate"],
  "attribution_rule_id": null
}
```

`query_spec_id` 必须由不含自然语言的 canonical JSON 计算；`sample_id`、`family_id`、
`language_variant_id` 和 `split` 是后续 materializer/audit 的记录字段，不属于 QuerySpec 本身。

### 3.3 字段合同

| 字段 | 允许值与不变量 |
| --- | --- |
| `schema_version` | 固定为 `olist-query-spec-v1`。未来破坏性变化只能创建 v2，不得静默兼容。 |
| `workspace` | 必须逐字段匹配上述 v2 快照；任何 Catalog、指标、数据、策略、Prompt 或方言版本漂移都拒绝渲染。 |
| `metric_ids` | 1--4 个、唯一、按声明顺序稳定；必须与当前 `CatalogRetriever.max_metrics=4` 保持一致。每个都必须存在于当前 Catalog，顺序决定最终 metric alias 顺序。 |
| `result_shape` | 仅 `scalar`、`state_grouped`、`category_grouped`、`time_series`。分组形状可带已确认的绝对时间过滤，但不允许“维度 + 时间序列”双分组、城市、多个普通维度、Top-N 或比较结果。 |
| `dimension` | `scalar`/`time_series` 必须为 `null`；`state_grouped` 必须为 `customer_state`；`category_grouped` 必须为 `product_category_name`，且只允许单指标 `gmv`、`item_count` 或 `freight_amount`。 |
| `time.mode` | `all_time`、`absolute_range`、`series`。`absolute_range` 要求 ISO 日期 `start` 和严格大于它的 `end_exclusive`；采用半开区间 `[start, end_exclusive)`。`series` 必须有 `day`、`week`、`month`、`quarter` 或 `year`，可同时带已确认绝对范围，但只允许 `time_series` 形状；其他形状不得给 grain。 |
| `join_program_id` | 必须是 coverage v2 对当前指标、结果形状和时间模式明确允许的 ID；不能由调用方提供任意 Join 文本。 |
| `required_result_columns` | 是派生冗余字段，只为审计可读性保存。验证器必须重新计算并要求完全一致：`state_grouped` 为 `customer_state + metric_ids`；`category_grouped` 为 `product_category_name + metric_ids`；`scalar` 为 `metric_ids`；`time_series` 为 `metric_ids + ["time"]`，与当前 QueryPlan 顺序一致。 |
| `attribution_rule_id` | v1 必须为 `null`。存在 `requires_attribution`、敏感维度投影或未来 server-owned rule 时，renderer 必须拒绝，而不是补规则。 |

`QueryPlan` 与 QuerySpec 不是同一个对象。前者由当前运行时根据用户问题、Catalog 和 WorkingMemory
构造，仍有一般性警告与 Router 语义；后者是离线、预审查、版本锁定的可生成 Gold 的最小子集。
物化时必须先从 QuerySpec 派生出与它一致的服务器 `QueryPlan` / `ResultContract`，然后再渲染真实
Prompt，不能反过来由模型或自然语言直接填写 QuerySpec。

### 3.4 v1 明确拒绝的请求

以下情况 `validate_query_spec()` 必须 fail closed，并给出机器可读 reason code：

| 场景 | reason code |
| --- | --- |
| Catalog 或 Prompt 版本不匹配 | `workspace_version_mismatch` |
| 未知/重复指标，或超出 4 项 | `invalid_metric_ids` |
| `result_shape`、维度、时间或 join program 组合不在 coverage v2 | `coverage_shape_not_permitted` |
| 输出列不是派生合同列 | `result_columns_do_not_match_contract` |
| 日期无效、非半开区间或时间粒度不匹配 | `invalid_time_contract` |
| 需要归因/分摊、支付方式、品类/卖家订单或评价事实 | `attribution_not_frozen` |
| 输出或分组将暴露 `seller_id` 等敏感标识 | `sensitive_dimension_not_displayable` |
| 排名、排序、LIMIT、自由过滤、相对时间、同比环比或因果字段存在 | `unsupported_query_feature` |

特别地，`seller_id` 虽然天然关联商品行，却是当前 analyst 的敏感投影列。它不能出现在最终
SELECT 或 GROUP BY，因此 v1 QuerySpec 不能支持“按卖家”。将来若要支持，必须先新增非敏感展示维度
或单独授权、Policy、ResultContract 和评测合同；不能通过训练数据规避现有 Policy。

## 4. Deterministic Gold SQL renderer

### 4.1 唯一职责

renderer 接收**已经通过 QuerySpec 验证**的施工图，以及与之匹配的 Catalog 和静态指标表达式注册表，
返回一个 canonical SQL artifact：

```text
Validated QuerySpec + pinned Catalog + MetricSqlDefinition registry
-> canonical PostgreSQL SQL + derived output contract + renderer evidence
```

同一输入必须产生字节稳定的 SQL。实现优先使用受控 SQL AST builder；若局部采用固定 SQL 片段，
片段只能来自代码中的只读注册表，不能插入用户文本、模型文本或任意 SQL 字符串。

### 4.2 renderer 必须做什么

1. 重新验证 QuerySpec 的版本、coverage、归因、敏感投影和派生结果列；
2. 从 `MetricSqlDefinition` 注册表取得每个指标的固定 PostgreSQL 聚合表达式、必要表、默认过滤和时间字段；
3. 以完全限定的 `analytics.<table>` 表名和 Catalog 已登记的 Join 路径构建 SQL；
4. 将绝对时间统一渲染为对应指标 `time_field` 上的 `[start, end_exclusive)` 过滤；
5. 对多指标标量使用每指标一个 CTE 后 `CROSS JOIN`；对州级/同时间字段多指标使用每指标先聚合的 CTE，再按唯一分组键合并；
6. 以 QuerySpec 派生的 `required_result_columns` 作为最终顶层 SELECT 的唯一列和稳定顺序；
7. 返回只含版本、QuerySpec hash、指标/Join 程序、别名、SQL hash 和 renderer version 的脱敏证据。

每项指标表达式的语义源仍是十指标合同，例如：GMV 只求商品行 `price`，AOV 先得到订单级
`SUM(price)` 再平均，履约天数按 `(delivered_customer - purchase)` 转天，两个比例以非整数分子/分母或
明确 decimal cast 避免整数除法。注册表应把这些表达式编码为可审阅程序，不能仅靠 Catalog description
或 Prompt 中的自然语言临时拼接。

### 4.3 renderer 不得做什么

- 不解析自然语言、不决定 Router 状态、不补全时间、不调用 LLM；
- 不执行 SQL、不调用 reader role、不调用 SqlPolicy、ResultValidator 或 repair；这些是 Gold 准入的后续独立 gate；
- 不修改 QuerySpec、不选择 split、不生成中文改写、不读取 protected holdout；
- 不添加排序、Top-N、LIMIT、自由过滤、支付归因、卖家展示或任何未在 QuerySpec 与 coverage 中冻结的能力；
- 不把 `QueryPlan` warning、Policy 追加 LIMIT 或数据库偶然返回的行数当作业务语义。

### 4.4 canonical SQL 约定

| 主题 | v1 约定 |
| --- | --- |
| 方言与对象 | PostgreSQL；所有真实表均显式使用 `analytics.` schema；只使用 Catalog 允许表、列与 Join。 |
| CTE 命名 | 多指标按声明顺序使用 `m01_<metric_id>` 至 `m10_<metric_id>`；中间 AOV 订单聚合使用该 metric CTE 内固定子 CTE 名，不从问题文本生成。 |
| 过滤 | 指标默认过滤不省略；绝对时间一律作用于该指标自身 `time_field`，并使用 `[start, end_exclusive)`。 |
| 时间序列 | 使用 `date_trunc(grain, time_field)`，最终 alias 固定为 `time`；多指标序列只允许同一时间字段组。按日必须有确认绝对范围，且 QuerySpec 预检不得允许超过 analyst 200 行结果预算的形状。 |
| 多指标粒度 | 先独立聚合后组合，禁止商品行、订单、评价或支付明细裸 Join 后再聚合。 |
| 输出 | 仅输出 `required_result_columns`，不投影敏感 ID、辅助键或调试列；SELECT 列顺序与 QuerySpec 派生列顺序完全一致。 |
| 排序与 LIMIT | v1 renderer 不输出 `ORDER BY` 或 `LIMIT`；SQL Policy 可以在实际执行时附加安全行数上限，但该策略副作用不能成为 Gold 语义。 |
| 空值 | v1 客户州和商品品类分组统一排除空维度值，分别使用 `customer_state IS NOT NULL` 和 `product_category_name IS NOT NULL`；避免无标签结果和跨 CTE 空键无法稳定相等。该过滤是分组语义的一部分，必须反映在人工审核和数据描述中。 |

## 5. 设计后的实现与测试边界

下一轮实现只能包含以下内容：`QuerySpec` immutable model、验证器、只读指标表达式注册表、
renderer 和确定性单元测试。不得在同一轮开始 split audit、生成正式 QuerySpec、执行数据库、
物化 Prompt/Gold、token 审计或 GPU 任务。

最低测试集应覆盖：

1. 十项单指标 scalar 的 expression、默认过滤和最终 alias；
2. AOV 的订单内聚合、评价行口径、准时率/取消率分母和运费/GMV 分离；
3. 客户州、商品品类（仅商品行单指标）和时间序列的允许路径；
4. scalar/州级/同时间字段多指标 CTE 结构与稳定输出列顺序；
5. 每一种拒绝 reason code，尤其是版本漂移、归因、敏感 `seller_id`、混合时间字段和自由排序；
6. 对同一个 QuerySpec 重复渲染得到完全相同 SQL/hash；
7. renderer 输出可由 SQL Policy 解析，但 Policy/reader execution/ResultContract/人工语义审核仍作为下一阶段准入，不被 renderer 单测替代。
