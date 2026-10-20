# 第二业务库在线接入验收：TheLook

2026-10-20 完成产品完善计划第四项。结论是服务端配置不同 PostgreSQL 业务 Schema 后，在线链路能够复用；本次不是本地模型成绩、企业上线或任意数据库零配置接入证明。

在线实例使用 `thelook-demo` / `thelook_analytics`，默认 provider 仍为 `deepseek-ai/DeepSeek-V3.2`。7 张安全视图、20 项指标、13 个维度；原始业务表与旧离线 workspace 身份保持独立。来源是既有 Kaggle 镜像，不是本次直接从 BigQuery 导出。

## 实际测试

| 项目 | 结果与范围 |
| --- | --- |
| TheLook 浏览器、真实模型与 PostgreSQL | 最终完整重跑 11/11 轮通过 |
| 独立数值核对 | 8/8；以独立 reader SQL 与下载 CSV 逐列比较，不使用 Gold renderer 自证 |
| 覆盖业务 | 完成订单/成交额概览、品类前五、月度订单趋势、未售库存部门分布、事件流量来源 |
| 连续分析 | 最高成交额客户州 → 该州品类前十 → 取消州限制；每轮数值独立核对 |
| 换图复用 | 横向柱状图换成柱状图，工件 ID 相同，SQL 调用数为0 |
| 敏感/原始数据 | 邮箱/地址/用户 ID、直接访问原始表两类，均为 unauthorized 且 SQL 调用数为0 |
| 工作区隔离 | 指定 Olist workspace 返回403；同用户实际存在的其他 workspace 会话与 CSV 返回404；匿名接口401 |
| 数据库角色 | reader 不可访问原始/应用 Schema、不可写业务；writer 可写应用元数据、不可写业务 |
| 第三个自构造 Schema | 工单库使用新的表、列、指标、维度和 `support` Schema，复用加载、提示、意图、计划、Policy与帮助；确定性验证，无核心数据集分支 |
| Python 定向联合回归 | 293 passed、1 skipped；跳过的是未显式开启的旧 Demo DB 检查 |
| 实际数据库/账号/工件路由 | 24 passed |
| Olist 页面回归 | 5 passed、15 deselected |
| Olist 真实九轮连续分析 | 1组通过，264.72秒；州排名、品类下钻、改Top-N、换图、换指标、换州、取消筛选、解释结果 |
| 打包与公共 DDL | wheel 构建/离仓库安装，安装资源可加载两套 Catalog；全新独立PG18.3初始化两次均1,200订单，角色权限通过，临时进程已停止 |

TheLook 业务查询单轮18.22–54.40秒；换图10.98秒，两种敏感拒答9.50/10.01秒。这里只是本次样本的端到端时间，不是稳定延迟或 SLA。Olist/TheLook 当前业务库运行于原 PG12.20，隔离安装回归是 PG18.3。

## 本轮实际修复

首先发现在线会话存储仍默认 Olist，首轮执行后保存触发 workspace 冲突。装配现在把工作区及数据/指标版本传入 Store，启动回归专门覆盖该问题。

独立数值核对还发现品类查询错误：模型把商品级销售汇总按 `product_id` 关联多行 `inventory_items`，重复放大成交额。原因包括同义维度来源按字母顺序选到库存事实，以及既有关联键检查仅在有筛选时启用。现在按已配置来源优先级选择维度来源，并把已知多对一 Join 检查用于无筛选查询。保留合法库存键、派生聚合关联的测试，没有新增通用“所有 Join 都拒绝”规则。最终品类排行与取消筛选均在完整重跑中通过。

`app.sql` 抽出公共会话/账号/运行/审计 DDL；Olist loader 与部署入口同步更新。新增 `--metadata-only` 只配置现有业务库的应用存储和独立 writer，不导入 Olist 业务表，也不改变已有 reader 密码。产品系统提示、帮助、示例和 bootstrap 由服务端配置/Catalog 驱动。历史离线模板和数据文件不覆盖迁移。

## 证据位置与限制

仓库外根目录：`/disk2/gengnan/data-analysis-agent-data/evals/product-readiness-stage4-20261020/`。

- `browser-r4/report.json`：最终11轮完整结果；`trace-audit.json`复核终止状态、路由和SQL次数。
- 同目录每题 `.conversation.json`、`.sse.txt`、`.csv`、`.png`：实际浏览器和模型证据。
- `role-permissions.json`：实际数据库角色检查。
- `bootstrap-rc39mzwd/report.json`：已安装wheel在独立PG18.3的新建与重复初始化。
- `browser-r1`：工作区装配遗漏的初次失败；`browser-r2`：独立数值发现Join放大；`browser-r3`：测试启动早于服务就绪的连接失败，均保留，不混入最终通过分母。
- Olist 九轮证据：`evals/analysis-memory-phase5-20261016/flow-continuous/1792469695426-uzspvxjk1/`。

结果只覆盖上述开发场景，没有穷举20项指标，也不能证明任意模型 SQL 的业务等价性。暂未验证 MySQL/BigQuery driver、企业 SSO、多租户 SaaS 或外部企业部署。Docker 本机权限限制仍在，不把原生安装证据写成容器实运行通过。下一项是多人使用、安全和故障处理，本轮没有开始。
