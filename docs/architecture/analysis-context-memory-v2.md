# 分析上下文与记忆 v2：Phase 0 代码基线

核对日期：2026-10-15。代码基线：`f89bd1f`；本阶段只审阅现有实现、明确职责和执行后续改造计划，不修改运行逻辑。目标设计与后续任务统一见 [实施计划](../../plan/architecture-analysis-context-memory-v2.md)。本文件回答“现在实际上怎样工作”，计划回答“接下来怎样改”。

## 1. 本阶段任务卡

| 项目 | 范围 |
| --- | --- |
| 目标 | 完成 TASK-001～003：入口/读写/失败边界表、线上与旧 Router 职责划分、统一记忆术语 |
| 输入 | 当前 Web 装配、ChatHandler、语义解释器、WorkingMemory、会话存储、摘要过滤器、执行审计及结果工件代码 |
| 输出 | 本基线、计划 Phase 0 完成标记、项目记录与文档导航 |
| 不变量 | 不恢复关键词准入；不改变模型、权限、SQL/结果校验、查询预算及离线训练评测协议 |
| 验收 | 对照函数调用和数据库字段核对；运行现有相关回归，单独运行项目数据库会话存储测试 |
| 非目标 | 本阶段不实现 ConversationHistory 新接口、AnalysisState、ExecutionMemory 类、ReferenceBinding 或增量摘要 |

## 2. 当前在线调用顺序

`examples/trusted_olist_web_demo.py:create_app()` 在服务启动时构造会话存储、Catalog、SQL runner、Agent 和 `BudgetedChatHandler`，并明确注入 `SemanticIntentInterpreter`。每个正常分析请求按以下顺序运行：

```mermaid
flowchart TD
    A[服务端解析身份并加载所属会话] --> B[检查 workspace / 数据和指标版本]
    B --> C[读取 WorkingMemory]
    C --> D[SemanticIntentInterpreter 理解本轮问题]
    D --> E[选择 Catalog 并校验业务能力]
    E --> F{需要查询数据库?}
    F -->|否| G[口径答复 / 通用解释 / 结果追问 / 澄清]
    F -->|是| H[生成 QueryPlan / ResultContract / ChartContract]
    H --> I[先保存本轮 WorkingMemory]
    I --> J[Vanna 加载历史并构建有界模型上下文]
    J --> K[模型生成 run_sql 工具调用]
    K --> L[计划一致性检查 / SqlPolicy / PostgreSQL reader / ResultValidator]
    L --> M[保存有效结果工件并按合同展示及绘图]
    M --> N[保存结果引用 / 历史摘要并结束 run 审计]
```

这是正常链路；解析失败、SQL 拒绝、空结果、超预算和状态冲突分别有分支，不能认为每个请求都会到达最后一步。`/help` 等 workflow 命令保留 Vanna 自身入口。

有两个不同的模型上下文入口，当前不能混为一谈：

- **语义解释调用**在 Vanna 主对话之前发生，只接收当前问题、可用 Catalog 指标及部分 WorkingMemory；没有接入近期聊天原文、HistorySummary 或结果实体行。
- **Vanna 主对话调用**由 `Agent._build_llm_request()` 从会话消息经 `ContextBudgetFilter` 构造。它能看到保留的历史消息，`CatalogContextEnhancer` 还加入本轮 Catalog、计划及结构化状态。现阶段没有完成按“语义理解 / SQL 生成 / 回答”分别构建 history projection，历史工具调用中的 SQL 也尚未统一剥离。

`candidate_sql_generator.py` 的运行时请求构造器在 Handler 中生成候选组件的对齐证据，不代表默认在线 Provider 已被独立的 SQL-only 小模型替换。

## 3. 组件入口、输入输出与持久化

