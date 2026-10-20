# 分析上下文与记忆 v2：实现与验收

核对日期：2026-10-17。Phase 1–5 已交付任务状态、执行记忆、可信引用与滚动摘要；当前进一步改为按 token 读取历史、回答后后台压缩。第 1–7 节保留早期审计记录；当前摘要行为以第 10 节为准，第 11–12 节记录此前验收，第 13 节记录组合筛选改造。后台优化的计划与本轮证据见 [后台压缩任务](../../plan/feature-background-context-compaction.md) 和 PROJECT.md。

## 1. 本阶段任务卡

| 项目 | 范围 |
| --- | --- |
| 目标 | 完成 Phase 0 基线、Phase 1 active state/ConversationHistory 和 Phase 2 执行证据及结果复用 |
| 输入 | 当前 Web 装配、ChatHandler、语义解释器、WorkingMemory、会话存储、摘要过滤器、执行审计及结果工件代码 |
| 输出 | 本基线、Phase 1/2 代码与测试证据、项目记录与文档导航 |
| 不变量 | 不恢复关键词准入；不改变模型、权限、SQL/结果校验、查询预算及离线训练评测协议 |
| 验收 | 对照函数调用和数据库字段核对；运行现有相关回归，单独运行项目数据库会话存储测试 |
| 非目标 | Phase 0/1 未实现 ExecutionMemory，现已在 Phase 2 补充；ReferenceBinding 和跨会话长期记忆仍未实现 |

## 2. 当前在线调用顺序

`src/data_analysis_agent/server.py:create_app()` 在服务启动时构造会话存储、Catalog、SQL runner、Agent 和 `BudgetedChatHandler`，并明确注入 `SemanticIntentInterpreter`。每个正常分析请求按以下顺序运行：

```mermaid
flowchart TD
    A[服务端解析身份并加载所属会话] --> B[检查 workspace / 数据和指标版本]
    B --> C[读取 WorkingMemory]
    C --> C1[AnalysisContextBuilder 装配任务内近期对话与持久摘要]
    C1 --> D[SemanticIntentInterpreter 理解本轮问题]
    D --> E[选择 Catalog 并校验业务能力]
    E --> F{需要查询数据库?}
    F -->|否| G[口径答复 / 通用解释 / 结果追问 / 澄清]
    F -->|是| H[生成 QueryPlan / ResultContract / ChartContract]
    H --> I[先保存本轮 WorkingMemory]
    I --> J[Vanna 加载历史并构建有界模型上下文]
    J --> K[模型生成 run_sql 工具调用]
    K --> L[计划一致性检查 / SqlPolicy / PostgreSQL reader / ResultValidator]
    L --> M[保存有效结果工件并按合同展示及绘图]
    M --> N[保存结果引用并结束 run 审计]
```

这是正常链路；解析失败、SQL 拒绝、空结果、超预算和状态冲突分别有分支，不能认为每个请求都会到达最后一步。`/help` 等 workflow 命令保留 Vanna 自身入口。

有两个不同的模型上下文入口，当前不能混为一谈：

- **语义解释调用**在 Vanna 主对话之前发生，接收当前问题、可用 Catalog、受控状态、任务内最近完整问答、持久叙事摘要和不含行值的结果引用元数据。实体值仍由服务端解析。
- **Vanna 主对话调用**由 `Agent._build_llm_request()` 经 `ContextBudgetFilter` 装配历史。只保留任务内近期叙事，剥离旧轮次的工具调用和工具结果；当前轮次的工具调用与返回仍成对保留。`CatalogContextEnhancer` 加入本轮 Catalog、计划及结构化状态。`ObservedLlmService` 在发给提供方前统一计算整个请求预算。

`candidate_sql_generator.py` 的运行时请求构造器在 Handler 中生成候选组件的对齐证据，不代表默认在线 Provider 已被独立的 SQL-only 小模型替换。

## 3. 组件入口、输入输出与持久化

