---
goal: Persist workspace scope and optimistic state versions for trusted Copilot conversations
version: 1.0
date_created: 2026-09-21
last_updated: 2026-09-21
owner: Data Analysis Agent
status: Complete
tags: [feature, runtime, embedded-copilot, workspace, concurrency, m2]
---

# M2：Workspace 与会话状态版本

## 合同

每个 `app.conversations` 行必须绑定一个服务端 workspace 和单调递增的
`state_version`。历史单 workspace 数据只允许回填到当前默认 `olist-demo`，不能猜测迁移到
TheLook 或其他未来 workspace。

读取、列表和删除均以 `(conversation_id, user_id, workspace_id)` 为范围；更新先锁定行，比较
对象携带的 `state_version`，只有相等时才写入并递增一版。版本不一致抛出
`ConversationStateConflict`，聊天边界返回可恢复提示，不允许 silent last-write-wins。

## 实现

- `infra/postgres/security.sql`：幂等增加 `workspace_id`、`state_version`、检查约束和 workspace 索引。
- `PostgresConversationStore`：默认 workspace、严格范围查询、版本递增与 stale-write 拒绝。
- `BudgetedChatHandler`：新建会话时写入服务端 workspace/version；并发冲突转成可恢复状态卡。
- trusted demo list/detail DTO：返回 workspace/version，便于 UI 后续显示和刷新判断。

## 验收

真实迁移通过 Unix socket 执行，206 条已有会话回填为 `olist-demo`。`tests/test_postgres_conversation_store.py`
和 `tests/test_trusted_routes.py` 在 `RUN_PROJECT_DB=1` 下为 `6 passed`；包括 workspace 交叉读取返回空、
state version 递增和 stale object 被拒绝。非数据库 M1/身份/工作区回归保持 `20 passed`。

## 不做

本轮不接入 TheLook 在线 workspace，不做数据库 RLS、不做跨域认证 token、不改 SQL 生成/Policy/reader/
ResultValidator，不实现 M3 ResultArtifact presenter 或 M5 清空/失效 UI。
