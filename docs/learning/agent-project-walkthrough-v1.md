# Agent 项目代码学习记录

本记录承接 [`四周完善与学习计划`](../../plan/process-agent-internship-improvement-v1.md)，只放代码理解与问答。项目进度见 PROJECT.md，后训练知识继续使用既有 post-training/learning 目录。每轮阅读 1–3 个关键函数，问题答完再进入下一段。

## Day 1：沿一条问题认识请求链

场景：“按客户州统计有效订单数前五名，并生成柱状图”。完整调用关系和本次请求证据见 [`产品请求架构`](../architecture/product-request-architecture-v1.md)。目标是用户能画出请求链，区分代码/模型职责，以及离线 QuerySpec 与在线 QueryPlan；入口复述已完成，当前进入 Catalog 与 Router 小段，Day 1 尚未完成。

### 第一小段：启动装配与请求入口

先阅读两处，不要求读完整文件：

1. `examples/trusted_olist_web_demo.py:create_app()` 的组件创建部分，以及 `register_chat_routes(..., BudgetedChatHandler(...))`。它在启动时构建可重复使用的组件，并把它们交给 HTTP 路由。
2. `src/vanna/servers/fastapi/routes.py:chat_sse()`。它接收 JSON 请求、补充 HTTP 上下文、调用 handler，并把 handler 产出的块编码成 SSE。它本身没有写 SQL 的逻辑。

前端通过 `frontends/webcomponent/src/services/api-client.ts:streamChat()` 发送请求。主要字段是：

```json
{
  "message": "按客户州统计有效订单数前五名，并生成柱状图",
  "conversation_id": "同一段会话的标识",
  "request_id": "这一次请求的标识"
}
```

`conversation_id` 用来串联多轮，`request_id` 用来追踪单次请求；两者不能证明用户身份。服务端从实际 HTTP cookies/headers 中解析认证凭据，再由身份解析器验证，不能信任请求体自称的用户或角色。

核心调用为 `async for chunk in chat_handler.handle_stream(chat_request)`。handler 持续产出状态、表格、图表或文本块，HTTP 路由逐块传给前端；SSE 数据块数量不等于模型调用次数。

2026-10-10 问答：

1. `create_app()` 和 `chat_sse()` 分别在什么时候运行？用户再发一条消息，会重新装配所有组件吗？
2. 为什么单凭请求里的 conversation_id 或 user_id，不能决定用户可查询哪些数据？

用户回答：

1. “create-app在服务启动时运行，chat-sse在用户发送问题模型返回回答时运行。router，agent，数据库runner只需要在启动时创建一次就够了。”
2. “因为身份信息由服务端的解析器验证登录凭证，可能包括cookie之类的？单靠id可能不行。”

反馈：启动装配和身份验证的理解正确。精确时序是后端收到 SSE 请求就进入 `chat_sse()`，再调用 handler，模型生成发生在后续处理过程中；拒绝或 Catalog 定义回答可能完全不调用模型。组件在本项目当前运行方式下由每个应用实例启动时创建并复用，多 worker 时各进程有自己的实例，不能理解为所有部署进程共享一个 Agent。cookies 或认证 header 是凭据载体，必须验证其有效性；请求体中的 user_id 既不是凭据，也不决定权限。

### 第二小段：Catalog 检索与 Router 分类/准入

本段只关注三个函数：

- `src/data_analysis_agent/semantic_catalog.py:CatalogRetriever.retrieve()`：用受控指标名称、别名和匹配规则选取用户角色可见的指标、表、列与 Join，返回 `CatalogSelection`，含定义对象、模型上下文 `prompt` 和可追踪的 `trace`。这里不是查询数据库里的业务结果，也不是模型生成 SQL。
- `src/data_analysis_agent/question_router.py:QuestionRouter.classify()`：判断用户是在查数据、问定义、追问结果、请求帮助还是提出不支持/越权操作，返回 `QuestionRoute`。
- 同文件 `QuestionRouter.admit_sql_request()`：对需要查库的请求再检查指标、维度/归属规则、查询形态、时间等是否足够明确；可把先前的查库意图改成澄清状态。通过不代表 SQL 已经正确或获准执行。

运行时的连接位置为 `chat_runtime.py` 中第一次 `retriever.retrieve()`、`classify()` 和查库分支中的 `admit_sql_request()`。中间还有受控历史复用和无指标匹配时的可选 LLM grounder；本段只跟踪首次单轮且明确命中指标的路径，不能推断所有请求均无模型辅助。

本问题中的“有效订单数”匹配 `data/catalog/olist_catalog_v3.yaml` 的 `paid_order_count`，定义为排除 canceled/unavailable 后按订单去重计数。按客户州需要选取 `fact_orders`、`dim_customers` 以及 `orders_customers` Join。Catalog 提供的是定义和关联依据，不含实际州订单数量。

本地直接执行现有检索与路由函数，三例核验通过；未请求 HTTP、数据库或模型：

| 问题 | classify 状态 | 最终状态 | 执行查库路径 |
| --- | --- | --- | --- |
| 按客户州统计有效订单数前五名，并生成柱状图 | answerable | answerable | 是 |
| 有效订单数的统计口径是什么？ | catalog_answered | catalog_answered | 否，定义来自 Catalog |
| 本月有效订单数是多少？ | answerable | missing_time | 否，要求补具体时间 |

第三例是在没有已确认时间范围的初始会话中运行的。当前实现不会自行把“本月”解析成明确日期窗口；这是当前产品行为，不能泛化成所有 Agent 都应这样处理。三个例子只验证这些问法，不证明规则覆盖所有中文表达。

本段待用户回答：

1. 两个问题都提到“有效订单数”，为什么问统计口径不用查数据库，而问订单有多少需要查？
2. “本月有效订单数是多少”已经被 classify 识别成数据查询，为什么 admit_sql_request 仍可以阻止生成 SQL？

### 后续小段（尚未讲解）

- 在线 QueryPlan 与模型上下文：怎样约束候选 SQL，与离线 QuerySpec 有什么区别。
- 工具执行到结果证据：为什么 SQL 可运行、合同通过、业务正确是不同层次。

本次真实请求已核验，但运行成功不能代替用户读代码。Router 本段、后续两段和闭卷链路复述完成后，才将 Day 1 标记完成。