| 当前代码入口 | 输入 → 输出 | 更新触发与保存位置 | 失败边界 / 尚缺内容 |
| --- | --- | --- | --- |
| `chat_runtime.py:BudgetedChatHandler.handle_stream()` | ChatRequest + 认证用户 + 会话 → 路由结果、查询计划、SSE | 请求前创建所属会话及 run；计划生成后保存 canonical state；主对话后保存结果引用 | 处理 stale state 冲突；ExecutionMemory 独立视图仍待 Phase 2 |
| `conversation_store.py:PostgresConversationStore._get_sync()` | conversation ID + 用户 + workspace → Conversation 与完整消息；另提供近期完整 turn 读取 | 从 `app.conversations`、`app.messages` 读取，按 message_index 排序；消息 metadata 带 message/turn 关联 | 不返回他人/其他 workspace 会话；结果实体引用仍待后续实现 |
| `conversation_store.py:_update_sync()` | Conversation 或增量消息 → 事务提交与 state_version + 1 | 复用既有 message 主键/index，增量追加不重复历史；metadata 写入 working_memory JSONB | `FOR UPDATE` + 版本比对；拒绝归属/版本冲突。禁止 silent last-write-wins |
| `semantic_intent.py:SemanticIntentInterpreter.interpret()` / `prompt()` | 当前问题 + 角色可见 Catalog + state → 类型化 SemanticIntent | 无独立记忆写入；Handler 记录到本轮 trace | Provider 异常最多重试一次；JSON/字段/角色校验失败不给 SQL 权限。只收到结果存在布尔值，没有实际实体引用 |
| `semantic_intent.py:select_intent_catalog()` / `route_semantic_intent()` | SemanticIntent + Catalog + 当前 state → CatalogSelection / QuestionRoute | 不独立持久化 | 校验指标、维度、归属、筛选和预算；当前额外筛选只支持一个两字母大写客户州，不支持任意条件编译 |
| `working_memory.py:WorkingMemory.from_mapping()` / `as_dict()` | 旧字段 ↔ `AnalysisState` 兼容映射 | `conversation.metadata['working_memory']`，最终进 conversations JSONB | `AnalysisState` 是唯一 active state 权威；WorkingMemory 不再与其双写竞争 |
| `chat_runtime.py` 中语义分支的 state patch | 本轮 SemanticIntent → 增量 patch 后的 canonical state | SQL 执行之前保存；澄清时更新 pending_question/pending_missing | patch 只接受 Catalog/QueryPlan 能表达的字段；旧结果与新计划通过 task/turn 边界区分 |
| `analysis_lifecycle.py:prepare_turn_memory()` / `clear_active_analysis_state()` | Conversation + 当前 workspace → 可复用 state 或新 task | 版本漂移和手动清空都隔离 active task；历史与审计保留 | 清空不自动恢复旧条件；结果工件只挂到对应成功 turn |
| `context_builder.py:ContextBudgetFilter.filter_messages()` | 主对话全部消息 → 近期完整轮次及省略边界/可选语义摘要 | 在构建 Vanna LLM 请求时运行；按 source digest 使用进程内缓存 | 摘要失败返回 provenance 边界；超大当前轮次有压缩分支，不能等同所有极端预算下工具配对已验证 |
| `history_summary.py:HistorySummary` / `build_llm_summary_provider()` | 省略前缀 turns → 来源证据和可选自然语言摘要 | Handler `_persist_history_summary()` 保存到 metadata；Store 写入 `working_memory.__history_summary` | 压缩器未读持久摘要续接；summary_prompt 每消息最多 2,000 字符、总计最多 8,000 字符，但来源范围按整个省略区记录 |
| `run_recorder.py:PostgresRunRecorder.start()` / `finish()` | request、用户、版本、BudgetUsage → AgentRun 与审计结果 | `app.agent_runs` 保存一次请求；runner 的 SQL 审计通过 run_id/request_id 关联 | 已有执行证据基础，不是完整 task/state/execution 记忆；一个 run 可能含 SQL 重试，不能把 run_id 当成一次 SQL 尝试 ID |
| `trusted_sql_tool.py:TrustedRunSqlTool.execute()` / `_remember_result_artifact()` | SQL 工具参数 + 计划/结果合同 → ToolResult 与工件指针 | 计划 guard → runner 的策略/执行/结果校验；只有 validation.state=valid 才保存完整结果 CSV | 不以工具 UI success 代替校验；受控 repair 有独立证据，不支持无限修复 |
| `result_artifact_store.py:ResultArtifactStore` | 有效结果与 scope → CSV/Plotly manifest；按 scope 回读 | 仓库外，按 user/workspace/artifact 隔离，保存版本、大小和完整性证据 | Store 校验 scope 和文件完整性，版本适用性由生命周期/调用方判断；没有可信结果实体/排序绑定接口 |
| `result_artifact.py:ResultArtifact.from_trusted_usage()` / Handler `_persist_result_artifact()` | 本轮可信摘要 + trace → 会话内结果引用 | 主对话后存入 canonical state 的 artifact 指针；图表另有历史引用 | 保存摘要、列、版本、CSV/图表指针；可信实体/排序 binding 仍待 Phase 3 |

关键阅读路径：

1. [Web 装配](../../src/data_analysis_agent/server.py) → [Handler](../../src/data_analysis_agent/chat_runtime.py)。
2. [语义解释与能力校验](../../src/data_analysis_agent/semantic_intent.py) → [WorkingMemory](../../src/data_analysis_agent/working_memory.py) → [生命周期](../../src/data_analysis_agent/analysis_lifecycle.py)。
3. [会话存储](../../src/data_analysis_agent/conversation_store.py) → [消息/会话 DDL](../../infra/postgres/security.sql) → [Vanna 主对话与上下文装配](../../src/vanna/core/agent/agent.py)。
4. [历史过滤](../../src/data_analysis_agent/context_builder.py) → [摘要](../../src/data_analysis_agent/history_summary.py) → [结果引用对象](../../src/data_analysis_agent/result_artifact.py) / [外部工件](../../src/data_analysis_agent/result_artifact_store.py)。

## 4. QuestionRouter 的保留职责

“去掉旧路由”指默认在线请求不再调用 `QuestionRouter.classify()`、`admit_sql_request()` 来解释自然语言，不代表删掉同名类的所有职责。

