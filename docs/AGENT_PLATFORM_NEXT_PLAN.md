# Agent 平台下一阶段计划

> 文档状态：实施路线；P0 会话/预算基础、P1 运行时可靠性合同、一次修复生命周期、嵌入窗口回归、v2 确定性评测和 24 条真实模型人工标签核验已完成，当前聚焦已发现失败的定向修复与回归。
> 更新日期：2026-08-18
> 关联研究：[Text-to-SQL 专项调研](TEXT_TO_SQL_RESEARCH.md)

## 1. 目标

当前项目已经完成“单次可信查询”闭环：用户提出中文问题，Vanna Agent 生成只读 SQL，SQL
经过 `sqlglot` 策略和 PostgreSQL 只读角色校验，系统返回表格、图表、中文结论、指标/数据
版本和审计记录。

下一阶段的目标不是把项目扩展成一个大而全的 BI 平台，而是把这个闭环升级为可连续使用、
可恢复、可解释、可控制资源的分析 Agent：

```text
一次查询 Demo
    -> 持久会话与多轮追问
    -> 分层上下文与指标语义
    -> 请求级工具/token/时延预算
    -> 歧义澄清、失败修复和可信拒答
    -> 可回放评测与作品集证据
```

## 2. 现状边界

### 已完成

- Python 3.12、FastAPI、Vanna 2.0.2、PostgreSQL、SiliconFlow/DeepSeek-V4-Flash；
- Olist analytics 数据和版本化指标上下文；
- 单语句、只读、Schema/表/列白名单、敏感标识限制、LIMIT、超时和角色化 SQL Policy；
- `daa_analytics_reader` 与 `daa_app_writer` 双角色；
- `app.query_audits` 持久查询审计；
- PostgreSQL `ConversationStore`、`agent_runs` 运行台账和请求级工具/上下文预算的第一版骨架；
- 可信 Demo 已接入会话存储、预算工具注册表、上下文裁剪过滤器和安全 DTO 历史 API；
- 嵌入宿主页已接入历史列表、点击恢复、新建会话、刷新恢复、删除失败提示和角色切换隔离；
- Vanna 原生 `<vanna-chat>` 嵌入窗口、表格/图表/SQL/证据展示；
- 60 条第一轮确定性用例、3 条固定演示场景和 60 条 v2 路由/QueryPlan golden 的安全与数据库/结构化合同校验；Text-to-SQL/预算/Policy/结果/工作区专项回归当前 88 项通过；
- 嵌入窗口浏览器回归覆盖拖拽、八方向缩放、移动端、图表尺寸、会话恢复、窄窗口长 Markdown 和固定 SSE 多轮澄清；完整嵌入测试文件当前 9 项通过，starter UI 空会话不再写入历史。
- 通用 `WorkspaceProfile` 已把数据集、版本、Schema、角色、白名单和 Catalog 路径从 Olist 适配器中分离；`TrustedRunSqlTool` 已把一次修复接入 Vanna `RunSqlTool` 生命周期。
- `QuestionRouter` 已从指标命中判断扩展为证据感知路由：帮助、Catalog 定义、通用业务/知识、数据查询、数据分析、混合请求和结果追问分别带有 `intent`、`requires_database`、`evidence_mode`、`confidence` 和 `reason_code`。通用回答可复用模型但不提供 SQL/图表工具，帮助与指标定义不调用模型。
- 多指标请求已由服务器生成 `QueryPlan`，明确标量概览、分组查询、事实粒度、时间字段、维度和必需结果列；计划进入 Catalog Prompt、Vanna `ToolContext`、结果合同和 Agent Run trace。通过 `ResultValidator` 的结果会生成有界摘要并写入 WorkingMemory，结果追问无摘要时先澄清。
- `evals/cases/text_to_sql_v2.yaml` 已冻结 60 条版本化用例，`scripts/run_text_to_sql_evaluation.py` 已提供离线路由/QueryPlan 评测；2026-08-06 为 **60/60 passed**。报告不保存原始问题、模型回答、密钥或结果行，因此该数字只代表确定性合同，不代表在线 LLM 准确率。
- `evals/cases/text_to_sql_online_v1.yaml` 已从 v2 选择 24 条代表性请求，并通过 Trusted Demo SSE 实际调用 SiliconFlow。每条人工标签均记录路由、澄清、SQL、指标语义、结果合同、权限、回答有据、修复、耗时及 token 状态；24/24 均有 Agent Run。已发现的失败是 BRL 币种格式、合同通过后的多余 SQL、品类/评价多指标 Catalog slice、支付拆分订单归因和一次 180 秒未完成请求。

