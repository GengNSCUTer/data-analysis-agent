# Text-to-SQL v2 验证记录

> 验证日期：2026-08-03
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
| 运行证据 | `app.agent_runs.catalog_trace` JSONB；会话 `app.conversations.working_memory` JSONB |

## 2. 已实现闭环

```text
server user + question
  -> role-scoped deterministic Catalog
  -> route: answerable / clarification / refusal
  -> structured working memory
  -> one SQL candidate through AST + reader role
  -> sanitized execution failure / bounded repair contract
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
- `OneShotSqlRepair` 对错误只传稳定类别和安全提示，候选 SQL 必须重新通过完整 `SqlPolicy`；
  `postgres_runner.py` 对数据库异常向 Agent 暴露安全分类，不泄漏驱动堆栈。
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

项目 PostgreSQL 连接可选验证：

```bash
RUN_PROJECT_DB=1 /disk2/gengnan/conda_envs/data-analysis-agent/bin/python -m pytest -q \
  tests/test_postgres_conversation_store.py tests/test_postgres_run_recorder.py \
  tests/test_postgres_runner.py
```

该验证包含 `working_memory` 会话 JSONB 往返、`catalog_trace` Agent Run 字段、reader/writer 跨
Schema 权限和真实 Olist 查询审计。它不调用 SiliconFlow，不代表在线模型语义准确率。

## 4. 对抗性审查结果

| 攻击/失败模式 | 控制点 | 当前状态 |
| --- | --- | --- |
| 问题文本伪造 `role=admin` | 角色只来自服务器解析的 `User` | 已测试 |
| Prompt injection 要求读取 `app.query_audits`/文件 | Catalog 白名单 + AST Policy + reader role | 已测试 |
| 检索上限丢失指标列/Join | 必需对象优先，连接路径固定点，超限拒答 | 已测试 |
| 澄清后只剩补充句 | 会话 `working_memory` 记录结构化指标/时间 | 已测试（服务层）；浏览器多轮待补 |
| 执行异常泄漏驱动信息 | `sanitize_sql_error`/`SafeSqlExecutionError` | 已测试 |
| 修复 SQL 绕过安全层 | `OneShotSqlRepair` 重新 `SqlPolicy.evaluate` | 已测试；reader 执行回链待接入 |
| 空结果/截断结果被写成数字 | `ResultValidator` 状态门 | 已测试 |
| 并发用户共享 trace | `BudgetUsage`/会话状态按请求和 user 隔离；无进程级 last-result | 设计与单测覆盖 |

## 5. 未完成与限制

- 当前 `ResultValidator` 的指标列、时间列、请求时间范围和 Join 元数据已定义为运行时合同，仍需从
  Catalog/WorkingMemory 传入 `ToolContext` 并在真实 PostgreSQL 查询中验证；Prompt 需要显式展示
  Catalog、数据集、指标、策略和提示版本，才能形成完整可回放证据。
- 尚未用真实 SiliconFlow 批量运行 v2 评测集，因此没有在线准确率、token 成本或 P95 延迟数字。
- Vanna 的工具循环仍由 Agent 驱动；需要下一轮把 `OneShotSqlRepair`、原/修复 SQL 和
  `ResultValidation` 证据完整接到生命周期中，并保证第二次失败直接拒答。
- 尚未完成“本月销售额 -> 补充日期 -> 后续指标追问”的浏览器 Playwright 回归；已有服务层测试。
- 演示会话不是生产认证，尚未实现组织级行范围策略。
- 上游可选数据库/LLM 集成的全量测试不属于本项目质量门；缺少可选依赖的失败不作为本轮回归结论。