| 场景 | 当前行为 |
| --- | --- |
| `create_app()` 默认产品入口 | 注入 SemanticIntentInterpreter；语义成功走 `route_semantic_intent()`；失败返回澄清，不退回关键词准入 |
| 线上仍使用的 QuestionRouter 能力 | 持有 CatalogRetriever、资源参数，并提供口径/帮助格式化；确定性能力校验由 `route_semantic_intent()` 执行 |
| 未注入解释器的调用方和历史离线测试 | Handler 仍保留 classify/admit、关键词补全与 WorkingMemory.apply 兼容分支；不能据此描述默认在线产品 |
| 后续处理 | 按计划先理清职责，再决定拆类/命名；Phase 0 不删除兼容代码、不重写冻结离线协议 |

`tests/test_semantic_intent.py:test_handler_semantic_path_does_not_call_legacy_router` 将 classify、admit 和 WorkingMemory.apply 替换为抛错函数，验证语义查询、澄清和解析失败三条分支均不经过它们。该测试使用模型桩，证明调用关系，不证明真实模型理解率。

## 5. 统一术语及权威来源

| 名称 | 本方案中的固定含义 | 当前实现映射 | 后续边界 |
| --- | --- | --- | --- |
| ConversationHistory | 有序消息原文及完整 turns，是意图与对话脉络来源 | conversations/messages + 近期窗口/cursor 读取 + Vanna Conversation.messages | 原文不等于已执行事实；历史 SQL/tool projection 仍待 Phase 4 |
| AnalysisState | 一个 active task 当前想查询的结构化条件 | `analysis_state.py` + conversations JSONB，WorkingMemory 兼容映射 | Phase 1 已定义唯一 canonical state 和 patch；不双写两份权威状态 |
| ExecutionMemory | 每次执行尝试及验证结果的可追溯视图 | 现有 agent_runs、SQL 审计、BudgetUsage trace | Phase 2 补 task/state/plan/attempt/artifact 关联，复用现有审计，不另造日志系统 |
| ResultArtifact | 已验证执行产生的结果数据及引用 | 会话内 ResultArtifact 指针 + 仓库外 CSV/Plotly/manifest | Phase 2 增加排序和实体元数据；不是聊天文本或缓存命中即可可信 |
| ReferenceBinding | 将“这个州/上一轮第二名”解析到具体可信结果行/维度值的服务端绑定 | 尚无专用实现 | Phase 3 从 artifact 解析并验证，不从助手回答或摘要抽取结果值；用户显式条件仍可经正常校验进入 state |
| HistorySummary | 带来源范围的派生叙事摘要 | HistorySummary + 持久化 metadata | Phase 4 增量续接并按真实覆盖范围推进；不能覆盖 state 或充当执行证据 |
| ContextCompaction | 为一次模型调用裁剪近期窗口、压缩旧历史的过程 | ContextBudgetFilter + summary provider | 不删除原始历史，不等于长期记忆；现有本轮内部工具轨迹与历史投影需要区分 |
| Semantic Catalog | 服务端维护的指标口径、表列和维度能力 | CatalogLoader / CatalogRetriever | 继续按 workspace/version 加载；与暂缓的跨会话用户偏好记忆分开 |

`conversation_id` 是会话；`turn_id` 是用户一问及其后续处理；`task_id` 是一项分析任务；`run_id` 是一次请求运行；`execution_id` 是一次 SQL 尝试；`artifact_id` 是有效结果。现有数据库已实现 conversation/run/artifact 及消息主键，尚未形成完整 turn/task/execution 模型。计划中的名称不代表已有对应类或表。

## 6. 已确认的缺口及实施归属

| 现状 / 具体失败场景 | 后续处理 |
| --- | --- |
| 历史整段读取/遍历，稳定 message_id 未传到消息读取视图；长聊天每轮成本增长 | Phase 1 已完成：近期完整 turns、cursor 消息分页、增量追加和 turn 关联；兼容 Vanna 现有存储调用 |
| 语义解释器看不到近期问答和实体值；“这个州”缺少可靠解析路径 | Phase 3/4：一次语义调用提出 reference request，服务端从 artifact 绑定，再装配各类调用的上下文 |
| 当前 state 在执行前更新，旧 artifact 缺少原计划版本；新查询失败后可能混淆两个范围 | Phase 1/2：分开当前意图与 last-successful execution/plan/artifact |
| 版本漂移有清空逻辑，但手动清空仍保留历史/摘要；接入近期历史后旧条件可能回流 | Phase 1：task/reset 边界；清空后不自动重建旧条件 |
| 持久摘要未被压缩器用于续接；来源证据覆盖范围可大于实际送入摘要模型的内容 | Phase 4：增量摘要、实际消息 watermark、重启恢复和源编辑失效 |
| 历史 SQL/tool 消息仍可能进入主对话，尚无按调用用途拆分的投影 | Phase 4：存储保留原文，模型读投影；Phase 1 只保证近期完整 turn 和工具配对 |
| ContextBudgetFilter 类注释仍称摘要不含自然语言且不持久化，与当前 summary_provider/Handler 行为不一致 | Phase 4 更新相关注释；当前基线以实际调用为准，不依据旧注释宣称能力 |

这些缺口不要求新增 Router、向量库或第二次引用理解 LLM。首个产品验收仍是“最高州 → 这个州的品类”，同时验证沿用/替换/撤销条件、失败后结果归属、清空和刷新。

## 7. 验证证据与结论边界

