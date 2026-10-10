# 开源 Agent 上下文、会话与记忆实现调研 v1

> **状态：** 代码级调研完成；仅提供设计参考，不引入第三方框架或源码。  
> **日期：** 2026-09-18  
> **问题：** 可嵌入数据分析 Copilot 如何处理长对话、会话持久化、压缩、并发与长期记忆？  
> **非目标：** 不推断任何闭源产品的内部实现；不把开源实现的许可证或依赖自动带入本项目。

## 1. 调研方法与边界

本机 `gh` 未登录，无法使用 GitHub Search API 的完整发现/排名流程。因此本轮采用公开仓库的只读浅克隆和实际源码阅读，不宣称“覆盖了全量产品”。克隆副本位于被 Git 忽略的 `github-research-output/agent-context-memory/`，以便日后按下表 revision 复核。

`openai/codex` 是公开的 Codex CLI 代码，能够说明该仓库的 thread/history/compaction 实现；它**不能**证明 ChatGPT 或托管 Codex 产品使用相同内部机制。

| 仓库 | Revision | License | 本轮阅读的核心文件 | 价值 |
| --- | --- | --- | --- | --- |
| [openai/codex](https://github.com/openai/codex) | `7498521d288b9b3b96ffba4eedf089d8d6e06a84` | Apache-2.0 | `codex-rs/core/src/compact.rs`、`session/mod.rs`、`protocol/src/turn_input.rs`、`thread-store/.../thread_history_materialization.rs` | 线程、上下文压缩、持久化 history、并发开始语义 |
| [Aider-AI/aider](https://github.com/Aider-AI/aider) | `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` | Apache-2.0 | `aider/history.py`、`aider/coders/base_coder.py`、`aider/repomap.py` | token 预算触发的后台历史摘要、上下文分块 |
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | `c81c13533ee48c1ae0ef2de314737ef0c455f2be` | MIT | `libs/checkpoint/.../base/__init__.py`、`libs/checkpoint-postgres/.../base.py`、`libs/checkpoint/.../store/base/__init__.py` | thread checkpoint、namespace、parent version、PostgreSQL 持久化与可选 TTL/vector store |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | `84bf468176f0c5e82493bb95aec5484eb2d92bc1` | Apache-2.0 | `mem0/memory/main.py`、`mem0/proxy/main.py`、vector store 抽象 | 通用长期记忆/向量召回的能力和对本项目不适用的风险 |
| [letta-ai/letta](https://github.com/letta-ai/letta) | `5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a` | Apache-2.0 | 根 README / 仓库结构 | 当前根仓库未包含可供本轮深入审阅的 Agent runtime 实现，故不作为结论来源 |

## 2. 结论先行

四个可审阅实现并没有支持“每轮把全部聊天做 embedding 再搜索”是唯一或默认的工程解。相反，它们共同体现了四个更基础的分层：

```text
持久化的完整记录 / thread
        !=
当前模型可见的 live context

受控状态或 checkpoint
        !=
从自然语言中自动抽取的长期事实

需要时的摘要 / compaction
        !=
删除历史或失去来源

身份 / namespace / thread 作用域
        !=
浏览器客户端自报的任意 ID
```

这与本项目应该优先实现的 `ConversationScope + AnalysisState + ResultArtifact` 一致。我们需要借鉴**状态分层、版本与并发语义**，不需要接入一套新的 Agent 框架。

## 3. Codex CLI：压缩 checkpoint 不等于遗忘线程历史

### 3.1 实际实现

`codex-rs/core/src/compact.rs` 不是简单截掉旧消息。压缩会：

1. 以 compaction prompt 对历史运行一个独立的 compact turn；
2. 构造 replacement history，并有 `window_number`、`window_id`、压缩 response/model hash 等 metadata；
3. 调用 `Session::replace_compacted_history()`；
4. 在 `session/mod.rs` 中为替换项分配 ID，持久化 `CompactedItem`、保留 context checkpoint、token usage 和 world-state baseline；
5. 发出压缩事件/telemetry，并显式警告多次压缩可能降低准确性。

`thread-store/src/local/thread_history_materialization.rs` 还把 append-only rollout 以 byte offset、ordinal 和异常记录投影到 SQLite。它会跳过不完整 JSON 行、重复 ordinal 和未知记录，但记录 anomaly，而不是假装一切正常。

并发方面，`TurnInputMode` 定义了 `StartIfIdle` 与 `ContinueIfIdle { expected_previous_turn_id }`；前者只允许 thread 空闲时开新回合，后者要求前一回合 ID 精确匹配。这是比“最后写入覆盖”更清晰的会话语义。

### 3.2 对本项目可吸收的部分

| Codex CLI 模式 | 本项目适配 | 不直接照搬的原因 |
| --- | --- | --- |
| live history 与持久化 rollout 分离 | 将 `app.messages` / `agent_runs` / `query_audits` 视为可回读记录；Prompt 只拿有预算的 context projection | 不需要 Rust rollout/event-sourcing 子系统 |
| compaction 是有 metadata 的 checkpoint | R4 `HistorySummary` 至少绑定 `source_message_start/end`、source hash、生成时版本 | 初版不应让模型摘要决定指标、筛选或 SQL |
| 压缩后重新注入基础 world state | Context assembly 每轮重新从服务器放入 Catalog、WorkspaceProfile、ResultContract，而不信任旧 chat 文本 | 我们没有 Codex 的 world state 类型，需保持 Python 领域合同 |
| idle / expected predecessor 限制 | 同一会话一个 in-flight request + `state_version` 乐观并发控制 | 不能把 CLI thread protocol 原样暴露给浏览器 |
| 压缩有事件、token 数据、错误分类 | 记录 context source、截断/摘要、state version、artifact reference 与耗时 | 仅记录脱敏聚合，不导出用户原始结果 |

### 3.3 明确不做的推断

- 不声称公开 Codex CLI 等于托管 Codex/ChatGPT 的记忆实现。
- 不为“看起来像 Codex”而加入多 Agent、world-state、SQLite rollout 复制品。
- 不以 LLM compaction 替代本项目的 SQL Policy、ResultValidator、Catalog 或版本 pin。

## 4. Aider：有预算的上下文分块与摘要提交条件

### 4.1 实际实现

`aider/history.py` 的 `ChatSummary` 先按模型 tokenizer 计数；超过 `max_tokens` 后保留一段较新的 tail，对旧 head 做摘要，必要时递归。`base_coder.py` 将消息区分为：

- `done_messages`：已完成的较早回合；
- `cur_messages`：正在进行的当前回合；
- `system`、examples、repo map、只读文件、聊天文件等独立 `ChatChunks`。

`summarize_start()` 在超预算时后台启动摘要，`summarize_end()` 仅当待摘要的源消息仍等于 `done_messages` 时才接受结果。这一点避免了后台摘要覆盖期间新追加的历史。组装 prompt 前，代码明确按 chunk 构造，而不是把所有字符串拼成一个不可解释的大块。

### 4.2 对本项目可吸收的部分

1. 把当前数据分析 Agent 的 prompt 输入显式分类为：当前问题、受控页面上下文、AnalysisState、ResultArtifact 投影、Catalog/QueryPlan/ResultContract、近期完整回合和可选历史摘要。
2. 每块都记录独立长度/来源/是否被截断；而不是只看总 `max_context_chars`。
3. 若将来引入后台 R4 摘要，必须带 `source_message_range + hash`，并且只有源范围未变、workspace/version 未漂移时才提交。

### 4.3 不能直接套用的部分

Aider 的摘要主要服务于代码编辑连续性。数据分析领域不能让摘要中的“GMV 是 xxx”“筛选了 xxx”自动变成下轮 SQL 条件。因此：

- `metric_ids`、时间、维度、筛选、权限必须来自结构化 R2，而非 R4 摘要；
- 上轮可信结果必须来自 R3 `ResultArtifact`，而非普通 assistant 消息；
- 不能把数据结果当成 Aider 的 repo map 一样的静态上下文。

## 5. LangGraph：thread checkpoint 与跨会话 store 是两种不同对象

### 5.1 实际实现

LangGraph 的 `BaseCheckpointSaver` 明确以 `thread_id` 保存/恢复每次 interaction 的 state。checkpoint 含有单调递增 ID、父 checkpoint、channel values、channel versions 和写入 metadata。PostgreSQL 实现的复合主键是：

```text
(thread_id, checkpoint_ns, checkpoint_id)
```

它另有 `BaseStore` 用于跨 thread 的长期内容。Store 的 `namespace + key` 是主作用域；结构化 filter、TTL 和向量索引均是可选能力，而不是 checkpoint 的替代品。

### 5.2 对本项目可吸收的部分

- `conversation_id` 之外，需要显式的 `workspace_id` 和 `state_version`；workspace 是安全作用域而不是 display label。
- Conversation/AnalysisState 的更新要有前序版本，支持“最新状态”“历史状态”“冲突”而不是静默覆盖。
- 如果未来确实有用户偏好或长生命周期、非事实性的知识，使用 namespace：

  ```text
  (organization_id, subject_id, workspace_id, memory_kind)
  ```

  而不是只按 `user_id` 检索。

- TTL 是 policy，而非技术默认值。可给无状态偏好/缓存使用，不能自动让可信查询证据或用户预期的会话状态消失。

### 5.3 为什么不引入 LangGraph

本项目已经有 Vanna、FastAPI 和 PostgreSQL 会话存储。为获得 checkpoint 概念再引入一个完整图执行框架，会重复 Agent 生命周期、工具与状态管理。应借鉴它的**数据模型纪律**，以当前 PostgreSQL schema 做最小实现。

## 6. Mem0：为什么“向量长期记忆”不是本项目的第一步

Mem0 的 `Memory`/proxy 模式支持 `user_id`、`agent_id`、`run_id`、metadata/filter 和多个 vector store。`proxy/main.py` 会在用户消息到来时异步写入 memory，检索相关 memories，并将其格式化后拼进最后一条用户消息。`memory/main.py` 还明确禁止调用方通过普通 metadata 改写身份作用域。

这些身份字段和 filter 设计值得参考；但默认行为不适合直接用于可信数据分析：

| 风险 | 为什么对本项目不合适 |
| --- | --- |
| 自动从自然语言写 memory | 用户、模型或工具文本可能含有未验证指标解释、过期条件、错误结论 |
| 相似度召回后拼入当前用户消息 | 模型难以区分召回内容、用户指令和当前工作区事实，可能污染 SQL 语义 |
| 默认 scope 不含 workspace/catalog/policy version | 即使有 `user_id` filter，也不足以隔离 Olist/TheLook 或 Catalog 变更 |
| 异步写入 | 适合偏好类记忆，但不适合作为“上一轮结果”的即时、可审计事实来源 |

因此本项目的决定是：**现在不接入 Mem0/pgvector 作为分析记忆。** 如果未来用户明确要求跨会话的“语言/时区/图表偏好”，可借鉴其 namespace/filter 思想，但存储内容必须是用户可见、可编辑、非业务事实的偏好记录。

## 7. 对 v1 设计的校正与冻结结论

研究强化了以下顺序，已写入 [嵌入式 Copilot 与对话记忆设计 v1](embedded-copilot-and-memory-design-v1.md)：

1. **M1：Host Contract。** 身份、workspace、页面上下文由服务端验证，前端不能自报权限。
2. **M2：ConversationScope。** 持久化 `workspace_id + state_version`，并在 load/list/update 时做隔离与冲突检查。
3. **M3：ResultArtifact。** 将纯文本 `previous_result_summary` 升级为绑定 QueryPlan/ResultContract/审计/版本的可信引用。
4. **M4：产品壳。** 从 Demo Host 提取无 Demo 控制面的 `EmbeddableCopilotShell`。
5. **M5：生命周期。** 显式清空分析上下文、版本失效和单会话并发语义。
6. **M6：历史摘要。** 只在 context 超预算后引入、有 source range/hash、且不能成为 SQL 事实的 R4 压缩层。

这六项不在本周直接实施。它们是 Week 1 复盘结束后、按一个小任务逐项完成的产品化路线。实现中应优先保留当前可信 SQL 链：

```text
Router / Catalog / QueryPlan
-> SqlPolicy
-> PostgreSQL reader role
-> ResultContract / ResultValidator
-> ResultArtifact
```

任何 memory 机制只能引用该链已验证的输出，不能绕过它。

## 8. 可复核的后续问题

本次调研尚未回答、也不应凭外部仓库猜测的项目问题：

- 宿主系统实际采用哪种认证（同源 cookie、OIDC、反向代理 JWT 或跨域 iframe）？
- Olist 与 TheLook 在产品运行时何时共存，是否需要一个用户可切换的 workspace？
- 审计 artifact 的业务保留期限、删除/软删除和合规要求是什么？
- R4 历史摘要应由在线大模型、离线任务还是确定性模板生成？

这些是 M1/M2/M6 开始前需要冻结的产品合同，不是通过复制开源代码即可解决的问题。
