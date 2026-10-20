# SQL 策略与数据库权限

`sql_policy.py` 以 PostgreSQL 方言解析 AST，只允许单条 SELECT 或只读 WITH 查询。Schema、表列和敏感字段来自当前服务端 WorkspaceProfile/Catalog；不依赖模型保证权限，也不通过关键词黑名单识别 SQL。禁止写库、多语句、锁定查询、系统目录、文件/网络函数、任意表和 `SELECT *`；`COUNT(*)` 是合法聚合。

权限由数据库再次约束。reader 只获得明确安全业务表/视图的 SELECT、默认只读事务和超时；writer 只负责 `app` 的会话、账号、运行、审计和偏好等元数据。Olist 权限在 `infra/postgres/security.sql`，TheLook 安全视图权限在 `thelook_security.sql`；公共应用表定义位于 `app.sql`。新增已有业务库使用 `data-analysis-agent-init --metadata-only`，详见 [接入说明](workspace-onboarding.md)。

在线聚合预算由 QueryPlan 的分组、时间范围和明确 Top-N 生成，受进程行数、字节与超时兜底。UI 初始预览 200 行，模型只接收最多 8 行/1,200 字符摘要；这两个展示限制不是数据库扫描量上限。策略添加执行 LIMIT 时多读一行检测溢出，探测行不进入工件、模型或页面；不返回截断的聚合统计。冻结离线评测继续使用原协议。完整设计见 [result-budget-v2](architecture/result-budget-v2.md)。

`plan_sql_guard.py` 检查已确认的筛选、排名、归因及已知多对一关联键，发现遗漏先走现有修复流程。它能识别已经覆盖的错误，例如将商品汇总按商品 ID 关联到多行库存后放大金额；它不是 SQL 等价性证明，也无法保证任意业务公式正确。ResultValidator 检查返回列、空值、类型、数值约束与结果范围。业务准确性还需独立数值对照，不能由 AST 或结果合同通过推出。

产品使用服务端验证的本地账号会话或受限嵌入凭证，身份、角色、workspace 和工件归属均由服务端解析；演示身份切换不是生产认证。开发 PostgreSQL 的 Unix socket 当前使用 trust；生产部署必须配置独立密码/密钥和受控网络。
