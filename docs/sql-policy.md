# SQL 策略与数据库纵深防御

`src/data_analysis_agent/sql_policy.py` 是所有模型 SQL 的第一个执行关口。它以 PostgreSQL
方言解析 AST，仅接受单条 `SELECT` 或只读 `WITH ... SELECT`，并执行：

- 强制 `analytics` Schema 和显式表白名单；
- 物理列白名单，拒绝未知字段和跨 Schema 查询；
- 拒绝 DML、DDL、多语句、锁定查询和 `pg_sleep` 等危险函数；
- `analyst` 禁止投影原始订单、客户、卖家、商品、评价标识和邮编等敏感字段，允许
  `COUNT(order_id)` 一类聚合；`admin` 可查看数据版本元信息；
- 自动附加最大 `LIMIT`（`analyst` 200、`admin` 1000），超出时收紧。

第二层是 `infra/postgres/security.sql`。`daa_analytics_reader` 只有 `analytics` 的
`SELECT` 权限，默认只读事务和 5 秒语句超时；`daa_app_writer` 只能写 `app.query_audits`。
当前用户态开发集群通过 Unix Socket 的本地信任认证运行，生产部署必须改为独立密码/密钥
和受控网络认证。`SecurePostgresRunner` 已是可信 Olist Demo 中 `RunSqlTool` 的唯一执行器：
策略拒绝、允许执行和 PostgreSQL 失败均以 `daa_app_writer` 写入 `app.query_audits`；实际
查询以 `daa_analytics_reader` 的只读事务和 5 秒超时执行。Demo 身份来自受控请求头，只用于
演示角色策略，不能替代真实认证。
