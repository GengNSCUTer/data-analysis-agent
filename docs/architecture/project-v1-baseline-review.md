# Project v1 基线审查记录

> **审查日期：** 2026-09-18  
> **范围：** Week 1；恢复并验证当前用户请求入口、意图路由、结构化查询计划、可信 SQL 执行、结果呈现、会话状态和本地模型运行时边界。  
> **非范围：** 不改写运行时行为；不启动 SFT/DPO；不读取或重跑 protected final test；不改动冻结数据、模型或 Adapter。

## 1. 这份记录解决什么问题

`PROJECT.md` 保留了从原型、数据合同到多轮后训练的大量历史；它不能单独作为“此刻 Web 产品如何工作”的短基线。本文件只记录经当前代码与测试核实的事实，并明确标出尚未验收的部分。

当前可信 Olist 演示入口是 `examples/trusted_olist_web_demo.py` 的 `create_app()`，默认地址为 `http://127.0.0.1:32010/embedded-demo`。它是一个演示环境：通过服务器签名 cookie 在 `analyst` / `admin` 演示角色之间切换，不是生产身份认证。

Week 1 已结束。主模型实验的冻结数据、checkpoint 选择、两套 final test 与可主张边界见 [`../post-training/experiments/project-v1-model-baseline.md`](../post-training/experiments/project-v1-model-baseline.md)；DPO 不启动的诊断证据见 [`../post-training/experiments/project-v1-dpo-decision.md`](../post-training/experiments/project-v1-dpo-decision.md)。产品与记忆的下一阶段依赖见 [`embedded-copilot-and-memory-design-v1.md`](embedded-copilot-and-memory-design-v1.md)，Week 2--4 的小任务顺序见 [`../../plan/process-project-v1-week2-4.md`](../../plan/process-project-v1-week2-4.md)。

## 2. 已核实的真实请求路径

```text
<vanna-chat> / 用户中文消息
  -> FastAPI 注册的 SSE chat route
  -> BudgetedChatHandler.handle_stream()
  -> ConversationStore 读取受控 WorkingMemory
  -> CatalogRetriever 检索 Catalog 上下文
  -> QuestionRouter（是否查库、澄清、拒绝或免工具回答）
  -> QueryPlan + ResultContract + 可选 ChartContract（仅可查库路径）
  -> Vanna Agent 受控工具循环
  -> TrustedRunSqlTool
  -> SqlPolicy -> PostgreSQL readonly role -> ResultValidator/ResultContract
  -> 结果表/可视化/审计记录；可信结果摘要回写 WorkingMemory
```

入口装配的关键对象如下：

| 代码位置 | 已确认职责 |
| --- | --- |
| `examples/trusted_olist_web_demo.py` | 读取环境、装配 FastAPI、Vanna Agent、PostgreSQL runner、Catalog、预算、对话/运行审计、可信 SQL 与图表工具。在线候选模型默认是 SiliconFlow 的 `deepseek-ai/DeepSeek-V4-Flash`。 |
| `chat_runtime.py` | 每个用户回合建立 budget/run 记录；写入服务器派生的 Catalog/QueryPlan/ResultContract metadata；把不应查库的路径提前终止。 |
| `question_router.py` | 确定性第一道意图与可回答性闸门，不依赖 LLM 作出是否开放数据库工具的决定。 |
| `working_memory.py` | 保存受控的指标、时间、比较基线、待澄清信息和可信结果摘要；不从 assistant 文本或 client metadata 信任状态。 |
| `query_plan.py` | 将 Catalog 已选指标与问题中的维度/时间形态变为有限的查询合同；它不是 SQL，也不执行查询。 |

## 3. Day 1 已验证的行为

### 3.1 Router：先决定“允许什么证据”，再决定是否生成 SQL

`QuestionRouter.classify()` 当前将请求分为以下已测路径：

| 用户意图 / 示例 | 当前处理 | 是否进入 SQL 工具循环 |
| --- | --- | --- |
| `统计 GMV` | Catalog 匹配且形状可答，进入数据查询 | 是 |
| `GMV 是什么` / `统计口径是什么` | 用 Semantic Catalog 的确定性定义回答 | 否 |
| `如何提升 GMV` | 通用业务解释，可调用无工具 LLM | 否 |
| `你能做什么` / `/help` | workflow 的确定性帮助回答 | 否 |
| `这个结果为什么这么高` 且有可信摘要 | 基于上一轮可信结果的无工具解释 | 否 |
| 同类追问但无可信摘要 | 要求说明上一轮结果或重新描述条件 | 否 |
| `本月销售额是多少` | 不臆定相对日期，要求绝对日期范围 | 否 |
| `按卖家统计 GMV` / 未冻结归因维度 | 在 SQL 前按 Catalog 口径要求澄清 | 否 |
| 写库、系统表、密钥、任意文件 | 拒绝 | 否 |
| 预测、运行 Python、自动下单 | 标为当前不支持 | 否 |

这里的关键产品边界是 `_tool_free_answer()`：通用业务/知识/可信结果追问可以使用模型，但 `tools=None`，系统提示明确禁止模型声称查询过当前数据库或杜撰当前数据事实。因此“提到 GMV”本身不会自动触发查库。

### 3.2 WorkingMemory：只携带受控事实，不将闲聊变成隐式 SQL 条件

当前状态字段是：`metric_ids`、`time_range`、`dimensions`、`filters`、`comparison_baseline`、`previous_result_summary`、`pending_question`、`pending_missing`。

已测的关键回合：

```text
“本月销售额是多少”
  -> route: missing_time；memory 保留 gmv + pending time_range
“2017-01-01 至 2017-12-31”
  -> route: answerable；恢复 GMV 并加入绝对日期范围
```

`previous_result_summary` 只能由受可信结果门保护的 `with_result_summary()` 写入，且长度被限制；普通建议、help、Catalog 定义不应把其中出现的指标悄悄写成下一轮数据库查询默认值。

### 3.3 QueryPlan：给模型 SQL 生成的“施工合同”，不是第二次生成 SQL

`QueryPlan.from_selection()` 根据已选 Catalog 指标、问题及受控 memory 生成：

