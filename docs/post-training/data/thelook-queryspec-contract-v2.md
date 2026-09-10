# TheLook v2 QuerySpec 合同

**状态：** 已冻结；用于受保护的 `thelook-cross-schema-eval-v2` 评测施工图。
**实现：** [`src/data_analysis_agent/thelook_v2_queryspec.py`](../../../src/data_analysis_agent/thelook_v2_queryspec.py)。
**依赖：** [`thelook-catalog-v2`](../../../data/catalog/thelook_catalog_v2.yaml) 与 [`TheLook 二十项业务指标合同 v2`](../../metric-contracts/thelook-metrics-v2.md)。

## 1. 职责边界

`TheLookV2QuerySpec` 是离线、版本锁定的结构化查询计划书：它表达已经审核过的指标、结果形态、维度、时间和结果列合同。它**不是**自然语言解析器、模型输出 SQL、数据库连接器或产品运行时的 API 入参。

它的职责是将“可评测的业务意图”与“可重复编译的 Gold SQL”之间的模糊空间降到最小：自然语言问题只是在计划冻结后由受控模板生成的表面形式，不能反向改变查询含义。

## 2. 固定字段

| 字段 | 含义与不变量 |
| --- | --- |
| `schema_version` | 必须是 `thelook-query-spec-v2`。 |
| `query_spec_id` | 对 canonical JSON 计算 SHA-256 后取前缀的 `tqs2_...`；任何字段被篡改都会失配。 |
| `workspace` | 固定 `workspace_id=thelook-cross-schema-eval-v2`、Catalog/数据/指标/Policy/Prompt 版本和 PostgreSQL dialect。 |
| `metric_ids` | 1--4 个唯一、合法的 v2 指标 ID。 |
| `result_shape` | 仅 `scalar`、`dimension_grouped`、`time_series`。 |
| `dimension` | 仅分组型计划可以携带，并且须在每个指标的 `allowed_dimensions` 内。 |
| `time` | `all_time`、左闭右开 `absolute_range`，或带 `day/week/month/quarter/year` 粒度的 `series`。 |
| `join_program_id` | 从指标事实域、形态和维度确定性派生，禁止由调用方自填任意连接计划。 |
| `required_result_columns` | 从计划确定性派生：分组先维度、标量仅指标、序列为指标后 `time`。 |

`query_spec_id` 的 canonical payload 不含问题、SQL、执行结果、模型名称或训练 split。这避免把 holdout 内容塞进可被训练或 Prompt 使用的结构中。

## 3. Fail-closed 规则

验证器 `validate_thelook_v2_query_spec()` 会拒绝：

1. 未知、重复、超过四个或不符合安全标识符规范的指标；
2. Catalog、数据、指标、Policy、Prompt 或 PostgreSQL dialect 的版本 pin 漂移；
3. 只在 Catalog 或只在 renderer 事实域注册表出现的指标，防止 Catalog/renderer 漂移；
4. 混合不同事实域的多指标组合；
5. 未被所有指标共同许可的维度、敏感维度和维度/事实域不匹配；
6. 非法日期、结束时间不晚于开始时间、未指定序列粒度，或把序列时间用于非序列结果；
7. 快照库存指标出现任何时间范围或时间序列；
8. 非法 `join_program_id`、结果列重排、字段增删与 hash 篡改。

同一事实域的多指标组合是允许的，例如“事件数 + 去重会话数”、`average_order_value + average_items_per_completed_order` 和“已售库存单元数 + 平均售出周期”。订单、事件、库存、用户等跨事实组合即使措辞看似合理也会拒绝，而不是悄悄选择一个时间字段或隐含归因规则。

## 4. QuerySpec 到结果的离线链路

```text
冻结指标合同 / Catalog
  -> 结构化 TheLookV2QuerySpec
  -> QuerySpec 验证器（版本、组合、维度、时间、列）
  -> deterministic v2 Gold renderer
  -> SqlPolicy -> daa_thelook_reader -> ResultValidator
  -> 仓库外 protected evaluation record
```

自然语言五种中文问法在上述计划之后生成；每个 `query_spec_id` 以 SHA-256 稳定选择其中一条作为本次评测的主问法。五种措辞是同一个语义样本的 presentation overlay，不会把一个计划计成五条测试样本。

## 5. 与 Olist / v1 的隔离

- v2 仅共享物理只读数据库和 `analytics` 脱敏 views，不共享 v1 的 Catalog、QuerySpec ID、renderer 或历史 206 条评测证据。
- TheLook v2 的 `QuerySpec`、问题、Gold SQL、原始结果和生成结果均为 protected holdout，不能进入 Olist SFT 的 train/validation、训练提示、few-shot、模型选择或错误驱动 Prompt 调优。
- 这份合同的通过证明冻结后的离线 Gold 施工图一致；不证明 Base/Adapter 的自然语言到 SQL 生成质量。该对照只能在 final test 物化后按独立 matching protocol 运行。
