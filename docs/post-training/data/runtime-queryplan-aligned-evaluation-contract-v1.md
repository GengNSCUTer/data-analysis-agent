# Runtime QueryPlan 对齐评测合同 v1

这份合同定义运行时候选 SQL 的主评测目标。它与历史 `runtime-candidate-v2`
matching 合同并行存在，不改写、不覆盖 r2/r3 的输入、模型输出、Gold 或结果。

## 决策

本项目的主指标评估：

```text
明确语义的用户问题
→ 服务端 QueryPlan
→ deterministic QueryPlan Gold SQL
→ 候选 SQL 的受控执行与 denotation 对照
```

原始 QuerySpec/Gold 匹配保留为辅助指标，用于检查数据集标签兼容性、复用历史
实验结果和定位 source/Gold 漂移。两者不合并为一个分数，也不以较高的那个分数
作为 headline。

选择运行时 QueryPlan 作为主目标，是因为线上模型实际接收的是 Catalog、Workspace
scope、QueryPlan、ResultContract 和用户问题；它不会接收离线 QuerySpec 或 Gold
SQL。候选 SQL 即使匹配旧 Gold，但没有遵循当前 QueryPlan，线上仍可能查询错误的
时间范围、维度或结果列。

## 1. 适用范围和非目标

本合同只评估模型是否把已经由服务端确定的查询计划编译成候选 PostgreSQL SQL。
它不评估：

- Router 是否正确理解用户意图；
- Catalog 是否选对指标；
- QueryPlan 是否由自然语言正确解析；
- SQL Policy、reader role 和 ResultValidator 是否可以被绕过；
- 自由问题在未见过的数据库 Schema 上的泛化。

Router、Catalog 和 QueryPlan 的错误必须在输入审计中单独报告，不能伪装成 SQL
模型错误。候选 SQL 仍必须经过 QueryPlan Guard、SqlPolicy、对应 workspace 的
PostgreSQL reader role、ResultContract/ResultValidator。

## 2. 对齐输入

新建对齐评测 release 时，每条记录必须至少包含：

| 字段 | 约束 |
| --- | --- |
| `source_id` | 固定且唯一，跨 release 不复用含义 |
| `workspace` | `olist` 或 `thelook`，绑定独立 Catalog、数据库和 reader role |
| `question` | 用户实际看到的文本；如果有日期范围，必须明确包含开始日、不包含结束日 |
| `query_plan` | 服务端实际构造的 QueryPlan，含 `time_range.end_semantics=exclusive` |
| `result_contract` | 与 QueryPlan 的指标、列序、时间粒度和边界一致 |
| `catalog_snapshot` | Catalog、dataset、metric、policy 版本和 hash |
| `runtime_prompt_sha256` | 共同候选构造器产生的 prompt hash |
| `alignment_status` | `aligned`、`ambiguous` 或 `route_rejected` |

时间语义规则：

- `start` 必须包含；`end` 必须排除；数据库谓词统一为 `time >= start AND time < end`；
- “至”“到”这类没有排除语义的旧问题不能直接进入 aligned 主评测；
- all-time 查询可以进入 aligned，但必须显式记录没有时间边界；
- 不能为达到目标行数而改日期、改问题、复制 family 或把 ambiguous 样本静默删除；
- 历史 r3 protected 输入保持原样，另建新 release 进行端点对齐。

对齐 release 的固定分母仍然保留所有来源记录。`route_rejected` 和
`alignment_status=ambiguous` 计入总量并单独报告，不计作模型成功；只有
`alignment_status=aligned` 的 ready 记录进入主 denotation 分母。

## 3. 两套 Gold 的职责边界

### 3.1 主 Gold：QueryPlan Gold

主 Gold 必须由 deterministic renderer 根据以下输入生成：

```text
QueryPlan + Semantic Catalog snapshot + Workspace + ResultContract
```

renderer 必须使用 QueryPlan 的实际指标、维度、事实粒度、JOIN 关系、过滤规则、
时间粒度和 `[start, end)` 边界。它不得读取模型候选 SQL、模型输出、历史结果或
用户对话摘要。