- `plan_type`：单指标、多个标量指标概览、多个分组指标；
- `metric_ids`、允许的维度、时间粒度与已确认时间范围；
- 顶层结果列白名单；
- 事实粒度独立聚合、再 `CROSS JOIN` / 分组键关联的执行策略；
- 归因规则与警告。

`QueryPlan.preflight()` 在模型调用前拒绝不可展示维度、无范围日粒度序列和可能超过执行行数预算的形状。这里需要特别区分两个预算：执行上限是 **1,000 行**；UI 预览预算是 **200 行**。201--999 行可以完整执行、仅标记预览截断；达到 1,000 行才可能是执行截断。

### 3.4 Demo 装配与会话可见性

`create_app()` 为 Olist workspace 装配了 `SecurePostgresRunner`、`PostgresConversationStore`、`PostgresRunRecorder`、`QuestionRouter`、受 budget 约束的工具注册表、可信 SQL/图表工具和 Vanna Agent。

历史会话 API 以当前 demo user 为范围；详情接口将 tool message 统一显示为“工具结果已记录”，而不是将原始工具内容或 SQL 参数直接回传页面。演示页 HTML 当前包含北/东/南/西边以及四角共八个 resize handle；是否在真实浏览器的所有历史记录与异常网络情形下完全正确，仍需在后续 UI 专项审查验证。

## 4. Day 1 验证证据

| 层次 | 命令 / 操作 | 结果 | 说明 |
| --- | --- | --- | --- |
| 运行环境确认 | `/disk2/gengnan/conda_envs/data-analysis-agent/bin/python` | Python 3.12.13，含 FastAPI 与 Click | 这是 Web Agent 实际运行环境；不是训练用 `data-analysis-agent-qlora`。 |
| Router / memory / plan / workflow | `PYTHONPATH=src:. ...data-analysis-agent/bin/python -m pytest -q tests/test_question_router.py tests/test_working_memory.py tests/test_query_plan.py tests/test_trusted_workflow.py` | **53 passed** | 覆盖上述确定性路由、澄清 carry-forward、QueryPlan 预算与 starter/help 行为。 |
| FastAPI + 本地 PostgreSQL smoke | `RUN_PROJECT_DB=1 PYTHONPATH=src:. ...data-analysis-agent/bin/python -m pytest -q tests/test_trusted_routes.py tests/test_postgres_conversation_store.py` | **5 passed** | 临时创建并 finally 清理测试会话；验证角色隔离、工具内容脱敏和会话存储，不触碰 Olist 业务表。 |
| 已运行服务只读探测 | `curl http://127.0.0.1:32010/api/project/session`、`curl .../embedded-demo` | 两端均 HTTP 200 | 证明现有本地服务可访问；**不**单独证明该进程已加载本次 worktree 的所有未提交更改。 |

测试期间观察到 FastAPI/Starlette `TestClient` 对当前 `httpx` 的弃用 warning；测试仍通过。它是依赖维护项，不是当前聊天链路失败。

## 5. 已知空白与 Day 2 最小审查单元

本日没有验证的项目必须继续标为“未验证”，不能从上述测试推导为已完成：

1. `SqlPolicy -> PostgreSQL readonly role -> ResultValidator/ResultContract` 的每一层拒绝路径、一次受限修复和审计字段；这是 Day 2 的核心。
2. 真实模型 SQL 调用、在线模型超时/降级，以及 candidate SQL 到最终答案的流式体验；本日没有发起在线 API 消耗。
3. 图表从可验证结果到前端渲染的完整路径，以及缩放、历史恢复、刷新后 Markdown 渲染等浏览器真实交互；本日只确认页面入口和已有测试结构，未跑浏览器 E2E。
4. `QuestionRouter` 是规则优先的 Router；模糊中文表达的召回/误判率尚未有独立标注评测。
5. `IntentKind` 类型定义内存在重复的 `"data_analysis"` literal。运行时无影响，作为后续低风险代码洁净修复项记录；Day 1 不改变行为。
6. `docs/architecture/repository-map.md` 的标注日期是 2026-08-30。其职责地图仍可使用，但后续应由本文件和本周 review 逐步补充“当前已验证”与“历史描述”的差异，不做仅为整齐的大规模重构。

## 6. 下一步（Day 2）

保持同样的小单元，审查并测试：

```text
候选 SQL
  -> completion 清洗
  -> SqlPolicy（语法/对象/单语句/结果上限）
  -> PostgreSQL reader role
  -> ResultContract / ResultValidator
  -> 可展示结果、审计与一次受限修复
```

目标是为每道门写出输入、允许/拒绝证据、不会暴露的数据与尚未覆盖的失败模式；此过程不授权修改安全边界或启动新的训练。

---

## 7. Week 1 / Day 2：可信 SQL 执行链审查

> **审查日期：** 2026-09-18  
> **范围：** 候选 SQL 进入工具、AST Policy、PostgreSQL reader role、结果合同、一次受限修复、运行审计与结果摘要。  
> **非范围：** 未修改 Policy、数据库角色、结果合同或修复次数；未调用在线模型、未读取 protected final test。

### 7.1 已核实的执行证据图

```text
不可信 SQL 候选（Agent / repair model）
  -> TrustedRunSqlTool / Vanna RunSqlTool
  -> SecurePostgresRunner
       -> SqlPolicy.evaluate()：AST、对象、投影、LIMIT
       -> PostgreSQL analytics reader role + readonly transaction + timeout
       -> ResultValidator：结果合同、完整性、值域、时间、粒度
       -> 通过：可信摘要 + result_contract_satisfied
       -> 失败：安全错误分类或结果合同拒绝
  -> 仅 retryable 执行错误：OneShotSqlRepair
       -> 最多一个 repair candidate
       -> 再次经过相同 SqlPolicy、reader role、ResultValidator
  -> query_audits（每次 SQL）+ agent_runs（每个用户请求）
```

这是“纵深防御”，不是多次重复的字符串检查：

