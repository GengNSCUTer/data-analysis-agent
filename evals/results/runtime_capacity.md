# 第五项：单实例容量、取消与故障验收

日期：2026-10-20。范围为产品的单进程 SSE 服务，不评测模型 SQL 语义泛化能力，也不重跑后训练 final test。

## 改动与使用边界

产品分析请求默认同时执行 4 个、最多等待 8 个，排队最长 15 秒，取得执行槽后整轮最长 180 秒。
前台语义理解、SQL 生成、修复与后台摘要共用最多 4 个模型调用槽。同一用户/workspace/conversation
不能同时修改分析状态；处理期间编辑、清空与删除会返回 409，用户可先停止当前请求。
登录按真实连接来源 IP 每分钟 10 次，非空提问按用户每分钟 30 次。排队、忙、限速和停止返回可读状态，
不限制用户用什么自然语言提问。产品只开放 SSE，不暴露 polling/WebSocket 旁路。

前端新增“停止生成”；SSE 失败或没有收到 DONE 即结束时明确提示，不自动换 polling 重发。
取消接口按用户、workspace、conversation 和 request ID 查当前请求；它等待后台清理后返回。
取消通过 psycopg2 connection.cancel() 传至真实 SQL 线程，线程结束并关闭连接后才释放请求槽。
短元数据事务在取消时等待提交/回滚完成；run INSERT 期间取消也会补上终态，不遗留 running 记录。
原有 SQL AST、reader 权限、结果校验和独立模型阶段超时继续生效。进程退出取消摘要并关闭模型客户端。

Cookie 最长 8 小时，嵌入令牌最长 5 分钟；退出时递增账号 session_version，该账号已签发的所有 Cookie
和嵌入令牌均失效。角色/账号仍需数据库存在；嵌入令牌检查来源和 workspace。升级前不带版本的旧凭证需
重新登录。服务默认不信任代理来源头；反向代理后的统一来源限速需部署者根据实际入口调整。

完整 CSV/Plotly 按最后保存后 7 天过期，启动及此后每小时清理。保留期内重启可恢复同一工件；分页/下载
先查归属和完整性，过期返回 410，已清理或文件缺失返回 404。已经打开的 CSV 下载可继续读完。
Vanna 的重复 query_results CSV 缓存也按相同时间清理。只处理配置的运行目录和固定工件/缓存文件名；
不清理实验、原始数据、训练结果或数据库历史与审计。

## 可控 provider、真实数据库容量

入口 `scripts/verify_runtime_capacity.py` 使用独立服务进程和工件目录、每轮唯一测试账号。
模型固定返回语义意图及候选 SQL，每次模型调用等待 100ms；真实运行 Intent → QueryPlan → Agent/tool →
SqlPolicy → PostgreSQL reader → ResultValidator → CSV/Plotly，不伪造成功 SSE。
每档分别 20 个请求，每个请求一个会话；各档使用不同用户，遵守默认 30 次/分钟提问限制。

| 客户端并发 | 完整通过 | 首个有效响应 P50 | 整轮 P50 | 整轮 P95 | 服务 RSS 峰值 |
| --- | --- | --- | --- | --- | --- |
| 1 | 20/20 | 745ms | 799ms | 871ms | 375.3MiB |
| 5 | 20/20 | 818ms | 921ms | 1,798ms | 378.2MiB |
| 10 | 20/20 | 1,883ms | 2,019ms | 2,983ms | 381.0MiB |

| 客户端并发 | 请求排队 P50 | 模型调用合计 P50 | SQL P50 | 绘图及保存 P50 |
| --- | --- | --- | --- | --- |
| 1 | 0ms | 225ms | 352ms | 29ms |
| 5 | 1ms | 225ms | 356ms | 28ms |
| 10 | 1,019ms | 231ms | 394ms | 30ms |

以上模型/SQL/绘图时长来自已有 run.catalog_trace.performance；阶段可能包含调用关系，不能直接相加解释。
首个有效响应由客户端观察 text/dataframe/chart，等待状态不计入。进程实际峰值为请求 4、排队 6、模型 4；
结束时 active/waiting/inflight 均为 0。数据库连接采样峰值 6（含监测连接及同库其他服务），无连接耗尽。
20 次本地 reader 连接/关闭抽测中位数 2.59ms、P95 4.05ms，因此本轮不新增连接池；这不排除更大规模的连接瓶颈。

这组结果依赖本机、固定问题、100ms 可控 provider，未模拟真实公网模型的吞吐和长尾，不是 SLA。

## 浏览器、数据库与认证故障验证

真实 Chromium 已验证停止按钮、断连、后端槽释放、正常查询与图表、刷新后同工件及相同 CSV、
跨用户下载 404；故意中断 SSE 验证不会调用 polling 重发。确定性回归覆盖队列满/超时、等待中取消、
同会话冲突、整轮超时、服务退出、模型流式/非流式槽释放、摘要关闭及元数据写入期间取消。
SQL 取消直接验证 PostgreSQL 工作连接消失；statement_timeout 用真实 reader 查询触发，不向线上放行 pg_sleep。
认证验证登录限速 429、Cookie/令牌过期与退出重放、错误来源/workspace 拒绝；保留既有用户/工作区下载隔离。

真实在线 provider 单列：TheLook 的完成订单总量与事件来源环形图均通过独立 reader 数值核对，
分别约 14.06s/17.12s；首个有效响应约 9.91s/8.51s，模型调用合计约 13.43s/14.51s。
这两题说明改造后真实链路仍可用，不是新的模型准确率估计。另有 5 项 Olist 页面回归通过。

联合 Python 回归为 **141 passed、5 skipped**，包含实际 PostgreSQL、账号/工件路由、上下文与预算。
5 项跳过是需要单独设置浏览器开关的流式用例；本轮页面及故障浏览器验证单列，不将跳过计为通过。
定向 Ruff、前端 TypeScript/Vite 构建和 diff 检查通过。

本机 Olist 首次启动清理已到期的 26 份结果工件（约 89KB）和 177 个重复查询 CSV 缓存；
会话、SQL 审计、业务数据和仓库外实验目录未删除。TheLook 同次清理数量为 0。

## 证据位置与复核

- 完整容量及浏览器报告：仓库外 `evals/product-readiness-stage5-20261020-r3/report.json`。
- 补充浏览器网络故障报告：`evals/product-readiness-stage5-20261020-browser-r4/`。
- 真实 DeepSeek-V3.2 两题及 SQL/CSV/trace：`evals/product-readiness-stage5-20261020-live/`。
- r1 缺少监测依赖、r2 浏览器响应体读取失败保留；监测改用 Linux /proc，修复成功 SSE 结束时误取消 reader 后，r3 完整通过。

```bash
python scripts/verify_runtime_capacity.py --output /absolute/private/stage5-new-run
python scripts/verify_runtime_capacity.py --browser-only --output /absolute/private/stage5-browser-new-run
```

需要已有 Olist 开发库、app Schema、受限 reader/writer 与 Playwright Chromium；脚本新增唯一前缀测试账号及
会话/审计，不清理已有账号和业务表，临时 HTTP 进程结束即停止。不要对正式用户库做压力验证。
默认参数及配置方法见 [部署说明](../../docs/deployment.md)。阶段 6（CI、开源交付和面试事实）尚未实施。