### 必须准确区分的未完成能力

- 会话和消息已经持久化，历史恢复目前只回放安全的文字消息；原始 SQL、图表和 DataFrame 结果不会伪造回放，结构化结果回放尚未实现；
- `DemoAgentMemory` 仍是进程内辅助记忆；当前只持久化最近可信结果的有界摘要，完整结构化旧轮次摘要、SQL/图表证据回放尚未实现；
- `max_tool_iterations=4` 仍是模型-工具循环上限，项目新增了总工具、SQL、图表、输入、上下文和输出预算，但用户配额/费用台账尚未实现；
- 上下文目前按完整轮次和字符/消息预算裁剪，结果追问已有受限摘要，但尚未生成完整的旧轮次结构化摘要；
- Catalog YAML、确定性检索器、证据路由、working memory、服务器拥有的 `QueryPlan`/`ResultContract` 和 `TrustedRunSqlTool` 已接入 Trusted Demo 的 SSE 主链路；结果合同、修复证据、路由计划、受限结果摘要和版本字段进入 Vanna `ToolContext`、Agent Run 与 SQL 审计。仍未完成完整结构化结果历史回放、旧轮次摘要、真实认证/组织级 RLS、第二个真实数据集，以及真实 SiliconFlow 批量语义/修复成功率报告。
- 24 条真实 SiliconFlow 人工标签核验已经完成，但它是单轮小样本：不公布总体在线语义准确率、一次修复恢复率、第二次修复增量收益、token 成本或 P50/P95 延迟。22/24 条 provider usage 未回传，已记录为 `unknown`，不能计作 0。

### P0 第一版实现状态

本轮已落地 `app.conversations`、`app.messages`、`app.agent_runs` 及 `query_audits.run_id`；
`PostgresConversationStore` 对读取、更新、删除和分页执行用户归属校验，`PostgresRunRecorder`
记录模型/数据版本、预算上限、工具用量、上下文裁剪和终止状态。`BudgetedToolRegistry` 会逐个
计算同一模型响应中的工具调用，`BudgetSafetyMiddleware` 在预算耗尽后阻止未经验证的数值回答，
`ContextBudgetFilter` 保留完整的最新轮次并记录裁剪标记。宿主页已经消费历史 API，支持当前会话、
历史恢复和新建会话；starter UI 的空会话不会提前写入数据库，历史接口也过滤旧的零消息记录。
历史详情只回放可安全展示的文字内容，不能把历史文字当作原始 SQL 或图表证据。

## 3. P0：会话、上下文和资源预算

P0 必须保持单仓库、Vanna 原生前端、PostgreSQL 状态存储和单一 PostgreSQL 方言，不引入
Redis、队列、多 Agent、MCP 或任意 Python 执行。

### 3.1 持久会话与恢复

实现 Vanna `ConversationStore` 的 PostgreSQL 适配，并建立最小应用表：

- `app.conversations`：会话 ID、用户、标题、创建/更新时间、数据集/指标版本、状态；
- `app.messages`：用户问题、助手最终回答、工具调用摘要、消息序号、时间、可见性；
- `app.agent_runs`：一次请求的模型名、数据/指标版本、预算配置、开始结束时间和终止原因；独立 Prompt/Policy 版本列作为下一项可回放性增强；
- `app.query_audits` 保持 SQL 级事实记录，并通过 `conversation_id` / `run_id` 关联。

原始 SSE 碎片不作为长期事实保存。成功轮次至少保存问题、最终结论、最终 SQL、结果摘要、
图表产物引用、指标/数据版本和策略状态；失败轮次保存失败类型和用户可执行的下一步。