| 层 | 它保护的对象 | 已确认的责任 |
| --- | --- | --- |
| `SqlPolicy` | 不可信 SQL 文本 | SQL AST 必须是单条只读 Query；仅允许工作区 analytics schema/table/column；禁止危险函数、敏感标识外显与非字面量 LIMIT；补/收紧执行 LIMIT。 |
| PostgreSQL reader role | 数据库实际对象 | 即使应用层漏判或调用者绕过某段应用逻辑，reader 仍无 app schema/写入权限；runner 还显式建立 readonly transaction 与 statement timeout。 |
| `ResultValidator` | 已执行的表格结果 | 验证服务器结果合同，而不是只看 SQL 是否能跑：必需/精确列、空结果、全空/非有限/越界指标、join 放大、时间覆盖、潜在执行截断。 |
| `TrustedRunSqlTool` | 修复生命周期 | 只对脱敏且 retryable 的执行错误申请一次修复；修复候选仍是未可信输入，必须重过完整链路。 |
| `PostgresQueryAudit` / `PostgresRunRecorder` | 可追溯性 | SQL 级审计与请求级审计分层记录，避免将一个请求的预算/模型情况和一条 SQL 的最终语句/行数混为一谈。 |

### 7.2 一次受限修复的实际边界

可进入修复的数据库执行错误只有：`unknown_column`、`unknown_table`、`ambiguous_reference`、`syntax`。超时、权限、连接失败、未知错误均不修复。结果合同失败也不修复，因为它代表“可执行结果不能证明满足本轮业务合同”，而不是可由第二次猜测自动证明的拼写错误。

```text
原候选执行失败
  -> sanitize_sql_error() 只暴露稳定类别与中文安全原因
  -> OneShotSqlRepair 最多请求一个候选
  -> repair candidate 必须先通过完整 SqlPolicy
  -> 最多再执行一次，并重过 ResultValidator
  -> 任一步失败：不输出未经验证的数字
```

已经由测试覆盖：未知列可成功修一次、危险 repair SQL 在执行前被拒绝、结果列缺失不会进入 repair、修复次数不会超过一次。

### 7.3 行数与展示的三个独立概念

| 概念 | 当前线上 Web Demo 合同 | 当前行为 |
| --- | --- | --- |
| 数据库执行上限 | 1,000 行 | `SecurePostgresRunner` 用 `settings.max_rows=1000` 配置在线 Policy。达到服务器补/收紧的 cap 时，结果标为可能截断。 |
| UI 表格预览 | 200 行 | 201--999 行仍可以完整执行；UI 只显示前 200 行，并标记 `display_truncated=true`。 |
| 后续会话可信摘要 | 8 个合同列样本行、最多 1,200 字符 | 仅对已通过 ResultValidator 的 DataFrame 构造，写入 `WorkingMemory`，供结果追问使用。 |

**注意：** 当前轮给 Vanna Agent 的 `result_for_llm` 仍来自 CSV 的前 1,000 个字符；它不是上面的 8 行结构化可信摘要。完整 DataFrame 会保存为结果文件，UI 也拿到前 200 行预览。该差异不绕过结果验证，但会造成当前轮模型看到的表格上下文可能在 CSV 行中间截断。

### 7.4 已验证审计证据

| 验证范围 | 命令 | 结果 |
| --- | --- | --- |
| SQL repair 与可信工具终止 | `... python -m pytest -q tests/test_sql_repair.py tests/test_trusted_sql_tool.py` | **13 passed** |
| PostgreSQL Policy/reader/ResultValidator/审计集成 | `RUN_PROJECT_DB=1 ... python -m pytest -q tests/test_postgres_runner.py` | **7 passed** |
| AST Policy | `... python -m pytest -q tests/test_sql_policy.py` | **26 passed** |
| ResultValidator 与 UI preview | `... python -m pytest -q tests/test_result_validator.py tests/test_run_sql_tool.py` | **15 passed** |
| 请求预算合同与 SQL/Agent run 链接 | `...test_budget.py tests/test_text_to_sql_contracts.py`、`RUN_PROJECT_DB=1 ...test_postgres_run_recorder.py` | **18 passed；1 passed** |

`query_audits` 的 SQL 级字段包括：`original_sql`、`final_sql`、`policy_status`、`policy_reason`、`row_count`、安全错误原因、`repair_evidence`、版本与请求/会话/运行 ID。`agent_runs` 的请求级字段包括：工具/SQL/LLM 调用计数、token、context 截断、终止原因、错误类别、经过脱敏的 Catalog trace、结果合同是否通过、受抑制的额外 SQL 数、性能与 repair evidence。

### 7.5 Day 2 发现的后续改进项（尚未实施）

1. **当前轮结果上下文格式不统一（中优先级）。** `RunSqlTool` 向当前轮 LLM 传 CSV 前 1,000 字符；后续会话才传结构化、合同化的 8 行摘要。后续可设计“当前轮也使用同一份结构化可信摘要 + 明确结果文件/UI 预览说明”的改造，但必须检查图表工具依赖的 filename 与 Agent finalization 行为。
2. **执行完整性为保守判断（中优先级）。** Policy 自动补/收紧 `LIMIT 1000` 且正好返回 1,000 行时，目前无法判断数据源恰好有 1,000 行还是实际更多，因此合理地返回 `needs_clarification`。若要提升可用性，应先冻结“内部取第 1001 行 sentinel、对外最多 1000 行”的独立执行合同及审计/测试语义；不能只把 `fetchmany(1000)` 改成 `fetchmany(1001)`。
3. **SQL completion 清洗入口的重叠（待下一小单元）。** 离线 `unwrap_sql_completion()`、在线 repair 的 `extract_sql_candidate()`、历史 SQLite baseline 的清洗函数有相似围栏/前缀规则，但失败语义不同。先比较输入输出合同与调用方，确认一个 shared primitive 是否合理，再决定是否迁移；不直接删除历史路径。

### 7.6 SQL completion 清洗函数对比（Day 2 最后小单元）

审查的三个函数都只处理**展示包装**，不验证 SQL AST、安全性、字段存在性或业务语义；这些必须仍由下游 Policy/数据库/结果合同完成。但它们服务的场景不同，不能直接互相替换：

