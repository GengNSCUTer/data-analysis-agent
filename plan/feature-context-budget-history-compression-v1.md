---
goal: Implement provenance-backed long-history compression and observable context budgets for the trusted Agent
version: 1.0
date_created: 2026-09-20
last_updated: 2026-09-20
owner: gengnan
status: In progress
tags: [feature, runtime, context, history, budget, safety]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

本计划将当前“只丢弃旧 turn、极端时截断 message 文本”的 `ContextBudgetFilter` 升级为可回读、无模型、按完整 turn 运行的历史压缩与预算记录机制。它只处理**同一会话内**的长历史，不建立跨会话长期记忆、向量检索或用户画像。

## 1. Requirements & Constraints

- **REQ-001**: `ContextBudgetFilter` 必须以完整 user-led turn 为选择单位；普通超预算不得把一个旧 tool message 单独保留，也不得在正常路径任意截断 message content。
- **REQ-002**: 当旧 turn 因 `max_context_chars` 或 `max_context_messages` 被移除时，向 LLM 输入插入一条 server-owned `HistorySummary v1` 边界消息；该消息只含被压缩的 turn/message 数、位置范围、内容 SHA-256 与固定免责声明，不含用户原文、助手回答、SQL、结果行、工具输出或模型摘要。
- **REQ-003**: 当前问题、最近完整 turn、`WorkingMemory`、`ResultArtifact`、Catalog、QueryPlan 与 ResultContract 的职责不变。HistorySummary 仅提供叙事连续性提示，不能成为 SQL 指标、时间、筛选、权限、ResultContract 或数据库事实来源。
- **REQ-004**: 若最新完整 turn 本身超出剩余上下文预算，系统必须保留当前 user message；必要时以与 tool-call ID 匹配的固定 server marker 压缩同 turn 的 tool content，而不是截断任意自然语言片段。该异常路径必须被观察记录。
- **REQ-005**: 每次 filter 必须记录 `context_budget` 结构化证据：输入/输出 message、turn、字符数；保留与压缩范围；summary 是否插入；异常 current-turn compaction；预算上限。证据不含原文或 SQL。
- **REQ-006**: `BudgetUsage` 与 `PostgresRunRecorder` 必须将上述脱敏 evidence 写入现有 `catalog_trace`，使仓库外 runner 可按 request 审计；safe report 只允许投影枚举状态、数量、布尔值和 digest，不能出现摘要内容或原始历史。
- **SEC-001**: 本任务不改变 `SqlPolicy -> PostgreSQL readonly role -> ResultContract / ResultValidator`，不允许历史文本绕过 Router 或补充 SQL 条件。
- **SEC-002**: 原始 SSE、原始 SQL、完整消息、结果行和完整诊断继续只放仓库外 `/disk2/gengnan/data-analysis-agent-data/`；Git 内的测试只使用无敏感 fixture。
- **CON-001**: 不能依赖在线 LLM 生成压缩摘要。历史压缩必须在本地、确定性、无网络调用下完成。
- **CON-002**: 本轮不新增数据库表或迁移；summary 是每次 LLM request 的瞬时上下文工件，持久会话仍保存原始历史、WorkingMemory 与 ResultArtifact v1。
- **CON-003**: `max_context_chars` 当前是 history filter 的字符上限，不是 provider tokenizer 的精确 token 计数，也不包括动态 system prompt/Catalog 全量文本。证据字段必须明确标为 character budget，不能宣称为精确 token budget。
- **GUD-001**: 先完成 deterministic unit/contract 测试，再做一个小批真实 Phase-C 长历史回归；不因改动 ContextBudget 而重跑 Phase B、模型训练或 protected final test。

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: 冻结无模型的 `HistorySummary v1` 结构、完整 turn 选择算法和数据不变量。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 新建 `src/data_analysis_agent/history_summary.py`。实现 frozen `HistorySummary`，字段固定为 `version`、`source_turn_start`、`source_turn_end`、`source_message_count`、`source_chars`、`source_sha256`、`reason`；实现 `as_message()`，固定输出带“不可作为 SQL/权限/业务事实”的 server-owned system message。 | Yes | 2026-09-20 |
| TASK-002 | 改造 `src/data_analysis_agent/context_builder.py`：先按完整 turn 分组，再预留 HistorySummary 字符预算并从最新 turn 倒序选择；仅在有被移除的旧 turn 时插入 summary。普通路径不得调用 message content slice。 | Yes | 2026-09-20 |
| TASK-003 | 为超大最新 turn实现受限 fallback：始终完整保留最新 user message；关联 tool message 改为固定 marker，保留 role/tool-call ID；记录 `current_turn_compacted=true`。如果当前 user message 单独超过预算，应保留并记录 `current_user_exceeds_budget=true`，由请求输入预算负责拒绝。 | Yes | 2026-09-20 |

