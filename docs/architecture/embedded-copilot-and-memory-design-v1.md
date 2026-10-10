# 可嵌入分析 Copilot 与对话记忆设计 v1

> **状态：** M1–M7 已有产品入口。M7 只保存用户可见的展示偏好和版本绑定的分析结果，不进入 SQL。  
> **日期：** 2026-09-18  
> **依据：** 设计当时对照 `trusted_olist_web_demo.py` 和已删除的 `embedded_analyst_host.html`。当前产品页是 `/embedded-copilot`，旧宿主页不再存在。  
> **范围：** 为可信数据分析 Agent 冻结前端产品边界、宿主集成合同与长对话状态模型；不改动冻结评测/训练资产，不接入向量数据库，不重写当前 Demo。

## 1. 要解决的问题

当前 `/embedded-demo` 同时承担两种互相冲突的职责：

1. **可信链路演示台。** 页面显示演示身份切换、最近查询 SQL、指标版本和审计信息，便于开发时验证权限、Policy 与审计是否生效。
2. **用户产品原型。** 页面内含可缩放、可拖动的 `<vanna-chat>`，会话历史也直接放在宿主页面中。

这让真实使用者看到不属于其任务的“分析员/管理员”切换、内部审计和 Demo 文案；也使“宿主系统提供真实身份与业务上下文”这一生产前提被演示 cookie 替代。

会话方面，现有 `WorkingMemory` 已经是受控实现，而不是把聊天全文直接塞给模型：它保存指标、时间、维度、筛选、比较基线、待澄清字段和一段通过 ResultValidator 后写入的结果摘要。问题在于其仍缺少工作区隔离、版本/清理语义、并发语义和可引用的结果 artifact。因此它只足以支持简单的连续澄清，尚不足以承载真实长对话。

## 2. 已核实的当前边界

### 2.1 当前页面不是生产嵌入壳

`examples/embedded_analyst_host.html` 直接在同一页面中：

- 读取本地 `localStorage` 的 `conversation_id` 与窗口位置；
- 调用 `/api/project/session`、`/api/project/audits`、`/api/project/conversations`；
- 渲染演示角色切换与最近 SQL；
- 以固定路径挂载 `<vanna-chat>`。

它适合回归可信链路，不应被直接当作任意业务网站的集成方式。现有浏览器测试已经覆盖“澄清完成后 `Sending message...` 消失”、窗口尺寸和部分会话恢复，但尚未在当前 live 服务上重新完成真实浏览器回归。因此“没有 bug”不是当前结论。

### 2.2 当前上下文为何要限长

`ContextBudgetFilter` 以完整回合为单位保留最新对话，并有字符与消息数预算。这个限制不是因为早期聊天“无关”，而是两种风险同时存在：较早的回合可能已不再相关，且整段历史会挤占 Catalog、QueryPlan、ResultContract 和当前问题的上下文预算。它保留完整 turn 也是为了避免只留下孤立 tool response。

会话持久化则服务于另一件事：刷新后、历史会话恢复后，用户仍可追问“刚才结果为什么高”“按同一口径改按州看”。但**保留历史**不等于**把全部历史都送进每次模型调用**。

### 2.3 当前可信状态来源

```text
浏览器 localStorage
  -> 仅为恢复不透明 conversation_id / 窗口展示状态；不是权限或事实来源

PostgreSQL app.conversations / app.messages
  -> 按当前 server-resolved user 的历史、所有权与 WorkingMemory JSONB

WorkingMemory
  -> 当前会话的已确认分析槽位；只接受 Router、QueryPlan、ResultValidator 等服务器路径写入

query_audits / agent_runs / query-result artifact
  -> SQL 执行、合同、耗时与展示结果的审计证据
```

`previous_result_summary` 目前只有最多 1,200 字符，且仅在 Router 已判定为 `result_followup` 时交给无工具模型。常规 SQL Prompt 不会自动包含它。这一 fail-closed 边界应保留。

## 3. 产品目标与明确非目标

### 3.1 产品目标

目标是一个可以嵌入现有数据分析页面的可信 Copilot：

```text
宿主业务页面
  -> 用户通过真实 SSO/session 已登录
  -> 宿主提供已选择的工作区和有限页面上下文
  -> 侧边栏 / 悬浮 Copilot
  -> 受控的 Catalog、SQL、结果和后续追问
```