| 函数 | 主要调用方 | 输入失败时的语义 | 是否从任意 prose 中搜索 `SELECT/WITH` | 当前结论 |
| --- | --- | --- | --- | --- |
| `candidate_sql_generator.unwrap_sql_completion()` | 当前离线 Olist/TheLook candidate、DPO、Spider bridge、matching evaluation | 空/仅包装时抛 `CandidateSqlGenerationError`；其他未知 prose 原样保留，交给 Policy 记录拒绝 | 否；只接受有限 heading/fence/`SQL:` 包装 | 这是当前 multi-schema SQL-only 评测的规范 presentation contract。 |
| `trusted_sql_tool.extract_sql_candidate()` | 在线一次 repair 模型输出 | 空或不是以 `SELECT/WITH` 开始的可识别 SQL 返回 `None`，repair fail-closed | 是；repair 响应中的解释性前缀可被截去 | 运行时修复需要“没有可靠候选就不重试”的 `None` 契约，不能改成离线路径的原样保留。 |
| `frozen_sqlite_baseline.extract_sql_candidate()` | 历史 frozen SQLite baseline | 空时抛 baseline error；DML、多语句、尾部 explanation 仍保留给 SQLite benchmark Policy 诊断 | 是 | 历史 SQLite 基线的可复现资产；不应在 Week 1 无迁移计划地删除或改变历史评分。 |

已执行的定向回归：

```text
tests/test_sql_completion_unwrap.py
tests/test_frozen_sqlite_baseline.py
tests/test_trusted_sql_tool.py
tests/test_sql_repair.py

32 passed
```

**结论：** 当前不存在“把三个函数简单改名为同一个函数”就能安全解决的问题。真正值得做的最小改造是后续新增一个无副作用的、有限 presentation-wrapper parser（例如返回 `str | None`，且不从任意 prose 搜 SQL），再让离线包装成“空则抛错/其余原样留给 Policy”的调用约定、让 repair 包装成“空则 `None` 并终止”的调用约定。开始这项改造前必须先冻结以下兼容测试：围栏、`SQL:`、未知 prose、DML、多语句、尾部 explanation、过长 repair response。

在没有这套迁移合同前，`frozen_sqlite_baseline.extract_sql_candidate()` 的重复逻辑仅作为**已知技术债**保留；不能以“统一”名义改写历史 benchmark 语义。

---

## 8. Week 1 / Day 3：会话与 WorkingMemory 生命周期

> **范围：** 状态来源、持久化位置、基本恢复、Prompt 使用方式与所有权边界。  
> **非范围：** 本地模型接入、完整浏览器 UI 验收、TTL/多 workspace/并发状态改造。

### 8.1 三层状态及其职责

| 状态层 | 保存什么 | 可信边界 / 生命周期 |
| --- | --- | --- |
| 浏览器 `localStorage` | 当前 active `conversation_id` | 只是 UI 恢复指针；页面加载后仍从后端按当前 signed user 拉取会话，不能作为业务上下文或权限依据。切换演示角色时清除。 |
| `app.conversations` / `app.messages` | 会话历史、所有权、版本、WorkingMemory JSONB；逐条 message/tool metadata | `PostgresConversationStore` 以 `user_id` 限制 get/list/delete；conversation ID 有格式验证；删除时数据库级 cascade 清理关联 message/run。 |
| `WorkingMemory` | 指标、绝对时间范围、维度、过滤、比较基线、可信结果摘要、待澄清问题/字段 | 只从受控 Router/结果门派生；长度和字段白名单受限；它不等价于模型记忆、长期用户画像或原始结果副本。 |

### 8.2 当前回合的更新顺序

```text
请求带 conversation_id
  -> Store 按当前 server-resolved user 读取会话（否则新建）
  -> WorkingMemory.from_mapping(working_memory JSONB)
  -> retrieval_context 仅辅助 Catalog 检索，最多 4,000 字符
  -> Router 以当前问题 + memory 状态决定路由
  -> memory.apply() 合并可验证指标/绝对日期/比较基线/待澄清字段
  -> 更新 conversation.working_memory 并持久化
  -> 若 SQL 结果通过 ResultValidator：再读取会话，with_result_summary() 写入最多 1,200 字符可信摘要
```

已验证行为：相对时间问题可保存 `gmv + pending time_range`，用户下一轮提供绝对日期后恢复可答查询；无上一轮可信摘要的“这个结果为什么这么高”会澄清；有可信摘要时可进入无工具结果解释。普通通用建议、help、Catalog 定义、拒绝和不支持请求不会把它们出现的指标悄悄写成新的 pending SQL 条件。

### 8.3 Memory 如何进入模型上下文

`WorkingMemory` 不是将历史聊天记录全文拼进 SQL Prompt。它分成两条受控用途：

| 入口 | 作用 | 会带入什么 | 明确不带入什么 |
| --- | --- | --- | --- |
| `retrieval_context(question)` | 供 `CatalogRetriever` 用当前补充信息检索所需指标/表/Join | 当前问题、pending 原问题、指标 ID、绝对日期、比较基线；总长最多 4,000 字符 | 结果摘要、任意 assistant 文本、未知 JSON 字段 |
| `prompt_context()` | 由 `CatalogContextEnhancer` 拼进 SQL Agent 的 system prompt | 受控指标、时间、维度、过滤、比较基线 | `previous_result_summary`、pending 原问题、未知字段、原始 tool rows |

结果追问是不同分支：只有 Router 判定 `result_followup` 且存在 `previous_result_summary` 时，`_tool_free_answer()` 才将最多 1,000 字符的可信摘要交给 **无 SQL 工具** LLM。它不能因此再调用 `run_sql`，也不能把摘要以外的数字说成当前数据库结论。

