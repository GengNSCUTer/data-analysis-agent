---
goal: "Build a stateful, evidence-backed context and memory architecture for the data analysis Agent"
version: "2.1"
date_created: "2026-10-14"
last_updated: "2026-10-15"
owner: "GengNSCUTer/data-analysis-agent"
status: "In progress"
tags: ["architecture", "memory", "context", "multi-turn", "result-reference", "data-analysis"]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

本方案将当前数据分析 Agent 的上下文和记忆能力改造成“有状态、可追溯、可验证的分析工作流”。它吸收 [`docs/数据分析Agent上下文设计.md`](../docs/数据分析Agent上下文设计.md) 对对话记忆、分析状态、执行记忆和语义记忆的划分，也结合当前已有的 `WorkingMemory`、`ResultArtifact`、`HistorySummary`、workspace/version 和 SQL 治理链。

本方案的核心判断是：

```text
聊天历史负责理解自然语言脉络
分析状态负责描述当前分析任务
执行记忆负责证明 SQL 如何执行以及结果是否通过验证
结果引用负责把“这个州/刚才那个品类”绑定到已验证结果
上下文压缩负责控制 token，不负责创造业务事实
```

目标场景是让下面的链路可复现地工作：

```text
哪个州交易额最高？
    -> 得到已验证结果，第一名为 SP
这个州哪些商品种类卖得好？
    -> 服务端把“这个州”解析为 customer_state=SP
    -> 保留合适的指标和时间范围，改为按商品类别分组
    -> 生成新的 QueryPlan 和 SQL
```

本文件是实施计划，不把当前已有的单轮查询能力、SQL 安全能力或离线后训练工作误写成多轮记忆已经完成。

### 记忆层设计决定（2026-10-15）

本节是实施时的统一设计。`ConversationHistory` 是必须明确实现的基础层，不再隐含在“已有会话存储”中。采用五类持久数据和一个上下文装配器；“五类”是逻辑职责划分，不要求新建五套存储系统。

| 数据 / 组件 | 保存和负责什么 | 持久化与读取方式 | 不能承担什么 |
| --- | --- | --- | --- |
| ConversationHistory | 用户原话、助手回复、工具调用与返回、消息顺序和轮次关联 | 复用 PostgreSQL `app.conversations` / `app.messages`；原文按消息保存，按完整 turn 读取 | 助手写过的数字不能代替实际查询结果；历史工具内容不能自动升级为当前指令 |
| AnalysisState | 当前分析任务的指标、时间、分组、筛选、待澄清项及状态版本 | 首版复用 `working_memory` JSONB，迁移为唯一 canonical state；同一会话只保留一个 active task | 不能承载全部聊天记录；不能把执行失败前的查询条件冒充已成功结果的条件 |
| ExecutionMemory | 哪次查询、对应哪个计划/状态、成功还是失败、哪个结果工件 | 复用现有 run recorder / SQL 审计记录，补齐 task / turn / execution 关联，提供结构化读取视图 | 不能只凭“调用过 SQL”认定结果可信；不重复建设另一套日志系统 |
| ResultArtifact | 已验证结果的完整数据、预览、图表及可引用实体 | 复用现有工件存储和访问控制；模型只读取有界元数据/引用，完整结果通过受控分页/导出读取 | 不能把全表塞进 prompt；自然语言结果摘要不能代替结构化行值 |
| HistorySummary | 已压缩历史的目标、讨论脉络、未解决问题和来源范围 | 持久化 summary 文本、覆盖到的消息位置和版本；重启后读取并增量更新 | 不能独立创建指标定义、筛选条件、权限或结果事实 |
| ContextBuilder | 针对一次模型调用从上述数据中装配有界上下文 | 请求内构建；记录所用来源和预算；不作为另一份长期记忆 | 不能把所有历史、审计 SQL 和完整结果统一灌给每个模型 |

数据关系为：一个 conversation 包含多个完整 turns 和按时间先后发生的 analysis tasks；首版每次只激活一个 task。一个 task 可以有多次 execution，每个成功并通过验证的 execution 关联自己的 artifact。summary 只覆盖历史的一个连续前缀，近期完整 turns 保留在前缀之后。

业务知识继续由现有 Semantic Catalog 提供，包括指标定义、表列含义、关联和可用维度；它按 workspace/version 加载，是受维护的业务知识层。暂缓的是自动学习用户偏好、跨会话回忆等长期个人记忆，不是取消已有 Catalog。

#### ConversationHistory 的具体实现边界

复用 `conversation_store.py`，不新建向量库或独立聊天数据库。数据库已有稳定 `message_id` 和 `message_index`；新读取视图必须暴露这两者，并用服务端生成的 `turn_id` 将一条用户输入、后续工具调用和助手答复归为同一轮。`request_id` / `run_id` 若已有则复用作为关联，不额外发明 hash 身份。

