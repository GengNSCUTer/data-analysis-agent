# 接入新的 PostgreSQL 业务库

产品按服务端工作区配置装配，同一实例当前接入一个业务数据库。Olist 和 TheLook 使用同一套自然语言理解、QueryPlan、SqlPolicy、只读执行、会话、结果工件和图表流程，没有在核心代码中按数据集名称切换 SQL 模板。

这不等于从 DDL 自动推导业务含义。业务方需要提供订单状态、指标公式、时间归属、维度、敏感字段和关联关系。例如 TheLook 的已完成成交额是 `Complete` 订单的商品行 `sale_price` 总和，不能照搬 Olist 的 `price` 和状态过滤。

第一次接入请从[可复制模板与填写清单](templates/workspace/README.md)开始，包含可由现有加载器
直接读取的workspace/Catalog YAML，以及每一项该由Schema还是业务负责人确认。
[TheLook填写实例](workspaces/thelook.md)按同一清单说明7张表、20项指标、时间、关联与能力边界。
维度基数需在配置中填写，或由版本匹配的shape bounds提供；不要拿虚构小值限制真实结果。

```bash
python scripts/check_workspace.py --workspace /your/config/workspace.yaml
python scripts/check_workspace.py --workspace /your/config/workspace.yaml \
  --env-file /private/runtime.env --database --output /private/workspace-check.json
```

这是接入诊断，不添加在线请求拦截。配置能解析、列存在和权限正确仍不等于业务公式正确。

## 配置内容

| 配置位置 | 内容与职责 |
| --- | --- |
| 私有环境变量 | `DATA_ANALYSIS_POSTGRES_HOST/PORT/DB`、`PGPASSFILE`、API key、状态目录；不提交凭证 |
| 工作区 YAML | workspace/dataset ID、页面说明与示例、Catalog 路径、reader/writer 角色、物理 Schema、维度绑定、可相加构成图指标 |
| Catalog YAML | 表列与敏感性、角色可见性、指标名称/别名/公式/过滤/时间/允许维度、Join 与基数 |
| PostgreSQL 权限 | reader 只读明确允许的业务表或安全视图；writer 只写 `app` 元数据；原始敏感表不授权 |
| 可选 shape bounds | 与快照版本对应的维度基数/逐日事实行数，用于估计结果预算；不是查询结果，不进入模型 |

参照 [Olist](../data/workspaces/olist.yaml) 或 [TheLook](../data/workspaces/thelook.yaml)。文件中的相对路径相对于工作区 YAML 所在目录。表列白名单和敏感字段从服务端 Catalog 生成，复用已有类型与 CatalogLoader 校验；浏览器不能上传 Catalog 或切换连接。

维度的 `sources` 按优先顺序列出来源。指标事实表已有该维度时直接使用；需额外关联且匹配分数相同时，优先靠前来源。TheLook 品类先列 `products.category`，库存指标仍可直接用 `inventory_items.product_category`。多对一关联使用 Catalog 的键，不能把商品级汇总关联到多行库存后重复累加。

## 已有业务库启动

先准备安全视图与只读角色。TheLook DDL 在 `infra/postgres/thelook_schema.sql`、`thelook_security.sql`，原始数据不随 Git/wheel 分发。本地已有库无需重导入，不运行 Olist 的 `analytics.sql` 或合成数据初始化。

按 [部署说明](deployment.md) 安装。安装包包含两套工作区/Catalog、TheLook shape bounds 和公共应用 SQL。查看资源目录：

```bash
python -c 'from data_analysis_agent.runtime_paths import RESOURCES; print(RESOURCES)'
```

将下面路径替换为实际位置，写入私有环境文件：

```dotenv
DATA_ANALYSIS_WORKSPACE_CONFIG=/actual/package/resources/workspaces/thelook.yaml
DATA_ANALYSIS_POSTGRES_HOST=127.0.0.1
DATA_ANALYSIS_POSTGRES_PORT=5432
DATA_ANALYSIS_POSTGRES_DB=thelook_analytics
DATA_ANALYSIS_STATE_DIR=/private/thelook-state
PGPASSFILE=/private/thelook-state/pgpass
DATA_ANALYSIS_DEMO_HOST=127.0.0.1
DATA_ANALYSIS_DEMO_PORT=32104
SILICONFLOW_API_KEY=your-private-key
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V3.2
```

只在初始化进程中提供管理员 `DATA_ANALYSIS_ADMIN_DSN`、现有 `DATA_ANALYSIS_READER_PASSWORD` 和专用 writer 的 `DATA_ANALYSIS_WRITER_PASSWORD`，执行：

```bash
data-analysis-agent-init --metadata-only
```

该命令创建/迁移公共 `app` 表、配置 YAML 指定的 writer、生成 `0600` passfile。不改业务表、现有 reader 密码或业务授权。管理员必须使用专用普通 writer，不复用超级用户或已有业务写角色。本机 Unix socket 使用 trust；本轮验证角色授权，不声称验收生产密码认证。

从应用进程环境移除管理员 DSN 和初始化密码，保留受限 passfile，再运行 `data-analysis-agent`。访问 `/embedded-copilot`，用服务端生成的本地账号密码登录。bootstrap 由服务端返回已授权工作区，不再写死 Olist。启动多个实例时使用不同端口、状态目录与数据库配置。

## 验证与边界

`tests/test_runtime_workspace.py` 用第三个自构造工单 Schema 验证新指标、新维度、不同物理 Schema 下的 Catalog、意图、QueryPlan、SQL 策略和帮助，未添加工单数据集分支。

```bash
python scripts/verify_workspace_onboarding.py \
  --url http://127.0.0.1:32104 \
  --password-file /private/thelook-state/bootstrap-password.txt \
  --output /private/onboarding-evidence --workers 2
```

脚本通过真实浏览器/模型执行 `evals/cases/thelook_product_queries.yaml` 的60个新编问题，
以独立reader SQL核对CSV；覆盖全部20指标、分组、趋势、两组各五轮连续追问、图表、
下载/分页、空窗口、敏感访问和最小澄清。两名浏览器worker只并行独立对话，同一对话按顺序。
支持 `--case-id` 定向复测，复测证据另存目录，不覆盖首次分母。问题与参考SQL用于产品开发，
不用于训练或替换protected final；脚本不修改业务数据，不新增跨工作区fixture。
选择连续追问中的一题时，会运行其完整对话组，保证该题有真实上文；不存在的题号直接报错。

原始日志、截图、CSV 留在仓库外指定目录。实际本轮结果见 [阶段4记录](../evals/results/workspace_onboarding_thelook.md)。本轮只验证 PostgreSQL 跨业务 Schema；不宣称其他数据库引擎、裸表零配置理解、多租户 SaaS、企业 SSO 或真实企业上线。TheLook 来自 Kaggle 镜像，数据许可不因此重新授权。

模板补充后的60题结果见[独立查询报告](../evals/results/thelook_product_queries.md)：首次复核58/60，
两项修复有定向成功证据，但额外重复城市统计仍有一次分组错误被拦截；不是稳定100%。
模板和检查/评测脚本随源码提供，上述 `scripts/` 命令从仓库根目录执行；wheel仍使用同一运行时格式。
