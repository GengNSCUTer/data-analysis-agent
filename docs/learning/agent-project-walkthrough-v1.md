# Agent 项目代码学习记录

本记录承接 [`四周完善与学习计划`](../../plan/process-agent-internship-improvement-v1.md)，只放代码理解与问答。项目进度见 PROJECT.md，后训练知识继续使用既有 post-training/learning 目录。每轮阅读 1–3 个关键函数，问题答完再进入下一段。

## Day 1：沿一条问题认识请求链

场景：“按客户州统计有效订单数前五名，并生成柱状图”。完整调用关系和本次请求证据见 [`产品请求架构`](../architecture/product-request-architecture-v1.md)。目标是用户能画出请求链，区分代码/模型职责，以及离线 QuerySpec 与在线 QueryPlan；当前处于第一小段，尚未完成复述。

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

待用户回答，尚未记录答案：

1. `create_app()` 和 `chat_sse()` 分别在什么时候运行？用户再发一条消息，会重新装配所有组件吗？
2. 为什么单凭请求里的 conversation_id 或 user_id，不能决定用户可查询哪些数据？

### 后续小段（尚未讲解）

- Catalog → `classify()` → `admit_sql_request()`：怎样决定查库、澄清、拒绝或直接回答。
- 在线 QueryPlan 与模型上下文：怎样约束候选 SQL，与离线 QuerySpec 有什么区别。
- 工具执行到结果证据：为什么 SQL 可运行、合同通过、业务正确是不同层次。

本次真实请求已核验，但运行成功不能代替用户读代码。上述三段和闭卷链路复述完成后，才将 Day 1 标记完成。