Phase 0 的历史验证曾在 `data-analysis-agent` conda 环境运行：

```bash
conda run -n data-analysis-agent python -m pytest -q \
  tests/test_semantic_intent.py tests/test_runtime_handoffs.py \
  tests/test_working_memory.py tests/test_analysis_lifecycle.py \
  tests/test_context_builder.py tests/test_result_artifact.py \
  tests/test_result_artifact_store.py tests/test_postgres_conversation_store.py
```

结果：**68 passed, 6 skipped**。这部分只作为 Phase 0 的基线记录。Phase 1 完成后，新增定向联合回归为 **116 passed, 15 skipped**，项目数据库会话存储测试包含在该回归中；定向 Ruff、compileall、diff check 通过。真实浏览器通过 8 项组合回归，以及一项真实查询/工件/刷新/分页/清空/新查询链路。数据库测试命令如下：

```bash
RUN_PROJECT_DB=1 conda run -n data-analysis-agent python -c \
  'from dotenv import load_dotenv; load_dotenv(".env"); import pytest; raise SystemExit(pytest.main(["-q", "tests/test_postgres_conversation_store.py"]))'
```

结果：**6 passed**，覆盖工具消息回读、owner 隔离、参数校验、workspace/state version 及撤回最后一轮。该命令是 Phase 0 的数据库基线，Phase 1 的 116 项定向回归已再次覆盖相关路径。

可确认：当前语义路径不回退旧准入；现有状态解析、摘要生成失败回退、结果工件读写/完整性和会话存储行为符合已覆盖测试。`test_runtime_handoffs.py`、部分 WorkingMemory/生命周期测试还在覆盖旧兼容路径，不能将总通过数全部算作新语义多轮能力。

未验证：结果实体引用、增量摘要续接、跨会话长期记忆和生产认证尚未实现；真实浏览器验收只覆盖当前任务/会话状态、结果工件、刷新、分页和清空，不代表任意业务语义正确。SQL 可执行与合同通过也不保证模型生成质量。

Phase 0 的三个任务以及 Phase 1 的 TASK-004～008、TASK-030～032 已完成。定向 Python 回归为 **116 passed, 15 skipped**，真实浏览器 M1–M4 组合回归通过。Phase 2 的实际实现见下节，结果实体引用仍由 Phase 3 负责。

## 8. Phase 2：执行记忆与结果复用

`ExecutionMemory` 是已有 `query_audits` 与 `agent_runs.catalog_trace` 的读取视图，不是新数据库表。
一次 SQL 尝试使用 `audit-{audit_id}`，保留 run/task/turn、父/当前 state version、计划与范围、
模型、状态、耗时、行数和对应 artifact。Runner 的审计写入返回既有主键，在本轮 trace 的
`execution_attempts` 中记录该次 validation；只有实际通过验证并保存 CSV 的那次才关联 artifact。
同一 run 先失败后成功，两次记录各自保留，不能套用整轮的最终成功状态。旧记录缺少逐次验证证据时，
显示 `not_validated`，不补推成功。

新增读取入口 `GET /api/project/conversations/{id}/execution-memory?limit=100`，先校验登录及
所属 conversation/workspace，返回最近至多 100 次尝试，按发生顺序展示。接口不返回原始 SQL、
数据库原始错误或完整行数据；SQL 仍在既有受限审计中。单次完成时间取审计时间，开始时间按已记录耗时估算。

结果工件现在带 `query_scope`（指标、维度、时间、筛选、比较、Top-N、结果形态及版本）和
`result_metadata`。后者包含 execution ID、验证状态、外层 SQL AST 可证明的输出列排序和
最多 8 行分组实体，每个字符串最多 128 字符。不会从助手回答或摘要提取实体，不把第一行
自动解释为唯一最高；无法映射到输出列的排序表达式暂不声明排序依据。完整 CSV 与 Plotly
继续保存在仓库外，沿用原有完整性校验。

复用决策分为 `display_only`、`validated_result` 和 `query_required`。展示/分页继续读原工件，
既有路由校验归属、版本和完整性；在线结果解释额外核对当前查询范围。范围相同且工件验证通过时，
无需再次执行 SQL；改变指标、维度、筛选、时间、比较、Top-N 或版本必须重新查询。若当前条件
没有对应成功结果，只提示重新查询，不能把更早的成功结果冒充当前结果。文件丢失、篡改或用户不匹配
也不能复用。当前没有新增按时间自动过期的 TTL；失效依据是版本、权限、范围和实际工件可用性。

浏览器验收还发现并修复了既有版本写入问题：`ResultContract.as_tool_metadata()` 提供平铺字段，
保存工件原先读取不存在的嵌套 `result_contract`，导致 manifest 版本为 `unknown`。现已直接读取
真实 `dataset_version_id/metric_version`，删除错误的嵌套读取，不为旧 unknown 工件提供兼容回退。

同时删除页面启动时“遍历旧历史直到加载成功”的回退：只恢复选定的会话，并等待 Web Component
定义完成后再调用其恢复方法；用户已开始新分析或切换会话时，过时的加载响应不得覆盖当前选择。
这避免了刷新后追问被发到其他旧会话，以及组件尚未注册时历史列表被错误标记不可用。

这一步为 Phase 3 的“最高州 → 这个州的品类”准备可信数据来源。

