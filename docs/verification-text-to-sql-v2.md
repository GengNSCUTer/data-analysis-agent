# Text-to-SQL v2 验证记录

> 验证日期：2026-08-06
> 运行环境：`/disk2/gengnan/conda_envs/data-analysis-agent`，Python 3.12
> 目标：验证第二轮可靠性改造的确定性边界；不把 SQL 可执行率当成在线模型语义准确率。

## 1. 基线事实

| 项目 | 当前值 |
| --- | --- |
| Agent/交互 | vendored Vanna 2.0.2、FastAPI/SSE、原生 `<vanna-chat>` |
| 模型配置 | OpenAI-compatible SiliconFlow；默认 `deepseek-ai/DeepSeek-V4-Flash` |
| 数据库 | 本项目独立 PostgreSQL `data_analysis_agent`，loopback `127.0.0.1:35434` |
| SQL 方言 | PostgreSQL；`sqlglot` AST Policy；分析查询使用 `daa_analytics_reader` |
| 语义目录 | `olist-catalog-v1`，9 张表（8 张分析表 + 1 张 admin-only 元数据表）、4 个指标、7 条 Join |
| 请求预算 | 默认最多 4 次工具迭代、4 次工具调用、2 次 SQL、1 次图表调用、4000 输入字符、1200 输出 token |
| 运行证据 | `app.agent_runs.catalog_trace` JSONB；会话 `app.conversations.working_memory` JSONB；ToolContext 中的服务器 `ResultContract` 和版本字段 |

## 2. 已实现闭环

```text
server user + question
  -> role-scoped deterministic Catalog
  -> route: answerable / clarification / refusal
  -> structured working memory
  -> one SQL candidate through AST + reader role
  -> sanitized execution failure / one bounded repair candidate
  -> repair candidate through AST + reader role again
  -> deterministic result validation
  -> evidence-backed answer or safe refusal
```

当前代码边界：

- `semantic_catalog.py` 将问题映射到有限的指标、表、列和 Join，并记录不含原文的
  fingerprint/selection trace；小上限不会截断指标必需的列或 Join，无法完整提供上下文时 fail closed。
- `metric_context.py` 不再重复注入完整 Olist Schema，只保留数据版本、PostgreSQL 只读边界、
  Prompt injection 边界、证据要求和图表规则；Trusted Demo 通过 `CatalogContextEnhancer` 注入请求切片。
- `QuestionRouter` 在 SSE 前判断缺失时间、指标、比较基线、越权和不支持能力；澄清轮次不调用
  Agent、不消耗 SQL/tool budget，并把问题和缺失字段写入用户所属会话。
- `WorkingMemory` 只接受结构化服务端字段（指标、时间、维度、筛选、比较基线、上一结果摘要），
  不从助手自然语言猜测；补充时间后能恢复原指标。
- `ResultContract` 在 Catalog/WorkingMemory 之后由服务器构建，向 `ToolContext` 传递指标列、合法时间
  别名、请求时间范围、选中的 Join 和 `catalog/dataset/metric/policy` 版本；客户端同名 metadata 会被
  服务器派生值覆盖。Catalog Prompt 要求指标使用 `metric_id` 别名、时间分组使用 `time` 别名。
- `TrustedRunSqlTool` 在不修改 Vanna Agent 核心循环的前提下包装原生 `RunSqlTool`：错误只传稳定类别
  和安全提示，候选 SQL 必须重新通过完整 `SqlPolicy`，并由 reader role 重执行；`postgres_runner.py`
  对数据库异常向 Agent 暴露安全分类，不泄漏驱动堆栈。
- `ResultValidator` 将空结果、缺少指标列、非有限数值、时间越界、行数截断和 Join 放大表示为
  `valid`、`needs_clarification` 或 `refuse`；失败不得变成确定性数字。

## 3. 自动化验证