| 当前代码入口 | 输入 → 输出 | 更新触发与保存位置 | 失败边界 / 尚缺内容 |
| --- | --- | --- | --- |
| `chat_runtime.py:BudgetedChatHandler.handle_stream()` | ChatRequest + 认证用户 + 会话 → 路由结果、查询计划、SSE | 请求前创建所属会话及 run；计划生成后保存 state；主对话后保存摘要和结果引用 | 处理 stale state 冲突；尚无显式分析 task/turn 生命周期 |
| `conversation_store.py:PostgresConversationStore._get_sync()` | conversation ID + 用户 + workspace → Conversation 与全部消息 | 从 `app.conversations`、`app.messages` 读取，按 message_index 排序 | 不返回他人/其他 workspace 会话；消息模型未暴露数据库 message_id/index，无近期窗口/cursor 消息分页 |
| `conversation_store.py:_update_sync()` | 完整 Conversation → 事务提交与 state_version + 1 | 遍历所有消息，按 conversation/index 冲突不重复插入；metadata 写入 working_memory JSONB | `FOR UPDATE` + 版本比对；拒绝归属/版本冲突。并非增量追加接口，也不支持直接改写同 index 旧消息 |
| `semantic_intent.py:SemanticIntentInterpreter.interpret()` / `prompt()` | 当前问题 + 角色可见 Catalog + state → 类型化 SemanticIntent | 无独立记忆写入；Handler 记录到本轮 trace | Provider 异常最多重试一次；JSON/字段/角色校验失败不给 SQL 权限。只收到结果存在布尔值，没有实际实体引用 |
| `semantic_intent.py:select_intent_catalog()` / `route_semantic_intent()` | SemanticIntent + Catalog + 当前 state → CatalogSelection / QuestionRoute | 不独立持久化 | 校验指标、维度、归属、筛选和预算；当前额外筛选只支持一个两字母大写客户州，不支持任意条件编译 |
| `working_memory.py:WorkingMemory.from_mapping()` / `as_dict()` | 存储字段 ↔ 有界结构化状态 | `conversation.metadata['working_memory']`，最终进 conversations JSONB | 忽略摘要等未知字段；当前混合查询条件、待澄清项和上一结果，尚无 task ID / query state version |
| `chat_runtime.py` 中语义分支的 `replace(memory, ...)` | 本轮已准入 SemanticIntent → 更新后的 WorkingMemory | SQL 执行之前保存；澄清时更新 pending_question/pending_missing | 当前是整体字段替换，不是明确的 set/add/remove patch；旧结果可与新待执行条件共存 |
| `analysis_lifecycle.py:prepare_turn_memory()` / `clear_active_analysis_state()` | Conversation + 当前 workspace → 可复用记忆或清空状态 | 版本漂移清 state 和摘要；手动清空保留消息与叙事摘要 | 尚无 task/reset 边界；未来接历史时必须防止旧条件被重新激活 |
| `context_builder.py:ContextBudgetFilter.filter_messages()` | 主对话全部消息 → 近期完整轮次及省略边界/可选语义摘要 | 在构建 Vanna LLM 请求时运行；按 source digest 使用进程内缓存 | 摘要失败返回 provenance 边界；超大当前轮次有压缩分支，不能等同所有极端预算下工具配对已验证 |
| `history_summary.py:HistorySummary` / `build_llm_summary_provider()` | 省略前缀 turns → 来源证据和可选自然语言摘要 | Handler `_persist_history_summary()` 保存到 metadata；Store 写入 `working_memory.__history_summary` | 压缩器未读持久摘要续接；summary_prompt 每消息最多 2,000 字符、总计最多 8,000 字符，但来源范围按整个省略区记录 |
| `run_recorder.py:PostgresRunRecorder.start()` / `finish()` | request、用户、版本、BudgetUsage → AgentRun 与审计结果 | `app.agent_runs` 保存一次请求；runner 的 SQL 审计通过 run_id/request_id 关联 | 已有执行证据基础，不是完整 task/state/execution 记忆；一个 run 可能含 SQL 重试，不能把 run_id 当成一次 SQL 尝试 ID |
| `trusted_sql_tool.py:TrustedRunSqlTool.execute()` / `_remember_result_artifact()` | SQL 工具参数 + 计划/结果合同 → ToolResult 与工件指针 | 计划 guard → runner 的策略/执行/结果校验；只有 validation.state=valid 才保存完整结果 CSV | 不以工具 UI success 代替校验；受控 repair 有独立证据，不支持无限修复 |
| `result_artifact_store.py:ResultArtifactStore` | 有效结果与 scope → CSV/Plotly manifest；按 scope 回读 | 仓库外，按 user/workspace/artifact 隔离，保存版本、大小和完整性证据 | Store 校验 scope 和文件完整性，版本适用性由生命周期/调用方判断；没有可信结果实体/排序绑定接口 |
| `result_artifact.py:ResultArtifact.from_trusted_usage()` / Handler `_persist_result_artifact()` | 本轮可信摘要 + trace → 会话内结果引用 | 主对话后存入 WorkingMemory.previous_result_artifact；图表另有历史引用 | 保存摘要、列、版本、CSV/图表指针；不保存可用于“这个州”的排名行值及原计划版本 |