验收：`RUN_PROJECT_DB=1` 定向 Python 回归 **127 passed**；真实浏览器组合回归 **9 passed,
9 deselected**；Ruff、compileall、diff check 通过。浏览器覆盖真实查询 → 执行记录及工件 → 刷新 →
解释已有结果（审计确认零新增 SQL，reuse=validated_result）→ 清空 → 新查询，并覆盖历史会话恢复、
新会话、移动端、结果预览及受控 SSE。截图位于仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/analysis-memory-phase2/browser/`。

## 9. Phase 3：可信结果实体引用

Phase 3 新增 `ReferenceRequest` 和 `result_reference.py`。语义解释器只能提出
`previous_ranked_entity`、维度、排名、`first_row`/`unique_highest` 语义及可选工件 ID，不能
提交实体值。服务端从当前已验证 `ResultArtifact` 的 `ranked_entities` 读取最多 8 条有界排名证据，
校验用户/工作区、数据和指标版本、结果列、SQL AST 证明的排序、排名范围和并列；值不从助手文本、
历史摘要或客户端字段提取。

唯一最高要求降序第一行且不与第二行并列；“第一行”则使用 `first_row`，不会被表述为唯一最高。
解析成功后，服务端将值写入 `AnalysisState` 的受控筛选，后续继续经过 Catalog、QueryPlan、
ResultContract、SqlPolicy、PostgreSQL reader 和 ResultValidator。解析失败只返回可解释澄清，不进入
SQL。Phase 3 定向联合回归 **193 passed**；真实浏览器跨轮实体下钻独立测试 **1 passed**，完整
浏览器组合 **9 passed**。一次全套重跑遇到在线模型提供方超时，台账确认引用已经解析成功且没有
产生未经验证结果。后续 Phase 4 的短期上下文改造见下一节。

## 10. Phase 4：任务内上下文包与持久滚动摘要

入口为 `analysis_context.py:AnalysisContextBuilder.build()`，由 Handler 在版本检查之后、
语义解释之前调用。读取已有摘要并按游标装配尚未压缩的完整轮次，不以 6 轮截断历史。
它复用会话 JSONB 和现有消息索引，没有新增表或兼容写入路径。
当前问题仍作为本轮用户消息单独传入；Catalog 与工具定义由各调用方装配，最后一起检查预算。

### 10.1 对话、状态和结果各自负责什么

| 信息 | 来源及用途 |
| --- | --- |
| 当前问题 | 本轮用户输入，不能被截成半句来满足预算 |
| 当前分析状态 | `AnalysisState`，负责指标、范围、筛选、未解决澄清和 task/version |
| 最近对话 | 摘要覆盖位置之后的完整原文；24K tokens 触发后台压缩，压缩后目标约 16K，剥离历史工具调用与工具返回 |
| 叙事摘要 | 目标 2K、上限 4K 估算 tokens，保存目标、纠正、待办和表达偏好；请求输出上限也是 4K |
| 执行与结果引用 | 当前成功 artifact/execution ID、列名、维度、排序和验证状态；不带完整行或 CSV |
| 当前工具轨迹 | 保留当前调用及返回配对；旧 SQL/工具结果不作为聊天历史重放 |

无需新 SQL 的解释调用也接入近期叙事和摘要。解释可信结果时，额外提供该 artifact 的
已验证查询范围，不能仅凭数值摘要猜时间/分组。数字复述检查允许该可信范围里的日期与 Top-N，
仍拒绝叙事历史里出现、但不在可信结果或范围中的新数字。

结构化状态/引用目标 4,000 tokens，超过目标记录并使用完整请求剩余空间，不在目标处直接拒绝或截断。
总体沿用 64,000 tokens 上限和 48,000 history ceiling。完整请求检查在
`llm_observability.py:_check_context()`，包括 system、Catalog、消息、工具 schema、包装估算
和输出预留。语义调用、摘要调用、SQL/回答调用都经过这层。

本地 tokenizer 是代理估算器；与在线提供方的实际 tokenizer/template 不同，不能宣称精确计数。
无本地 tokenizer 时使用明确标记的字符估算；中文与 ASCII 分别计量。
运行证据记录 tokenizer mode/id、prompt tokens 和 output reserve，不记录完整 prompt。
摘要另记录失败类型和耗时，原始异常文本不进入 safe trace。

### 10.2 何时压缩、如何续接

参数统一来自 RequestBudget。会话存储每批读取 20 个完整 turns，按游标续读；这里的 20
是读取批大小，不是记忆轮数上限。语义理解和 Vanna 后续调用使用同一近期范围；未摘要原文
不会被第二层字符数或消息数窗口删除。48K 历史上限和 64K 完整请求上限共同限制实际上下文。

本轮对话、状态和结果保存后，Handler 调度后台任务。同一会话最多一个任务，当前单进程
服务最多两个并发压缩；触发后分批压缩到约 16K 原文。摘要输入是“旧摘要 + 新增旧问答”，
含提示总计最多 24K tokens。工具 SQL/结果不进入摘要；超大单轮按文本边界分片，
各片临时合并，整轮成功才推进数据库覆盖位置。每次摘要模型调用超时 60 秒。

成功后使用 `save_history_summary()` 专用事务更新现有 `working_memory.__history_summary`，
检查 owner/workspace/task/数据指标版本、旧 watermark 及源 message 主键和位置。不会写入旧
Conversation 或修改消息、state_version；普通状态更新保留数据库最新摘要，切换任务则清除。
覆盖记录包括真实 `source_message_start/end`、累计轮次/消息数、scope 与本次增量起点。
既有 `source_sha256` 仍只识别本次增量，不新增 hash。

失败只保存脱敏失败类别，旧摘要与未压缩原文保留，当前后台任务不立即重试。下轮请求完成后
可再次调度。普通请求不等待摘要；仅真实上下文不足时等待已有任务或做一次同步压缩并重建请求，
仍不足则明确返回预算错误。当前轮次的工具调用及返回保持配对，不能被压缩为历史叙事。
后台用独立 BudgetUsage，摘要计量不计入已结束的前台请求。应用关闭时取消未完成任务；
重启后从持久摘要覆盖位置续接。进程内调度不保证停机期间继续工作，也不是跨进程任务队列。

### 10.3 失效与受限召回

摘要只有 task/workspace/数据版本/指标版本全部一致才使用。清空或切换任务后旧摘要不进入新任务；
编辑/撤回最后一轮由 store 恢复该轮开始前的 AnalysisState，并清除派生摘要；不再清空整个任务。
快照只存于服务端用户消息 metadata；清空或版本变更后不能借编辑恢复旧范围。摘要只传给语义理解/叙事层，
不会由程序解析为 SQL 筛选；结果实体仍需 Phase 3 的可信 artifact 绑定。

`AnalysisContextBuilder.recall()` 提供同一会话的显式接口：最多读取 6 条消息，返回最多
3 条完整叙事片段 / 800 tokens，每条带 `message_index`。读取沿用 owner/workspace 过滤，
不修改分析状态，不找回旧结果实体。本阶段只交付接口；没有自动召回触发器、向量检索或跨会话长期记忆。

### 10.4 验收

测试覆盖数据库重载与新 builder 续接、真实 watermark、摘要失败和过长输入、scope 变化、
旧工具剥离与当前工具配对、完整请求预算、状态不截断、owner/workspace 隔离及编辑后失效。
浏览器验收通过真实模型和 PostgreSQL 查询，在插入长对话 fixture 后刷新并解释已验证结果，
随后继续追加历史、刷新并检查摘要增量续接；两轮解释均不得新增 SQL，也不得改变 artifact/筛选。
具体通过数在 `PROJECT.md` 的本轮记录维护。此处不将通过测试等同任意长对话准确率。

## 11. Phase 5 联合验收

2026-10-16 的 Phase 5 以真实 SSE、PostgreSQL reader 和 Playwright 浏览器矩阵验收：覆盖任务切换、
增删改筛选、指标切换、时间粒度、结果解释、图表复用、排名实体下钻、刷新恢复、空结果、过期工件、
未知指标、写入请求、恶意摘要和显式新实体覆盖。确定性 Python 定向回归为 244 passed，上下文/意图
补充回归为 58 passed。浏览器矩阵的逐轮记录、模型调用、延迟和脱敏证据保存在仓库外；摘要文本
只进入叙事解释，不进入 QueryPlan/SQL 条件链路。当前没有自动历史召回，召回命中率不适用；真实
模型提供方偶发 `semantic_service_unavailable` 时请求 fail closed，不能把该外部依赖失败写成产品
逻辑通过或业务语义准确率。

### 11.1 V3.2 补验与引用条件贯通修复

在线默认模型切换为 `deepseek-ai/DeepSeek-V3.2`，并修复 OpenAI 兼容客户端未发送
`LlmRequest.temperature` 的问题。语义提示明确合法增量 patch、未改字段的继承、
指标定义与结果追问的区别，以及引用来源维度与目标分组的区别；没有增加关键词路由。

真实测试发现引用绑定只改了 state、没有进入有效 SemanticIntent 的问题。Handler 现在
在 `apply_reference_filter()` 后，以合并状态构造有效意图，供 Catalog、QueryPlan、候选
生成和会话保存共同使用；删除后置重复注入 filter 的分支。回归不只检查页面和 state，
还要求引用筛选进入 QueryPlan，并保留原年份及新的目标维度。

确定性联合回归 **256 passed**。完整真实浏览器矩阵 **10 组、36 轮全部通过**；
最终提示下排名第一/第二下钻独立复验 **2 passed**。既有网页套件初跑 15 passed、
3 failed、2 skipped，三项失败修复后分别复验通过，18 个有效场景均有通过证据；
两项 skip 为已移除浮动宿主的拖拽/缩放测试。长会话摘要生成和增量续接已在网页套件中通过。
最终提示定向回归 **45 passed**。各次测试独立记录，没有把分次通过写成单次全绿。

证据目录：仓库外 `evals/analysis-memory-phase5-20261016/v32-r4/`（完整矩阵）及
`v32-r5/`（最终排名复验）。测试验证状态/引用/结果合同与交互，不包含逐条 Gold denotation；
不能据此推断任意业务语义准确率。跨会话长期记忆、自动历史召回仍未实现。

## 12. 短对话的状态连续性修复（2026-10-16）

用户报告“最高销售额州 → 这个州卖得好的品类 → 前十品类”在第三问失败。
这不是长历史压缩造成的：当时历史很短，州与指标曾被正确识别；问题来自字段继承、编辑和澄清的状态写入，以及不完整的 SQL 上下文。

| 操作 | 当前实现 | 代码 |
| --- | --- | --- |
| 改分组 | 不携带上一种分组的 Top-N；本轮明确指定数量则使用该数量，补丁顺序不影响结果 | `AnalysisState.apply_patch()` |
| 补充澄清 | 保留已经验证的品类、筛选、时间和排名，仅等待尚缺字段 | `BudgetedChatHandler.handle_stream()` |
| 编辑上一问 | 用户消息 metadata 保存该轮开始前状态；事务中删除最后一轮并恢复前缀状态，旧摘要失效 | `PostgresConversationStore._truncate_from_last_user_sync()` |
| 执行失败后重试 | 当前意图与上次成功结果分开；解释器看到 failed 状态，重试使用当前范围 | `SemanticIntentInterpreter.prompt()` |
| 筛选后按品类统计 | 在线 Catalog 上限为 6 表，已选必要维度的关联路径不再被后续维度挤掉 | `CatalogRetriever.retrieve()` / Web 装配 |
| 一次 SQL 修复 | 完整 QueryPlan 与 Catalog 传入修复，取消按字符截掉后半段上下文；输出预留采用统一预算，默认 4,000 tokens；截断响应拒绝执行 | `build_repair_prompt()` / `LlmRepairCandidateProvider` |
| 只换图 | 解释器直接获得绘图模块的规范类型，使用 `horizontal_bar` 等规范 ID，不自造后缀 | `SemanticIntentInterpreter.prompt()` / `ChartType` |
| 度量口径 | 明确区分事实粒度计算与维度关联；只有计划要求的归因才分摊，不因订单/商品关联自造金额权重 | `metric_context.py:SYSTEM_PROMPT` |

例如第二问解析后保留 `gmv`、绑定可信结果中的 `customer_state=SP`，改为按 `product_category_name` 分组，并移除旧的 `top_n=1`。
第三问只更新 `top_n=10`。SQL 失败不会清空这些条件；编辑第三问只撤销第三问的变化，不撤销第二问的州筛选和分组。
编辑快照不对旧消息补写；没有快照的历史轮次无法据此恢复，不能从助手叙述猜回条件。

本轮未调整摘要触发阈值，也未新增语言关键词路由、hash、存储表或兼容别名。
SQL 仍走计划检查、安全策略、只读角色和结果合同；是否符合真实业务还需结果对照。

最新定向回归 250 passed。最终真实浏览器五组共十八轮分两批通过，其中九次品类排名
逐行匹配独立 reader 的类别、顺序与金额；另有一次真实模型漏筛选修复验收通过。
失败状态续接使用受控夹具；旧消息不回填快照，提供方超时仍会作为失败记录。
详细证据路径和早期失败说明见 PROJECT.md 最新记录及修复任务卡。

## 13. Catalog 组合筛选与实体值来源（2026-10-17）

后续真实用户对话暴露了另一类第三问失败：“哪个州销量最好 → 这个州的热门品类 → 这个州第一名品类的相关数据”。
州与品类均已解析，但旧能力检查只允许一个客户州条件，因此在 SQL 生成前拒绝了组合筛选。
本轮将此限制替换为 Catalog 维度绑定，没有增加用户句式规则。

`query_filters.bind_query_filters()` 从本次已授权 Catalog 切片取得指标允许维度、非敏感列和来源。
多个不同维度的等值条件取交集，保存在 `QueryPlan.filters`；筛选不会自动成为分组。
状态补丁只增删改本轮指定条件，指标、其他筛选和可信引用继续由现有 AnalysisState 管理。

品类还有两种值编码：原始葡萄牙语和英文展示名。Catalog 声明展示列同属品类维度；
`SecurePostgresRunner.resolve_filter_sources()` 对存在多个来源的条件执行参数化 `EXISTS`，
只在已授权维度列中确认该值的来源，使用 workspace reader、只读事务和既有超时，
将授权来源与实际匹配的 `value_sources` 分别写入计划，不读取整表给模型。
合法的英文名/原名 `COALESCE` 仍可通过，直接用英文值筛选原始编码列则不算正确筛选。
找不到值不会强制澄清，保留条件允许正常查空。
这处理的是明确值的字段来源，不实现任意中文品类翻译或模糊实体检索。

`plan_sql_guard.sql_plan_mismatch()` 对结构化筛选检查 AST 中实际参与结果的正向谓词，
支持表别名、CTE 投影、等值、单值 IN、同名关联键的 IN 子查询、大小写处理和关联 EXISTS。
注释、不同列、NOT、`条件 OR TRUE` 和未使用的 CTE 不构成已筛选证据；独立多指标分支均需保留条件。
所有分组键已被等值限定时，不再强制补冗余 Top-N LIMIT，避免合法单组查询无意义地触发重写。
历史离线计划不重写；在线 SQL 仍经过 SqlPolicy、对象白名单、只读角色和 ResultValidator。

数值核对另发现模型单独 `COUNT(DISTINCT order_item_id)`：它是订单内序号，不能统计全局商品件数。
Catalog 现在明确计数与完整复合行键。没有为这个错误再增加字符串拦截器；公式正确性仍需结果对照，
AST 筛选检查也不宣称能证明任意 SQL 等价。已覆盖行为、失败记录和验收数字统一见 PROJECT.md。

同时删除意图提示中“取消州就清空全部筛选”的旧示例：撤销单个条件使用对象数组的 `remove`，
变更某维度先移除旧值再添加新值，其他条件保留。Catalog 还明确品类只筛选对应商品行，
支付方式仅作筛选时不自动变成支付占比分摊；分摊仍由计划中的分组归因要求决定。

主端口数值复验还发现模型把商品明细按品类名重新关联原始商品表，477,794.20 被放大成
1,447,238,631.80。`QueryPlan.lookup_joins` 现在携带选中 Catalog 的多对一关系，
`_lookup_join_mismatch()` 对可追踪的原始维度表 JOIN 核对关系键，沿 CTE 投影追溯列来源。
不符合声明关系的非键关联会触发既有一次修复；正常键关联、品类翻译关联和派生聚合间关联有正向回归。
这项检查只覆盖可识别的原始维度表键误用，不替代数据库安全策略或任意 SQL 语义证明。

本轮没有改摘要、历史任务召回、失败后解释旧结果的目标选择、多维 Top-N 或训练。
后续仍需分别处理这些能力，不能据本轮筛选链路通过推断所有多轮分析已完善。

## 14. 连续分析约束审计与结果目标分离（2026-10-17）

在线流程把“当前待查询任务”和“上一份成功结果”作为两个独立对象保存。语义解释器输出
`result_target=current_task` 或 `last_successful_result`；后者只在用户明确要求解释、展示或重绘上一份
成功结果时使用。它不会回滚当前失败任务，也不会把旧数字冒充新查询结果。引用和换图都从已验证工件读取，
而不是从 8 行叙事摘要猜实体或重新构造一份 SQL 计划。

图表复用的职责边界是：模型只提出规范图表类型，服务端从工件的 `query_scope`、指标列和结果列构建
`ChartContract`，随后直接读取归属已验证的 CSV 生成图表。这个路径不再重新检索 Catalog 或重建 QueryPlan，
所以“只换图”不会因为当前失败任务缺少筛选维度而被错误挡住；仍会执行图表类型、列、空值、重复键和 200 行展示检查。

本轮还把在线聚合的外层 Top-N 统一交给服务端执行策略，移除模型必须重复生成 `LIMIT` 的检查；SQL 仍依次
经过 SqlPolicy、PostgreSQL reader 和 ResultValidator。引用取消固定 `rank <= 8`，改为从完整 CSV 按游标读取
目标行；唯一最高额外读取相邻行确认并列。8 行仍只是模型叙事上下文预算，不是服务端数据访问上限。

连续验收覆盖九轮状态推进、排名第十行、失败范围后复用旧结果和当前范围重试。每轮在仓库外记录语义意图、
路由、AnalysisState、QueryPlan、SQL 审计、结果合同、复用决定及终止原因；原始 SQL 不进入仓库内文档或模型摘要。
本轮确定性回归 **337 passed**，真实浏览器流程 **9 passed**，主端口网页回归 **5 passed**。结果只证明这些受控场景
的状态连续性和安全边界，不能推断任意业务问题的 SQL 语义准确率。

## 15. 分区 Top-N 与结果解释数字判定（2026-10-17）

连续分析审计之后补齐了两项在线能力。语义意图中的 `top_n` 现在要求至少一个业务分组维度；`dimensions` 的最后一个维度是排名目标，前置维度和 `time_grain` 组成分区键。例如，`[customer_state, product_category_name]` 表示每个州内品类排名，`[product_category_name] + month` 表示每个月内品类排名。没有分区键时继续走原有全局 Top-N；只说“每月前五名”但没有排名维度仍澄清。

`QueryPlan.top_n_partition_dimensions` 进入现有计划证据和结果预算。在线分区 SQL 必须包含窗口排名函数、与计划一致的 `PARTITION BY` 和外层 `rank <= N`（或等价上界）。检查使用 sqlglot AST，避免把普通文本中出现的字段当成分区证据；对结果列 `time`，允许 CTE 使用 `month_start` 等明确时间字段作为窗口分区。服务端不再在分区 Top-N 上附加全局排名 `LIMIT`，而是用分区基数乘 N 计算聚合行预算，其他 SQL 安全和结果校验不变。

结果解释的数字证据检查仍禁止模型编造业务数字，但识别并忽略展示格式中的行首编号、排名序号、`Top 10`、`前 5 项`、`共 10 行` 等标签；金额、比例、日期年份和业务计数仍需要可信结果摘要支持。

实现入口：`query_plan.py`、`semantic_intent.py`、`plan_sql_guard.py`、`postgres_runner.py`、`chat_runtime.py`。定向回归 **147 passed、10 skipped**；真实浏览器验收覆盖每州前五品类和每月前五品类各一组，并复跑原九轮连续分析流程，均通过。任务合同和逐轮证据见 [Top-N 与追问数字修复任务卡](../../plan/fix-topn-and-followup-number-20261017.md) 和仓库外 `evals/flow-constraints-20261017-final/`。