默认用户界面只呈现：会话、澄清、可信结果表/图和可理解的来源说明。SQL、审计明细和指标/版本证据应是按权限、按需展开的“查看依据”，不是主画面的 Demo 面板。

### 3.2 非目标

- 不实现一个从浏览器传入 `role=admin` 就信任的认证方案。
- 不把宿主 DOM、任意 URL 参数、完整表格行或原始 SQL 当作模型上下文。
- 不把所有对话向量化并在每轮召回；这会使过期、权限和事实来源无法审计。
- 不让模型摘要自行成为 SQL 条件、指标口径或权限事实来源。
- 不在本任务删除 Demo；Demo 仍是安全/审计回归入口，产品壳稳定后再单独收束。

## 4. 前端收束：Demo Host 与可嵌入 Shell 分离

### 4.1 两个明确的页面角色

| 层 | 名称 | 受众 / 用途 | 可以显示 | 不应承担 |
| --- | --- | --- | --- | --- |
| 开发验证层 | `TrustedDemoHost` | 开发、面试演示、策略回归 | 演示身份、审计、SQL、版本、测试入口 | 生产认证与通用宿主集成 |
| 产品嵌入层 | `EmbeddableCopilotShell` | 原有 BI / 数据分析站点的真实用户 | Chat、会话、新建/清空上下文、结果卡、可选“查看依据” | 演示角色、全局审计列表、模拟登录 |

`/embedded-copilot` 已提供一份**不含 Demo 控制面**的壳；`/embedded-demo` 仍保留为开发验证宿主。这避免为了视觉简化而丢失现有可信验证能力。

### 4.2 Host Integration Contract（必须先冻结）

宿主和 Copilot 的边界应是一个小型、版本化合同，而非从网页猜测上下文：

| 字段 / 通道 | 来源 | 是否持久化 | 规则 |
| --- | --- | --- | --- |
| `subject_id`、角色、组织成员关系 | 宿主认证 cookie / Bearer token，由服务端验证 | 不由浏览器持久化 | Copilot 后端以此解析用户；前端不可自报身份或角色 |
| `workspace_id` | 宿主路由或 bootstrap 请求 | 会话绑定 | 服务端验证用户有该 workspace 成员资格；加载历史时必须匹配 |
| Catalog / dataset / metric / policy version | 服务端 workspace profile | 会话、artifact 绑定 | 版本漂移时不能复用分析状态或结果为事实 |
| `surface_id`、页面路由 | 宿主声明 | 可作为审计元数据 | allowlist、长度限制；不作为 SQL 条件 |
| `selected_entity_refs`、可见时间范围 | 宿主受控 API 输出 | 默认仅当轮 | 必须是 opaque ID/受控日期；服务端按权限验证，不能接收 raw DOM/任意 SQL |
| UI theme、初始展开状态 | 宿主前端 | 浏览器本地可保存 | 纯展示，不影响身份、权限或 SQL |

推荐启动协议为：宿主将已认证 cookie 带给同源 `/api/copilot/bootstrap`；后端返回已验证的 `workspace`、显示能力和短生命周期 `surface_context`。若必须跨域，使用后端签发、绑定 `audience + origin + subject + workspace + expiry` 的短 token，并对 `postMessage` 严格校验 `origin`、schema 与 nonce。不能让 iframe 自行提交任意 `user_id/workspace_id`。

### 4.3 产品交互最小集

1. 页面右下角触发器，打开固定侧边栏；移动端全屏，不先做拖拽和八向缩放。
2. 首屏为当前会话，顶部仅显示工作区名和“新分析 / 清空分析上下文”操作；历史会话列表作为可展开抽屉，而不是挤占宿主仪表盘。
3. 每次结果显示“已基于当前工作区数据”及指标/时间范围；“查看依据”按权限展示口径、结果是否展示截断、SQL（可选）和审计 ID。
4. 澄清、拒绝、网络错误、超时、取消和成功都有不同状态；无 SQL 路径也必须关闭 loading。现有 `Sending message...` E2E regression 要成为新壳的必测项。

## 5. 记忆模型：以可信 artifact 为中心，而不是聊天全文