### 8.4 Day 3 已运行证据

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| WorkingMemory、Router 与 handler 的受控 carry-forward / tool-free 分支 | `...data-analysis-agent/bin/python -m pytest -q tests/test_working_memory.py tests/test_question_router.py` | **45 passed** |
| PostgreSQL conversation 所有权、round trip、非法 ID、空 starter 隐藏；FastAPI history DTO 脱敏 | `RUN_PROJECT_DB=1 ...data-analysis-agent/bin/python -m pytest -q tests/test_postgres_conversation_store.py tests/test_trusted_routes.py` | **5 passed** |
| 受控 Memory 到 Catalog Prompt 直接探针 | Python 进程内构造 `BudgetUsage + CatalogContextEnhancer`，不连 DB/不调 LLM | 已确认指标/时间出现；未知注入字段和上一轮摘要不出现；trace 不保存原问题 |
| 对话上下文整回合裁剪与 Catalog 受限上下文 | `...data-analysis-agent/bin/python -m pytest -q tests/test_context_builder.py tests/test_semantic_catalog.py` | **36 passed** |

浏览器 E2E 已有“历史会话加载、刷新后 Markdown 仍渲染、新建会话不显示旧历史”的测试代码；本轮尚未将它作为当前 worktree 的 live-browser 验收，因为这需要确认运行中的 `:32010` 服务确实来自当前构建。该项留给 Day 4 的 UI 专项。

### 8.5 已确认但暂不直接修改的边界

1. 当前在线 Web runtime 仍只挂载 Olist；M2 已将 `workspace_id` 持久化并纳入 Store 的 get/list/delete/update 条件。历史行统一回填到 `olist-demo`，TheLook 仍未接入在线聊天。
2. 当前 Memory 无 TTL、版本字段或“只清空分析上下文”的用户动作；新会话和删除会话是现有边界。是否加 TTL 是产品语义选择，不是单纯后端清理。
3. M2 已加入 `state_version` 乐观并发合同：旧对象更新抛出 `ConversationStateConflict`，聊天边界返回可恢复的“会话状态已更新”提示；结果摘要写入仍采用独立的 owner/workspace 读取和版本更新，后续 M3/M5 继续补更细的状态生命周期测试。

---

## 9. Week 1 / Day 3：Semantic Catalog、QuerySpec 与 Gold renderer 边界

> **范围：** 审查业务语义目录、线上查询/结果合同，以及离线训练与评测数据构造使用的 `QuerySpec` / deterministic PostgreSQL Gold SQL。  
> **非范围：** 不把 Gold renderer 接入线上自然语言请求；不重建任何冻结数据集；不将单元测试结果表述为已经完成全量 PostgreSQL 执行验收。

### 9.1 两条链路解决不同问题，共享版本事实但不可互相替代

```text
线上产品请求
自然语言问题
  -> Semantic Catalog 检索
  -> QuestionRouter / QueryPlan / ResultContract
  -> 候选模型 SQL
  -> SqlPolicy -> reader role -> ResultValidator

离线受控数据构造
人工或脚本审阅后的 QuerySpec
  -> validate_query_spec()
  -> deterministic render_gold_sql()
  -> GoldSqlArtifact（SQL hash + 版本证据）
```

两条链路都绑定 workspace、Catalog、指标、Policy 等版本，因而对“GMV 是什么、哪些表/Join 可用”使用同一份受控事实；但职责不同：

| 对象 | 已确认职责 | 明确不负责 |
| --- | --- | --- |
| `Semantic Catalog` | 为线上请求提供按角色受限的指标、表、列、Join、指标定义、维度/归因规则及可复核检索上下文 | 不执行 SQL；不替代 `SqlPolicy`；不把任意自然语言直接解析成 SQL |
| `QueryPlan` / `ResultContract` | 由当前问题与 Catalog 形成有限的查询/结果语义合同，约束候选 SQL 和返回字段 | 不生成最终 SQL；不绕过结果校验 |
| `QuerySpec` | 离线训练/评测构造的版本固定“施工图”：指标、形状、维度、时间、join program、期望结果列 | 不是线上自然语言 parser，也不是 SQL 文本 |
| `validate_query_spec()` | 在编译前 fail-closed 地拒绝版本漂移、非法指标/维度/时间/形状/Join/结果列/hash | 不执行数据库，不调用模型，不判定用户业务意图 |
| `METRIC_SQL_REGISTRY` | 将每个冻结指标的公式、分母、时间字段、过滤和来源表集中为唯一实现 | 不允许模型临时改写指标口径 |
| `render_gold_sql()` | 将已验证的 QuerySpec 编译为稳定的 PostgreSQL Gold SQL，并输出 hash 与版本证据 | 不执行 SQL、不调用 LLM、不理解自由文本用户问题 |

这一区分直接回答一个常见误解：Gold SQL 是**离线对照答案与可复现数据构造证据**，不是应当拿来替代在线模型候选 SQL 的第二个产品生成器。线上仍须先接受模型候选，再走安全与结果合同门。

### 9.2 QuerySpec 已验证的 fail-closed 语义

`QuerySpec` 是 frozen dataclass。它将 canonical JSON（不含自身 ID）计算为 `query_spec_id`；内容或版本被修改后，预期 hash 不再匹配。`WorkspacePin` 同时绑定 workspace、Catalog、dataset、metric、Policy、prompt 和 SQL dialect，因此不能把旧快照静默套到新指标定义上。

当前验证器已覆盖的核心边界包括：

- 指标必须存在于 Catalog 与 `METRIC_SQL_REGISTRY`，数量上限为四个；
- `scalar`、`state_grouped`、`category_grouped`、`time_series` 的维度、必需结果列和时间参数必须匹配；
- `seller_id`、`seller_city`、`seller_state` 是敏感展示维度，直接拒绝；`payment_type` 尚无冻结归因规则，直接拒绝；
- `category_grouped` 仅允许 item-grain 安全指标，避免把订单/评论指标错误归因到品类；
- time-series 不能混用 purchase 与 review 时间族；按日序列必须给出绝对日期范围；
- workspace/Catalog 快照、结果列和 hash 不匹配时 fail closed。

### 9.3 renderer 已验证的指标粒度保护

`METRIC_SQL_REGISTRY` 的存在不是为了方便字符串拼接，而是防止每条训练样本各自发明指标定义。renderer 对复杂聚合采用固定程序：例如 `average_order_value`、`average_items_per_order` 先形成订单粒度 CTE，再做平均，避免一对多 item join 直接放大订单值；多个分组指标则先独立生成 metric CTE，再以 canonical key `FULL OUTER JOIN` 合并。

