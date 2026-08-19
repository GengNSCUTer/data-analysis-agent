# Data Analysis Agent — 协作与迭代规则

本文件约束在本仓库内执行的所有后续工作。若用户当前指令与本文件冲突，以用户最新指令为准；否则必须遵守以下流程。

## 1. 项目事实来源

- `PROJECT.md` 是需求、架构、数据策略、技术决策、阶段路线和变更记录的本地基线；
- 飞书项目文档（<https://my.feishu.cn/docx/QIr2dfKp7oIJvqxcPerckYd6nfC>）是用户可直接查看的同步项目记录；
- GitHub 仓库 `GengNSCUTer/data-analysis-agent` 是项目代码、可复现脚本、文档和提交历史的唯一远端；
- Vanna 已按当前项目决策合并进本仓库，仓库同时保留 `upstream` 远端用于跟踪上游；不要再创建第二个自有 Vanna 仓库。项目不再建设独立 Next.js/TailAdmin 前端，`<vanna-chat>` 是嵌入既有业务页的唯一交互基座。

## 2. 每次迭代的强制闭环

每次完成一个有意义的迭代（需求变化、设计决策、功能、测试、数据管道、Bug 修复、发布准备）时，按下列顺序执行：

1. **开始前记录**：读取 `PROJECT.md` 与飞书项目文档的相关部分，明确本轮目标、范围、验收条件和不做项。若需求或架构发生变化，先更新设计记录再编码。
2. **实施与验证**：只实现本轮范围内的内容；运行与变更相称的格式化、静态检查、单元/集成/E2E 测试，并记录结果。
3. **本地项目文档更新**：更新 `PROJECT.md` 的需求、设计、路线图、决策或变更记录；必要时新增 ADR、评测报告或数据字典。不要只改代码不留决策依据。
4. **飞书同步**：在飞书项目文档追加本轮迭代记录，至少包含日期、目标、实现内容、设计决策、验证结果、风险/限制和下一步。重大架构变更须同步正文对应章节，而不只写日志。
5. **Git 更新**：检查 `git diff` 与 `git status`；只提交本轮相关文件。提交信息使用清晰的 Conventional Commit 风格，例如 `feat(sql-policy): add AST allowlist validation`。
6. **远端推送**：在确认提交内容不含密钥、原始受限数据、缓存和构建产物后，推送到 `origin`。若推送失败，保留本地提交并在飞书迭代记录中注明状态，不能假称已同步。
7. **交付汇报**：说明完成内容、验证证据、飞书文档链接/更新位置、Git commit SHA、是否已推送，以及仍存在的限制。

小型纯讨论不必产生 Git commit；一旦形成实际项目决策，必须更新 `PROJECT.md` 和飞书记录。

## 3. 安全、数据与开源边界

- 永远不要提交 API Key、数据库密码、访问令牌、`.env`、模型密钥或含敏感信息的日志；提供 `.env.example` 代替。
- 原始第三方数据集、大型 CSV/Parquet、Kaggle 下载内容、数据库 dump 不得提交 Git。提交数据集 manifest、下载/加载脚本、DDL、转换脚本、许可证与署名说明、小型合成 fixture。
- 使用第三方数据和代码时，记录来源、许可证、版本/commit 和必要的署名；不得把 Olist 或其他境外数据描述为中国真实平台数据。
- 分析数据库必须使用独立只读账号；应用元数据账号和分析查询账号不得混用。
- 不得用字符串黑名单替代 SQL AST 安全策略。所有用户/模型产生的 SQL 都必须经过 AST、对象白名单、单语句、LIMIT 和超时策略。

## 4. v1 架构约束

- 当前基线：Python 3.12、仓库内 Vanna 2.0.2、FastAPI、Vanna 原生 `<vanna-chat>`，先通过 `examples/siliconflow_sqlite_web_demo.py` 跑通最小闭环；
- 后续目标后端：Python 3.12、FastAPI、Vanna、PostgreSQL、SQLAlchemy、Alembic、`sqlglot`、pytest；
- 后续目标前端：原生 Vanna `<vanna-chat>` Web Component；通过宿主页 HTML/CSS、元素属性和浏览器事件完成浮动/侧栏形态、中文文案与业务结果呈现，优先不改 Vanna 组件核心；
- 交互：当前使用 Vanna 原生 SSE 返回进度、表格和结论；后续扩展 SQL、图表和证据对象；
- v1 持久化：PostgreSQL；没有实际缓存、限流、异步导出或多实例协调需求时，不引入 Redis；
- v1 不做多 Agent、MCP、任意 Python 代码执行、写库操作或多数据库方言支持。

## 5. 质量门槛

