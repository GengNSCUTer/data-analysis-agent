# TheLook Cross-Schema Final Test v1

**Release：** `thelook-cross-schema-final-test-v1`
**Purpose：** 对冻结的 Olist LoRA Adapter 和同一 Base 做未见电商 PostgreSQL schema 的 matching 生成评测
**External artifact：** `/disk2/gengnan/data-analysis-agent-data/evals/thelook-cross-schema-final-test-v1-20260908/`

## 评测定位

本集是 TheLook workspace 的 final evaluation-only 资产，不是训练集、验证集、few-shot
示例、Prompt 调优输入或 Olist runtime 资产。它用于回答一个受限问题：在完全相同的 TheLook
问题、受控语义上下文、解码参数、Policy、reader role 和结果合同下，加载 Olist Adapter 是否比
同一 Base 更能生成可执行且与确定性 Gold 结果一致的 PostgreSQL 候选 SQL。

任何结果只能说明当前冻结 TheLook snapshot 与本评测合同上的跨 schema 行为，不能解释为通用
Text-to-SQL 能力、生产准确率或业务上线资格。

## 冻结规模与覆盖

`cases.jsonl` 包含 206 条中文问题，每条有独立 `case_id`、`family_id`、QuerySpec、Gold SQL
SHA-256 和执行证据。问题、family、QuerySpec 和 Gold SQL hash 均为 206 个唯一值。

| 维度 | 数量 / 范围 |
| --- | --- |
| 标量汇总 | 60 |
| 单维度分组 | 66 |
| 时间序列 | 80 |
| 时间模式 | 18 all-time、108 absolute range、80 series |
| 指标 | 六项首批 TheLook 指标全部覆盖 |
| 分组维度 | `traffic_source`、`category`、`department`、受限年度窗口的 `state` |
| 多指标组合 | 成交额+订单数、订单数+客户数、订单数+平均履约天数、订单数+退货率 |

`city` 和 `brand` 被明确排除：它们的完整分组结果可超过 analyst 的 200 行上限，不能用于
“完整结果对照”。`state` 仅使用 2019、2020 和 2021 年上半年窗口；全量/后期年度可能触及或
超过该上限。所有已冻结 case 的 ResultValidator 均为 `valid`，返回行数范围为 1--194。

## 构造与准入

构造器为
[`scripts/post_training/evaluation/build_thelook_cross_schema_evaluation.py`](../../../scripts/post_training/evaluation/build_thelook_cross_schema_evaluation.py)。
它只枚举静态的 QuerySpec 组合，以 renderer 生成 Gold SQL，再逐条执行：

```text
QuerySpec validation
  -> deterministic TheLook renderer
  -> SqlPolicy(thelook workspace)
  -> daa_thelook_reader, statement timeout 5s
  -> ResultValidator(exact columns, metric ranges, time coverage, row cap)
```

生成产物的 `cases.jsonl` SHA-256 为
`f10b4c73663398f9140a4ad23a479e8fb5b2206e4a3a161fb9417e9f55a31576`；它绑定的
TheLook snapshot manifest SHA-256 为
`1720e6990b314ae10a18d3834b0f5bb08c7925394a978e82a2945c427e4520f7`。

本构造步骤没有运行 Base/Adapter、没有读取 Olist 训练/验证数据、没有修改默认 Olist runtime。

## 下一步 Matching 评测合同

下一步应单独冻结 TheLook candidate prompt、Base/Adapter 相同模型 revision、bf16/量化策略、
最大输入/输出 token、解码参数、GPU、恢复规则和判分顺序。生成阶段不能读取 Gold SQL 或结果
行；只有两侧生成文件均冻结并通过 case coverage/matching verifier 后，才允许执行候选 SQL、
ResultContract 和 Gold denotation 对照。