每条消息继续保存 `role`、`content`、`tool_calls`、`tool_call_id`、时间，并增加必要的 `turn_id`、`task_id` / `execution_id` 关联。数据库中的工具调用原文可以保留，但模型历史投影剔除完整结果、历史原始 SQL 和凭证；当前 SQL 生成/执行所需内容仍走原有受控工具链。工具调用与对应工具返回要成组保留，不能留下无配对的 tool 消息。

区分三个读取用途：

1. **页面回看**：按 cursor 分页读取消息，可以逐步看到完整聊天。翻看历史不自动改变 active task。
2. **模型近期上下文**：只读取最新完整 turns，并受该次调用的 token 预算约束；当前用户问题单独传入，不重复加入历史。
3. **旧历史定位**：仅在用户提到较早讨论时，读取同一会话的少量指定 turns / 任务索引。历史结果引用仍需加载对应 execution/artifact 并验证，不能从助手句子中提取“SP”直接执行。

当前 `get_conversation()` 会读取全部消息，`update_conversation()` 会遍历全部消息并以 `(conversation_id, message_index)` 去重插入。改造目标是给在线路径增加近期窗口读取和增量追加，而不是每轮把整段历史读出后再全部遍历。历史页面分页单独使用读取接口；兼容现有 Vanna 调用方，不一次性重写所有存储接口。

重试、编辑和并发的语义沿用现有明确入口：普通消息增量追加；服务端重试定位同一 turn，不重排旧消息身份；现有“撤回最后一轮再重发”操作保留，并使覆盖该轮的摘要及结果引用失效。并发更新仍复用现有 state version 与事务，不使用静默覆盖。失败/中断的轮次有明确状态，不伪装成成功答复。

#### 三种模型上下文分开装配

| 调用 | 输入内容 | 首版预算 / 行为 |
| --- | --- | --- |
| 语义解释 | 当前问题、近期对话、active state、待澄清项、可信结果引用信息、叙事摘要、可用 Catalog 能力 | 初始近期窗口最多 6 个完整 turns / 6,000 tokens，摘要最多 1,000 tokens，状态和引用最多 2,000 tokens；同时受整体输入预算约束 |
| SQL 候选生成 | 已解析并验证的 QueryPlan、Catalog、ResultContract 和本轮问题，以及原有受控生成上下文 | 不依赖历史助手回答补齐 SQL 条件；不默认附加所有历史工具调用和完整结果 |
| 最终回答 | 本轮已验证结果摘要、执行口径、对应计划、展示要求和必要的近期叙事 | 复用现有最多 8 行 / 1,200 字符模型结果摘要；完整结果通过前端预览、分页/导出展示 |

上述语义窗口值是首版可配置默认值，不是新的业务准入门。64K prompt 是整体上限，48K history 是原有全局 ceiling，并非每个调用都要填满 48K。预算计算必须包含 system、Catalog、工具定义与输出预留；实际不足时先减少旧 turns / 叙事摘要，当前问题、计划和必要引用缺失时明确处理，不静默截断后继续执行。tokenizer 为兼容估算时据实记录。

语义解释器一次调用提出 `action + state_patch + reference_requests`，服务端验证并应用；不额外增加一个“先让模型读历史、再让另一个模型提取引用”的调用。用户原话是意图来源，可以提出新的指标、日期或州筛选；助手文字和摘要只能辅助理解讨论对象，不能作为执行事实来源。比如“这个州”必须解析到可信 artifact 中的州值，再形成受控 filter。

#### 状态更新、摘要和恢复

状态明确分为“当前想查询什么”和“上次成功查到什么”：active state 可在澄清或执行前更新，但 `last_successful_execution_id` / artifact 连同其 plan/state version 单独保存。新 SQL 失败时，旧成功结果仍可查看，不能被标成新条件下的结果。

滚动摘要使用“已保存摘要 + 新进入省略区的完整 turns”增量生成，保存实际覆盖到的 `message_id` / 顺序位置和源版本；模型单批容纳不下时分完整 turns 处理，不能只输入前 8,000 字符却宣称覆盖整个省略区。未完成压缩的历史不推进 watermark。刷新/重启复用持久摘要，进程缓存只用于加速。

清空分析保留聊天记录，但创建新的 task 边界并清除 active state / active artifact；旧摘要和旧消息不能自动重新激活旧条件。用户明确要求回到旧分析时，才按 task/execution 关联恢复并重新验证版本和权限。workspace / 数据 / 指标版本改变后，聊天原文仍可回看，旧查询状态、结果引用和相关摘要标为不可直接执行。长期偏好记忆本轮暂不实现。

#### 现有能力与待实施缺口

已经存在：PostgreSQL 消息持久化、workspace/owner 隔离、state version、WorkingMemory、结果工件和摘要生成/保存。尚未完成：显式 turn/task/execution 历史关联、近期窗口读取、语义解释器读取近期对话及可信引用、持久摘要增量复用、结果实体下钻。当前解释器只读取部分 WorkingMemory 字段及 `has_trusted_result`；当前摘要主要命中进程内缓存，持久摘要未成为压缩器重启后的续接输入。上述表格描述的是目标设计，不是完成声明。