- 每个成功分析结果必须可追溯到指标口径、最终 SQL、来源表/字段、结果摘要和策略记录；
- 安全测试必须覆盖写操作、多语句、越权对象、无界查询和注释绕过；策略拒绝必须有可读原因；
- 新增或修改指标时，必须同步数据字典、指标版本和相应评测用例；
- 新增功能至少有与风险匹配的测试；不要以“手工页面能打开”代替后端安全/语义测试；
- 任何声称的准确率、延迟、拦截率或性能数据，必须有对应评测用例、运行配置和结果记录。

## 6. 文档与命名约定

- 文档、API、数据模型和代码使用清晰的中英文术语；对外展示使用中文业务语言，并保留原始数据字段映射；
- 长期设计变更可以在 `docs/adr/` 中新增 `NNNN-title.md`；
- 数据集相关内容位于 `data/` 下，但仅提交 manifest、schema、transforms 与 fixtures，不提交原始数据；
- 自有目录约定为 Vanna 源码根目录、`examples/`、`src/data_analysis_agent/`、`tests/`、`docs/`、`data/`、`evals/` 和 `infra/`；本仓库是唯一开发、提交和推送位置。若仍保留 `/disk2/gengnan/_upstream/tailadmin-nextjs-dashboard/`，它只作历史参考，严禁在其中实现本项目业务代码；
- 文档更新应陈述已验证事实与未决假设，不能把计划描述成已实现能力。

## 7. 最近一次同步记录

### 2026-08-18：真实 SiliconFlow 人工标签评测

- 目标：将已完成的前端加固、v2 确定性资产收敛为可复核的真实模型小样本，不把 SQL 执行成功伪装成语义正确。
- 实现：新增 24 条 online v1 清单和通过 Trusted Demo SSE 的运行器；报告按 request ID 回读 Agent Run/SQL 审计，逐条记录路由、澄清、SQL、指标口径、结果合同、权限、回答有据、工具/SQL/修复次数、时延和 token 状态。原始问题、回答、SQL、结果行、cookie、密钥和运行报告均不提交 Git。
- 结论：24/24 Agent Run；路由 23 pass/1 fail，权限 24 pass，回答有据 17 pass/7 fail。`data_014`/`data_016` 的首个正确 SQL 后续被无关 SQL 破坏，ResultValidator 已安全阻断；`multi_003` 因 Catalog slice 遗漏可用 Join 而在 180 秒后未完成。当前没有 repair lifecycle recovery；provider 未返回 usage 时明确记为 unknown。
- 后续：先修复币种、已通过合同后的工具停止、多指标 Catalog slice 和支付归因；修复后对已失败 case 做固定回归，再评估多次修复或延迟优化。

### 2026-08-18：通用语义检索与结果合同加固

- 目标：将在线评测暴露的币种误标、多跳 Catalog 缺表、支付维度自行猜口径和合同通过后重复 SQL，收敛为可复用的 Workspace/Catalog/预算机制。
- 实现：Catalog 支持工作区币种元数据与 `DimensionPolicy`；检索在可见 Join 图上做 BFS 路径闭包，受表、Join、列和 Prompt 预算约束；QuestionRouter 对声明为歧义的维度返回零 SQL 澄清；ResultValidator 成功后标记 `result_contract_satisfied`，预算层拦截后续冗余 `run_sql` 并保留可用图表调用；Prompt 增加币种和保守趋势表述合同。没有加入 Olist 问题文本特判。
- 验证：相关专项测试 71 passed；`run_text_to_sql_evaluation.py` 60/60 passed；全量 pytest 的失败来自 Vanna 上游可选依赖/缺失 fixture，不计入本项目质量门。
- 后续：补充真实 runner 对合同状态的集成证据，复跑固定失败样本，随后再评估在线模型延迟、usage 采集和第二数据集适配。

### 2026-08-19：真实 PostgreSQL 合同链路集成测试

- 目标：验证结果合同状态不是仅存在于单元测试或内存对象，而是能沿真实 SQL 工具链和 Agent Run 持久化链路闭环。
- 实现：新增 `test_result_contract_state_flows_through_real_runner_and_budget_registry`，使用项目专属 PostgreSQL 执行真实有效订单聚合，经 `ResultValidator` 通过后检查 `ToolContext`/`BudgetUsage` 状态；再次提交 `run_sql` 时由 `BudgetedToolRegistry` 抑制，不增加 SQL/tool budget，且查询审计不产生第二条记录。运行记录将合同通过和冗余 SQL 抑制计入既有 `catalog_trace` JSONB，不改数据库表结构。
- 验证：`RUN_PROJECT_DB=1 DATA_ANALYSIS_POSTGRES_HOST=/tmp pytest tests/test_postgres_runner.py tests/test_postgres_run_recorder.py` 为 **5 passed**；ruff、compileall 和 diff check 通过。
- 风险/下一步：测试验证的是确定性真实数据库链路，不包含在线模型多轮行为；后续可针对真实 SSE 请求补充一条模型发出重复工具调用的固定 mock 回归。