生成的 `GoldSqlArtifact` 包含 `query_spec_id`、`renderer_version`、指标/Join/结果列以及 SQL SHA-256。它不保存用户自然语言问题。renderer 的 SQL 不自动附加 `LIMIT`，因为它用于完整 Gold denotation；线上最多 1,000 行的执行上限仍由 Day 2 的 Policy/runner 链处理。

### 9.4 本次证据与尚未覆盖项

| 范围 | 命令 | 结果 | 可证明的边界 |
| --- | --- | --- | --- |
| Catalog、QuerySpec 校验、v3 指标/renderer 回归 | `/disk2/gengnan/conda_envs/data-analysis-agent/bin/python -m pytest -q tests/test_semantic_catalog.py tests/test_olist_queryspec.py tests/test_olist_v3_metrics.py` | **106 passed, 11 skipped** | 受控 Catalog、版本/shape/time/attribution 拒绝条件、确定性编译与指标公式回归 |

这里的 11 个 skipped 是受条件控制的 PostgreSQL runtime 集成覆盖，不能据此声称所有 Gold SQL 已在当前数据库完整执行。真正的数据库执行准入属于冻结数据集构建/评测流程的独立证据，仍须在相应 release artifact 中读取。

### 9.5 本小单元结论与下一步

当前 Catalog、线上 QueryPlan/ResultContract 与离线 QuerySpec/Gold renderer 的职责边界是清晰的：前者帮助产品把用户问题约束为可审计请求，后者帮助训练与评测样本在固定口径下可复现；二者共享受控业务事实，但均不能替代 Day 2 的 SQL 安全和结果可信门。

下一小单元只审查 **TASK-007：已验证结果如何成为表格、图表和最终用户可读回答**。重点不是改 UI，而是分别追踪 scalar、grouped 与 time-series 的 ChartContract、服务器渲染、表格预览/截断，以及前端能否正确呈现这些 artifact。

---

## 10. Week 1 / Day 4：表格、图表与可信结果呈现

> **范围：** 追踪已验证 SQL 结果到表格、图表、最终文本回答与嵌入式 Web Component 的数据流；分别检查 scalar、分组和时间序列的合同语义。  
> **非范围：** 不改造视觉样式；不让模型自由选择图表字段/类型；不把 UI 预览当成新的数据导出通道；不以源码阅读代替浏览器 E2E。

### 10.1 结果展示不是 SQL 后的自由发挥

```text
validated DataFrame
  -> ResultValidator（最多 1,000 行执行；前 200 行 UI 预览）
  -> RunSqlTool（当前 CSV artifact + DataFrameComponent）
  -> [未请求图表] 服务器生成可信结果摘要并确定性收束文本回合
  -> [请求图表] ChartContract -> TrustedVisualizeDataTool -> Plotly ChartComponent
  -> vanna-chat ComponentManager -> table / chart / text
```

`ResultValidator` 将“执行截断”和“展示截断”分开：201--999 行是完整执行、但 UI 只预览前 200 行；恰好达到 1,000 行仍保守地要求用户缩小范围。前者可以展示，后者不能被描述为完整业务结论。

服务器在 SQL 已通过结果合同时，构造最多 8 行、1,200 字符、只含合同列的结构化摘要。**未请求图表**时，`ObservedLlmService` 可用该摘要收束本回合，避免再让在线模型把趋势、因果或额外数字编造为结论。它也是下一轮结果追问可使用的受控信息来源。

### 10.2 三类请求的图表行为

| 已审查形态 | ChartContract 的固定语义 | 前端产物 | 当前解释边界 |
| --- | --- | --- | --- |
| scalar，例如“统计 GMV 并生成图表” | 拒绝并要求横轴；不会把单个数字伪装成趋势图 | 澄清状态卡 | 没有图表，也不输出未经验证的推断 |
| 分组，例如“按州统计 GMV 并生成柱状图” | 横轴固定 `customer_state`、指标列固定 `gmv`、类型为 bar | 直接 Plotly 柱图 + 当前结果表 | 图表不重新聚合；任何文字说明仍只应基于结果 artifact |
| 时间序列，例如“2017 年按月统计 GMV 并生成折线图” | 横轴固定 `time`、类型为 line；显式要求折线图但没有时间粒度时要求澄清 | 时间排序后的 Plotly 折线图 | 不接受模型将非时序分组强行画成折线趋势 |

`TrustedVisualizeDataTool` 只允许可视化**当前请求中刚由成功 `run_sql` 写出的文件**。`ChartContract.validate_frame()` 还拒绝未知/缺失列、空值、非数值指标、超 200 行、超 120 个横轴值、超 8 个系列、或同一横轴/系列多行；它只允许时间排序，绝不在显示层 groupby、sum、count、去重、补值。最终 Plotly 图形由服务器直接生成 traces，模型给出的标题或字段选择不会生效。

### 10.3 当前表格组件的能力与已观察到的 UX 缺口

后端 `RunSqlTool` 将 UI 限制到前 200 行，并在文本描述中写明完整行数；Web Component 的通用 `DataFrameComponentRenderer` 提供搜索、排序与 CSV 导出。但审查发现它仍是通用组件，而不是业务结果 artifact 的专用视图：

1. **预览标识在表格内丢失（P1）。** 后端传给 `DataFrameComponent.from_records()` 的已经是 `display_results`；`from_records()` 因而将 `row_count` 重新设为预览行数。对完整执行的 250 行结果，组件实际拿到 200 行并显示 `200 rows`，其 `hasMoreRows` 为 false，不能在表格内部显示“200 / 250”的截断提示。虽然 Python 侧 description 含“previewing first 200 rows”，但它是通用英文文案，不是稳定的受控展示字段。
2. **导出语义易误导（P1）。** 通用组件默认 `exportable=True`，导出的是当前组件的 200 行 `data`，却命名为普通 `data.csv`，没有标记“预览”。这不会泄露第 201 行之后的数据，但用户很容易误以为导出了完整受控查询结果。
3. **业务可读性尚未产品化（P2）。** 标题/描述仍是 `Query Results`、`rows` 等英文，列名直接采用 SQL alias；虽然后端可信摘要有 Catalog label，但该 label 未形成 UI 表头、单位/百分比/货币格式或“结果依据”面板的统一 presenter contract。