### Implementation Phase 2

- **GOAL-002**: 记录可审计但不泄露原文的 context-source 与 character-budget evidence。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | 扩展 `src/data_analysis_agent/budget.py` 的 `BudgetUsage`，新增有界 `record_context_budget()` 与 `context_budget_evidence()`；保留现有 `context_chars/context_truncated` 兼容字段，并使 `as_dict()` 输出新的结构化 evidence。 | Yes | 2026-09-20 |
| TASK-005 | 改造 `src/data_analysis_agent/run_recorder.py`，将 `BudgetUsage.context_budget_evidence()` 写入已有 `catalog_trace["context_budget"]`，不改数据库 schema。 | Yes | 2026-09-20 |
| TASK-006 | 扩展 `scripts/run_product_capability_online.py` 的 `_safe_runtime()`，只投影 `context_budget` 的 reason、计数、布尔值、budget upper bounds 与 digest；修改 `scripts/run_product_capability_conversations.py` 的实际投影，供 Phase-C safe report 比较/展示。 | Yes | 2026-09-20 |

### Implementation Phase 3

- **GOAL-003**: 用定向测试证明压缩不会制造 SQL 事实或破坏当前回合结构。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | 扩展 `tests/test_context_builder.py`，覆盖：无超预算不插 summary；旧完整 turn 被 summary 替代；summary 无原文/SQL；无 orphan tool；最新 user 完整保留；超大当前 tool 内容变 marker；输出满足可用字符预算或明确记录 current-user 超限。 | Yes | 2026-09-20 |
| TASK-008 | 扩展 `tests/test_budget.py`、`tests/test_postgres_run_recorder.py`、`tests/test_product_capability_conversation_contract.py`，验证 context-budget evidence 有界、持久化、safe projection 无 SQL/错误/历史文本。 | Yes | 2026-09-20 |
| TASK-009 | 更新 `docs/architecture/embedded-copilot-and-memory-design-v1.md`、`docs/architecture/product-capability-baseline-v1.md`、`PROJECT.md`：明确 HistorySummary v1 是瞬时无模型压缩，不是长期记忆、不是持久化事实、不是 token 精确统计。 | Yes | 2026-09-20 |

### Implementation Phase 4

- **GOAL-004**: 对长历史路径做最小真实回归，不扩大为全量重新评测。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | 在当前 `data-analysis-agent-trusted` 服务重启到本轮 revision 后，使用 `pcv1-c-long-history-budget` 做一次新的仓库外 live run；保存原始 SSE 与受限诊断，生成 safe report。验收 evidence 存在且不泄露内容；不把 `completed` 写成业务语义正确。 | Yes | 2026-09-20 |
| TASK-011 | 运行本轮定向 pytest、ruff、compileall、`git diff --check`；汇报实际是否触发 compression、是否出现 current-turn fallback、测试空白与停止条件。 | Yes | 2026-09-20 |

## 3. Alternatives

- **ALT-001**: 在线 LLM 自动概括旧历史。未选择：模型摘要会增加调用、延迟和不可回读业务事实，且不应在当前 SQL Agent 中成为隐式条件。
- **ALT-002**: 仅增大 `max_context_chars`。未选择：不能解决成本、延迟、上下文污染和来源不可解释问题。
- **ALT-003**: 只保留最近消息、完全删除旧历史。未选择：已有 `WorkingMemory`/ResultArtifact 可维护关键事实，但仍缺少对“确实压缩过哪些范围”的可观测边界。
- **ALT-004**: 将 HistorySummary 写入 `app.conversations` 或向量数据库。未选择：这是持久化/长期记忆设计，超出本轮单会话、瞬时压缩范围。