### 2026-08-19：SSE 模型响应合同回归

- 目标：验证冗余 SQL 在模型响应边界被抑制，而不只依赖工具注册表的最后防线。
- 实现：用固定 `LlmService` 驱动真实 `Agent`、`BudgetedChatHandler`、PostgreSQL ConversationStore/RunRecorder 和生产 SQL 工具链；同一请求中第一轮模型调用受控 SQL，第二轮模型再次返回 `run_sql`。`BudgetSafetyMiddleware` 移除第二轮工具调用，Agent 正常完成。
- 验证：真实数据库专项由 5 项扩展为 **6 passed**；合并预算、Catalog、路由、SQL 工具、PostgreSQL 与运行记录专项为 **75 passed**，v2 golden **60/60 passed**。断言 Agent Run 为 1 次 SQL/工具调用、`completed`、合同/抑制证据完整，且仅有 1 条 allowed 审计。
- 后续：接着处理在线模型的 token usage 可观测性和长响应超时边界；仍不据固定模型回归声称在线准确率或延迟分位数。

### 2026-08-19：LLM 可观测性与 6 条定向 SiliconFlow 复测

- 实现：`ObservedLlmService` 统一 asyncio/OpenAI/httpx timeout，记录 bounded `llm_observations`；Vanna OpenAI-compatible 同步调用移到工作线程，避免阻塞事件循环。新增 targeted manifest 和 `--allow-small-sample`，默认批量评测门槛仍为 20--30 条。
- 验证：LLM/线程/评测器单测 **8 passed**；项目 PostgreSQL/run recorder **10 passed**；真实 SSE 定向复测 **6/6 Agent Run、0 客户端错误**，路由 6/6、SQL 可执行 5/5、结果合同 5/5、权限 6/6，5 条查库请求各 1 条 SQL，usage 均 reported，总客户端耗时 499,174 ms。人工语义/最终 grounded 仍有 3 条 pending，不发布在线准确率或 P50/P95。
- 边界：在线模型仍可能有较高延迟；人工标签尚未覆盖全部定向结果；报告只保存在被忽略的 `evals/reports/`，不含问题、回答、SQL、数据行或密钥。下一步是将定向失败转成固定回归并优化模型轮次/缓存，而不是扩大 SQL 修复次数。

### 2026-08-19：可信结果确定性收口

- 实现：未显式要求图表时，已通过 `ResultValidator` 的 SQL 结果由服务端依据结果合同收口，不再调用模型生成最终摘要；收口不产生趋势、币种或因果判断。图表请求显式禁用收口，保留 `visualize_data`。运行台账和在线脱敏评测均记录 `deterministic_result_finalized`。
- 验证：真实 `data_005` SSE 回归为 `deterministic_result_finalized=true`、1 条实际 PostgreSQL 执行、2 次模型调用、68,516 ms；首次模型 SQL 被 AST Policy 以敏感 `order_id` 拒绝，第二次有效，因此两轮不是冗余总结。单测 25 passed（1 skipped）、PostgreSQL 10 passed、v2 60/60。
- 边界：这是一个定向单次时延观测，不是 P50/P95；服务端收口优先保证“表格可核对、结论不越界”，更丰富的业务解释应通过后续明确追问和可信结果摘要完成。

### 2026-08-06：证据路由、QueryPlan 与可信结果记忆

- 目标：按 Text-to-SQL 改造顺序拆开“是否命中指标”和“是否允许查库”，补齐多指标查询的服务器计划，并让结果追问只依赖可信结果证据。
- 实现：`QuestionRouter` 新增 `intent`、`requires_database`、`evidence_mode`、`confidence` 和 `reason_code`，覆盖帮助、指标定义、通用业务/知识、数据查询、数据分析、混合请求、结果追问和澄清；通用回答使用 `_send_llm_request` 的 `tools=None` 边界，帮助/指标定义走确定性回答。新增 `QueryPlan`，把多指标标量概览约束为每指标独立聚合后 `CROSS JOIN`，把分组维度和时间列加入 `ResultContract` 与 ToolContext。`ResultValidator` 成功后生成有界摘要，`BudgetUsage`、Agent Run trace 和 `WorkingMemory.previous_result_summary` 均可记录；没有可信摘要的结果追问会先澄清。
- 验证：QuestionRouter、QueryPlan、WorkingMemory、ResultValidator、预算、TrustedRunSqlTool、SQL Policy 和修复专项 **88 passed**；`ruff check`、`compileall`、`git diff --check` 通过。全量 Vanna 上游可选驱动测试仍受环境依赖影响，未作为本项目质量门；未运行在线 SiliconFlow 批量语义评测。
- 设计决策：指标命中不等于查库；通用回答不得看到 SQL/图表工具；QueryPlan 当前是服务器拥有的 grounding/result contract 和 Prompt 约束，尚不是完整 SQL AST 形状证明；结果摘要只来自通过 ResultValidator 的 DataFrame，不接受助手文本或客户端 metadata。
- 风险/限制：仍需真实模型路由/多指标批量评测，QueryPlan 的 CTE/Join 形状还要在后续 AST 检查中增强；结果追问目前只解释有限样例摘要，不是任意历史结果回放；Olist 仍是当前 adapter，演示 cookie 不是生产认证。
- 下一步：建立版本化 v2 路由/语义 golden，先做固定问题集上的人工核验，再决定是否加入低置信度结构化分类器和更强的 QueryPlan 执行校验。