这些是后续专门的“ResultArtifact presenter 合同”任务，而不是本周临时改一个 `row_count` 就能彻底解决的问题：它必须同时明确完整数、预览数、是否允许下载、下载的是 preview 还是服务器签名文件、字段显示名与单位、以及 ResultContract/审计引用。

### 10.4 前端交互现状与测试边界

本轮代码阅读确认 `vanna-chat` 在正常 SSE 流结束时，如果状态仍为 `working`，会执行 `settleCompletedStream()` 清为 idle；因此它针对以前“服务端短路返回而前端停在 Sending message”的通用兜底已经存在。SSE 或 polling 都失败时则进入可见的 `Connection failed` 状态。

嵌入宿主页的会话加载、Markdown、拖拽/八向调整尺寸和响应式 Plotly resize 均已有实现；`PlotlyChart` 通过 `ResizeObserver` 根据宿主实际宽度 relayout。不过 `frontends/webcomponent/package.json` 的 `npm test` 明确是失败占位，当前没有自动化 DOM/浏览器测试去证明当前 build 对上述真实路径仍有效。故源码不能替代 UI E2E 验收。

### 10.5 Day 4 证据与结论

| 范围 | 命令 | 结果 | 可证明的边界 |
| --- | --- | --- | --- |
| ChartContract、受控图表渲染、结果校验/预览边界 | `/disk2/gengnan/conda_envs/data-analysis-agent/bin/python -m pytest -q tests/test_chart_contract.py tests/test_visualization_policy.py tests/test_result_validator.py` | **27 passed** | scalar/grouped/time-series 合同选择、模型不能改图表字段/类型、图层无隐式聚合、执行与 UI 截断区别 |

上述 Python 测试不覆盖 DataFrame Web Component 的 DOM 渲染、表格预览文案、导出按钮或真实浏览器 resize/history。表格预览/导出问题是由后端 component payload 与前端 renderer 的实际字段传递直接观察到的，已加入缺口地图；下一步修复时应先为完整 250 行/预览 200 行补一条浏览器或最小 DOM 回归，再实现 presenter contract。

---

## 11. Week 1 / Day 4：1.5B Adapter 的产品运行时接入状态

> **范围：** 只判断当前选择的 `Qwen/Qwen2.5-Coder-1.5B` Base + multi-schema SQL-only SFT `adapter_best` 在何处可被加载，何处实际被调用。  
> **非范围：** 不加载 GPU 模型、不改变 Web 默认模型、不启动任何生成/评测/训练，也不因已有离线分数推断线上可用性。

### 11.1 结论：Adapter 目前是离线候选生成资产，尚未进入 Web 产品路径

当前可信 Demo 的 `create_app()` 固定构造 `OpenAILlmService`，默认从环境读取 SiliconFlow 的 `deepseek-ai/DeepSeek-V4-Flash`。该服务被 `ObservedLlmService` 包装后同时承担对话、工具调用和一次 SQL repair。该文件没有 `transformers`、`PeftModel`、本地 model directory、Adapter path 或运行时模型选择开关。

反过来，当前 1.5B Base/Adapter 的加载出现在仓库外数据资产配合的离线脚本中：评测/生成脚本以 `AutoModelForCausalLM.from_pretrained(...)` 加载冻结 Base，只有 `--run-label adapter` 时才通过 `PeftModel.from_pretrained(..., is_trainable=False)` 叠加 Adapter；输出为仓库外 candidate JSONL 与安全 evidence。训练入口则只创建/更新 LoRA 参数。它们不导入 FastAPI/Vanna Web request lifecycle，也不接触 production PostgreSQL runtime。

因此准确表述是：

```text
已完成：冻结 Base + adapter_best 可在离线、固定 prompt / greedy decode 合同下生成 SQL 候选。
未完成：将该 candidate generator 作为 Web Agent 一个受预算、可超时、可降级的候选提供者接入产品。
```

这不是简单把 SiliconFlow 名称替换成本地模型：产品接入至少需要把当前 server-owned `Catalog -> QueryPlan -> ResultContract` 投影为和训练合同兼容的 prompt，处理模型加载/单 GPU 排队/超时/并发，明确何时 fallback 到在线模型，并保持候选仍必经 `SqlPolicy -> reader role -> ResultValidator`。在这些合同和端到端回归完成前，Adapter 不得成为线上默认路径。

### 11.2 代码与证据边界

| 位置 | 已确认作用 | 不能据此声称 |
| --- | --- | --- |
| `candidate_sql_generator.py` | 定义 server-derived candidate context、SQL-only prompt 和有限 completion wrapper 清洗；模块刻意不依赖 Transformers/PEFT/DB | 它本身不是模型服务，也没有被 Demo 调用 |
| `multi_schema_sql_only_sft.py` | 离线训练样本的 Catalog / QuerySpec / ResultContract prompt 投影与 target SQL 拼接 | 与线上 Router/QueryPlan prompt 自动字节一致，或已经被线上使用 |
| `scripts/post_training/inference/`、matching runners | 在隔离资产目录加载 Base/Adapter、生成候选、写入证据，随后可走离线评测 | 这些 runner 是低延迟、多用户、可降级的 Web service |
| `examples/trusted_olist_web_demo.py` | 当前唯一已审查 Web 装配点；仍调用 SiliconFlow 在线模型 | 当前 Adapter 已替代 API，或得到 runtime traffic 证据 |

下一最小单元（TASK-011）只做**仓库外模型与 Adapter 的只读 preflight**：确认实际路径、冻结 ID/revision、Adapter 文件 hash、Tokenizer 可加载与当前 prompt 边界。即使 preflight 成功，也只证明资产可用，不改变本节的“未接入 Web runtime”结论。

### 11.3 TASK-011：只读资产 / 环境 preflight

选定 Base 与 `adapter_best` 均存在于仓库外目录：