主 Gold 输出除了 SQL 和 SQL hash，还必须保存：

- `query_plan_sha256`；
- Catalog / metric / dataset / policy 版本；
- `required_result_columns`；
- `time_range` 与 `end_semantics`；
- 使用到的 metric、join program、physical table/column 依赖；
- deterministic renderer 版本和代码 hash。

### 3.2 辅助 Gold：Legacy QuerySpec Gold

辅助 Gold 使用历史 QuerySpec/Gold SQL，保持旧数据集和旧实验的可复现性。它只回答：

> 候选结果是否匹配历史标签定义？

如果 QueryPlan Gold 和 Legacy Gold 不一致，必须写入
`gold_disagreement`，并同时报告两个结果。不得选择匹配率较高的一套作为主结果。

## 4. 候选 SQL 的判定顺序

每个候选按以下顺序处理：

```text
共同 runtime prompt
→ unwrap_sql_completion()
→ QueryPlan time/shape adherence audit
→ SqlPolicy
→ workspace reader-only execution
→ ResultContract / ResultValidator
→ primary QueryPlan Gold denotation
→ legacy Gold denotation（辅助）
```

其中：

- `QueryPlan time/shape adherence audit` 检查每个指标计算块都使用计划声明的
  `[start, end)` 边界、`DATE_TRUNC` 粒度和结果列；
- denotation 相同不代表计划遵循。比如额外一天没有数据时，结果可能碰巧相同，
  但 `plan_adherent=false`；
- 通过 Policy、reader 和 ResultContract 只说明候选安全、可执行、可展示，不说明
  它一定符合业务计划；
- 候选、主 Gold 或辅助 Gold 执行失败必须保留错误状态，不能换 Gold、改标签或
  直接算模型失败。

## 5. 输出指标

主报告至少包含以下维度：

| 指标 | 含义 |
| --- | --- |
| `primary_ordered_denotation_match` | 候选与 QueryPlan Gold 的顺序和值均匹配 |
| `primary_bag_denotation_match` | 忽略行顺序后与 QueryPlan Gold 匹配 |
| `plan_adherent` | 候选 SQL 忠实使用计划中的边界、粒度、列和形状 |
| `legacy_ordered_denotation_match` | 候选与历史 QuerySpec/Gold 匹配 |
| `gold_disagreement` | 两套 Gold 的执行结果或合同不一致 |
| `valid_execution` | Policy、reader、ResultContract 均通过 |
| `route_rejected` | Router 没有准入候选生成 |
| `alignment_ambiguous` | 输入的自然语言与计划/标签不能确定对齐 |

headline 主指标为 `primary_ordered_denotation_match`，但必须同时报告
`plan_adherent`。以下情况不能被报告成模型语义提升：

- 只在 `legacy_ordered_denotation_match` 上提升；
- 只在去掉 ambiguous 样本后提升；
- 只在结果碰巧相同而 `plan_adherent=false` 的样本上提升；
- 只在 ResultContract valid 或 SQL 可执行率上提升。

## 6. 与历史 r3 的关系

历史 r3 保留以下事实：固定 1,500 条来源、模型输出、SQL hash、旧 Gold、reader
执行证据和 `safe-report.json` 均不可修改。r3 中 Olist 704 条时间问题的
source/Gold 端点不一致继续作为历史诊断记录。

本合同产生的 aligned release 必须使用新的 release 目录、manifest、inputs、
primary Gold、生成物和 report；不能覆盖 r3，也不能在看到 aligned 结果后替换
旧 headline。

## 7. 验收门

在任何 GPU 生成前，必须完成 CPU 侧检查：

1. 所有 ready 时间问题的 QueryPlan 与 ResultContract 都明确为 exclusive end；
2. QueryPlan Gold renderer 对小批样本生成稳定 SQL，重复运行 SQL hash 相同；
3. 主 Gold 通过 SqlPolicy、reader role、ResultContract/ResultValidator；
4. 主 Gold 与辅助 Gold 的 disagreement 有逐条 hash 和原因；
5. 每条候选可回溯到输入、prompt、计划、主 Gold 和辅助 Gold hash；
6. 原 r3 文件和模型权重 hash 与新 release 完全隔离。