### 2026-08-06：通用工作区与一次 SQL 修复生命周期

- 目标：完成本轮四项优化，明确 Olist 适配层与通用分析 Agent 核心的边界，并把一次 SQL 修复从独立契约接入 Vanna 工具生命周期。
- 实现：新增 `WorkspaceProfile`；`SqlPolicy`、`CatalogLoader`、`SecurePostgresRunner` 和预算处理器读取工作区配置。新增 `TrustedRunSqlTool`，失败 SQL 只经过一次脱敏修复；修复候选重新通过 AST Policy，由 PostgreSQL reader role 重执行，成功后再过 `ResultValidator`，二次失败直接可信拒答。`query_audits`、`agent_runs` 和预算记录保存 `repair_evidence`。
- 测试：专项集合 `84 passed`；项目 PostgreSQL `test_postgres_run_recorder.py` 与 `test_postgres_runner.py` 为 `4 passed`；固定 SSE 的浏览器多轮澄清回归 `1 passed, 6 deselected`；ruff、compileall、`git diff --check` 通过。未将这些确定性结果写成在线 LLM 语义准确率。
- 边界：Olist 仍是当前 adapter/展示案例，尚无第二真实数据集；演示 cookie 不是生产认证，未实现组织级 RLS；尚未做 SiliconFlow 批量修复成功率和 token/P95 评测。
- 下一步：建立版本化 v2 评测集，先用固定 Olist golden 和人工标签核验真实模型，再决定是否引入更复杂的 schema retrieval、judge 或多候选策略。

### 2026-08-06：嵌入式可靠性与延迟定位

- 目标：处理 `/embedded-demo` 的窗口恢复布局、非数据库问题误走 SQL、历史消息 Markdown 原样显示和响应过慢四项反馈。
- 实现：SQL Policy 增加多 CTE 输出列/外层别名识别，并修复 scalar subquery 关联键的敏感列误判；`QuestionRouter` 对纯指标定义/统计口径问题直接从 Catalog 返回 `catalog_answered` Markdown；历史 assistant 消息与流式文本共用转义后渲染的 Markdown renderer；宿主页通过双 `requestAnimationFrame` 与 `ResizeObserver` 同步组件高度。预算台账增加 `route_catalog`、`llm_request`、`sql_policy`、`postgres_sql` 阶段耗时证据。
- 验证：Web Component `npm run build` 通过；相关后端专项 **63 passed, 1 skipped**；Playwright `tests/e2e/test_trusted_embedded_window.py` **7 passed**；`ruff check`、`compileall` 和 `git diff --check` 通过。真实多指标请求已完成，代表性观测约 77.7 秒，其中两轮 SiliconFlow 模型调用约 77.2 秒，PostgreSQL 约 0.39 秒。
- 边界：该延迟是单次观测，不是 P95；仍未完成模型批量语义准确率、模型超时/流式优化、真实认证、组织级 RLS 和第二真实数据集。
- 下一步：先建立带人工标签和 golden SQL 的 v2 评测集，再基于阶段耗时做模型调用超时、流式状态和 Prompt/上下文压缩优化。

### 2026-08-03：Text-to-SQL 第一阶段运行时合同

- 目标：修正 Catalog/WorkingMemory 派生的结果语义合同没有进入实际 Vanna `ToolContext` 的硬缺口。
- 实现：新增服务器拥有的 `ResultContract`，接入指标/时间别名/请求范围/Join 与版本证据；补充
  `ResultValidator` 和 PostgreSQL Runner 的别名校验；统一 Prompt、Catalog trace、Agent Run 与 SQL
  审计中的版本字段；预算 trace 改为合并写入。
- 验证：Text-to-SQL 专项 `68 passed`；项目 PostgreSQL 集成 `9 passed, 1 warning`；编译检查和
  `git diff --check` 通过。未将在线模型准确率、token 成本或完整自动修复生命周期写成已完成能力。
- GitHub：`cc8b688 feat(text-to-sql): wire semantic result contract` 已推送到 `main`；后续同步文档提交
  会继续记录在飞书项目文档。