本项目把“记忆”拆成四件不同的事情。它们的写入、检索和失效规则不同，不能因为都跨越了多个回合就混成一个向量库：

| 用户口语中的概念 | 本设计中的层 | 解决的问题 | 不能误解为 |
| --- | --- | --- | --- |
| **短期记忆** | R0 当前请求/页面上下文 + R2 `AnalysisState` + 当前 active R3 | “刚才查的是 GMV，这一轮补上日期”“按刚才的结果解释原因” | 聊天全文或 assistant 自由文本自动成为 SQL 条件 |
| **持久会话记忆** | R1 原始 history + R2 会话状态 + R3 ResultArtifact | 刷新、恢复历史会话、在同 workspace 下继续追问 | 跨工作区、跨版本永久沿用旧指标/旧结论 |
| **长期记忆** | R5 显式用户偏好 + 用户主动保存的 R3 artifact | 下次打开时使用语言/时区/展示偏好，或找到用户主动保存的历史分析 | 自动记住用户/模型说过的一切业务事实，或把旧结果直接作为新 SQL 的事实 |
| **会话压缩** | R4 provenance-backed history summary | 很长会话超出 token 预算后，仍保留可回读的叙事连续性 | 为了省 token 把摘要当成指标、权限、时间筛选或数据库结果 |

其中 R3 还承担“长期保存一个可信分析结果”的职责：用户可以显式保存/固定它，之后以 audit/version 绑定的 artifact 找回；它不是把历史结果全文重新注入模型。

### 5.1 六层信息，六种规则

| 层 | 内容 | 生命周期 | 可进入 SQL Prompt？ | 写入来源 |
| --- | --- | --- | --- | --- |
| R0 请求 / 页面上下文 | 当前页面、选中对象、可见时间范围 | 仅本请求 | 经服务端验证后可作为明确约束 | 宿主 bootstrap |
| R1 会话原始历史 | 用户/助手消息、tool 调用引用 | 用户删除或数据保留策略到期 | 仅按 ContextBudget 取完整最近回合 | 服务端 ConversationStore |
| R2 `AnalysisState` | 指标、维度、筛选、时间、比较、待澄清、`active_result_artifact_id` | 会话级；可清空 / 版本失效 | 可以，但只有通过验证的结构化字段 | Router / QueryPlan / validated result |
| R3 `ResultArtifact` | 已通过治理的查询证据、结果 schema、聚合摘要、展示引用、版本 pin | 受 workspace / retention 约束 | 追问时以结构化摘要/引用进入；绝不把全表塞入 Prompt | `TrustedRunSqlTool` + ResultValidator |
| R4 历史压缩摘要 | 早期完整 turn 的可回读摘要与 message range/hash | 仅在历史超预算时生成 | 可用于叙事连续性，不能作为指标/SQL 事实 | 服务器压缩任务，带 provenance |
| R5 用户偏好（后续） | 语言、时区、默认图表偏好 | 用户账户级、可编辑 | 仅影响呈现/默认，不决定指标事实 | 用户明确设置 |

这里 R2/R3 是本项目的核心。“上一次的 GMV 查询”不应被保存成一段助手自然语言，而应保存为可验证的 `ResultArtifact` 引用：它知道自己的 workspace、Catalog/metric/dataset/policy 版本、QueryPlan/ResultContract、行数、展示截断状态、结果摘要和审计 ID。模型只能读由服务器为当前追问挑选的安全投影。

R5 也不是模糊的“用户画像”。v1 只允许用户可见、可编辑、非业务事实的显式偏好，例如语言、时区、默认图表类型、数值/币种显示方式；检索键必须至少包含 `(organization_id, subject_id, workspace_id, memory_kind)`。业务分析结果的长期保存走 R3 的“保存分析”动作，不走 R5，也不能用 R5 影响 SQL 的 metric、filter、time range 或权限。

### 5.2 建议的下一版结构（概念 schema）

```text
ConversationScope
  conversation_id, subject_id, workspace_id, state_version
  catalog_version, metric_version, dataset_version, policy_version
  active_analysis_state_id, last_activity_at

AnalysisState v1
  metric_ids, dimensions, filters, absolute_time_range, comparison_baseline
  pending_question, pending_missing
  active_result_artifact_id
  source_turn_id, updated_at, expires_at

ResultArtifact v1
  artifact_id, workspace/version pins, query_audit_id, request_id
  result_contract_evidence, schema, row_count, display_truncated
  safe_summary, sample_rows (bounded), result_file_ref, created_at

HistorySummary v1
  summary_id, conversation_id, source_message_start/end, source_hash
  narrative_summary, created_at, invalidated_at
```