关键阅读路径：

1. [Web 装配](../../examples/trusted_olist_web_demo.py) → [Handler](../../src/data_analysis_agent/chat_runtime.py)。
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
| ConversationHistory | 有序消息原文及完整 turns，是意图与对话脉络来源 | conversations/messages + Vanna Conversation.messages | 增加近期读取、稳定消息/轮次关联；原文不等于已执行事实 |
| AnalysisState | 一个 active task 当前想查询的结构化条件 | 当前由 WorkingMemory 部分承担 | Phase 1 定义唯一 canonical state 和 patch；旧 WorkingMemory 只做兼容映射，不双写两份权威状态 |
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
| 历史整段读取/遍历，稳定 message_id 未传到消息读取视图；长聊天每轮成本增长 | Phase 0.5：窗口读取、增量追加和 turn 关联；兼容 Vanna 现有存储调用 |
| 语义解释器看不到近期问答和实体值；“这个州”缺少可靠解析路径 | Phase 3/4：一次语义调用提出 reference request，服务端从 artifact 绑定，再装配各类调用的上下文 |
| 当前 state 在执行前更新，旧 artifact 缺少原计划版本；新查询失败后可能混淆两个范围 | Phase 1/2：分开当前意图与 last-successful execution/plan/artifact |
| 版本漂移有清空逻辑，但手动清空仍保留历史/摘要；接入近期历史后旧条件可能回流 | Phase 1：task/reset 边界；清空后不自动重建旧条件 |
| 持久摘要未被压缩器用于续接；来源证据覆盖范围可大于实际送入摘要模型的内容 | Phase 4：增量摘要、实际消息 watermark、重启恢复和源编辑失效 |
| 历史 SQL/tool 消息仍可能进入主对话，尚无按调用用途拆分的投影 | Phase 0.5/4：存储保留原文，模型读投影；本轮合法工具调用/返回仍按协议配对 |
| ContextBudgetFilter 类注释仍称摘要不含自然语言且不持久化，与当前 summary_provider/Handler 行为不一致 | Phase 4 更新相关注释；当前基线以实际调用为准，不依据旧注释宣称能力 |

这些缺口不要求新增 Router、向量库或第二次引用理解 LLM。首个产品验收仍是“最高州 → 这个州的品类”，同时验证沿用/替换/撤销条件、失败后结果归属、清空和刷新。

## 7. 验证证据与结论边界

2026-10-15 在 `data-analysis-agent` conda 环境运行现有定向测试：

```bash
conda run -n data-analysis-agent python -m pytest -q \
  tests/test_semantic_intent.py tests/test_runtime_handoffs.py \
  tests/test_working_memory.py tests/test_analysis_lifecycle.py \
  tests/test_context_builder.py tests/test_result_artifact.py \
  tests/test_result_artifact_store.py tests/test_postgres_conversation_store.py
```

结果：**68 passed, 6 skipped**。6 项数据库测试默认要求显式启用，随后加载本地环境配置并启用项目数据库单独执行：

```bash
RUN_PROJECT_DB=1 conda run -n data-analysis-agent python -c \
  'from dotenv import load_dotenv; load_dotenv(".env"); import pytest; raise SystemExit(pytest.main(["-q", "tests/test_postgres_conversation_store.py"]))'
```

结果：**6 passed**，覆盖工具消息回读、owner 隔离、参数校验、workspace/state version 及撤回最后一轮。两次合计验证 74 项不同测试；本阶段无业务逻辑变更，不新增镜像实现的测试。

可确认：当前语义路径不回退旧准入；现有状态解析、摘要生成失败回退、结果工件读写/完整性和会话存储行为符合已覆盖测试。`test_runtime_handoffs.py`、部分 WorkingMemory/生命周期测试还在覆盖旧兼容路径，不能将总通过数全部算作新语义多轮能力。

未验证：本阶段没有调用真实模型、没有运行新多轮产品验收或浏览器回归；尚未实现的历史接口、增量摘要、任务状态及结果实体绑定不能由上述测试推出。SQL 可执行与合同通过也不保证任意业务语义正确。

Phase 0 的三个任务已完成；下一实施单元是计划中的 **Phase 0.5：ConversationHistory 读写与关联**。