## 1. Requirements & Constraints

- **REQ-001**: 同一个 `conversation_id` 可以按时间先后包含多个分析任务；首版只有一个 active task，可按受控关联回看旧任务，不实现并行任务编排；不得把整个聊天记录直接当作当前 SQL 状态。
- **REQ-002**: `AnalysisState` 必须保存当前任务的指标、时间范围、维度、筛选、比较、时间粒度、结果形态、workspace/version 和状态版本。
- **REQ-003**: 每一轮用户输入必须被解释为新任务、当前任务的增量修改、当前结果的展示操作、结果下钻引用或需要澄清的请求之一。
- **REQ-004**: 当前任务状态通过受控 patch 更新；新增、替换、撤销和清空条件必须有明确语义，不能让模型每轮自由重写全部状态。
- **REQ-005**: 每次真实 SQL 执行都必须生成独立的执行证据，至少能关联 task、state version、QueryPlan、SQL hash、数据版本、结果工件和验证状态。
- **REQ-006**: 结果实体引用只能来自通过 `ResultContract`/`ResultValidator` 的可信结果工件；不能从助手回答、历史摘要或模型臆测中提取 SQL 条件。
- **REQ-007**: 首个结果引用版本支持有排序合同的“上一轮排名第 N 的分组实体”，优先实现第一行；验证对应行的维度值和排序语义。若只查询 Top-1，不宣称已经证明不存在并列；已知并列且用户要求唯一最高时澄清，无法确定所指行或工件已过期时不绑定。
- **REQ-008**: 完整聊天历史、分析状态、执行记录、结果原始工件和摘要必须逻辑分离，并保留各自的 owner、workspace、版本和生命周期边界。
- **SEC-001**: 所有新生成的筛选条件仍必须经过 Catalog、QueryPlan、SqlPolicy、PostgreSQL reader role、ResultContract 和 ResultValidator；结果引用不得绕过任何一层。
- **SEC-002**: 用户消息可以作为意图来源，提议指标、日期和筛选，经 Catalog/计划验证后进入 state；助手回复、摘要和客户端 metadata 不能成为权限、指标定义、SQL 或已执行结果事实的权威来源，也不能未经验证自动恢复旧条件。
- **SEC-003**: 结果引用必须校验用户、workspace、dataset version、metric version、artifact 完整性和当前会话权限。
- **SEC-004**: 原始 SQL 和完整结果可保留在受限审计/工件存储，但不得因为支持多轮记忆而注入完整 SQL、全表或隐藏行到普通模型上下文。
- **CON-001**: 保留现有 Vanna/FastAPI/PostgreSQL 在线架构；本方案不要求立即改写为 LangGraph，也不新增没有验收场景的 Redis、向量数据库或多 Agent。
- **CON-002**: `QuestionRouter` 不再承担线上自然语言关键词路由；在线语义入口继续使用 `SemanticIntentInterpreter` 和服务端准入策略，旧 `classify/admit` 仅保留离线兼容，后续可做职责拆分和改名。
- **CON-003**: 现有 SQL 生成、只读执行、结果预算、图表确定性生成和离线训练/评测协议保持不变，除非独立任务明确修改并回归验证。
- **CON-004**: 当前实现优先支持单 workspace、单用户会话内的短期分析状态；跨会话长期事实记忆暂不纳入首个实现。
- **GUD-001**: 对状态和执行证据采用结构化数据；对自然语言历史采用有界最近窗口和可追溯摘要；对旧历史采用按需召回而不是默认全部注入。
- **GUD-002**: 按既定阶段完成可验证的改造，不额外拆分阶段或重复冻结合同；优先复用主键、类型、事务、版本和普通测试。每阶段完成后运行完整请求链路与浏览器测试，不以“看起来更智能”为验收标准。
- **PAT-001**: 采用“原始事实可回放、派生摘要可重建、权威状态不可由摘要覆盖”的分层模式。
- **PAT-002**: 结果复用与查询修改必须分开：改变图表或分页优先复用可信工件；新增筛选、维度、指标或比较要求必须生成新的受控查询。

## 2. Implementation Steps

### Implementation Phase 0 — 基线和职责冻结

- **GOAL-001**: 统一当前代码、设计文档和术语，明确 `QuestionRouter`、`SemanticIntent`、`WorkingMemory`、`ResultArtifact`、`HistorySummary` 各自的职责。

2026-10-15 完成。代码事实、读写/失败边界表、术语及验证证据见 [Phase 0 基线](../docs/architecture/analysis-context-memory-v2.md)。现有回归 68 passed / 6 skipped，随后单独启用项目数据库 6 passed；不代表后续记忆改造或真实多轮验收已经完成。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | 审阅 `chat_runtime.py`、`semantic_intent.py`、`question_router.py`、`working_memory.py`、`result_artifact.py`、`context_builder.py` 和 `conversation_store.py`，形成当前入口、状态来源、更新触发和失败边界表。 | ✅ | 2026-10-15 |
| TASK-002 | 在 `docs/architecture/` 新增上下文与记忆基线，明确线上语义路径不调用旧 `classify/admit`，但保留 `QuestionRouter` 的 Catalog/准入辅助职责和离线兼容路径。 | ✅ | 2026-10-15 |
| TASK-003 | 为后续状态、执行记忆和结果引用冻结术语：`ConversationHistory`、`AnalysisState`、`ExecutionMemory`、`ResultArtifact`、`ReferenceBinding`、`ContextCompaction`。 | ✅ | 2026-10-15 |