在 Conda 环境执行：

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python -m pytest -q \
  tests/test_semantic_catalog.py tests/test_question_router.py \
  tests/test_working_memory.py tests/test_sql_repair.py \
  tests/test_result_validator.py tests/test_text_to_sql_contracts.py \
  tests/test_budget.py tests/test_context_builder.py tests/test_llm_context_enhancer.py
```

本轮确定性测试覆盖：

- Catalog 版本/Policy 白名单一致、别名排序、Unicode NFKC、角色隔离、Prompt injection、零命中、
  YAML 缺字段/未知表列/敏感字段/未知 Join fail closed；
- `max_tables`、`max_columns_per_table`、`max_joins` 和 `max_prompt_chars` 上限不会提供不完整语义对象；
- Catalog enhancer 使用实际请求切片并记录 trace，trace 和预算证据不包含原始问题；
- Router 的 answerable/missing_time/missing_metric/missing_comparison/unauthorized/unsupported；
- 澄清轮次 `sql_calls_used == 0`、`tool_calls_used == 0`，且 Agent 不被调用；
- working memory 的指标/时间跨轮次恢复和未知字段丢弃；
- 错误分类、一次修复、候选 SQL Policy 二次校验、修复提示长度和无原始错误泄漏；
- 结果缺列、空集、非有限数值、行数截断、时间覆盖和 Join 放大拒答。

当前专项回归结果：本轮指定集合在前一阶段为 `84 passed`，随后补充证据路由、QueryPlan、可信
结果摘要和比较基线恢复回归后，当前专项集合为 **88 passed**。项目 PostgreSQL
`test_postgres_run_recorder.py` 与 `test_postgres_runner.py` 为 `4 passed`；固定 SSE 浏览器多轮
澄清回归为 `1 passed, 6 deselected`。ruff、compileall 和 `git diff --check` 均通过。没有把这些
确定性结果写成在线模型语义准确率。

新增 v2 路由/计划 golden 评测：

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python \
  scripts/run_text_to_sql_evaluation.py \
  --output evals/reports/text_to_sql_v2_deterministic.json
```

结果为 **60/60 cases passed (0 failed)**。评测覆盖帮助、指标定义、通用业务/知识、单指标与
多指标计划、维度与时间、澄清、WorkingMemory、结果追问、越权和不支持能力。报告只保存结构化
路由/计划字段和 case ID，`evals/reports/` 不提交 Git；这证明确定性合同稳定，不代表在线
SiliconFlow 的 SQL 或语义准确率。

新增浏览器回归 `test_desktop_window_supports_all_resize_directions` 和
`test_long_history_markdown_keeps_a_scrollable_view_after_restore` 后，完整
`tests/e2e/test_trusted_embedded_window.py` 在 `RUN_VANNA_E2E=1 RUN_PROJECT_DB=1` 下为
**9 passed**；Web Component `npm run build` 通过（38 modules transformed）。

项目 PostgreSQL 连接可选验证：

```bash
RUN_PROJECT_DB=1 /disk2/gengnan/conda_envs/data-analysis-agent/bin/python -m pytest -q \
  tests/test_postgres_conversation_store.py tests/test_postgres_run_recorder.py \
  tests/test_postgres_runner.py
```

该验证包含 `working_memory` 会话 JSONB 往返、`catalog_trace` Agent Run 字段、reader/writer 跨
Schema 权限和真实 Olist 查询审计。它不调用 SiliconFlow，不代表在线模型语义准确率。

## 4. “一次受限 SQL 修复”的含义

这里的“一次”不是说系统永远只能修复一次，而是当前版本对每个用户请求设置的安全上限：

```text
候选 SQL
  -> Policy + reader role 执行
  -> 仅对可恢复错误生成 1 个脱敏修复候选
  -> 修复 SQL 再过 Policy + reader role + ResultContract/Validator
  -> 成功回答，或可信拒答
```

