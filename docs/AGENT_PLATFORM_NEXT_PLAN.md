# Agent 平台下一阶段计划

> 文档状态：设计计划，尚未代表实现完成。
> 更新日期：2026-08-03
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
- Vanna 原生 `<vanna-chat>` 嵌入窗口、表格/图表/SQL/证据展示；
- 60 条确定性用例和 3 条固定演示场景的安全与数据库 golden 校验。

### 必须准确区分的未完成能力

- 查询审计历史不等于聊天会话历史；
- Vanna 当前默认 `MemoryConversationStore` 和 `DemoAgentMemory` 是进程内存储，重启会丢失；
- `max_tool_iterations=4` 是模型-工具循环上限，不是总工具调用、token 或费用预算；
- 当前没有持久消息摘要、上下文裁剪、每请求 token 用量、用户配额或成本台账；
- 当前没有稳定的澄清状态机、SQL 自动修复上限、置信度/拒答原因和在线模型语义准确率报告。

## 3. P0：会话、上下文和资源预算

P0 必须保持单仓库、Vanna 原生前端、PostgreSQL 状态存储和单一 PostgreSQL 方言，不引入
Redis、队列、多 Agent、MCP 或任意 Python 执行。

### 3.1 持久会话与恢复

实现 Vanna `ConversationStore` 的 PostgreSQL 适配，并建立最小应用表：

- `app.conversations`：会话 ID、用户、标题、创建/更新时间、数据集/指标版本、状态；
- `app.messages`：用户问题、助手最终回答、工具调用摘要、消息序号、时间、可见性；
- `app.agent_runs`：一次请求的模型/Prompt/策略版本、开始结束时间、终止原因、预算消耗；
- `app.query_audits` 保持 SQL 级事实记录，并通过 `conversation_id` / `run_id` 关联。

原始 SSE 碎片不作为长期事实保存。成功轮次至少保存问题、最终结论、最终 SQL、结果摘要、
图表产物引用、指标/数据版本和策略状态；失败轮次保存失败类型和用户可执行的下一步。

验收：刷新页面后能恢复当前会话；同一用户只能读取自己的会话；管理员可按权限查看审计；
删除会话后不能通过历史 API 恢复；同一会话重新提问能获得明确的 `conversation_id`。

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

### 3.4 前端展示

继续使用 Vanna `<vanna-chat>`，宿主页只增加轻量入口和状态：最近会话、当前会话标题、
“新建会话”、恢复失败提示、预算耗尽提示和证据入口。暂不建设独立 React/Next.js 页面。

## 4. P1：Text-to-SQL 可靠性增强

详细依据见 [TEXT_TO_SQL_RESEARCH.md](TEXT_TO_SQL_RESEARCH.md)。顺序如下：

1. 结构化 Schema/指标 Catalog 和简单的按问题检索；
2. 可回答性/歧义分类，先澄清缺失的时间、指标、对比基线或维度；
3. 一次受限的执行错误修复，保留原始 SQL、修复 SQL 和修复原因；
4. 空结果、异常 Join 放大、指标列缺失等结果级检查；
5. 低置信度时拒答或请求用户确认，而不是生成看似完整的结论；
6. 将线上模型运行结果纳入小规模人工核验集和回归报告。

P1 不做 Best-of-N 大规模采样、多模型投票或自动修改业务口径。先证明单候选 + 一次修复
的收益，再根据成本和评测结果决定是否扩展。

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
- 一次执行修复与结果校验；
- 线上模型小型回归集。

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