### Implementation Phase 1 — AnalysisState 与增量状态更新

- **GOAL-002**: 将当前会话的分析状态从松散的 `WorkingMemory` 字段提升为可版本化、可解释的 active analysis task。

按用户 2026-10-15 要求，ConversationHistory 读写与关联直接并入本阶段，不单独拆阶段；只使用既有消息主键、metadata、JSONB、事务与 state_version。阶段验收包含完整请求链路和真实浏览器。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-030 | 在 `conversation_store.py` 的 `_message_from_row()` 与 SQL 查询中暴露已有 `message_id` / `message_index`，在消息 metadata 保存服务端 turn 关联；旧消息按 user-led 完整轮次兼容读取。 | ✅ | 2026-10-15 |
| TASK-031 | 增加近期完整 turns、cursor 消息分页与增量追加，在线默认读取近期窗口；保持历史回看及现有事务/state version。 | ✅ | 2026-10-15 |
| TASK-032 | 贯通 turn/request/run/task 关联，失败状态复用 run 审计；保留撤回重发及摘要失效语义。 | ✅ | 2026-10-15 |
| TASK-004 | 新增 `src/data_analysis_agent/analysis_state.py`，定义 `AnalysisState`、`StatePatch`、`StatePatchOperation` 和 `AnalysisTaskStatus`；至少包含 task ID、state version、workspace/version、metric IDs、time range、dimensions、filters、comparison、time grain、result shape、active artifact ID 和 pending clarification。 | ✅ | 2026-10-15 |
| TASK-005 | 将 `WorkingMemory` 改造成兼容适配层或明确的持久化映射，不允许 `WorkingMemory` 和 `AnalysisState` 同时成为 SQL 状态权威；在线 QueryPlan 只能读取一个 canonical state。 | ✅ | 2026-10-15 |
| TASK-006 | 实现确定性的 patch 应用规则：`set`、`add`、`remove`、`clear`、`replace_task`；分别覆盖“再加一个筛选”“改成另一个地区”“取消地区筛选”“开始新分析”。操作仅限当前 Catalog/QueryPlan 能表达的条件；单值州筛选替换不冒充多值筛选，能力不足时明确说明。 | ✅ | 2026-10-15 |
| TASK-007 | 在 `conversation_store.py` 中持久化 active task/state version，并复用当前 workspace 过滤与乐观锁；状态冲突不得静默采用 last-write-wins。 | ✅ | 2026-10-15 |
| TASK-008 | 在 `analysis_lifecycle.py` 中定义新分析、清空当前分析、版本失效、会话刷新和恢复的状态转移；聊天历史保留，用 task 边界隔离旧条件；分开保存当前计划与最后成功 execution/artifact 的计划版本，失败后不得错配。 | ✅ | 2026-10-15 |

2026-10-15 验收完成：M1–M4 定向 Python 回归 **116 passed, 15 skipped**；项目 PostgreSQL 会话存储测试已包含在上述结果中。定向 Ruff、compileall、diff check 通过。真实浏览器通过 8 项组合回归，另有一项真实链路覆盖“查询 → 工件清单 → 刷新恢复 → 消息分页 → 清空分析上下文 → 清空后新查询”。浏览器验证使用真实认证、SSE、PostgreSQL reader、SqlPolicy、ResultContract 和 ResultArtifact。模型候选偶发不符合计划时，安全链路仍会拒绝该结果；这不被计为模型业务准确率。Phase 1 已完成，后续进入 Phase 2 执行记忆与可信结果工件复用。

### Implementation Phase 2 — ExecutionMemory 与可信结果工件

- **GOAL-003**: 让每一次 SQL 执行成为可回放、可审计、可判定是否仍可复用的执行对象。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-009 | 审阅 `run_recorder.py`、`TrustedRunSqlTool`、`ResultValidator`、`ResultArtifactStore` 和现有 PostgreSQL 审计表，优先复用已有字段；只有当前证据无法表达执行关系时才新增迁移。 | ✅ | 2026-10-15 |
| TASK-010 | 定义 `ExecutionMemory` 视图或数据类：复用 audit_id 作为 SQL 尝试 ID，关联 run/task/turn、父/当前 state version、QueryPlan 快照及结构化查询范围、model、逐次执行/验证状态、artifact ID、版本、时间与安全失败类别。原始 SQL 沿用受限 query_audits，不新增 Plan/SQL hash 或额外表。 | ✅ | 2026-10-15 |
| TASK-011 | 扩展 `ResultArtifact` 的结构化元数据，使其能够保存结果列、排序指标、排序方向、结果形态、结果版本、完整性摘要和有限的可引用分组实体；不通过解析自然语言摘要生成这些字段。 | ✅ | 2026-10-15 |
| TASK-012 | 将“只改变展示”“复用已验证工件”“需要重新查询”分别编码为可判定的复用规则；数据/指标/workspace 版本漂移、字段缺失、工件过期或验证不通过时禁止复用。 | ✅ | 2026-10-15 |