验收：刷新页面后能恢复当前会话；同一用户只能读取自己的会话；管理员可按权限查看审计；
删除会话后不能通过历史 API 恢复；同一会话重新提问能获得明确的 `conversation_id`。

当前已验证 PostgreSQL round-trip、跨用户隔离、分页上限、删除权限和 run/audit 外键关联；
浏览器刷新恢复、历史点击恢复、新建会话、删除失败状态和角色切换隔离已由 6 条 E2E 覆盖。

### 3.2 分层上下文

每次模型调用都通过一个显式 Context Builder 组装上下文，不把整个历史和审计表直接回灌：

1. 当前用户问题与角色/权限；
2. 当前请求需要的指标定义、时间字段、Join 规则和 Schema 子集；
3. 最近若干轮的完整问题与最终结果；
4. 更早轮次的结构化摘要（已确认的指标、时间范围、筛选条件、上一步 SQL/结果摘要）；
5. 当前请求的预算、可用工具和数据/指标版本。

上下文层次必须与“受控业务记忆”分开：指标目录和审核过的成功 SQL 示例可长期版本化；
普通聊天、异常信息和审计细节默认不自动成为长期业务记忆。

验收：测试能证明上下文只包含当前用户可见历史；敏感列值、其他用户会话和异常堆栈不会被
拼入后续 Prompt；超出预算时按优先级压缩并记录 `context_truncated`。

当前第一版按完整轮次、最大消息数和最大字符数裁剪，未把异常堆栈或其他用户数据拼入上下文；
结构化旧轮次摘要与按问题选择 Schema 将在 P1 实现。

### 3.3 请求级预算

先保留 `max_tool_iterations=4` 作为模型-工具循环上限，再增加明确的总调用预算：

    - `run_sql` 每请求最多 2 次；第一次失败时允许一次受控修正；
- `visualize_data` 每请求最多 1 次；
- 所有工具调用合计最多 4 次；
- 设置用户输入最大长度、模型输出 `max_tokens`、结果进入 Prompt 的最大摘要大小；
- 已有数据库语句超时和结果行数限制继续作为底层预算；
- 每次请求记录 `tool_calls_used`、`sql_calls_used`、`output_tokens`（若提供方返回）、
  `elapsed_ms`、`row_count` 和 `termination_reason`。

终止原因使用稳定枚举：`completed`、`clarification_required`、`tool_budget_exhausted`、
`context_truncated`、`sql_policy_rejected`、`query_timeout`、`execution_error`、
`unsupported_request`。超过预算必须给用户可理解的下一步，不能悄然输出不完整结论。

当前预算由 `RequestBudget.from_environment()` 读取，默认工具/SQL/图表/输入/上下文/输出上限
已接入 trusted Demo，并持久化到 `app.agent_runs`；provider 未返回 usage 时保持 token 字段为
未知，不把未知成本记为零。

### 3.4 前端展示

继续使用 Vanna `<vanna-chat>`，宿主页只增加轻量入口和状态：最近会话、当前会话标题、
“新建会话”、恢复失败提示、预算耗尽提示和证据入口。上述历史列表、会话切换和新建会话控件
已完成并通过浏览器回归；不建设独立 React/Next.js 页面。

## 4. P1：Text-to-SQL 可靠性增强

详细依据见 [TEXT_TO_SQL_RESEARCH.md](TEXT_TO_SQL_RESEARCH.md)，第一版冻结计划见
[feature-text-to-sql-reliability-v1.md](../plan/feature-text-to-sql-reliability-v1.md)，本轮执行计划见
[feature-text-to-sql-reliability-v2.md](../plan/feature-text-to-sql-reliability-v2.md)。顺序如下：