## 4. Dependencies

- **DEP-001**: `src/data_analysis_agent/context_builder.py`、`budget.py`、`run_recorder.py`、`chat_runtime.py` 和 Vanna `ConversationFilter` 的当前接口保持可用。
- **DEP-002**: 当前 trusted demo 使用 `ContextBudgetFilter(max_chars=budget.max_context_chars, max_messages=budget.max_context_messages)`，并由 `CURRENT_BUDGET` 接收请求级 usage。
- **DEP-003**: Phase-C live regression 需要当前 trusted demo、项目 PostgreSQL 和 SiliconFlow 配置可用；不可用时保留 deterministic tests 并如实标为环境阻塞。

## 5. Files

- **FILE-001**: `src/data_analysis_agent/history_summary.py` — HistorySummary v1、hash 与 server-owned summary message。
- **FILE-002**: `src/data_analysis_agent/context_builder.py` — complete-turn selection、summary insertion、异常 fallback。
- **FILE-003**: `src/data_analysis_agent/budget.py` — context-budget evidence。
- **FILE-004**: `src/data_analysis_agent/run_recorder.py` — evidence 持久化到 catalog trace。
- **FILE-005**: `scripts/run_product_capability_online.py`、`scripts/run_product_capability_conversations.py` — safe evidence projection。
- **FILE-006**: `tests/test_context_builder.py`、`tests/test_budget.py`、`tests/test_postgres_run_recorder.py`、`tests/test_product_capability_conversation_contract.py` — 定向回归。
- **FILE-007**: `docs/architecture/embedded-copilot-and-memory-design-v1.md`、`docs/architecture/product-capability-baseline-v1.md`、`PROJECT.md` — 架构与阶段事实。

## 6. Testing

- **TEST-001**: Context filter 仅在省略旧完整 turn 时插入 HistorySummary，summary 不包含任意 fixture 原文或 SQL-like 内容。
- **TEST-002**: 完整 turn 边界与 tool-call ID 在普通/异常路径均不产生孤立 tool response。
- **TEST-003**: `BudgetUsage.as_dict()` 和 `agent_runs.catalog_trace.context_budget` 只含计数、布尔值、范围和 SHA-256；safe runner projection 不出现 message/SQL/error/result 内容。
- **TEST-004**: 定向 Phase-C long-history live run 产出新外部路径；报告明确记录是否触发压缩，且不据此宣称 SQL 业务正确。

## 7. Risks & Assumptions

- **RISK-001**: 当前 Vanna 会在每个 LLM round 应用 filter；同一用户请求内的 tool output 也可能触发压缩。缓解：完整 turn 优先、关联 tool marker fallback、定向测试。
- **RISK-002**: 现有 character budget 不等于模型 tokenizer token budget。缓解：字段与文档明确为字符预算；后续若需要 token 预算，单独使用实际 tokenizer/ provider usage 设计。
- **RISK-003**: 非语义 HistorySummary 可能不足以支持非常复杂的长对话叙事。缓解：先保证结构化 WorkingMemory/ResultArtifact 与安全；只有真实产品证据证明缺口后，再单独评审受控语义摘要。
- **ASSUMPTION-001**: 当前用户输入由 `RequestBudget.max_input_chars` 先限制，正常用户消息不会超过 history char budget。
- **ASSUMPTION-002**: 长期记忆、跨会话检索、向量库、用户偏好和 workspace migration 不在本计划内。

## 8. Related Specifications / Further Reading

- [嵌入式 Copilot 与对话记忆设计 v1](../docs/architecture/embedded-copilot-and-memory-design-v1.md)
- [产品能力基线 v1](../docs/architecture/product-capability-baseline-v1.md)
- [Product Capability Baseline 实施计划](feature-product-capability-baseline-test-v1.md)
- [Project v1 缺口与优化地图](../docs/architecture/project-v1-gap-map.md)