```text
Base:    /disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b
Adapter: /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-multischema-sql-only-sft-v1-20260916/adapter_best
```

已核对的冻结事实：

| 项目 | 结果 |
| --- | --- |
| Base | `Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b`；`qwen2`，最大 position `32768` |
| tokenizer | `Qwen2TokenizerFast`，EOS/PAD token ID 均为 `151643`；仅加载配置与 tokenizer，不加载权重 |
| adapter | `LORA` / `CAUSAL_LM`，`r=16`、`alpha=32`、dropout `0.05`，目标为 q/k/v/o 与 MLP gate/up/down projections |
| adapter config SHA-256 | `d2fb8ebc074c919173f2c9e5244bf439209362d05f5a739ebdb6e9406cf52b80` |
| adapter weights SHA-256 | `76c77a75461ce5b3b8d30047c226a44087538259aa97b8509c3f7350bca8f6b5`（73,911,112 bytes） |

但这次 preflight 也确认了一个重要事实：真实 Web 运行环境 `/disk2/gengnan/conda_envs/data-analysis-agent/bin/python` 有 Transformers tokenizer/config 工具，却**没有 `torch`，也没有 `peft`**。因此它不可能加载 Base 权重或 LoRA Adapter。相比之下，专用的 `data-analysis-agent-qlora`（PEFT `0.14.0`、Transformers `4.48.3`、Torch `2.5.1+cu121`）与 `data-analysis-agent-qwen35`（PEFT `0.14.0`、Transformers `5.17.0`、Torch `2.5.1+cu121`）环境可以使用 CUDA，但它们不是当前 Web Demo 的解释器。

这不是“模型文件缺失”或 Adapter 质量问题，而是一个需要显式设计的部署边界。后续接入前必须在以下方案中做小任务决策：给 Web 运行环境构建最小、锁版本的 inference extras；或把本地候选模型放到独立的受认证内部推理进程，Web 仅调用其受限接口。两种方式都不能直接复用历史 QLoRA 训练环境作为生产服务，也不能绕过 SQL 链。当前不安装依赖、不启动 GPU，保持在线 Demo 默认行为不变。

**preflight 命令：**

```text
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python
  -> AutoConfig.from_pretrained(..., local_files_only=True)
  -> AutoTokenizer.from_pretrained(..., local_files_only=True)
  -> 检查 torch / peft 可用性与 adapter 文件 SHA-256
```

它成功验证了 Base metadata 与 tokenizer；对 `PeftConfig` 的读取按预期不能在 Web 环境执行（`ModuleNotFoundError: peft`）。没有把该失败掩盖成“模型可在 Web 环境运行”。

---

## 12. Week 1 / Day 4：失败分类、单次 repair 与 fallback

> **范围：** 复核当前候选 SQL 失败时系统实际会做什么；区分用户澄清、安全拒绝、一次可验证 repair、LLM timeout 的可信结果回退与尚未实现的 provider fallback。  
> **非范围：** 不增加多次 repair，不把结果合同失败改成“自动再猜一次 SQL”，不把本地模型未接入表述为已有 fallback。

### 12.1 当前决策表

| 触发点 | 当前实际处理 | 是否再生成 SQL | 对用户的安全边界 |
| --- | --- | --- | --- |
| Router 识别出相对时间、未冻结归因、无上轮可信结果的追问、输入不完整 | 直接澄清；不开放 `run_sql` | 否 | 不猜日期、口径或上一轮结论 |
| SQL Policy 拒绝写操作、多语句、非白名单表/列、敏感投影、危险函数等 | 安全拒绝 | 否 | 不向数据库发送被拒 SQL |
| PostgreSQL `unknown_column` / `unknown_table` / `ambiguous_reference` / `syntax` | 同一逻辑 tool call 最多一次 repair；repair 模型只收 sanitised category、原 SQL 与受限 Catalog；候选再次过 Policy 后才执行 | 最多一次 | 修复不扩大表/列/权限；失败不输出数字 |
| timeout / permission / connection / unknown database failure | 拒绝/提示缩小或稍后重试 | 否 | 这些不是应靠改 SQL 猜测解决的问题 |
| ResultContract / ResultValidator 失败 | 终止，不进入 repair | 否 | “能跑”不能证明指标、字段、时间/粒度或结果安全；不输出未经验证数字 |
| LLM 在**已有 validated result**之后超时 | 返回服务器已有的可信结果摘要 | 否 | 不丢弃已验证结果，也不补造新解释 |
| LLM 在结果产生前超时 | 终止为安全 timeout 提示 | 否 | 不输出数字 |
| 当前 SiliconFlow provider 不可用 | 有 timeout/error 分类和可见失败 | 否 | **没有**第二个在线 provider 或本地 Adapter 自动接管 |

`OneShotSqlRepair` 的异步 provider 异常会被 fail-closed 转为无 repair candidate；修复后的 SQL 即使 syntax/表名看上去更好，也必须重走 Policy、readonly PostgreSQL、ResultValidator。单次约束同时由 request metadata 和 repair coordinator 保证。

### 12.2 “repair”与“fallback”不是同一个功能

当前 repair 使用的仍是当前在线 LLM service，只服务于一条已经产生、且错误类型明确的 SQL 候选；它不是将用户请求切到另一模型，更不是一条新的自由分析链路。`ObservedLlmService` 有 timeout 观察与可信结果收束，但没有 provider 优先级、健康检查、fallback model policy、模型切换审计字段或本地 Adapter provider。

所以当前系统有**失败可分类、能安全收束、少数机械候选可修复**的能力；没有“在线失败自动本地模型接管”的能力。未来若引入本地 1.5B，应先定义它在 candidate generation 与 repair 中的角色、是否允许作为 online fallback、何时触发、超时/队列边界以及所有路径如何保留 model/provider evidence。不能仅因为两个模型都能生成 SQL 就把它们静默互换。

Day 2 已运行的 `tests/test_sql_repair.py` 与 `tests/test_trusted_sql_tool.py`（**13 passed**）覆盖 repair 分类、单次上限、policy rejection、terminal result validation；本节不重跑同一测试，也不把单元测试扩大解释为真实 provider 故障演练。