因此一次请求最多有 **2 次 SQL 执行**（初始候选 + 1 次修复），而不是允许模型无限试错。
当前只把 `syntax`、`unknown_column`、`unknown_table` 和 `ambiguous_reference` 视为可恢复；
`timeout`、`permission`、`connection` 和 `unknown` 不重试。每个修复候选都必须重新经过 AST
策略、对象白名单、敏感列、PostgreSQL reader role、超时、行数上限、结果合同和结果校验。

是否增加到两次修复必须由在线评测决定，至少比较初始成功率、一次修复恢复率、第二次修复的
增量恢复率、修复导致的语义错误率、P50/P95 延迟以及 token/tool 成本。即使未来将配置设为
`max_repair_attempts=2`，也必须保持候选去重、继承同一 `QueryPlan`/`ResultContract`、全局
预算和最终可信拒答；当前默认值仍为 1。

## 5. 对抗性审查结果

| 攻击/失败模式 | 控制点 | 当前状态 |
| --- | --- | --- |
| 问题文本伪造 `role=admin` | 角色只来自服务器解析的 `User` | 已测试 |
| Prompt injection 要求读取 `app.query_audits`/文件 | Catalog 白名单 + AST Policy + reader role | 已测试 |
| 检索上限丢失指标列/Join | 必需对象优先，连接路径固定点，超限拒答 | 已测试 |
| 澄清后只剩补充句 | 会话 `working_memory` 记录结构化指标/时间 | 已测试（服务层 + 固定 SSE 浏览器回归） |
| 执行异常泄漏驱动信息 | `sanitize_sql_error`/`SafeSqlExecutionError` | 已测试 |
| 修复 SQL 绕过安全层 | `TrustedRunSqlTool` 重新 `SqlPolicy.evaluate` 并由 reader role 重执行 | 已测试 |
| 空结果/截断结果被写成数字 | `ResultValidator` 状态门 | 已测试 |
| 并发用户共享 trace | `BudgetUsage`/会话状态按请求和 user 隔离；无进程级 last-result | 设计与单测覆盖 |

## 6. 在线人工标签证据（2026-08-18）

新增 `evals/cases/text_to_sql_online_v1.yaml`、
`scripts/run_online_text_to_sql_evaluation.py` 与对应合同测试。在线清单从 60 条离线 golden
中选择 24 条，真实请求必须经过 `POST /api/project/demo-session` 与
`POST /api/vanna/v2/chat_sse`，随后用同一 request ID 从 `app.agent_runs` 和
`app.query_audits` 读取脱敏运行证据。报告及人工标签文件位于 `evals/reports/`，目录被 Git
忽略；其内容不保存问题、回答、SQL、行数据、cookie 或密钥。

24/24 请求都有 Agent Run，人工标签已覆盖 route、clarification、SQL executability、metric
semantics、ResultContract、permission compliance、answer grounding、repair count、latency 与
token status。汇总如下：

| 维度 | 结果 | 说明 |
| --- | --- | --- |
| 路由 | 23 pass / 1 fail | `multi_003` 的 Catalog slice 不完整，未完成查询。 |
| 澄清 | 3 pass | 三条缺时间/指标/比较基线问题均未调用 SQL。 |
| SQL 可执行 | 13 pass / 11 N/A | 13 条数据问题至少有一条 allowed SQL。 |
| 指标语义 | 12 pass / 1 fail / 11 N/A | `data_010` 自行采用首笔支付归因，Catalog 未冻结该口径。 |
| 结果合同 | 11 pass / 2 fail / 11 N/A | `data_014`、`data_016` 的无关第二 SQL 缺合同列，被安全阻断。 |
| 权限合规 | 24 pass | 两次敏感列候选均被 Policy 拒绝，最终查询没有越权。 |
| 回答有据 | 17 pass / 7 fail | 包含币种误标、趋势夸张、未冻结支付归因和超时未完成。 |