Phase 2 验收：设置 `RUN_PROJECT_DB=1` 的定向 Python 回归 **127 passed**，覆盖同一 run 先失败后成功、逐次验证归属、跨用户读取、工件版本/范围变化、文件篡改/丢失及原有安全执行链。真实浏览器组合回归 **9 passed, 9 deselected**，其中一项使用真实模型与 PostgreSQL 覆盖查询、执行证据、刷新、结果解释不重复查库、清空及新查询。其余覆盖历史恢复、新会话、结果展示、移动端和受控 SSE 交互，不全部计为模型准确率。Ruff、compileall、diff check 通过。没有新增表、迁移、Plan/SQL hash、兼容层或额外 LLM 调用。旧 unknown 工件不回填；文件失效沿用既有版本/权限/完整性检查，不新增任意 TTL。下一步进入 Phase 3 可信实体引用解析。

### Implementation Phase 3 — Trusted ResultReferenceResolver v1

- **GOAL-004**: 支持第一个真实多轮下钻场景：上一轮排名结果中的实体可以被下一轮安全引用。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-013 | 在 `semantic_intent.py` 增加受限的 `reference_requests` 结构，首版只允许 `previous_ranked_entity`、维度、rank 和预期来源工件，不允许模型直接提交任意 dimension/value SQL 条件。 | ✅ | 2026-10-15 |
| TASK-014 | 新增 `src/data_analysis_agent/result_reference.py`，从当前有效 `ResultArtifact` 的结构化 binding 中解析引用；校验用户/workspace/version、结果列、排序合同、rank 范围、所指行的维度值和工件完整性；按 REQ-007 区分第一行与唯一最高。 | ✅ | 2026-10-15 |
| TASK-015 | 在 `chat_runtime.py` 的语义意图到 QueryPlan 之间加入引用解析；解析成功后生成服务端拥有的 filter binding，再由 Catalog/QueryPlan/SqlPolicy/reader/ResultValidator 继续执行。 | ✅ | 2026-10-15 |
| TASK-016 | 对“最高州 → 这个州哪些商品种类卖得好”实现端到端回归：第一轮保存 `customer_state=SP` 等结构化 binding，第二轮保留适用指标/时间条件、改为商品类别维度并生成新的查询。 | ✅ | 2026-10-15 |
| TASK-017 | 为引用失败定义可解释分支：没有可信工件、版本失效、没有排序依据、并列无法唯一确定、维度不允许筛选、用户显式覆盖旧引用时分别澄清或按新条件查询。 | ✅ | 2026-10-15 |

2026-10-15 验收完成：`ReferenceRequest` 只暴露受限引用字段；`ResultArtifact` 保存最多 8 条
有界排名实体；`ResultReferenceResolver` 校验当前工件的用户/工作区、版本、验证状态、输出列、
排序方向、排名范围和并列；成功值由服务端写入 `AnalysisState` 筛选后继续走原有查询治理链。
定向联合回归 **193 passed**，定向 Ruff、compileall、diff check 通过。真实浏览器跨轮验收通过：
第一轮排名查询、第二轮实体下钻均经过登录、SSE、语义解释、PostgreSQL、ResultValidator 和会话
持久化，第二轮得到 `customer_state=SP`；独立测试 **1 passed**。完整浏览器组合为 **9 passed**，
另有一次在线模型提供方超时；台账显示引用已解析但后续候选调用超时，未输出未经验证结果。
Phase 4 的上下文压缩不提前开始。

### Implementation Phase 4 — 短期上下文包与滚动压缩 v2

- **GOAL-005**: 让上下文压缩服务于当前任务连续性，而不是让摘要承担业务事实记忆。

