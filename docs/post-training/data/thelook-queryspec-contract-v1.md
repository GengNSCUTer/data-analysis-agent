# TheLook QuerySpec 合同 v1

**Schema：** `thelook-query-spec-v1`
**Workspace：** `thelook-cross-schema-eval`
**Catalog：** `thelook-catalog-v1`
**Prompt 版本：** `thelook-query-prompt-v1-design`

本文定义 TheLook 后续零样本评测使用的结构化查询计划。QuerySpec 是
`Semantic Catalog` 与 deterministic PostgreSQL Gold SQL renderer 之间的中间合同；它
不是自然语言解析结果，也不是模型生成的 SQL。当前版本只描述已冻结的指标、结果形态、
维度和时间范围，不连接数据库、不调用模型、不生成题目。

## 字段与职责

| 字段 | 作用 | 约束 |
| --- | --- | --- |
| `query_spec_id` | canonical JSON 的稳定指纹 | 由完整计划内容计算，内容变化必须改变 ID |
| `workspace` | 数据、Catalog、指标、策略和 Prompt 快照 | 必须与 `THELOOK_WORKSPACE` 完全一致 |
| `metric_ids` | 要计算的业务指标 | 1--4 个，唯一且必须存在于 TheLook Catalog |
| `result_shape` | 返回结果结构 | `scalar`、`dimension_grouped` 或 `time_series` |
| `dimension` | 分组维度 | 仅 `dimension_grouped` 使用；当前支持 `state`、`city`、`traffic_source`、`category`、`brand`、`department` |
| `time` | 统计时间合同 | 全部时间、半开绝对区间或带粒度的序列 |
| `join_program_id` | 后续 renderer 的固定程序族 | 由结果形态和维度确定，调用方不能自由填写 |
| `required_result_columns` | ResultContract 预期列顺序 | 由结果形态、维度和指标确定，不能手工篡改 |

## 当前允许范围

- 标量：单指标或最多四个指标的汇总。
- 分组：按一个用户/流量/商品维度分组；每个指标必须在 Catalog 中声明支持该维度。
- 时间序列：使用 `orders.created_at` 作为订单指标的统一时间字段，粒度为
  `day`、`week`、`month`、`quarter` 或 `year`；当前指标均以订单创建时间为时间口径。
- 时间范围使用 `[start, end_exclusive)` 半开区间；序列必须显式提供起止日期和粒度。

## Fail-closed 校验

验证器会拒绝版本漂移、未知或重复指标、超过四项指标、不支持的结果形态、敏感关联
字段作为维度、维度与结果形态不匹配、指标不支持该维度、非法日期、错误 Join 程序、
结果列顺序篡改以及错误的 `query_spec_id`。验证器只检查计划结构；它不证明 SQL 已执行、
结果符合业务口径或权限安全，这些属于后续 renderer、SqlPolicy、reader role 和
ResultValidator 的职责。

## 实现与验证

实现位于 [`src/data_analysis_agent/thelook_queryspec.py`](../../../src/data_analysis_agent/thelook_queryspec.py)，
测试位于 [`tests/test_thelook_queryspec.py`](../../../tests/test_thelook_queryspec.py)。本轮
TheLook Catalog、workspace 和 QuerySpec 回归共 `48 passed`，另通过 compileall 与 Ruff。

下一步是基于这些已验证计划设计 TheLook 专用 deterministic PostgreSQL Gold SQL renderer，
之后才可以构造约 200 条测试问题；本合同本身不包含自然语言或 SQL。