`repair_attempted` 在 24 条中均为 false；本轮没有修复恢复率证据。token 对每条都被记录为
数值或 `unknown`：仅两条通用问答返回 usage（total 1,343 与 1,213），其余 22 条为
`unknown`，绝不能当作零成本。总客户端时延为 1,374,945 ms；每条单次运行、且模型服务波动
显著，不把它转写为 P50/P95 或在线准确率。

## 7. 修复后回归（2026-08-18）

本轮将在线人工标签暴露的问题转换为通用合同：Catalog 的多跳 Join 图闭包、指标维度归因策略、结果合同通过后的冗余 SQL 抑制，以及工作区级币种/趋势展示约束。Olist 只提供 YAML 适配器元数据，运行时代码不识别评测 case 或数据集名称。

验证结果：语义 Catalog、QuestionRouter、预算、Trusted SQL 工具及路由评测专项 **71 passed**；`scripts/run_text_to_sql_evaluation.py` **60/60 passed**。全量 pytest 中的失败属于仓库内 Vanna 上游可选集成测试缺少 `ollama`、`chromadb`、数据库驱动或 fixture，不是本项目本轮改动引入的失败。

2026-08-19 追加真实数据库与 SSE 边界测试：第一条有效 SQL 在真实 PostgreSQL 通过 `ResultValidator` 后，固定模型第二轮仍返回 `run_sql`；`BudgetSafetyMiddleware` 在 LLM 响应边界删除该调用，`BudgetedToolRegistry` 保持为兜底。Agent Run 最终仅记录 1 次 SQL/工具调用、1 条 allowed 审计、`completed` 终止原因和 `result_contract_satisfied`/`extra_sql_suppressed` 证据。项目专项为 **75 passed**，v2 golden 仍为 **60/60**；该固定回归不消耗 SiliconFlow，也不用于声称在线模型性能。

## 8. 未完成与限制

- 当前 `ResultValidator` 的指标列、时间列、请求时间范围和 Join 元数据已经由服务器从
  Catalog/WorkingMemory 构建为 `ResultContract` 并传入 `ToolContext`；版本合同也已进入 Catalog Prompt、
  固定系统 Prompt、Agent Run trace 和 SQL 审计。`TrustedRunSqlTool` 已把原始 SQL、修复候选、错误类别、
  Policy 状态、reader 重执行、结果校验和终止原因收敛为 `repair_evidence`，并持久化到 Agent Run/query audit。
- 已完成一次 24 条真实 SiliconFlow 人工标签小样本，但它不是多次重复、前后对照或全量 v2
  评测，不能据此发布总体在线准确率、token 成本或 P95 延迟数字。
- Vanna 的工具循环仍由 Agent 驱动；项目层只包装 `RunSqlTool`，不修改 Vanna 核心循环。修复候选默认最多一次：
  每个请求最多一次修复、最多两次 SQL 执行，且只有语法/未知列/未知表/歧义引用等可恢复错误进入修复；超时、
  权限、连接和未知错误直接终止。尚未用真实 SiliconFlow 批量运行来测量修复成功率、语义准确率、token 成本
  或 P95 延迟，因此不应直接把上限提高到多次。
- `evals/cases/text_to_sql_v2.yaml` 和 `scripts/run_text_to_sql_evaluation.py` 已提供 60 条离线路由/QueryPlan
  golden；在线小样本已暴露币种格式、通过合同后的多余 SQL、Catalog slice 和支付归因四类问题，下一项是
  修复后对这批失败样本做固定回归，而不是继续堆叠更多离线规则。
- 浏览器已用固定 SSE mock 完成“本月销售额 -> 补充日期 -> 后续指标追问”多轮回归；该测试避免在线模型波动，
  不能替代真实模型评测。
- Olist 仍是当前 adapter/展示案例，`WorkspaceProfile` 尚未通过第二个真实数据集验证；演示会话不是生产认证，
  尚未实现组织级 RLS。
- 演示会话不是生产认证，尚未实现组织级行范围策略。
- 上游可选数据库/LLM 集成的全量测试不属于本项目质量门；缺少可选依赖的失败不作为本轮回归结论。
