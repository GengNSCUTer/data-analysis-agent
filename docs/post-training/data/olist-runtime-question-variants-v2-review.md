# Olist 运行时中文 Query 变体 v2 审核记录

## 任务卡

- 目标：为 6 条已准入 Olist Gold family 各增加 1 条中文自然语言变体，并确认变体不会改变运行时语义合同。
- 非目标：不生成模型 SQL、不执行数据库、不启动 GPU、不冻结最终 train/validation/test 评测集。
- 输入：仓库外的 6 条 admitted Gold 记录，以及 v2 中文变体 overlay。
- 输出：仓库外 12 条运行时 Prompt 物化结果和本记录。
- 不变量：同一 family 的两条问法必须得到相同的指标集合、维度、时间范围/粒度、QueryPlan 执行策略和结果列合同；仅自然语言与 Prompt hash 可以不同。
- 验收证据：12/12 物化；每个 seed 恰好 2 条；0 rejected；Router/Catalog/QueryPlan/ResultContract 全部通过；无模型、SQL、GPU 或 protected holdout 访问。

## 变体策略

第二条问法只做中文表达改写，不引入新的业务语义：

| Family | 改写方向 | 保持的语义 |
| --- | --- | --- |
| 运费总额 | “运费总额”改为“运费金额” | `freight_amount`，2017-01-01 至 2017-04-01，标量 |
| 平均客单价 | “平均客单价”改为“平均订单金额” | `average_order_value`，2017-07-01 至 2018-01-01，标量 |
| 州商品件数 | “统计各…件数”改为“按…查看数量” | `item_count`，`customer_state` 分组 |
| 州平均评价分 | “平均评价分”改为“平均评分” | `average_review_score`，`customer_state` 分组 |
| 月度评价指标 | “好评率/平均评分”改为“好评比例/平均评价分” | 两个评价指标，按月，2017-01-01 至 2018-01-01 |
| 州准时送达率 | “准时送达率”改为“按时送达率” | `on_time_delivery_rate`，`customer_state` 分组 |

以上是语义审核记录，不把表面改写计作新的 QuerySpec 或独立 SQL 程序。

## 运行时审核结果

变体 overlay：

```text
/disk2/gengnan/data-analysis-agent-data/evals/olist-small-gold-admission-v1/runtime-question-variants-v2.json
```

物化目录：

```text
/disk2/gengnan/data-analysis-agent-data/evals/olist-small-gold-admission-v1/runtime-variants-v2-20260904/
```

manifest 结论：

- 输入 admitted seeds：6
- 物化 rows：12
- rejected：0
- Router / Catalog / QueryPlan / ResultContract：全部 `true`
- `model_called` / `sql_executed` / `gpu_used`：全部 `false`
- `protected_holdout_read`：`false`

逐 family 对照显示，两条变体的指标集合、维度、时间粒度、时间范围、执行策略和必需结果列均一致；两条 Prompt 的 SHA-256 不同，说明生成上下文包含不同的用户问法。

## 证据与限制

代码入口为 `scripts/post_training/data/materialize_olist_runtime_prompts.py`，专项测试为 `tests/test_olist_runtime_prompt_materializer.py`。本轮验证结果为 `5 passed`，`ruff check` 和 `compileall` 均通过。

这是 AI 辅助的结构化口径预审，不等同于用户或业务专家签字。当前 12 条仍是小批变体准入资产，不是最终冻结的训练/验证/测试集，也不代表模型 SQL 准确率或业务泛化能力。下一步需在用户确认这 6 对问法后，单独进行扩展评测集的冻结设计。