1. 结构化 Schema/指标 Catalog 和简单的按问题检索（已完成）；
2. 可回答性/歧义分类，先澄清缺失的时间、指标、对比基线或维度（已完成）；
3. 一次受限的执行错误修复，保留原始 SQL、修复 SQL 和修复原因（已接入 Vanna 生命周期）；
4. 空结果、异常 Join 放大、指标列缺失等结果级检查（已完成）；
5. 低置信度时拒答或请求用户确认，而不是生成看似完整的结论（当前已覆盖确定性失败状态）；
6. 多指标查询由服务器 `QueryPlan` 明确结果列和独立聚合策略；当前以 Prompt + ResultContract 约束，后续增强 AST 形状检查；
7. 版本化 v2 路由/QueryPlan golden（已完成，60/60）；
8. 将线上模型运行结果纳入小规模人工核验集和回归报告（已完成，24 条）；下一项只针对已发现失败做定向修复和固定回归。

P1 不做 Best-of-N 大规模采样、多模型投票或自动修改业务口径。先证明单候选 + 一次修复
的收益，再根据成本和评测结果决定是否扩展。

这里的“一次修复”是每个请求的默认安全上限：初始 SQL 失败后只生成一个脱敏候选，候选
必须完整重过 AST Policy、对象/敏感列白名单、reader role、超时、行数上限和
`ResultContract`/`ResultValidator`，因此最多两次 SQL 执行。只有在线评测证明第二次修复带来
足够的增量恢复、且没有明显增加语义错误、延迟和 token 成本时，才考虑把配置提升到 2；不做
无限重试。

## 5. P2：平台化能力

- 第二个数据集/工作区及其独立的指标、Schema、会话和评测上下文；
- 真实身份提供方和组织级/行级权限；
- 导出 CSV/PDF、异步长任务和订阅式报告；
- 从人工反馈中审核后沉淀成功 SQL/指标解释；
- 运行台账和评测对比页面。

只有出现多实例限流、跨进程短期缓存、异步导出或任务进度共享时，才评估 `redis-py + arq`。
Redis 不作为会话、审计和评测事实来源。

## 6. 暂缓或明确不做

- 多 Agent 编排、MCP 和任意 Python/数据框执行；
- 支持多种 SQL 方言；
- 为了“平台化”引入向量数据库；当前 8 张表优先使用结构化 Catalog 和确定性检索；
- 把全量聊天记录自动训练成长期记忆；
- 以公开 benchmark 分数替代本项目指标口径、权限和真实数据评测；
- 把演示会话、Olist 数据或确定性安全评测包装成生产认证、真实业务数据或在线准确率。

## 7. 分阶段验收

### Phase A：设计冻结

- 形成会话/消息/运行/审计关联模型；
- 定义 Context Builder 输入输出和压缩优先级；
- 定义预算枚举、终止状态和指标；
- 为每个状态写 API、单元和集成验收用例。

### Phase B：持久会话与预算

- PostgreSQL ConversationStore；
- 刷新恢复、列表、新建、删除和权限隔离；
- 总工具调用/SQL/图表/输出长度预算；
- 运行台账和预算终止测试。

### Phase C：Text-to-SQL 可靠性

- Catalog 检索和上下文裁剪；
- 歧义澄清；
- 一次执行修复与结果校验（已完成，含 `repair_evidence` 和可信拒答）；
- 多轮澄清浏览器回归（已完成）；
- v2 路由/QueryPlan 离线 golden（已完成，60/60）；
- 线上模型小型回归集（已完成 24 条人工标签；不先声称总体准确率）。

### Phase D：作品集收敛

- 记录 P0/P1 真实指标；
- 更新 README、简历条目、演示脚本和 Feishu 迭代记录；
- 只从可复现报告提取准确率、延迟、预算和安全数字。

## 8. 主要风险

| 风险 | 控制措施 |
| --- | --- |
| 历史越长导致 Prompt 膨胀 | Context Builder、摘要、硬预算和截断审计 |
| 多轮追问沿用错误口径 | 每轮携带 metric/version，并允许显式重置/澄清 |
| 修复循环扩大越权面 | 每次修复重新经过完整 SQL Policy 和数据库只读角色 |
| 结果缓存或文件跨用户泄露 | 请求/用户归属、短 TTL、文件名校验和清理 |
| 论文方法与个人项目规模不匹配 | 先做可测量的最小版本，不复制复杂训练/多模型系统 |
| 把演示数字写成生产指标 | 文档区分 deterministic golden、online LLM 和人工判定 |
