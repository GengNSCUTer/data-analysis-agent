# Top-N 形状与结果追问数字检查修复

## 目标

支持真实的多维度和时间序列 Top-N，并修复结果解释把列表序号、排名序号误判为业务新数字的问题。

## 非目标

- 不支持任意嵌套排名、比较基线或多指标之间未定义的排名口径。
- 不删除 SQL Policy、reader、ResultContract、ResultValidator、行数/字节/超时预算。
- 不把模型回答中的未经验证业务数字放宽为可信数字。

## 输入与输出

- 输入：结构化语义意图中的 `dimensions`、`time_grain`、`top_n`，以及 SQL 候选。
- 输出：带有 `top_n` 排名维度和分区维度的 QueryPlan；SQL 候选必须使用窗口排名表达“每个分区前 N”，再由已有安全与结果合同执行。

## 语义合同

1. `top_n` 必须有至少一个业务分组维度；只有时间粒度没有排名维度时继续澄清。
2. 排名目标是 `dimensions` 的最后一个维度。
3. `dimensions[:-1]` 与时间粒度组成分区键：
   - `每个州前五个品类`：按 `customer_state` 分区，排名 `product_category_name`；
   - `每月前五个品类`：按 `time` 分区，排名 `product_category_name`；
   - `每月每州前五个品类`：按 `time, customer_state` 分区，排名 `product_category_name`。
4. 无分区键时是全局 Top-N，已有单维度行为保持不变。
5. 多指标排名暂按 QueryPlan 的首个指标排序；未定义的多指标排名不新增自动推断。

## 执行不变量

- 分区 Top-N 的 SQL 必须包含 `ROW_NUMBER`、`RANK` 或 `DENSE_RANK`，`PARTITION BY`，以及不大于 N 的排名过滤；不接受只写全局 `LIMIT`。
- 服务端不再对分区 Top-N 追加全局 Top-N `LIMIT`；结果预算仍使用 QueryPlan 的分区基数 × N 上限。
- SQL 候选仍经过 AST SqlPolicy、只读 PostgreSQL、超时、字节/行预算和 ResultValidator。
- `top_n` 形状写入现有 QueryPlan 证据，不新增数据库表或不必要的 hash。

## 数字检查规则

结果追问继续禁止摘要之外的业务数值，但忽略明确的展示编号：行首 `1.`/`1)`、`第 10 名`、`排名第 2`、`前 5 项`、`共 10 行` 等。金额、比例、日期年份和其他业务数字仍逐个与可信摘要比较。

## 验收

- 确定性：覆盖全局 Top-N、多维分区 Top-N、时间分区 Top-N、时间+多维分区 Top-N、无分组 Top-N 澄清、缺少窗口排名 SQL 拒绝，以及列表序号不触发而新业务数字触发。
- 浏览器：至少覆盖“每个州前五个品类”和“每月前五个品类”的真实连续请求，记录 QueryPlan、SQL 审计、结果合同和终止原因。

## 实际验收

- Python 定向回归：**147 passed、10 skipped**；覆盖全局/州分区/时间分区/时间+多维分区、无维度澄清、错误分区字段、窗口排名过滤和展示编号数字检查。
- 真实 Playwright + SSE + PostgreSQL：**2 个场景通过**。`每个州销售额最高的前五个商品品类` 返回 135 行（27 个州 × 5）；`2017 年每月销售额最高的前五个商品品类` 返回 60 行（12 个月 × 5）。两场景均记录 `top_n_partition_dimensions`、SQL 审计和 `result_contract_satisfied`。
- 原有九轮连续分析流程在同一修复前的最终服务版本中已通过；本轮只新增分区 Top-N 约束，未改变无分区的全局 Top-N 路径。
- 真实证据：`/disk2/gengnan/data-analysis-agent-data/evals/flow-constraints-20261017-final-r2/`；服务：screen `data-analysis-agent-fluency`、`http://127.0.0.1:32016/embedded-copilot`。
