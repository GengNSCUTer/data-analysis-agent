# SQL 策略与数据库权限

`sql_policy.py` 以 PostgreSQL 方言解析 AST，只允许单条 SELECT 或只读 WITH 查询。Schema、表列和敏感字段来自当前服务端 WorkspaceProfile/Catalog；不依赖模型保证权限，也不通过关键词黑名单识别 SQL。禁止写库、多语句、锁定查询、系统目录、文件/网络函数、任意表和 `SELECT *`；`COUNT(*)` 是合法聚合。

权限由数据库再次约束。reader 只获得明确安全业务表/视图的 SELECT、默认只读事务和超时；writer 只负责 `app` 的会话、账号、运行、审计和偏好等元数据。Olist 权限在 `infra/postgres/security.sql`，TheLook 安全视图权限在 `thelook_security.sql`；公共应用表定义位于 `app.sql`。新增已有业务库使用 `data-analysis-agent-init --metadata-only`，详见 [接入说明](workspace-onboarding.md)。

在线聚合预算由 QueryPlan 的分组、时间范围和明确 Top-N 生成，受进程行数、字节与超时兜底。默认查询上限5,000行、预览200行，两者可由用户设置但不得超过服务端限制。模型证据按本轮token预算提供完整小结果，或全结果统计与选定行；不再固定8行/1,200字符。这些限制都不是数据库扫描量上限。策略添加执行 LIMIT 时多读一行检测溢出，探测行不进入工件、模型或页面；不返回截断的聚合统计。冻结离线评测继续使用原协议。完整设计见 [result-budget-v2](architecture/result-budget-v2.md)。

`plan_sql_guard.py` 检查已确认的筛选、排名、归因、已覆盖指标来源与公式及关联约束，发现遗漏先走现有一次修复流程。它能识别将输出指标自身作最终分组键等已知错误，但不是 SQL 等价性证明。JOIN证据检查重复输入及未覆盖高风险SUM；ResultValidator检查返回列、空值、数值范围、时间、完整性与完整维度/时间组合唯一性。同一州可出现在不同品类或月份，不能只凭重复州名拒绝。业务准确性仍需独立数值对照，唯一性、AST或结果合同通过都不足以证明公式正确。

产品使用服务端验证的本地账号会话或受限嵌入凭证，身份、角色、workspace 和工件归属均由服务端解析；演示身份切换不是生产认证。开发 PostgreSQL 的 Unix socket 当前使用 trust；生产部署必须配置独立密码/密钥和受控网络。