只有 CPU 小批准入、SQL hash 稳定性和边界测试通过，才允许启动 Base/Adapter
aligned 生成。此前不训练、不做 DPO、不改变生产默认模型。

## 8. 当前决策

当前选择：**QueryPlan Gold 是主评测；Legacy QuerySpec Gold 是辅助评测**。

## 8.1 renderer 实现检查点

已实现 `src/data_analysis_agent/runtime_queryplan_renderer.py`。入口只接受运行时
`QueryPlan`、`ResultContract`、Catalog 和 Workspace，不读取离线 QuerySpec、训练标签、
候选 SQL 或历史结果。它复用已审阅的 Olist metric SQL registry 生成内部 CTE，并将
运行时计划的 hash、ResultContract hash、版本、时间边界、结果列和 `plan_adherent`
写入新的 runtime artifact；内部桥接用的 `query_spec_id` 不会进入 artifact 或 evidence。

当前 renderer 已覆盖：标量、多指标标量、客户州/商品品类分组、无维度时间序列，以及
“维度 + 时间粒度”组合。时间边界强制 `[start, end)`；ResultContract 的 metric ID
顺序可以不同，但结果列顺序必须和 QueryPlan 完全一致。Top-N、比较基线、支付方式等
尚未有独立确定性 renderer，遇到时 fail closed，不改写成近似查询。

同时修正了既有 Olist renderer 在运行时 workspace 上的版本解析，并补充了维度+时间
序列的结果列和复合 join key 支持；历史离线 QuerySpec 形状不变。

CPU 定向验证：`tests/test_runtime_queryplan_renderer.py` 与
`tests/test_olist_queryspec.py` 共 `54 passed`；定向 Ruff 通过。三条运行时样例已
通过 SqlPolicy（1,000 行执行上限）和稳定 hash 检查。尚未执行 PostgreSQL 小批准入，
也未启动 GPU 生成；下一项是从实际 aligned validation 输入选取小批，运行 renderer、
SqlPolicy、reader role、ResultContract/ResultValidator 并保存逐条证据。

## 8.2 Olist validation 小批准入

已从冻结的 `runtime-candidate-sql-v2-r1/validation.jsonl` 按四种结果形态各选 4 条，
共 16 条 Olist validation 输入；选择清单和逐条证据位于仓库外：

`/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-gold-admission-20261012-v2/`

16/16 通过 runtime renderer、SqlPolicy、`daa_analytics_reader` 的 PostgreSQL
read-only / `REPEATABLE READ` 执行和 ResultValidator；标量、客户州分组、商品品类分组、
时间序列各 4/4。全部未达到 1,000 行执行上限，也未超过 200 行展示预算。报告只保留
sample_id、QueryPlan/ResultContract/SQL hash、版本、reader 身份、行数和验证状态，不保留
原始结果行。时间序列额外验证了 day/week/month/quarter/year 中的四种粒度和 `[start,end)`
谓词；其中无维度时间序列的 runtime `time, metric...` 列序通过最终投影重排与验证。

第一版小批在时间序列上暴露了旧离线 renderer 的 metrics-first 与 runtime time-first
列序差异；该问题已修复并由测试锁住，未把失败样本静默计为通过。下一项仍是扩大到按
scenario family 的小批，补上未冻结能力的拒绝分类，再决定是否物化完整 aligned Gold。

随后按 `scenario_family_id` 做了第二轮 32 条 Olist validation 小批。它用风险标签、
结果形态和时间粒度做确定性 greedy set-cover，覆盖 32 个不同 family、9 类风险标签，
并覆盖 day/week/month/quarter/year 及无时间粒度；32/32 再次通过 renderer、Policy、
reader-only PostgreSQL 和 ResultValidator。形态比例由 family 自然分布决定（19 条时间
序列、6 条客户州、6 条标量、1 条品类），因此它是 family/risk 准入，不替代上一轮四种
形态各 4 条的平衡 smoke。证据目录为仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-gold-admission-20261012-v3/`。