2026-10-16 本轮任务卡：输入为当前问题、同会话近期完整 turns、持久摘要和当前分析状态；输出为
有界语义 context packet、增量摘要与真实消息 watermark，以及各模型调用的完整预算证据。
复用现有 JSONB/事务/消息索引，不新增数据库表、hash 或兼容分支。摘要失败保留上一有效摘要，
不推进覆盖范围；清空/版本变化后旧摘要不进入当前任务。验收包含数据库重载、增量续接、权限隔离、
超预算、摘要失败及浏览器真实长会话。非目标为跨会话记忆、任意历史结果自动恢复和生产 SSO。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-018 | 将每次模型调用前的上下文拆成明确的 packet：当前用户问题、active `AnalysisState`、最近可信 `ExecutionMemory`/`ResultArtifact` 引用、未解决澄清、最近完整 turns、历史摘要和本轮可用工具。 | ✅ | 2026-10-16 |
| TASK-019 | 改造 `ContextBudgetFilter`/`HistorySummary`：读取持久摘要，以旧摘要和新增省略 turns 增量生成，保存真实覆盖 watermark，支持重启续接和源消息编辑后失效；不得只摘要前 8,000 字符却标记全范围已压缩。state、binding 和 execution 不由摘要覆盖。 | ✅ | 2026-10-16 |
| TASK-020 | 保留 64K prompt / 48K history ceiling，语义解释器接入最多 6 turns / 6,000 tokens 的近期投影、最多 1,000 tokens 摘要和 2,000 tokens state/binding；整体预算包含 system/Catalog/tools/输出预留，记录实际 tokenizer mode、来源范围和 fallback；不把估算标成精确 token。 | ✅ | 2026-10-16 |
| TASK-021 | 增加线程内按需历史召回的最小接口设计：只在当前问题与旧 turn 相关时检索同一 conversation 的受限消息片段，默认上限为 3 个片段/800 tokens，记录 recall source；本阶段不实现跨会话向量记忆。 | ✅（显式接口，未自动触发） | 2026-10-16 |
| TASK-022 | 验证版本失效、清空当前分析和摘要失败时，active state、结果引用和权限边界均 fail closed；摘要失败只影响叙事连续性，不影响 SQL 安全链。 | ✅ | 2026-10-16 |

Phase 4 验收：项目数据库开启的定向 Python 联合回归 274 passed；浏览器组合 10 passed、
1 failed（30 秒内未生成摘要；状态及可信结果正常保留），长对话用例随后独立复验 1 passed，
59.58 秒；补齐无工具解释入口的可信查询范围后，最终独立浏览器验收 1 passed、49.59 秒。
未降低断言或增加自动重试。细节见 PROJECT.md 及架构文档第 10 节；
Phase 5 的整体正确率、时延和召回指标尚未开展。

### Implementation Phase 5 — 多轮产品验收与指标化

本轮任务卡（2026-10-16）：目标是联合验收 Phase 1–4 的真实多轮链路并修复实际失败；
不新增长期记忆、兼容接口或语言关键词兜底。输入为独立构造的公开 Olist 业务问题、现有
AnalysisState/Artifact 和真实浏览器会话；输出为可重复运行的多轮测试、仓库外逐轮证据、
脱敏汇总和必要修复。身份/workspace 隔离、版本事务、SQL 安全及摘要非事实来源保持不变。
验收分为确定性边界测试、真实模型/PostgreSQL 多轮浏览器测试，分别报告分母、失败类型、
模型调用和时延；召回尚无自动触发，自动召回命中率记为不适用。使用既有 run/audit ID
关联证据，不再添加新的 hash 或 baseline。

- **GOAL-006**: 用真实会话脚本确认上下文、分析状态和执行记忆在产品路径中共同工作。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-023 | 新增多轮合同用例：新任务、增加筛选、替换筛选、撤销筛选、切换指标、只改图表、结果下钻、清空分析、刷新恢复、版本失效和并发冲突。 | ✅ 受控矩阵覆盖；并发/版本用确定性回归 | 2026-10-16 |
| TASK-024 | 新增结果引用用例：排名第一、排名第二、并列、空结果、过期 artifact、显式新实体覆盖旧引用、恶意摘要/助手文本不得产生 SQL filter。 | ✅ 真实浏览器与确定性边界覆盖；并列拒绝路径保留 | 2026-10-16 |
| TASK-025 | 通过真实 SSE 和 PostgreSQL reader 运行受控小批，不把 protected test、完整 SQL 或完整结果写入仓库内 safe report；原始证据放仓库外并保存 hash。 | ✅ 证据在仓库外；safe report 不含 raw SQL/完整结果 | 2026-10-16 |
| TASK-026 | 统计并报告：状态 patch 正确率、引用解析准确率、引用后 SQL 计划一致率、结果复用正确率、错误结果拦截率、摘要触发率、召回命中率、模型调用次数和端到端延迟。 | ✅ 逐轮证据已记录；自动召回命中率 N/A，provider 失败单列 | 2026-10-16 |
| TASK-027 | 只有当上述指标和安全不变量通过后，才更新 `PROJECT.md`、架构文档和飞书项目文档；不能把单次 demo 通过写成完整多轮能力已经完成。 | ✅ 已更新；结论区分组件行为、外部依赖失败和未测业务泛化 | 2026-10-16 |