这不是马上加四张表的授权。第一步应先冻结字段、来源和迁移策略；实现时优先复用现有 `app.conversations`、`app.messages`、`query_audits`、`agent_runs` 和结果文件引用，只有确实缺少持久化关系时才加最小 schema。

### 5.3 Context Assembly 的固定优先级

每个请求按下列顺序组装上下文，并记录每一项是否命中、长度和版本：

```text
当前问题
  + 已验证的 R0 页面上下文（可选）
  + 同 workspace/version 的 R2 AnalysisState
  + 若 Router 判为结果追问：R3 active ResultArtifact 的安全摘要
  + 当前 Catalog 检索与 QueryPlan / ResultContract
  + 最近完整 R1 turns（预算内）
  + 只有超预算时才加入 R4 历史摘要
```

要求：

- 当前问题、Catalog、QueryPlan 与 ResultContract 优先级高于历史文本。
- 版本或 workspace 不匹配时，R2/R3/R4 直接不可用，Router 必须要求用户重新指定条件；不能“尽量猜”。
- 只读结果追问可以使用 R3 进行解释，但如果用户改变指标/维度/时间或要求新数字，必须走新的受控 SQL 查询。
- 原始结果行的 1,000 行执行上限和 UI 200 行预览边界不因记忆改变；给模型的 sample rows 仍维持最小必要、合同化的上限。

当前已实现的 `HistorySummary v1` 是确定性的历史边界标记，不是 LLM 生成的语义摘要。它只记录被省略的完整 turn 范围、消息/字符数量、SHA-256 和触发预算原因，并在本次 LLM request 中加入一条 server-owned message；原始历史仍保存在会话存储中。该消息明确禁止从边界推断 SQL、指标、时间、权限或业务结论。`BudgetUsage.context_budget` 与 `agent_runs.catalog_trace.context_budget` 只记录字符预算、保留/省略数量、摘要 digest 和当前回合压缩布尔值；这些字段是 character-budget evidence，不是 provider tokenizer 的精确 token 统计。

如果最新完整 turn 自身超出剩余预算，过滤器优先保留当前 user message，并将工具/助手内容替换成固定 marker；若当前 user 自身超过请求输入上限，仍由 `RequestBudget.max_input_chars` 拒绝。该异常路径不会静默截断自然语言，也不会把旧摘要写入 WorkingMemory 或 ResultArtifact。

### 5.4 清除、过期、版本与并发

| 场景 | 推荐语义 |
| --- | --- |
| “新分析” | 新 `conversation_id`；历史不删除，R2 不继承 |
| “清空分析上下文” | 保留 R1 历史，但清空 R2 和 active R3 引用；下一轮不得默认沿用指标/时间 |
| 用户删除会话 | 删除/软删除会话历史及其 R2/R4；R3 审计 artifact 按独立审计留存策略处理，不再可从聊天引用 |
| 刷新页面 | 若认证、workspace 和版本匹配，恢复当前会话与 R2；否则开始新分析并解释原因 |
| Catalog / workspace 变化 | 不迁移旧 R2；保留历史的版本标识，但拒绝将其当成当前查询事实 |
| 长时间未使用 | v1 不做隐形 TTL；先提供可见的“清空分析上下文”。数据保留期限由产品/合规策略单独确定后再实现 |
| 同会话双 tab 并发 | UI 每会话只允许一个进行中请求；数据库使用 `state_version` 乐观并发控制。版本冲突返回可恢复提示，不能静默 last-write-wins |

## 6. 公开实践与 Codex 参考的边界

本轮尝试通过官方 OpenAI Docs 搜索并打开 Codex 的“context/session”资料，但当前服务器对 `developers.openai.com` 返回 `Forbidden`，未能获取可引用页面。更重要的是，官方公开产品文档即使可访问，也不应被解读为披露 Codex 的内部会话记忆或检索实现。