Phase 5 验收记录：确定性联合 Python 回归 **244 passed**；上下文/意图回归另行 **58 passed**。
真实浏览器多轮矩阵共 10 组：最新代码下 9 组通过，恶意摘要隔离场景在修正合法持久摘要证据后
单独复验通过；因此功能场景覆盖通过。既有浏览器组合 11 个选中用例中 10 个通过，唯一失败的
排名实体下钻重跑被记录为语义提供方 `semantic_service_unavailable`，没有进入 QueryPlan 或 SQL
执行；同一场景在完整矩阵中曾成功绑定 `customer_state=SP` 并生成新查询。该失败不能归因于状态、
引用或 SQL 安全实现。每轮 model calls、状态、run/audit 关联和延迟写入仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/analysis-memory-phase5-20261016/`；不宣称自动召回
命中率（当前仅显式 recall 接口），也不将这些场景通过写成任意业务语义准确率。

### Implementation Phase 6 — 长期语义记忆（暂缓）

- **GOAL-007**: 为未来跨会话业务偏好和用户记忆保留扩展边界，但不在当前短期记忆改造中实现。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-028 | 仅冻结跨会话 semantic memory 的未来接口：subject、scope、fact、source、version、expiry、consent 和删除语义；不接入在线 SQL 条件。 |  |  |
| TASK-029 | 在没有真实跨会话需求、隐私策略和评测集之前，不引入向量数据库、自动事实写入或跨 workspace 召回。 |  |  |

## 3. Alternatives

- **ALT-001**: 只把更多聊天历史和更长摘要塞进模型上下文。未选择，因为它不能可靠保存 SQL 执行证据、版本、结果实体和值绑定，也会增加 token、延迟和提示污染。
- **ALT-002**: 让模型直接从上一轮自然语言回答中抽取 `dimension=value`。未选择，因为回答可能遗漏、改写或幻觉化结果，不能成为 SQL 条件来源。
- **ALT-003**: 首先引入向量数据库做跨会话长期记忆。未选择，因为当前最紧迫的问题是同一会话内的分析状态和可信结果引用；向量召回不能替代版本和权限校验。
- **ALT-004**: 立即将现有 Vanna runtime 全部迁移到 LangGraph。未选择，因为当前架构已有会话存储、QueryPlan、SQL Policy 和 ResultArtifact；先解决状态合同和引用能力，再评估编排框架迁移。
- **ALT-005**: 保留旧关键词 Router 作为线上兜底。未选择，因为语义解释失败时应澄清或拒绝，不能重新启用固定句式闸门；旧路径仅用于离线兼容和明确未启用语义解释器的调用方。

## 4. Dependencies

- **DEP-001**: 当前 Olist workspace、Semantic Catalog、`SemanticIntentInterpreter` 和 QueryPlan/ResultContract 实现。
- **DEP-002**: `PostgresConversationStore` 的 workspace/state version 隔离和乐观并发控制。
- **DEP-003**: `TrustedRunSqlTool`、`SecurePostgresRunner`、`SqlPolicy`、`ResultValidator`、`ResultArtifactStore` 和 `PostgresRunRecorder`。
- **DEP-004**: 当前本地 tokenizer/`TokenCounter` 与 `RequestBudget` 的 64K prompt/48K history 配置。
- **DEP-005**: 真实 SSE、PostgreSQL reader 和仓库外 raw evidence 存储，用于多轮验收。

## 5. Files

- **FILE-001**: `src/data_analysis_agent/analysis_state.py`；新增 canonical AnalysisState 和 StatePatch 合同。
- **FILE-002**: `src/data_analysis_agent/working_memory.py`；兼容旧字段并迁移到 canonical state。
- **FILE-003**: `src/data_analysis_agent/semantic_intent.py`；增加受限引用请求，不允许模型直接提供任意 SQL 条件。
- **FILE-004**: `src/data_analysis_agent/result_reference.py`；新增可信结果实体引用解析。
- **FILE-005**: `src/data_analysis_agent/result_artifact.py`、`result_validator.py`；保存结构化排序/绑定元数据。
- **FILE-006**: `src/data_analysis_agent/execution_memory.py` 或已有 `run_recorder.py`；表达执行证据视图，优先复用现有审计表。
- **FILE-007**: `src/data_analysis_agent/chat_runtime.py`；接入 state patch、reference resolution 和上下文 packet。
- **FILE-008**: `src/data_analysis_agent/conversation_store.py`、`analysis_lifecycle.py`；持久化 active task、版本和恢复/清空生命周期。
- **FILE-009**: `src/data_analysis_agent/context_builder.py`、`history_summary.py`、`token_budget.py`；实现有界压缩、provenance 和按需召回接口。
- **FILE-010**: `tests/test_analysis_state.py`、`tests/test_result_reference.py`、`tests/test_execution_memory.py`、`tests/test_context_builder.py`；覆盖新合同。
- **FILE-011**: `tests/e2e/` 和 `scripts/` 下的多轮 SSE/浏览器合同；只记录脱敏聚合结果。
- **FILE-012**: `docs/architecture/analysis-context-memory-v2.md`、`PROJECT.md`、`docs/README.md`；记录当前事实、边界和验收证据。
- **FILE-013**: `infra/postgres/security.sql`、`tests/test_postgres_conversation_store.py`；复用 messages 主键和 metadata，必要时补读写索引/迁移，验证历史窗口、增量写入、重试和隔离。

## 6. Testing

- **TEST-001**: Semantic online path never calls legacy lexical `classify/admit`; legacy offline fixtures continue to pass without改变其冻结协议。
- **TEST-002**: AnalysisState patch tests distinguish add, replace, remove, clear and new-task behavior, and reject stale state versions.
- **TEST-003**: ExecutionMemory tests prove failed execution and successful execution are separate records, and a result artifact points to one validated execution/version.
- **TEST-004**: ResultReferenceResolver tests resolve current, validated bindings with a known row and ordering contract; distinguish first-row from unique-highest semantics, and reject missing, stale, ambiguous or unauthorized artifacts.
- **TEST-005**: The “highest state -> categories in this state” SSE test produces a second QueryPlan with a server-owned state filter and never derives that filter from assistant prose.
- **TEST-006**: Result-only follow-up reuses the verified artifact without a new SQL call; changing metric, dimension, filter or comparison creates a new execution.
- **TEST-007**: ContextBudgetFilter retains newest complete turns, inserts bounded summary with source digest, preserves active state/reference evidence, and falls back safely when summary generation fails.
- **TEST-008**: Thread-scoped recall never crosses user/workspace/conversation boundaries and injects no more than its declared token/fragment budget.
- **TEST-009**: Clear, refresh, version invalidation and concurrent update tests verify that old state and artifact references cannot silently affect new SQL.
- **TEST-010**: Browser tests cover a visible explanation of active analysis context, result evidence, clear-analysis behavior and recovery after a state conflict.
- **TEST-011**: Release audit reports state coverage, reference resolution accuracy, SQL/ResultContract acceptance, latency and model-call counts separately; no single “memory accuracy” score replaces these dimensions.
- **TEST-012**: ConversationHistory 窗口包含完整 turns/tool 配对；分页无丢失/重复；增量追加不重复旧消息；重载、撤回重发、失败/中断和并发语义正确。
- **TEST-013**: 持久摘要在进程重启后复用，新增省略 turns 实际进入增量摘要输入；watermark 不超过真正处理的消息，编辑已摘要轮次后失效，摘要失败不覆盖旧有效摘要。
- **TEST-014**: 当前计划执行失败时，上一成功 artifact 仍关联原计划；清空后旧历史/摘要不自动恢复筛选。用户显式提供新条件可经验证进入 state，助手编造的结果值不能直接生成引用 binding。

## 7. Risks & Assumptions

- **RISK-001**: Natural-language references such as “卖得好” may be ambiguous between GMV, order count and item count; the resolver must preserve the previous metric only when the drill-down relation is explicit, otherwise ask one minimal clarification.
- **RISK-002**: 排序结果可能并列或只查询 Top-1；绑定必须携带排序语义，不把第一行描述成已证明唯一最高。已知歧义时澄清，不为未知并列默认增加昂贵查询。
- **RISK-003**: A semantic summary can contain a plausible but incorrect narrative; it must remain non-authoritative and be ignored by state parsing.
- **RISK-004**: Long-running execution records and result artifacts may grow storage; retention and cleanup must be separate from prompt compaction and must not delete evidence still referenced by an active task.
- **RISK-005**: Introducing a second canonical state beside `WorkingMemory` can create drift; Phase 1 must complete the authority migration before later phases write new fields.
- **ASSUMPTION-001**: Existing result artifacts and audit traces can expose enough validated metadata to build deterministic bindings without exposing full rows to the semantic interpreter.
- **ASSUMPTION-002**: The first supported reference is same-conversation, same-workspace, current-version, previous-ranked-entity; cross-conversation recall is deferred.
- **ASSUMPTION-003**: The current product continues to use online provider model for semantic interpretation and SQL candidate generation; local Adapter integration is outside this plan.

## 8. Related Specifications / Further Reading

- [`docs/数据分析Agent上下文设计.md`](../docs/数据分析Agent上下文设计.md)
- [`docs/architecture/product-request-architecture-v1.md`](../docs/architecture/product-request-architecture-v1.md)
- [`docs/architecture/embedded-copilot-and-memory-design-v1.md`](../docs/architecture/embedded-copilot-and-memory-design-v1.md)
- [`src/data_analysis_agent/working_memory.py`](../src/data_analysis_agent/working_memory.py)
- [`src/data_analysis_agent/context_builder.py`](../src/data_analysis_agent/context_builder.py)
- [OpenAI: Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)
- [OpenAI Responses API compaction](https://developers.openai.com/api/docs/guides/compaction)
- [OpenAI Agents SDK memory and compaction cookbook](https://developers.openai.com/cookbook/examples/agents_sdk/building_reliable_agents_memory_compaction)
- [Anthropic Claude Code memory](https://code.claude.com/docs/zh-CN/memory)
- [LangGraph persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx)
- [Letta memory architecture](https://github.com/letta-ai/skills/blob/main/letta/agent-development/references/memory-architecture.md)
- [Aider chat history summarization](https://github.com/Aider-AI/aider/blob/main/aider/history.py)