随后对公开的 Codex CLI、Aider、LangGraph、Mem0 做了 revision-pinned 的代码阅读，完整结果见 [开源 Agent 上下文、会话与记忆实现调研 v1](open-source-agent-context-research-v1.md)。可采用的是**可验证的公开工程原则**，而非猜测产品内部：上下文有预算；保留最近且相关的完整工作单元；把项目事实、权限和工具结果保留为结构化证据；压缩内容保留来源；状态按工作区/版本/用户隔离；允许用户清除；对并发有明确语义。这些原则与当前 Agent 的可信 SQL 链天然一致。

## 7. 实施顺序与验收证据

只按一个小目标推进，以下是候选顺序，不代表同一轮全部实施：

| 序号 | 最小目标 | 不变量 | 最小验收 |
| --- | --- | --- | --- |
| M1 | 冻结 `CopilotBootstrap`、`AnalysisSurfaceContext`、`ConversationScope` 合同 | 已完成：浏览器不能自报 user/role/workspace；Demo 保持既有会话路径 | `copilot_contract` + demo session + workspace profile 定向测试 20 passed；同源 bootstrap 已提供 |
| M2 | 给会话持久化 `workspace_id + state_version`，完成加载/列表隔离 | Olist/TheLook 不可串话；旧单工作区会话迁移 fail-closed | DB migration + store/router 定向测试 |
| M3 | 将 R2 的 `previous_result_summary` 升级为受控 R3 artifact 引用 | 摘要只能来自 validated result；无全表/原始 SQL 泄露 | scalar/grouped/time-series 追问 E2E；无可信 artifact 时澄清 |
| M4 | 已提取单一产品壳 `/embedded-copilot` | 演示角色和审计默认不显示；`/embedded-demo` 重定向到该页 | Playwright：产品壳入口、查看依据、新分析、手机端、结果预览 |
| M5 | 已加入清空上下文、版本失效和同会话并发合同 | 不静默覆写 state；不跨版本复用指标/结果 | `tests/test_analysis_lifecycle.py`：清空保留历史、版本失效、stale `state_version` 可恢复提示 |
| M6 | 只在 R1 已超预算时引入 R4 历史摘要 | 摘要有 message range/hash，且不作为 SQL 事实 | 长会话压缩回归与 prompt provenance 日志 |
| M7 | 已加入语言、时区、默认图表、数字格式，以及显式“保存本次分析” | 偏好和已保存分析都不进入 SQL Prompt；保存时钉住 dataset/metric 版本和 artifact 引用 | `tests/test_user_memory.py`：非法偏好在写库前拒绝；真实库上按 subject 隔离 |

M7 已提供显式偏好和保存入口。仍然不引入长期向量记忆、跨用户推荐，也不把历史业务事实拼入 SQL Prompt。

## 8. 需要产品确认的少数决策

M1–M7 和 2026-09-22 的本地登录、跨域令牌、完整结果下载已经落地。外部身份提供商和 TheLook 在线接入仍未做。下面保留当时的产品建议，供对照，不再当作未决开工条件：

1. 首版嵌入式产品是否先限定为 **Olist 一个 workspace**，TheLook 只保留离线评测？建议：是，先把 workspace 隔离合同做好，再开放多 workspace。
2. 会话历史是否保留为侧边栏中的可展开抽屉？建议：保留；它是追问和可回溯性的用户入口，但默认收起。
3. SQL / 审计是否默认展示？建议：默认不展示，只给“查看依据”；管理员/开发 Demo 才可看完整审计。
4. “清空分析上下文但保留聊天记录”是否作为首版显式操作？建议：是；比在用户不知情时设置 TTL 更可解释。

## 9. 与现有缺口地图的关系

本提案细化并不替代以下已有缺口：

- `GAP-RT-01`：当前轮模型交接需要合同化结构化结果摘要；它将成为 R3 的第一步。
- `GAP-RT-04` 至 `GAP-RT-07`：会话状态生命周期、workspace、清除/过期、并发。
- `GAP-UX-01`：浏览器端 loading、尺寸、历史和结果呈现回归。
- `GAP-OBS-01`：应额外记录 context source、state version、artifact 引用和版本失效原因，但只做脱敏聚合。

后续任何代码实现都应先追加到缺口地图和 Week 计划，再独立审查，不能以本设计文档为由直接大规模重写前端或数据库。
