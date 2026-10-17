# Data Analysis Agent（可信业务数据分析智能体）

> 文档导航：先看 [`docs/README.md`](docs/README.md)。本文是项目需求、架构、技术决策和当前阶段的本地基线；后训练学习材料统一从 [`docs/post-training/README.md`](docs/post-training/README.md) 进入。

## 0. 本地数据边界

仓库内的 `data/` 只保存可提交、可审查的小型资产：指标 Catalog、数据 manifest、DDL/转换定义和合成 fixture。原始第三方数据、SQLite 数据库、模型权重、Adapter/checkpoint、预测和实验日志统一保存在仓库外的 `/disk2/gengnan/data-analysis-agent-data/`，这是本项目唯一的大型本地数据根，不进入 Git。

外部数据根按职责分为 `text-to-sql/`（公开 Text-to-SQL 基准及其版本化 release）、`olist-v2-2026-08-03/`（展示案例原始与分析数据）、`models/`、`experiments/` 和 `evals/`。新增基准必须使用 `text-to-sql/<dataset>/<release>/`，原始压缩包置于该 release 的 `archives/`，并在同目录或仓库 manifest 中记录来源、许可证和 SHA-256。此边界避免仓库内 `data/` 与仓库外数据根被误解为两份同类数据。

2026-09-01 已完成 CSpider full release 的获取预检和官方三切分 SFT 输入构造：原始 archive、解压结果和 acquisition manifest 位于 `text-to-sql/cspider/cspider-1.0-official-2026-09-01/`，准备后的 JSONL 与 audit 位于同一 release 的 `prepared/official-splits-v1/`。官方 train/dev/test 原始记录数为 8,659/1,034/2,147，涉及 146/20/40 个互不重叠的数据库 schema；SFT 输出为 8,656 train、1,034 validation、2,147 final-evaluation-only test。train 有 3 条公开 gold SQL 无法在随 release SQLite/schema 上通过只读 `EXPLAIN`，已保留来源证据并隔离，不参与参数更新；dev/test 全量保留且 test 物理隔离。Trainer 的 split-audit 校验已显式支持 CSpider 官方三切分并保持历史 Spider 契约。2026-09-02 已按真实 Dataset 的 prompt + SQL + EOS 布局完成 train/validation token 审计并物化 `official-splits-length1536-v1`，冻结 `cspider-token-length-v1` 的 `max_seq_length=1536`：8,574 train、1,034 validation 与完整 2,147 final test，82 条 train 超长记录仅进入仓库外 exclusion manifest；超长行不得截断，test 不进入训练或模型选择。两 epoch bf16 LoRA 已在 RTX 4090 完成并 fresh reload；CSpider matching Base/Adapter 专用入口也已实现和测试，但尚未运行完整 1,034 条生成、SQLite/denotation 或任何 CSpider SQL 质量评测。

2026-09-02 补充状态：上段中“CSpider matching Base/Adapter 尚未运行”的历史表述已失效。完整 CSpider validation 的 Base/Adapter 生成、SQLite diagnostics 与 bounded denotation 已完成；最终 Adapter 随后完成一次受保护 Olist 迁移评测，结果仅支持“当前业务迁移子门未通过、保持生产默认路径”的结论。详见本文件变更记录与 [`docs/post-training/experiments/cspider-bf16-lora-2epoch-v1.md`](docs/post-training/experiments/cspider-bf16-lora-2epoch-v1.md)。

## 1. 项目定位

本项目面向企业内部的运营与业务分析人员。用户以自然语言提出经营分析问题，系统在受控的数据访问范围内生成并执行只读 SQL，流式返回结论、图表、数据表、指标口径和 SQL 证据。

项目不是把大模型直接接到数据库上的聊天 Demo，而是一个以 **正确性、安全性、可解释性、可复现性和可评测性** 为核心的可信 Text-to-SQL 系统。Vanna 提供 Agent 与流式交互底座；本项目的主要工程贡献在指标语义层、SQL 安全网关、证据链、审计与离线评测。

仓库：<https://github.com/GengNSCUTer/data-analysis-agent>

飞书同步文档：<https://my.feishu.cn/docx/QIr2dfKp7oIJvqxcPerckYd6nfC>

## 2. 第一性原理与目标

真实问题不是“如何生成一条能运行的 SQL”，而是：业务用户希望快速获得数据结论，但不能信任任意 LLM 生成的 SQL，也不能接受没有口径、来源和执行记录的结论。

因此，第一版必须满足以下性质：

1. **业务正确性**：系统理解并遵循 GMV、订单量、履约时长、好评率等指标的明确口径，而不只追求 SQL 可执行。
2. **执行安全性**：LLM 不得写库、执行多语句、访问未授权表/列，且单次查询受限于超时、扫描量和返回行数。
3. **结论可解释性**：每条完成的回答必须附带指标定义、最终 SQL、来源表、过滤条件、结果摘要及可选图表。
4. **结果可复现性**：记录数据集版本、Schema/指标版本、模型与配置、请求、SQL、策略决策和执行结果摘要。
5. **系统可评测性**：以标准业务问题和恶意/越权 SQL 用例评估执行成功率、口径正确率、安全拦截率和时延。

## 3. 需求清单

### 3.1 v1 必须实现（MVP）

| 编号 | 需求 | 验收标准 |
| --- | --- | --- |
| FR-01 | 自然语言业务问答 | 用户可在页面提交中文问题，并获得流式状态和最终结果。 |
| FR-02 | 受控 Text-to-SQL | Vanna 仅通过允许的工具查询 PostgreSQL 分析 Schema。 |
| FR-03 | 指标语义层 | 每个核心指标有名称、公式/SQL、统计粒度、默认过滤、可用维度、版本与说明。 |
| FR-04 | SQL 策略网关 | 仅允许单条 `SELECT` / 只读 `WITH ... SELECT`；校验 AST、表列白名单、角色范围、LIMIT、超时。 |
| FR-05 | 数据库纵深防御 | 分析查询使用独立 PostgreSQL 只读角色；应用元数据使用独立可写角色。 |
| FR-06 | 结构化结果呈现 | 返回结论、表格、最终 SQL、指标/数据来源证据和适用时的图表。 |
| FR-07 | 审计与历史 | 持久记录用户、请求、模型、指标版本、原始/最终 SQL、策略决策、耗时、行数、错误与结果摘要。 |
| FR-08 | 基础权限 | 至少支持 `analyst` 和 `admin` 两种角色，并能证明表/字段/行范围策略不同。 |
| FR-09 | 数据集可复现 | 提供数据清单、建表、清洗/加载脚本、字段字典、指标定义和小型合成测试数据。 |
| FR-10 | 离线评测 | 至少包含业务问答、复杂关联、指标口径和安全攻击四类用例；保存每次评测结果。 |

### 3.2 v1 不做的内容

为避免把核心问题稀释为“组件堆叠”，以下内容明确不进入 v1：

- 不做多 Agent 编排、MCP 平台或任意 Python 代码执行；
- 不在没有实际需求时引入 Redis、Celery 或复杂消息队列；
- 不支持写数据库、数据修改审批或自动化运营动作；
- 不在第一版接入多个分析数据库方言；
- 不把普通文档 RAG 当作必备能力。Schema 和指标规模可控时，优先使用结构化指标/数据字典上下文；
- 不把完整 BI 平台、拖拽式报表设计器作为目标。

### 3.3 后续候选能力（v2+）

- Redis + `arq`：大报表/导出任务、限流、短期缓存、多实例任务进度；
- 评测集自动回归、模型与 Prompt 对比；
- 导出 CSV/PDF 报告与订阅式报告；
- 多数据集/多工作区，细粒度行级权限；
- 对文档、口径说明进行受控检索增强；
- 通过复核工作流处理低置信度或高风险问题。

## 4. 用户、场景与核心问题

### 4.1 目标用户

- `analyst`：只能在被授予的业务范围内查询经营数据、查看自己的历史记录；
- `admin`：可管理指标定义、数据集版本、评测集和完整审计记录。

### 4.2 首批业务问题示例

- 本月各地区 GMV 与上月相比有什么变化？
- 哪些品类订单增长但好评率下降？
- 哪些卖家的平均履约时长异常，并对评分造成了什么影响？
- 某地区营收下滑主要由订单数、客单价还是品类结构导致？
- 使用信用卡付款的订单在不同地区的客单价和取消率有什么差异？

### 4.3 每次成功回答的统一输出契约

```text
自然语言结论
+ 指标口径与统计周期
+ 最终 SQL（可折叠）
+ 结果表与关键数值摘要
+ 数据来源表 / 字段
+ 图表配置或图表
+ 执行与策略信息（耗时、行数、是否限流/改写）
```

若 SQL 被阻断、超时、口径不明确或无权限，系统必须给出原因和可执行的下一步，而不是编造答案。

## 5. 总体设计

### 5.0 当前运行基线（Phase 1 与可信查询原型）

基础冒烟入口仍是
`examples/siliconflow_sqlite_web_demo.py`：它创建本地合成 SQLite fixture，使用
Vanna 的 `Agent`、`ToolRegistry`、`SqliteRunner`、`OpenAILlmService` 和原生 FastAPI
路由，页面由 Vanna 原生 `<vanna-chat>` 提供。模型通过 `.env` 中的
`SILICONFLOW_API_KEY` / `SILICONFLOW_BASE_URL` 调用
`deepseek-ai/DeepSeek-V4-Flash`，服务监听 `127.0.0.1:32009`。

同一 FastAPI 进程还提供 `/embedded-demo`：这是一个无框架的经营总览宿主页，加载本地构建
版 `<vanna-chat>`，以中文标题、提示词和 `window-state-changed` 事件组合原生组件。组件
初始为最小化入口；桌面端已验证最小化、恢复、最大化及真实 SSE 结果，390px 宽移动端已
验证没有页面横向溢出。`RunSqlTool` 使用项目注入的 `LocalFileSystem`，默认将下游 CSV
写到 `/tmp/data-analysis-agent-vanna-query-results/`，可通过 `VANNA_QUERY_RESULTS_DIR` 覆盖，
不再写入仓库根目录。

Vanna 2.0.2 自带的 RichText Markdown 解析器没有实现表格语法，导致模型最终回答中的
`|`、`---` 和 `**` 被原样显示。本项目在 `frontends/webcomponent/` 中做了局部修复：先
转义原始 HTML，再将合法 Markdown 表格转为语义化 HTML table；构建后的 bundle 由 FastAPI
在 `/static/vanna-components.js` 提供，根页面与宿主页共用该 bundle。该改动是为修复已验证
的原生组件缺陷，不是另建前端框架；`node_modules/` 和 `dist/` 均为忽略的本地构建产物。

可信查询原型入口为 `examples/trusted_olist_web_demo.py`，监听 `127.0.0.1:32010`。它以
`SecurePostgresRunner` 将 Vanna 的唯一 SQL 工具固定到 PostgreSQL `analytics` Schema：每条
SQL 先通过 `sqlglot` AST 策略，再由 `daa_analytics_reader` 只读角色执行，并通过
`daa_app_writer` 写入 `app.query_audits`。Demo 当前使用服务器签名、短期有效的 `analyst` /
`admin` 演示会话 cookie；它能验证策略和审计范围差异，但不能视为真实认证。`/api/project/evidence`
提供数据/指标版本，`/api/project/audits` 按角色返回审计历史；真实浏览器与 SSE 已验证中文问题、
结果表、中文结论、最终 SQL 和审计记录闭环。

SQLite fixture 只用于可重复的上游冒烟验证，不代表最终业务数据；可信原型使用已加载的
Olist 分析表。图表由服务器拥有的 `ChartContract` 从用户意图、`QueryPlan` 与 `ResultContract`
派生，固定允许的图表类型、横轴、指标列、可选系列、标题、当前 SQL 结果工件与行数边界。
当前只支持服务端直接渲染的柱状图、横向条形图、折线图、面积图和环形图；不支持或缺少必要横轴的
请求会在 SQL 前澄清/拒绝，不能由模型或前端静默改图。面积图仅用于单指标无业务分组的时间序列；
环形图仅用于用户显式请求的单个可加指标、单维度、至多 8 个非负类别。图表层仅渲染已经通过
`ResultValidator` 的当前结果，不补值、去重、聚合或重算。
前端在普通、缩放、最大化和 390px 窄屏嵌入窗口中均有确定性浏览器回归。真实认证、行级范围
与真实模型调用下的完整图表链路验收仍属于后续阶段，不能把演示请求头当作生产认证。

下一轮权限演示采用服务器签名、短期有效的 Demo 会话 cookie：页面只可选择预置的 `analyst`
或 `admin` 身份，服务端将该角色同时用于 SSE、SQL 策略和审计 API；不再接受客户端请求头
作为提权来源。它用于验证角色化策略差异和演示流程，不含密码、OAuth、组织目录或真实用户
身份校验，页面必须明确标为“演示会话，非真实登录”。

```text
既有业务网页 / 静态演示宿主页
└─ 浮动或右侧面板：Vanna <vanna-chat>
              │ HTTPS + SSE
              ▼
FastAPI 应用
├─ 身份认证与角色解析
├─ Vanna Agent / Tool Registry
├─ 指标与 Schema 上下文提供器
├─ SQL AST 策略网关
├─ 只读 SQL 执行器
├─ 证据链装配器
├─ 审计与评测服务
└─ SSE 响应适配器
              │
              ▼
PostgreSQL
├─ analytics schema：清洗后的业务分析数据（只读查询角色）
└─ app schema：用户、会话、审计、指标、数据集、评测（应用写入角色）
```

上图是通过原生 Vanna 基线后逐步建设的架构。当前没有独立 `frontend/` 应用或 PostgreSQL
业务应用；`src/data_analysis_agent/` 是通用扩展层，Olist 仅通过 `WorkspaceProfile` 和
`metric_context.OLIST_WORKSPACE` 作为当前数据集适配器。宿主页只负责嵌入和样式，不引入新的
前端框架。

### 5.1 后端模块职责

| 模块 | 职责 |
| --- | --- |
| API 层 | 鉴权、请求校验、SSE、错误语义和 OpenAPI。 |
| Agent 编排层 | 绑定 Vanna Agent，限制工具集合和最大工具循环次数，记录模型调用。 |
| Context 层 | 按角色提供经过筛选的 Schema、指标定义、样例问题与业务约束。 |
| SQL Policy 层 | 基于 `sqlglot` 解析 AST，执行语句类型、单语句、对象白名单、LIMIT、范围过滤和预算校验。 |
| Query Runner | 使用 PostgreSQL 只读角色执行 SQL，设置 `statement_timeout`、最大行数和取消机制；失败时由 `TrustedRunSqlTool` 最多发起一次受控修复，并重新经过 Policy、reader role 和结果合同。 |
| Evidence 层 | 将 SQL、指标版本、来源、结果摘要和图表统一为前端可展示的证据对象。 |
| Audit/Eval 层 | 持久化可回放记录，运行带标准答案或人工判定的评测集。 |

### 5.2 SQL 安全链路

```text
用户问题
  → 角色与组织范围解析
  → 受控 Schema / 指标上下文
  → Vanna 生成候选 SQL
  → SQL AST 解析与策略校验
      ├─ 拒绝：返回可解释的拒绝原因并审计
      └─ 放行：补充 LIMIT / 范围约束
  → PostgreSQL 只读账号执行（超时与行数上限）
  → 执行失败：脱敏错误 → 一次修复候选 → 重新 Policy/reader role
  → ResultValidator / ResultContract
  → 表格 / 图表 / 结论 / 证据对象，或可信拒答
  → SSE 返回并写入审计记录
```

SQL 策略的底线如下：

- 仅允许 `SELECT` 和最终为 `SELECT` 的 `WITH` 查询；
- 单条语句，禁止注释绕过、DDL、DML、存储过程、事务控制与外部文件访问；
- 仅允许白名单 Schema、表和列；
- 强制最大返回行数和查询超时；
- 对需要组织/工作区隔离的表追加不可被模型移除的行级条件；
- 数据库账号本身只拥有 `SELECT` 权限，作为策略层之外的第二道防线；
- 原始 SQL、规范化 SQL、策略决策和执行错误全部进入审计。

### 5.3 指标语义层

指标是控制业务正确性的关键资产。`metric_definitions` 中每项至少包含：

```text
metric_id / name / description / version
formula_or_sql / time_field / grain
default_filters / allowed_dimensions / source_tables
recommended_chart / owner / effective_from
```

首批指标暂定为：GMV、支付订单数、客单价、取消率、平均履约时长、准时交付率、好评率、卖家活跃数、复购用户数、品类/区域收入占比。具体口径在数据模型确定后再冻结，并以版本方式演进。

### 5.4 核心数据模型（初稿）

`analytics` Schema 以 Olist 主案例为参考，控制在 6–10 张表内：

- `dim_customers`、`dim_sellers`、`dim_products`、`dim_geography`；
- `fact_orders`、`fact_order_items`、`fact_payments`、`fact_reviews`；
- 可选日期维表与品类映射表。

`app` Schema 的第一批表：

- `users`、`roles`、`user_role_bindings`；
- `conversations`、`messages`；
- `metric_definitions`、`dataset_versions`；
- `query_audits`、`policy_decisions`；
- `evaluation_cases`、`evaluation_runs`、`evaluation_results`。

## 6. 技术路线与技术方案

| 层级 | v1 选择 | 选择原因 |
| --- | --- | --- |
| 后端语言 | Python 3.12 | 与 Vanna、数据处理、测试和类型生态匹配。 |
| API 与流式通信 | FastAPI + SSE | Python 生态成熟；Vanna 原生支持 FastAPI 路由和流式组件。 |
| Agent / Text-to-SQL | Vanna | Agent、工具注册、用户上下文、会话接口、图表与 Web Component 均已具备。 |
| 分析数据库 | PostgreSQL | 一种方言即可覆盖 v1；权限、超时、Schema 隔离和审计友好。 |
| ORM / 迁移 | SQLAlchemy 2.x + Alembic | 管理应用元数据与 Schema 演进。 |
| SQL 解析 | `sqlglot` | AST 级规则校验，避免脆弱的字符串过滤。 |
| 模型提供方 | OpenAI-compatible 配置接口 | 可按配置使用 OpenAI、DeepSeek、Qwen 等；审计模型和参数。 |
| 前端 | Vanna `<vanna-chat>` + 宿主页 HTML/CSS | Web Component 可嵌入任意已有网页，不建立独立前端工程。 |
| 宿主层 | 原生 HTML/CSS/浏览器事件 | 控制浮动入口、右侧面板、中文文案和业务页面上下文，降低上游组件修改成本。 |
| 聊天/富结果组件 | Vanna `<vanna-chat>` | 复用 SQL、表格、Plotly 图、SSE、最小化和最大化交互。 |
| 图表 | Plotly `graph_objects` + 服务端 ChartContract | 图表类型、字段、标题和当前查询工件均由服务器合同固定；不另引入图表前端框架。 |
| 测试 | pytest + API 集成测试；后续前端 E2E | 从 SQL 策略和评测集开始保证核心质量。 |
| 状态与队列 | v1 仅 PostgreSQL | 持久化状态是核心；Redis 只在确有缓存、限流或异步任务需求时引入。 |

### 6.1 Redis 决策记录

Vanna 不内置 Redis，Redis 也不是 Python 项目的必选组件。v1 使用 PostgreSQL 存储会话、审计、指标、数据集版本和评测结果；SSE 直接由 FastAPI 处理。只有在需要分布式限流、短期缓存、长报表异步导出或多实例任务进度时，再引入 `redis-py + arq`。Redis 不得作为审计或评测结果的唯一事实来源。

### 6.2 嵌入式前端决策记录

Vanna 自带 Lit Web Component，能够嵌入任意网页，并原生提供 SSE、表格、Plotly 图、
最小化和最大化交互。项目不再建设 TailAdmin 或独立 Next.js 页面；宿主页用于模拟真实
经营系统并承载浮动/右侧分析面板，查询历史、指标和评测优先由后端 API、审计数据和
静态证据页面呈现。只有当宿主层无法完成已验证需求时，才单独立项引入前端框架。

## 7. 数据集策略

| 定位 | 数据集 | 许可/来源 | 用法 |
| --- | --- | --- | --- |
| 开发与回归 | Chinook Database | MIT；<https://github.com/lerocha/chinook-database> | 小数据集，验证 SQL 策略、接口和回归测试。 |
| 主展示案例 | Olist Brazilian E-Commerce | Kaggle，CC BY-NC-SA 4.0；<https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce> | 多表经营分析主案例。 |
| 中文扩展案例 | 淘宝母婴购物数据 | Kaggle，CC BY-NC 4.0；<https://www.kaggle.com/datasets/akacoder404/taobao-maternal-and-infant-shopping-data-set> | 仅使用最小化的交易/商品类别字段，作为中文电商分析案例。 |
| 本地生活候选 | 天池 O2O 优惠券核销数据 | 具体条款待逐项核对 | 后续扩展，不在 v1 主线阻塞项目。 |

所有第三方数据遵循以下规则：原始大文件不提交 Git；仓库提交数据集清单、许可证/署名、下载/加载脚本、转换脚本、DDL、字段字典和小型合成 fixture。项目页面与 README 如实说明数据来源，不把境外数据包装为中国真实平台数据。

## 8. 评测与验收设计

评测集从第一份数据开始建设，初始目标为不少于 60 个可版本化用例：

| 类别 | 建议数量 | 验收关注点 |
| --- | ---: | --- |
| 单指标与筛选问题 | 20 | SQL 执行、筛选条件、时间范围和结果值。 |
| 多表关联与趋势问题 | 20 | Join 正确性、聚合、排序和图表适配。 |
| 指标口径/歧义问题 | 10 | 采用正确指标定义，或主动追问/明确假设。 |
| 安全与越权问题 | 10+ | 对 DDL/DML、多语句、越权对象、无界查询 100% 拦截。 |

v1 发布门槛：

- 所有安全攻击用例被策略层拦截，且拒绝原因可审计；
- 每个成功结果都返回 SQL、指标/来源和结果摘要；
- 核心业务问题集有可复现的基线结果和人工核验结论；
- 关键 API、SQL 策略和数据加载流程有自动化测试；
- 所有数据来源、版本、许可证和运行配置可追溯。

## 9. 分阶段实施路线

### Phase 0：立项与仓库基线（已完成）

- 冻结项目边界、架构、数据策略、风险和协作流程；
- 建立 GitHub 仓库、`PROJECT.md`、`AGENTS.md` 与飞书项目文档；
- 完成单仓库决策，Vanna 源码与项目文档位于同一仓库；未下载第三方业务数据。

完整的阶段门、仓库边界、交付物、退出条件、接口轮廓、测试策略与风险控制见 [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)。

### Phase 1：Vanna 原生垂直原型（已完成）

- 使用 Conda 环境 `data-analysis-agent` 运行 Vanna 2.0.2；
- 使用 SiliconFlow OpenAI-compatible API 和 `DeepSeek-V4-Flash`；
- 使用 SQLite 合成 fixture 验证 SQL 工具调用、SSE 和原生 Web UI；
- 以无框架宿主页嵌入 `<vanna-chat>`，验证窗口三态、移动端布局与 CSV 文件隔离；
- 记录 Vanna 当前能力、缺口和上游 API 事实，不修改 Vanna 核心。

退出条件：页面可访问，模型可实际调用 `run_sql`，结果表与中文结论在浏览器可见，且
不提交 `.env` 或生成的 SQLite 文件。以上条件已通过。

### Phase 2：数据与领域建模（已完成）

- 确定 Olist 主案例的表范围；
- 编写数据集清单、许可证/署名约束、分析 Schema、数据字典；
- 起草第一批指标定义、最小合成测试 fixture 和 20 条评测问题；
- 原始数据在仓库外完成 checksum 核验、确定性转换和 PostgreSQL 真实加载；固化核心指标
  golden 基线，仍不将草案口径描述为生产口径。

本阶段文档入口：[`data/manifest/datasets.yaml`](data/manifest/datasets.yaml)、
[`docs/data-dictionary.md`](docs/data-dictionary.md)、[`docs/metric-catalog.md`](docs/metric-catalog.md)、
[`docs/architecture/data-model.md`](docs/architecture/data-model.md) 和
[`evals/cases/draft.yaml`](evals/cases/draft.yaml)。

### 持续集成基线

GitHub Actions 的 `Project Quality Checks` 使用 Python 3.12，与项目运行时保持一致，
只运行不依赖外部模型、数据库或私有密钥的核心单元测试，并构建嵌入式 Web Component。
这条检查还会编译 SQLite/SiliconFlow 演示入口，以尽早发现语法错误。它不执行 Vanna
上游面向 Anthropic、OpenAI、Snowflake、Oracle、BigQuery、Qdrant、FAISS 等可选集成的
完整 `tox` 环境，也不执行需要真实 SiliconFlow 调用和浏览器的 E2E；后者必须在本机用
`RUN_VANNA_E2E=1` 显式触发，不能把开发密钥写入 GitHub Actions。

### Phase 3：可信查询后端（进行中）

- 创建 FastAPI 应用、认证/角色占位、PostgreSQL 双角色配置；
- 已接入 Vanna Agent 与受控工具，真实查询只能通过策略和 `daa_analytics_reader`；
- 已实现 `sqlglot` SQL Policy、超时/行数限制、持久审计、指标与数据版本证据；
- 已实现服务端 `ChartContract v2`：由用户意图、`QueryPlan` 和 `ResultContract` 固定 `bar`、`horizontal_bar`、`line`、`area`、`donut`、横轴、指标列、系列、标题与当前 SQL 结果工件；不支持的图形和缺少横轴的请求在 SQL 前终止，图表层不做二次聚合。
- 已提供演示级签名会话：固定 `analyst` / `admin` 身份映射同时约束 SSE、SQL 策略与审计 API，
  页面可切换并展示其用途和非生产边界；旧请求头不能提升权限。
- 已增加 PostgreSQL 会话/消息存储、Agent Run 台账、请求级预算和上下文裁剪基础；后端历史 API 已提供列表、详情和删除。
- 宿主页已接入历史列表、点击恢复、刷新恢复、新建会话、删除失败提示和角色切换隔离；历史恢复只回放安全文字，不伪造原始 SQL、图表或 DataFrame 结果。
- 已接入通用 `WorkspaceProfile` 边界：Catalog、Policy、PostgreSQL Runner 和预算处理器从工作区配置读取数据集、版本、Schema、角色和白名单；Olist 通过 `OLIST_WORKSPACE` 作为当前适配器。版本化、按角色裁剪的 `olist-catalog-v1` 请求提示使用 Catalog slice，而不是无条件注入完整 Schema；Catalog trace 写入 `app.agent_runs.catalog_trace`，并将服务器拥有的 `ResultContract`（指标列、时间别名、请求范围和版本）传入运行时 `ToolContext`。
- 已接入 `QuestionRouter`、`WorkingMemory` 和澄清边界：缺少时间/指标/比较基线时不调用 SQL；补充信息后从会话结构化状态恢复原指标，并写入 `app.conversations.working_memory`。
- 已把路由从“是否命中指标”扩展为证据路由：`help`、Catalog 定义、通用业务/知识、数据查询、数据分析、混合请求和结果追问分别标记 `intent`、`requires_database`、`evidence_mode` 与 `reason_code`；通用回答通过同一模型服务但显式不提供 SQL/图表工具，指标定义由 Catalog 直接回答。
- 已为多指标请求增加服务器生成的 `QueryPlan`：标记单指标、标量多指标概览和分组多指标形状，要求指标/维度/时间结果列，提示不同事实粒度先独立聚合再合法 Join；计划同时进入 Prompt、`ToolContext` 和 Agent Run trace。
- 已把通过 `ResultValidator` 的结果生成有界可信摘要，写入预算台账和会话 `WorkingMemory.previous_result_summary`；结果追问只可解释该摘要，未有可信摘要时先澄清，不把助手自然语言当作长期业务记忆。
- 已通过项目层 `TrustedRunSqlTool` 将一次受限 SQL 修复接入 Vanna 工具生命周期：原始 SQL 失败后只向修复模型提供脱敏错误和有界 Catalog，候选 SQL 必须再次通过 AST Policy，并由 reader role 重执行；成功结果还要经过 `ResultValidator`，第二次失败或合同失败直接可信拒答。原始/修复 SQL、错误类别、Policy 状态、执行状态、结果验证和终止原因写入 `repair_evidence`，同时进入 `app.query_audits`、`app.agent_runs` 和预算记录。结果校验继续覆盖空结果、缺列、时间越界、截断和 Join 放大。24 条真实 SiliconFlow 人工标签核验已完成，但尚未完成真实认证、组织行范围、第二个真实数据集和批量多次重复评测。
- 本轮补齐了四个运行链路缺口：SQL Policy 能识别多 CTE 输出列并避免把 scalar subquery 关联键误判为敏感投影；仅询问指标定义/统计口径的问题由 `QuestionRouter` 直接返回 Catalog Markdown，不调用 LLM、SQL 或工具；历史会话中的 assistant 文本与流式文本共用安全 Markdown renderer；宿主页用双 `requestAnimationFrame` 和 `ResizeObserver` 在最小化恢复、拖拽、缩放和窗口变化后重新同步聊天高度。`catalog_answered` 已加入数据库终止原因约束。
- 已为请求台账增加受限阶段耗时证据（`route_catalog`、`llm_request`、`sql_policy`、`postgres_sql`），写入现有 `agent_runs.catalog_trace.performance` JSONB，不扩大数据库表结构。真实 SiliconFlow 多指标请求已成功返回 GMV、有效订单数、平均履约天数和好评率；一次代表性请求约 77.7 秒，其中两轮模型调用约 77.2 秒，PostgreSQL 约 0.39 秒，说明当前主要瓶颈在在线模型响应而非数据库或策略层。该数字是单次观测，不代表 P95 或模型准确率。
- 已冻结 `evals/cases/text_to_sql_v2.yaml` 60 条版本化路由/QueryPlan golden，并新增 `scripts/run_text_to_sql_evaluation.py`；确定性离线结果为 **60/60 passed**。报告只保存结构化路由/计划字段和 case ID，`evals/reports/` 忽略，不含密钥、原始问题、模型回答或数据库结果。完整嵌入浏览器回归当前 **9 passed**，覆盖八方向缩放、最小化恢复和窄窗口长 Markdown。另已完成 24 条真实 SiliconFlow 小规模人工标签：24/24 有运行记录，路由 23 pass/1 fail、权限 24 pass、回答有据 17 pass/7 fail；明确记录 token 为数值或 unknown，不把未知 usage 记作零。
- 尚未完成真实认证、组织行范围、第二个真实数据集、批量多次 SiliconFlow 语义评测和模型调用的流式/缓存优化；下一步只针对本轮的币种、Catalog slice、支付归因和合同后多余 SQL 问题做固定回归。
- 2026-08-18 可靠性修复已完成：Catalog 维度检索使用可见 Join 图的 BFS 闭包，自动补齐多跳桥接表并在表/Join/Prompt 预算超限时 fail closed；MetricDefinition 支持 Catalog 声明的维度归因策略，支付方式等未冻结口径会在 SQL 前澄清；工作区 Catalog 可声明币种展示合同与趋势表述约束；ResultContract 通过后记录可信状态并抑制重复 `run_sql`，不消耗 SQL/工具预算且保留 `visualize_data`。Olist 仅更新适配器 YAML，不在运行时写 case 特判。专项 71 passed，v2 离线 golden 60/60；全量上游可选依赖测试仍不作为项目质量门。
- 2026-08-19 真实 PostgreSQL 合同链路集成测试已补齐：通过 `TrustedRunSqlTool`、`SecurePostgresRunner`、`ResultValidator` 和 `BudgetedToolRegistry` 执行真实有效订单聚合；首条 SQL 合同通过后，第二条 `run_sql` 被抑制且 SQL/tool 使用量仍为 1，查询审计仅保留一条 allowed 记录。`PostgresRunRecorder` 同时将 `result_contract_satisfied` 与 `extra_sql_suppressed` 写入现有 `catalog_trace` JSONB。项目 PostgreSQL 专项 **5 passed**（`RUN_PROJECT_DB=1`）。
- 2026-08-19 SSE/Agent 边界回归已补齐：固定 LLM 在同一 `BudgetedChatHandler` 请求中先返回有效 SQL、再返回冗余 SQL；真实 PostgreSQL 首次执行与结果合同通过后，`BudgetSafetyMiddleware` 在模型响应边界移除第二个工具调用，Agent Run 仅记录 1 次 SQL/工具调用和 1 条 allowed 审计，`catalog_trace` 持久化合同与抑制证据。该测试不调用 SiliconFlow；项目专项加 PostgreSQL 链路 **75 passed**，v2 离线 golden **60/60**。
- 2026-08-19 LLM 可观测性与定向在线复测：`ObservedLlmService` 增加 OpenAI/httpx/asyncio 超时归一、单轮安全结束和有界 provider usage/耗时观测；Vanna OpenAI-compatible 同步请求移入工作线程，避免真实 SiliconFlow 阻塞 FastAPI 事件循环。新增 6 条显式 opt-in 定向清单与评测器门槛测试；真实 SSE 结果为 6/6 Agent Run、0 客户端错误、路由 6/6、5 条查库请求各 1 条 SQL、SQL 可执行 5/5、结果合同 5/5、权限 6/6，5 条 usage 均 reported，总客户端耗时 499,174 ms。人工语义/最终表述 3 条 pending、1 条趋势表述 fail；不据此宣称在线准确率或 P95。`PostgresRunRecorder` 已验证观测写入既有 `catalog_trace` JSONB，数据库 schema 不变。
- 2026-08-19 可信结果确定性收口：对未显式要求图表的 SQL 请求，`ResultValidator` 通过后由服务器基于结果合同输出行数、指标列和校验状态，跳过最后一轮自由模型总结，防止把波动误述为持续趋势、擅自换算币种或补造因果。图表请求保留模型/`visualize_data` 路径。真实月度 GMV 回归记录 `deterministic_result_finalized=true`、1 条实际 PostgreSQL 执行、2 次模型调用、68,516 ms；其中第一次模型 SQL 因敏感 `order_id` 投影被 Policy 拒绝，第二次有效，不存在最终总结模型调用。专项单测 25 passed（1 skipped）、真实 PostgreSQL 10 passed、v2 golden 60/60。
- 2026-08-19 业务质量评测：新增 20 条真实业务请求与 5 条显式图表意图的在线清单；真实 SSE 为 20/20 Agent Run、11 条允许 SQL/结果合同、20/20 权限合规。人工复核得到指标语义 11 pass / 2 fail / 7 N/A、回答有据 11 pass / 9 fail；失败暴露未冻结的一对多支付归因，以及评价行指标跨商品行 Join 后的粒度丢失。原批次 3/5 发出图表组件、2/5 SQL 前超时；真实 Playwright 页面确认 SVG 和自适应布局可用，但折线图请求被生成成柱状图。下一轮应先把归因、度量粒度和图表类型收敛为服务端合同，再处理模型 120 秒超时与吞吐优化。
- 2026-08-19 可信结果呈现与归因边界收敛：确定性收口不再只显示行数、列名和指标 ID。服务端仅从已通过 `ResultValidator` 的有界 DataFrame 摘要中渲染中文结果概览、最多三条样例行和完整表格提示，不调用模型作趋势、排名、币种或因果推断；Catalog 提供字段展示名，SQL 别名仅大小写/下划线差异时仍可识别。`GMV × payment_type`、`average_delivery_days × product_category_name`、`positive_review_rate × product_category_name` 的一对多归因歧义均由 `dimension_policies` 在 SQL 前阻断，避免模型选择首笔支付或通过商品行放大订单/评价。当前工作区尚未配置这些归属/分摊规则，因此路由明确提示应改查无歧义维度或由管理员配置 Catalog，不假称用户补一句自然语言就可安全执行。该机制由每个工作区 Catalog 声明，不依赖 Olist case ID。
- 2026-08-24 可信图表、通用归因治理与后训练数据边界：新增服务器拥有的 `ChartContract`，仅在合同有效时允许图表工具继续运行；服务器直接构造 bar/line 图表，忽略模型提供的类型、标题和文件名，且拒绝额外列、缺失/非数值字段、重复横轴、无可解析时间轴和图表层二次聚合。Catalog 的维度策略升级为 `safe_direct`、`requires_attribution`、`server_owned_rule` 三态；后者只有对应归因规则被服务器真实注册时才可放行，当前 Olist 未注册归因 SQL compiler，继续 fail closed。新增后训练数据协议和 v2 golden holdout 清单，60 条确定性用例永久隔离，训练不能替代 AST、PostgreSQL role、结果/图表合同或服务器归因规则。本轮确定性专项 **96 passed**、v2 golden **60/60 passed**，并通过 `ruff`、`compileall` 与 diff 检查；不调用 SiliconFlow，不下载模型、不创建训练环境、不执行微调。
- 2026-08-24 数据获取替代方案与后训练审计模板：确认官方 Spider Test Suite 数据库的 Google Drive 在服务器上不可达；通过 `HF_ENDPOINT=https://hf-mirror.com` 只读核验了 `lamini/spider_text_to_sql`（7,000 train/1,034 validation）、`VictorDCh/spider-clean-text-to-sql`（含 train/dev/test parquet）和 `Glide-py/spider-text-to-sql`（MIT metadata、带本地 archive 恢复脚本）。这些是 Spider 派生训练/诊断资产，不是已验证的完整 Test Suite release，不能生成官方分数。新增 [`docs/text-to-sql-data-acquisition.md`](docs/text-to-sql-data-acquisition.md)，规定本地下载后上传、SHA-256、解压树哈希、release/许可/兼容性核验流程；新增 [`evals/manifests/post_training_candidates_v1.yaml`](evals/manifests/post_training_candidates_v1.yaml)、候选审计说明和自动化模板闸门测试，当前仍为 `template_only`、零候选、永久排除 v2 holdout。
- 2026-08-24 微调学习路线：新增 [`docs/post-training-learning-roadmap.md`](docs/post-training-learning-roadmap.md)，将 Transformer、SFT、LoRA/QLoRA、Text-to-SQL 评测、单卡工程和发布安全拆为阶段，并为每阶段列出原理与实习面试高频考点。正式训练前仍只做 tokenizer/1 batch forward smoke；每次实验需同步记录假设、配置、GPU 映射、数据哈希、结果、失败排查和面试表达。
- 2026-08-25 官方 Spider 数据包获取与版本隔离：通过用户本地 VPN 的临时代理直接下载官方 `spider_data.zip`，`unzip -tq`、路径安全、核心 JSON 哈希、166 个 SQLite 文件树和 20 个开发数据库均已验证，原始资产与 acquisition manifest 均留在仓库外。官方 `dev.json` 与已完成实验使用的 2020-01 Kaggle 镜像有 101 条有序记录差异，故旧 Base/Adapter prediction、SQLite diagnostic 和 Test Suite 输出仍只是历史内部对照，不能迁移为官方版本结果。下载的 Test Suite 资产与官方 dev 的 20 个库逐表/列/类型为零 mismatch；仍未对官方包运行新的全覆盖生成/评测，也未发布官方分数。详见 [`docs/spider-1.0-data-provenance.md`](docs/spider-1.0-data-provenance.md)。
- 2026-08-24 官方 Spider Test Suite 资产获取：用户本地 VPN 通过 SSH 反向代理提供临时 HTTP 出口，服务器成功下载官方链接 `testsuitedatabases.zip`（1,269,456,098 bytes，SHA-256 `9ec24ea8debc6bd04abfe137b5f1a739b5a8836f32c0464e4dfc94eb7f41da96`）并通过 `unzip -t` 校验；解压到仓库外后得到 3,194 个 SQLite 文件/28 个任务目录，SQLite tree SHA-256 为 `c9529ce837eeb68a7eb98af9dfa1caf721ff566ebb871835a9910e96b3d963bd`。当前 Spider dev 的 20 个数据库均存在，与现有 2020-01 镜像逐表/列/类型比较无差异。资产清单在外部 `acquisition-manifest.json`；独立条款与官方 release 可比性仍未证明，故尚未运行或发布 Test Suite Accuracy。
- 2026-08-24 Spider Test Suite 内部参考基线：新增 `scripts/start_official_spider_test_suite_screen.sh`，固定 Python/evaluator/data/prediction 路径并把日志与全部评测产物放在仓库外；首次运行暴露冻结预测含多行 SQL，bridge 已通用折叠格式空白并保留引号内容，含行注释则安全拒绝。补齐外部 evaluator 的 `nltk==3.8.1` 与现有 `punkt` 资源，设置 `PYTHONDONTWRITEBYTECODE=1` 保持 pinned checkout clean。`screen` 会话 `daa-spider-test-suite-v3` 完成 1,034/1,034 cases，官方 evaluator 返回 0；内部 execution diagnostic 为 easy `0.820`、medium `0.620`、hard `0.437`、extra `0.300`、all `0.585`。该结果只作为当前 2020-01 Kaggle mirror + Test Suite 资产的内部参考，不宣称 current official leaderboard score。专项 bridge 测试 **9 passed**，tokenizer smoke、ruff、compileall、diff check 通过。

### Phase 4：嵌入式交互与证据呈现（基础能力已完成）

- 提供可嵌入既有网页的宿主页示例，控制 Vanna Web Component 的浮动/右侧面板状态；
- 打通 SSE、表格、SQL、图表、指标证据和角色化展示；
- 已完成历史会话列表、恢复、刷新恢复、新建会话和受控删除交互；完整嵌入浏览器回归当前 9 条通过；
- 不创建独立 Next.js/TailAdmin 应用。

### Phase 4.5：平台基础收敛（进行中）

- 已完成 PostgreSQL 会话/消息存储、Agent Run 台账、请求级工具/SQL/图表/输入/上下文/输出预算；
- 已完成 starter 空会话生命周期修复，避免页面刷新制造零消息历史记录；
- Text-to-SQL 第二轮运行时合同已完成：WorkspaceProfile、Catalog/路由/working memory/结果合同、TrustedRunSqlTool 一次修复、repair evidence 和可信拒答均已落地；浏览器多轮澄清、八方向缩放和长历史 Markdown 回归已补齐。
- 已完成后训练准备第一阶段：`ChartContract` 与归因三态接入运行时，`QueryPlan`/`ResultContract`/审计记录携带归因需求证据；`docs/post-training-data-protocol.md` 固化样本字段、来源、脱敏、评测和训练边界，`evals/manifests/post_training_holdout_v1.yaml` 将 v2 60 条 golden 永久隔离。训练候选采集、schema-disjoint train/validation 切分、tokenizer/forward smoke 和极小 SFT 工程 smoke 均在独立外部实验目录完成。
- 2026-09-04 已冻结小型静态 Olist QuerySpec coverage seed 清单：`data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl` 含 15 条仅结构化记录，覆盖十项业务指标、`scalar`/客户州/品类/时间序列四种当前允许形状和 train/validation/in-domain-test 三类 split。它不是自然语言训练集，不含问题、Prompt、SQL、结果、原始 Olist 数据或 protected holdout；本轮未调用 materializer、未生成 Gold、未执行数据库、未启动 token/GPU。由于当前安全规则禁止同一个 Join program 跨 split，而可用 program 只有 11 个，该清单只能作为结构合同与隔离边界的审阅样本，不能用于最终模型质量结论。受限 protected-family summary 导出器已随后冻结；真实 summary 导出并获批准后，下一项才是小批 Gold 准入/结构物化。
- 2026-09-04 已实现仓库外 protected-family summary 受限导出器：它不读取 holdout 文件，也没有问题/SQL/结果/数据库路径参数；只转换隔离人工批准的、排序去重 `family_id` 清单为不可逆 SHA-256 fingerprint summary，并额外保存输入/输出 hash、WorkspacePin、source-manifest hash、family 数和 review reference evidence。真实 protected summary 尚未导出，因而仍禁止调用 materializer；下一步只能单独设计小批 Gold 准入，让 materializer 同时绑定真实 summary/evidence 后物化结构产物并逐层执行/合同/人工审核。
- 2026-09-04 已完成 protected-family summary 与小批 Gold 准入闭环：受限审阅将当前 QuerySpec 可表达的 protected 数据查询映射为 17 个外部 family，summary/evidence 均只存仓库外。物化器新增 evidence 强制绑定，验证 summary hash、family 数、版本和 WorkspacePin；并将多指标 family identity 规范为无序集合，防止只改列顺序绕过碰撞。选取 6 条无碰撞 seed 外部物化后，全部通过 SqlPolicy、`daa_analytics_reader`、QuerySpec 派生 ResultValidator 合同和 DeepSeek-V4-Flash advisory-only 语义审阅（6 admitted、0 rejected、0 needs-human-review）。DeepSeek 的 pass 不是人工签字；扩大语料前仍需抽样人工口径与粒度审核。这不是训练行、训练集或模型质量结论；下一项仅为该批构造真实运行时 QueryPlan/Prompt 和人工审阅的中文业务 query 变体。
- 2026-09-02 Olist 领域 Candidate SQL SFT 数据合同已冻结：训练输入必须复用 `olist-candidate-sql-v1` 的真实 `Semantic Catalog + QueryPlan + ResultContract + PostgreSQL` Prompt，目标仅为 canonical SQL 加 EOS；业务指标 ID 不得被当作物理列。合同固定 QuerySpec 驱动 Gold、语义程序族 split、60 条 protected holdout 隔离、`1536` 无截断长度门、Policy/reader-role/ResultContract/人工语义质量门与停止条件。尚未盘点 coverage、实现 renderer、物化数据、加载 tokenizer 或训练；当前下一项只能是可训练指标/维度/Join 覆盖矩阵。
- 2026-09-04 Olist Pilot v1 约 40 个 QuerySpec family 扩展：新增 `data/fixtures/olist_queryspec_coverage_seeds_v2.jsonl`，冻结 40 个结构化 family（train 24、validation 8、in_domain_test 8），覆盖十项指标、四种结果形态、单/多指标、客户州/商品品类及购买/评价时间序列。`materialize_olist_queryspecs.py` 的 `family_scoped_v2` 允许共享底层 Join program 跨 split，但要求 family、QuerySpec 和规范化 SQL hash 隔离并记录 overlap；两个客户州商品行指标的 Join program 错配已修正。外部物化结果为 40 accepted、0 rejected、0 protected collision，尚无自然语言问题、Prompt、SQL 执行或 GPU。该产物仍是结构化中间层，不是最终 SFT 数据集；下一项只做分批 runtime Prompt/中文问题准入和人工口径审核，之后才进行 tokenizer 长度审计与正式 train/validation/test 构造。
- 2026-09-04 Olist Pilot v1 第一批 Gold 准入：`admit_olist_gold_batch.py` 现支持重复 `--seed-id`，先校验完整外部 materialization 的 `status=pass`、hash、Gold/QuerySpec seed 集合和 artifact，再按源顺序选择 1--6 条执行；未知/重复/超限选择、Gold seed 重复、集合不一致或非 pass 源审计均 fail closed。实际从 40 条结构源选择一批 6 条，覆盖 train/validation/in_domain_test、标量/州/品类/购买及评价序列和多指标，结果为 `6 admitted / 0 rejected / 0 needs_human_review`，均通过 Policy、reader role、ResultValidator 与 DeepSeek advisory review。详细证据保留仓库外；这不代表剩余 34 条、最终 SFT 数据集或模型质量已通过。下一项仍为下一小批 Gold 准入，完成足够覆盖后才创建人工审核中文问题和 runtime Prompt。
- 2026-09-02 Olist 领域 Candidate SQL SFT v1 coverage 已冻结：仅使用当前 Catalog 的 `gmv`、`paid_order_count`、`average_delivery_days`、`positive_review_rate`。纳入标量、确认时窗、各指标自身时间字段的时间序列、按客户州/城市、GMV 按品类/卖家，以及受控的多指标标量/按州/同购买时间序列 CTE 形状；排除支付方式归因、履约/好评按品类、订单/评价事实按卖家、Top-N、自由筛选、相对时间、比较/因果和非 SQL 路由。设计目标为 `3,000/460/460` train/validation/test 行和至少 162 个 semantic families，不表示数据已构造。尚未实现 QuerySpec、renderer、split audit、长度审计或训练；当前下一项只能设计 QuerySpec/Gold renderer 责任边界。
- 2026-09-02 十项 Olist 指标 Catalog 已冻结：运行时升级为 `olist-catalog-v2` / `metric_version=0.2-frozen`，在原四项上增加商品件数、平均订单商品金额、平均评价分、准时送达率、取消率和运费金额。每项均固定粒度、时间字段、默认过滤、可安全直接汇总的维度与需澄清归属；其中商品/订单/评价粒度和金额/比率分母不得混用。读库聚合、单元测试和十指标 Golden 已验证当前快照，不表示在线模型或领域 Adapter 已具备这些 SQL 能力。旧四指标 coverage 不再可直接物化；下一项仅修订十指标 coverage matrix，之后才可设计 QuerySpec/renderer。详见 [`docs/metric-contracts/olist-metrics-v2.md`](docs/metric-contracts/olist-metrics-v2.md)。
- 2026-09-02 十指标 Olist 领域 SFT coverage 已冻结：新增 `olist-domain-sft-coverage-matrix-v2.md`，保留四指标 v1 为历史快照。所有十项均可覆盖标量、确认绝对时窗、各自时间字段的序列和客户州；客户城市的关联粒度安全，但高基数输出的排序/截断尚未进入真实 Prompt，因此暂缓。只有 `gmv`、`item_count`、`freight_amount` 可天然按商品品类聚合；卖家维度因 `seller_id` 是 analyst 的敏感结果/分组列而明确排除。订单/评价事实在品类、卖家或支付方式下的归属/分摊仍 fail closed；跨购买/评价时间字段的时间序列、Top-N、自由筛选、相对时间、比较/因果和非 SQL 路由仍排除。物化前最多规划 `4,460/665/665` train/validation/in-domain test 行、330 个 family，不表示样本已构造或足以证明模型效果。QuerySpec/renderer 设计冻结后，下一项只能实现并审阅该确定性组件与单元测试。
- 2026-08-26 官方 Spider release rebaseline 已完成：评测启动脚本使用显式 `DATASET_VERSION`/`MODEL_ID`，避免新旧数据集混淆；官方包 2-case Base smoke、完整 Base/Adapter 生成、只读 SQLite diagnostics、pinned Test Suite 和安全 paired analysis 均通过。Base 使用逻辑 CUDA 1 -> 物理 GPU 3，Adapter 使用逻辑 CUDA 0 -> 物理 GPU 2，均为 RTX 4090；两者均覆盖 1,034/1,034。Adapter 相对 Base 的 SQLite executed 为 `829 -> 671`，Test Suite internal all 为 `0.433 -> 0.376`，限定列错误为 `15 -> 296`，因此本质量门失败。下一步是官方 train-only 数据扩展和 fully-qualified Schema prompt v2，暂不进入 DPO/GRPO 或生产接入；详见 [`docs/post-training-official-base-adapter-analysis-v1.md`](docs/post-training-official-base-adapter-analysis-v1.md)。
- 2026-08-26 Spider SFT v2 独立复验已完成：3,048 条 train / 552 条 validation 的 2 epoch bf16 LoRA 在官方 dev 的前缀 100 条上出现 SQLite `94 -> 89` 与 bounded denotation `56 -> 69` 的冲突，因此不以该前缀放行。改用固定 seed、排除前缀及其全部 schema 的 164 条/17 个未观察 schema 复验；模型输入不含 dev gold SQL，gold 仅在两个候选输出冻结后用于仓库外 bounded audit。Base/Adapter 的 denotation 为 `97 -> 122`、SQLite executed 为 `153 -> 155`、`no_such_column` 为 `9 -> 8`；预冻结主判据和两项护栏均通过，允许进入完整 1,034-case 对照。该结果不是官方 Spider/Test Suite 分数、业务语义准确率或生产接入依据；后续候选仍须经过既有 Vanna/SQL Policy/PostgreSQL reader role/ResultValidator/ChartContract。详见 [`docs/post-training-spider-sft-v2-plan.md`](docs/post-training-spider-sft-v2-plan.md) 与 `evals/manifests/post_training_spider_independent_smoke_v1.yaml`。
- 已完成后训练候选第一版：新增 `scripts/build_spider_sft_candidates.py`，仅读取 Spider `train_spider.json`、`tables.json` 和 SQLite schema，按 `db_id + SQL shape` 固定轮询抽取 128 条；128/128 通过只读 `EXPLAIN QUERY PLAN`，holdout 碰撞为 0。候选 JSONL、训练文本和审计报告均写入仓库外实验目录，SHA-256 与边界记录在 [`evals/manifests/post_training_candidates_v1.yaml`](evals/manifests/post_training_candidates_v1.yaml)。这只是 forward/SFT smoke 的候选，不等于模型已经微调或指标已经提升。教学材料见 [`docs/post-training-learning-notes-v1.md`](docs/post-training-learning-notes-v1.md)。
- 已完成 QLoRA 前向 smoke：通过 `HF_ENDPOINT=https://hf-mirror.com` 下载并固定 Apache-2.0 `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67...` 到仓库外；以逻辑 CUDA 1/物理 GPU 3 的空闲 4090 在 4-bit NF4 + double quant + bf16 下加载，注入 `r=16` LoRA 后仅 2.035625% 参数可训练。一个 train-only 样本的 125 prompt token 全部 mask、47 SQL/EOS token 被监督，172 token 单 batch 得到有限 loss `0.884486`、峰值 allocated 显存约 1.94 GiB；未运行 backward、optimizer 或保存 adapter。详见 [`evals/manifests/post_training_forward_smoke_v1.yaml`](evals/manifests/post_training_forward_smoke_v1.yaml)，下一步才是按组切分的极小 SFT smoke。
- 已完成第一轮 QLoRA SFT 工程 smoke：128 条 Spider train-only 候选按 `db_id` 进行 schema-disjoint 切分，102 条训练/26 条验证来自 66/19 个不重叠 Schema，SQL shape 交集和 v2 holdout 碰撞均为 0。以 Qwen 1.5B 4-bit NF4、`r=16` LoRA、`batch=1`、accumulation=4、8 个 step 在逻辑 CUDA 1/物理 GPU 3 上训练，得到 train/eval loss `0.556203/0.466989`，峰值 allocated 显存约 3.31 GiB；仅保存 74 MB adapter 与外部可恢复 checkpoint，并完成 fresh PEFT adapter reload 的 validation 前向。该次 smoke 证明训练工程可用，不构成 SQL 准确率或业务语义提升结论。详见 [`evals/manifests/post_training_sft_smoke_v1.yaml`](evals/manifests/post_training_sft_smoke_v1.yaml)。
- 已完成 Qwen 1.5B base vs adapter 评测协议与双条 smoke：训练构造和推理共用 `spider-sft-schema-question-sql-v1` schema/question 序列化，生成侧只读 Spider dev 的 `db_id/question` 而不读其 gold SQL；base 与 adapter 固定为同一 revision、4-bit NF4、贪心解码、输入顺序、SQLite Adapter 和 Test Suite bridge。两条 smoke 验证 base/adapter 都能生成外部候选并进入相同诊断，且 adapter 仅为加载 74 MB LoRA 的变量；全量任务由单个 `screen` 顺序驱动，base 成功后才运行 adapter。它们不是质量结论，完整协议见 [`docs/post-training-base-adapter-evaluation.md`](docs/post-training-base-adapter-evaluation.md)。
- 2026-08-25 Base vs Adapter 全量评测容错与恢复：Base 已完成 1,034/1,034 外部候选生成（128,957 token、4,631,685 ms，预测 SHA-256 `8978b8...29cc0e`），SQLite 诊断因一条未闭合单引号 SQL 触发 `sqlglot.TokenError` 而中断；没有丢失预测，也未启动 adapter。离线 `SqliteBenchmarkPolicy` 现统一把 `SqlglotError`（含 parser 与 tokenizer 异常）映射为 `policy_rejected`，批处理继续执行后续 case。新增 two-case 回归验证坏 SQL 被报告、合法后续 SQL 正常执行；顺序 driver 增加显式 `BASE_REUSE_GENERATION=1`，只在已有 `generation_evidence.json` 与完整预测时复用 base 生成，且已有官方评测证据时拒绝恢复。SQLite/bridge 相关 **26 passed**，ruff、compileall、shell 语法和 diff 检查通过。该修复不改变生产 Vanna/PostgreSQL，不构成 base/adapter 质量结论；下一步是复用 base 预测完成诊断/Test Suite，再在空闲的映射 4090 上运行 adapter。
- 2026-08-25 Base 原始完成文本规范化：恢复官方 bridge 时发现 Base 的原始 completion 常在正确或错误 SQL 后继续生成 `### Answer`/Markdown 表格；旧 bridge 将表格分隔线中的 `--` 保守视为 SQL 行注释并拒绝整批。当时新增共享展示包装清洗器，原始 JSONL 不改写，只在 SQLite 诊断与官方 bridge 前一致移除代码围栏、`SQLQuery:`/`SQL:` 前缀及答案/解释 Markdown heading 后的展示尾部；不修 SQL、Schema、Join、聚合或读取 gold。该历史模块后来已合并并删除，唯一入口为 `candidate_sql_generator.unwrap_sql_completion()`；真实 SQL `--` 注释在单行 bridge 中转为等价块注释再折叠。Base 预检为 1,034 条中 806 条发生仅展示包装移除；专项 **32 passed, 1 skipped**，下一步将以相同规范化重跑 Base 诊断/bridge，保证与未来 Adapter 的比较公平。
- 2026-08-25 Qwen 1.5B Base 完整评测证据：在固定共享规范化合同下，Base 原始 1,034 条预测不改写，SQLite diagnostic 为 **831 executed、4 policy rejected、199 execution error、0 timeout**（报告 SHA-256 `433cf3...15d071b`）；此前未规范化报告留在仓库外审计，不参与 Base/Adapter 比较。未修改 Test Suite evaluator 完整运行 1,034 条，原始内部输出为 easy/medium/hard/extra/all = `0.748/0.448/0.241/0.094/0.427`，输入/输出 hash 已进入 manifest。`831/1034` 是单库受控可执行性，`0.427` 是固定资产组合上的 Test Suite 行为对照，二者不能互换；后者不是当前官方 Spider leaderboard。Adapter 已在同一逻辑 CUDA 1/物理 GPU 3 的 4090 上按同一 prompt、decode、normalizer 和 evaluator 顺序生成，未完成前不宣称 QLoRA 提升。
- 已完成后训练依赖环境 bootstrap 与首个基座冻结：独立 Conda 前缀 `data-analysis-agent-qlora` 使用 Python 3.11，冻结 CUDA 12.1 的 `torch==2.5.1+cu121`、accelerate、bitsandbytes、transformers、PEFT、TRL、datasets 以及训练专属 pytest/pytest-asyncio 的直接/传递依赖。宿主 535.54.03 驱动只支持 CUDA 12.2，故选择 CUDA 12.1 wheel 而非需更高驱动的 CUDA 12.4；`pip check` 通过。`CUDA_VISIBLE_DEVICES=1` 的最小探针只见进程内 `cuda:0`，其 UUID 确认为物理 GPU 3/RTX 4090。Qwen 1.5B 已下载并完成 forward-only smoke 与一次 8-step QLoRA SFT 工程 smoke；DPO/GRPO 尚未启动，v2 60 条 holdout 继续永久隔离。详见 [`docs/qlora-environment.md`](docs/qlora-environment.md) 和 [`evals/manifests/qlora_environment_v1.yaml`](evals/manifests/qlora_environment_v1.yaml)。
- 已完成后训练研究的 SQLite benchmark Adapter 基础设施：离线 `ReadOnlySqliteExecutor` 使用 SQLite 方言 AST、只读 URI、SQLite authorizer、超时和行数上限执行外部候选 SQL；`scripts/run_sqlite_benchmark.py` 仅消费 BIRD/Spider 原生 `dev.json` 和外部预测 JSONL，输出版本化模型/Prompt/候选/执行证据报告。它不接入 Vanna/前端/生产 PostgreSQL，也不自行计算官方 EX；BIRD/Spider 原始数据、模型预测和报告都继续留在仓库外或忽略目录，许可与 checksum 在下载前再冻结。详情见 [`docs/sqlite-benchmark-adapter.md`](docs/sqlite-benchmark-adapter.md)。
- 已完成冻结小模型基线的生成侧与合成端到端烟测：`frozen_sqlite_baseline.py` 只在内存中读取原生问题与 SQLite DDL，以无工具、固定解码的本地 Ollama 调用生成一条 SQL 候选，并且只向仓库外 JSONL 写入 Adapter 合同字段；脚本拒绝向 Git 工作树写入预测，Schema 超预算 fail closed，模型 SQL 不在生成侧执行或修复。当前冻结目标为 `qwen2.5-coder:3b` 的本地 Q4_K_M 量化分发，manifest/blob digest、Qwen Research License 与 Ollama server/CLI 版本均已记录。真实模型在合成 SQLite fixture 上生成后经 Adapter 得到 1/1 `executed`；随后原生 Spider 镜像的许可、哈希和布局门槛、20 条 smoke 与完整 1,034 条冻结生成均已完成。该基线结束时 QLoRA/SFT 尚未启动；当前 SFT smoke 状态以本节后续记录为准，详情见 [`docs/frozen-sqlite-baseline.md`](docs/frozen-sqlite-baseline.md)。
- Spider 离线研究数据的许可与可复现性门槛已完成：官方任务页直接给出 CC BY-SA 4.0 数据许可；因其 Google Drive 源在本机网络不可达，实际下载固定为指向官方来源的 Kaggle v1 镜像（2020-01，早于官方 2020-08 修订，不能用于榜单可比结论）。归档、`dev.json`、`tables.json`、全 SQLite tree 和开发库 tree 的 SHA-256 已写入 manifest；原生布局验证为 1,034 dev cases / 20 dev databases / 166 SQLite files。数据、gold SQL、预测和报告均在仓库外，首个 20 条冻结生成与 SQLite 诊断已完成。详见 [`docs/spider-1.0-data-provenance.md`](docs/spider-1.0-data-provenance.md)。
- 已完成首个原生 Spider 20 条冻结小模型 smoke：`qwen2.5-coder:3b` 在不读取 gold SQL/答案/行数据且不改变生产链路的条件下生成 20 条外部候选，SQLite Adapter 对其中 15 条执行成功、5 条为 `no_such_column` 执行错误，零策略拒绝、零超时。20 条以外的 1,014 dev case 是刻意未生成的 `missing_prediction`，不能将 `15/20` 或 `15/1,034` 说成 EX/EM/Test Suite Accuracy；该批只证明候选生成和受控执行链路可跑通，并得到 schema-linking 错误基线。模型生成耗时总计 13,123 ms，所有输出 SHA-256、设备实际放置和限制记录在 [`docs/frozen-sqlite-baseline.md`](docs/frozen-sqlite-baseline.md)。
- 冻结生成器已改为按成功 case 逐条 `fsync` 到仓库外 JSONL，再请求下一条模型响应；若后续超时或服务失败，已完成前缀保留，`--resume` 只补齐缺失的 primary case，不会把失败静默跳过或改变原生顺序。全量前只读 DDL 预检覆盖 20 个 Spider dev 数据库，schema 最大 3,012 字符、零个超过 20,000 字符服务器预算，因此可启动可恢复的 1,034 条完整批次；预检不读取表行、gold SQL 或答案。
- 已完成完整 Spider dev 冻结小模型基线：以 `CUDA_VISIBLE_DEVICES=1` 的临时项目专用 Ollama 服务（逻辑 CUDA 1、物理 GPU 3/RTX 4090）生成全部 **1,034/1,034** 主候选，预测 JSONL 有序、唯一且完整；服务随后关闭并释放显存。受限 SQLite Adapter 为 **884 executed、150 execution_error、0 policy_rejected、0 timeout**，错误主要是 138 个 `no_such_column`。生成耗时 466,667 ms、32,662 token；预测和诊断 SHA-256 均记录在 [`docs/frozen-sqlite-baseline.md`](docs/frozen-sqlite-baseline.md)。这些只是无 gold 的本地可执行性/Schema-linking 基线，不是 EX、EM、Test Suite Accuracy 或语义正确率；当前 2020-01 镜像和未核验 Test Suite 数据资产仍禁止生成官方分数。
- 已完成官方 Spider Test Suite 的代码边界与防误报桥接：固定官方 `taoyds/test-suite-sql-eval` commit `e97acc5...3a876c` 并要求干净 checkout；外部 bridge 只有在 1,034 条用例均有唯一 primary 预测时才会写 gold/pred 文件并调用未修改的官方 `evaluation.py`。这是为阻断上游 `zip(predictions, gold)` 对短预测静默缩短分母的风险；当前 20 条 smoke 已验证被提前拒绝。官方 Test Suite 数据库是独立 Google Drive 资产，其条款、release/hash 与当前 2020-01 Spider 镜像的兼容性尚未核实，因此未下载、未运行、未产生官方分数。详见 [`docs/official-spider-test-suite.md`](docs/official-spider-test-suite.md)。
- 本机 GPU 编号映射已冻结为：`CUDA_VISIBLE_DEVICES=0/1/2/3` 分别对应 `nvidia-smi` 物理 GPU `2/3/0/1`，即两张 4090 为逻辑 `0/1`、两张 3090 为逻辑 `2/3`。后续任何本地推理、评测和训练均须在启动前检查占用，并记录逻辑与物理编号；进程内部 `cuda:0` 不是物理 `nvidia-smi` 卡号。完整规则见 [`AGENTS.md`](AGENTS.md)。

### Phase 5：评测、加固与作品集

- 建立评测集、回归测试和安全测试；
- 完成部署说明、演示脚本、架构图、数据署名和项目 README；
- 形成可量化且可诚实写入简历的项目成果。

首屏 Demo 问题必须额外维护为独立的版本化场景契约，不能只保存在前端按钮配置中。每个
场景至少记录固定问题、允许角色、指标口径、必要来源表、排序/行数语义、是否应有图表、
结果证据和数据库 golden 查询。该资产用于回归 SQL 语义与演示展示链路；在线 LLM 只有在
另行记录模型、提示、运行结果和人工判定后，才能据此讨论语义表现。

第一轮验收矩阵见 [`docs/first-round-acceptance.md`](docs/first-round-acceptance.md)。

## 10. 当前决策与待确认项

已确认：单仓库 Vanna-first、Python/Conda、Vanna 原生 Web Component、SiliconFlow
开发模型、SQLite 合成冒烟 fixture、Olist 主展示案例草案、后续再引入 PostgreSQL 和
v1 不引入 Redis。

已确认数据加载方式、PostgreSQL 双角色和首批核心指标 golden 结果。Olist 仍是当前产品展示数据集，
通用链路通过 `WorkspaceProfile` 组织；TheLook 已作为独立、只读、跨 Schema 的后置评测 workspace
完成 v3 受保护 release。待确认真实认证方式、组织/行级权限的演示粒度、评测题标准答案与图表受控
生成方案。独立 Next.js/TailAdmin 外壳不再作为候选默认方案。

## 11. 变更记录

### 2026-09-21：M1 宿主上下文合同与文档入口治理完成

新增 `CopilotBootstrap`、`AnalysisSurfaceContext`、`ConversationScope` 及服务器拥有的
`WorkspaceAccessRegistry`。同源 `POST /api/copilot/bootstrap` 只接受受限 `surface_id`、请求的
workspace 和 opaque entity refs；用户、角色、workspace membership、dataset/metric/catalog/policy
版本均从签名 demo session 与 registry 推导。请求体中的 `user_id`、role、SQL、DOM 等额外字段被
Pydantic 拒绝，未授权 TheLook workspace 返回 403；TheLook 未接入默认在线产品。该任务没有改变
Router、QueryPlan、SQL Policy、reader role、ResultValidator、聊天流程或模型 provider。运行时 Conda
环境回归为 **20 passed**，其中包含真实 FastAPI route 测试。M2 才会将 workspace 与 `state_version`
持久化到会话，并单独处理迁移和并发语义。

本轮同时新增文档生命周期地图：`PROJECT.md` 与飞书项目文档承担当前事实，`docs/architecture/`
承担设计/合同，`docs/post-training/` 分离数据、实验和学习。没有批量删除历史 Markdown；它们仍可能
承载唯一的实验溯源或外部链接，必须在完整迁移、无引用和链接检查后才允许物理删除。

### 2026-09-21：M2 Workspace 与会话状态版本完成

`app.conversations` 新增非空 `workspace_id` 与非负 `state_version`；幂等迁移将已有 206 条单工作区
历史回填为 `olist-demo`，并新增 `(user_id, workspace_id, updated_at)` 索引。`PostgresConversationStore`
的读取、列表、删除和更新均加入 workspace 范围；更新先锁行校验对象携带的 `state_version`，成功后
递增，旧对象写入抛出 `ConversationStateConflict`，`BudgetedChatHandler` 返回可恢复状态卡，不做
silent last-write-wins。历史详情/列表 DTO 现在返回 workspace 与 state version；TheLook 仍未接入默认在线
页面。真实 PostgreSQL store/route 回归 **6 passed**，身份/运行时回归 **70 passed**，迁移、compileall、
Ruff 和 diff 检查通过。该任务未改 SQL 生成、Policy、reader role、ResultValidator、模型或训练评测链。

### 2026-09-21：Phase 3 长对话摘要与结果工件真实验收完成

完成 Phase 3 的代码收紧与隔离线上回归。`ResultArtifactStore.verify_manifest()` 会在恢复前重算
CSV/Plotly 的 SHA-256 与字节数；`TrustedRunSqlTool` 只有在显式
`ResultValidator.state == valid` 时才允许写完整 CSV；会话详情与 artifact manifest 路由已恢复为两个
可达的 owner/workspace-scoped endpoint。真实 SSE 使用隔离端口 `32012` 和临时 `1,200` history-token
预算运行：受控州 GMV 图表查询生成 10 行 CSV 与 Plotly JSON，manifest 恢复校验通过；连续 6 个 user-led
turn 触发并持久化 `history-summary-v2`，摘要后的“2017 年按月统计有效订单数”通过 reader role、ResultContract
和 ResultValidator；切换到另一个 demo role 访问同一 artifact 返回 404。摘要正文未进入 `WorkingMemory` typed SQL
state，tokenizer evidence 明确为 `local_tokenizer_estimate`，不是 DeepSeek-V4-Flash exact tokenizer。
一次真实回归还发现旧指标被无条件追加到新问题检索上下文的状态串扰，已改为只有未完成 clarification 才保留
`pending_question`，并补回归。确定性/定向测试 **82 passed**，Ruff、compileall、diff check 和隔离页面
Playwright（HTTP 200、无 console error）通过。安全报告和原始 SSE 均在仓库外：
`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase3-context-artifacts/`。
摘要 provider 失败回退与工件篡改检测为确定性故障注入覆盖，未伪造为线上故障发生。

### 2026-09-20：可信图表 ChartContract v2 完成

图表不再只有 `bar` / `line`：新增 `horizontal_bar`、`area` 与 `donut`，但保持
`SqlPolicy -> reader role -> ResultContract/ResultValidator -> 当前请求内 DataFrame -> 服务端 Plotly`
这一条受控链路。品类图默认横向条形图；面积图只接受单指标、无业务分组的时间序列；环形图必须显式
请求、只接受可加金额/数量指标、一个维度、实际至多 8 个非负类别。散点、热力、地图、直方、漏斗、
瀑布、堆叠图仍因缺少相应的结果语义合同而明确拒绝，绝不静默替换。图表不增加模型调用：两条真实
SSE 验收（品类 GMV 横向条形图、州 GMV 环形图）均为 1 次 SQL、0 次模型图表工具调用、1 轮 LLM，
并发出服务器 `chart` 组件；原始 SSE 留在仓库外。专项回归 **67 passed**、Ruff、compileall 与 diff
检查通过。完整 `pytest` 在服务运行 Conda 环境的收集阶段被既有后训练测试所需的 `torch` 缺失阻断，
这不等价于图表测试失败。嵌入页面历史会话当前只恢复安全文本/表格而不回放图表组件，这是既有会话
体验边界，留给后续 Phase C 处理，不据此否定实时 SSE 图表能力。

### 2026-09-20：产品能力基线 Phase C 结构性运行完成

新增 10 个多轮会话脚本与 `scripts/run_product_capability_conversations.py`，通过当前 trusted demo
SSE 运行 26 个 turn；原始事件位于仓库外，正式安全报告为
`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-c/rerun-v2/conversation-safe-report.json`。
报告只保留会话/request hash、route、WorkingMemory 投影、可信结果摘要存在性、工具调用和上下文预算字段，
并已通过 safe-evidence schema。修正 runner 将未完成 SQL 标为失败后，10 个脚本中 4 个严格通过，暴露四类
事实：`WorkingMemory.apply()` 没有记录当前轮新维度；一条“刚才排名第一的州是否合理”被下钻规则误路由为新
数据库分析；8 个 answerable turn 的真实终态不是 `completed`；长历史本批未触发 context truncation。
相对时间澄清后补绝对年份、无可信结果先澄清、有可信摘要的 tool-free 追问和新会话隔离均有通过证据。
Phase C 只完成测量，未直接实现长期记忆、workspace/state version 或 clear action。随后已补齐失败诊断、结果追问
分类、QueryPlan 维度写回和最小可信 ResultArtifact 回放；相关结构性回归通过。当前进一步将 `ContextBudgetFilter`
升级为无模型的 `HistorySummary v1`：超预算时保留最近完整 turn，以范围/数量/字符/哈希边界替代被省略旧 turn，
并把 `context_budget` 脱敏证据写入 `agent_runs.catalog_trace`；极端当前 turn 仅用固定 marker 压缩工具交互。
这不等于长期记忆，也不是 provider 精确 token 统计；长历史真实小批回归待服务重载后执行。

### 2026-09-18：产品能力基线测试 v1 Phase 0 完成

冻结 [`plan/feature-product-capability-baseline-test-v1.md`](plan/feature-product-capability-baseline-test-v1.md) 与 [`docs/architecture/product-capability-baseline-v1.md`](docs/architecture/product-capability-baseline-v1.md)：A 路由/澄清 12 条、B 单轮业务问答 16 条、C 多轮会话 10 组、D 浏览器 8 条、E 汇总的分阶段测试合同已定义。Phase 0 只完成 `product_capability_v1.yaml` 的 runtime-only 隔离骨架、safe evidence schema、SQL/Gold/结果行/protected-case fail-closed 校验及 8 条合同测试；A--D 尚未物化 case、未调用在线模型、未连接业务数据库、未启动浏览器真实流和未改运行时。后续按用户审阅后一次推进一个阶段。

### 2026-09-19：产品能力基线测试 v1 Phase A 完成

完成 12 条确定性 Router 基线：3 条数据库可答、2 条帮助/Catalog、3 条澄清、3 条安全拒绝、1 条无可信结果追问。新增 [`scripts/run_product_capability_routing.py`](scripts/run_product_capability_routing.py) 与 [`tests/test_product_capability_routing.py`](tests/test_product_capability_routing.py)，实际结果为 **12/12 passed**；runner 明确记录 `database_calls=0`、`llm_calls=0`，不生成/执行 SQL，不启动浏览器。安全聚合报告位于仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-a/routing-safe-report.json`。该结果只证明路由边界，不代表 SQL、业务语义、图表或多轮能力；B/C/D 仍未运行。

### 2026-09-19：产品能力基线测试 v1 Phase B 运行完成（待人工语义审核）

冻结 16 条单轮 case，并新增 [`scripts/run_product_capability_online.py`](scripts/run_product_capability_online.py)、[`src/data_analysis_agent/product_capability_review.py`](src/data_analysis_agent/product_capability_review.py) 及合同测试。通过当前 trusted demo SSE 正式运行 16 条：16/16 AgentRun 可回读，14 条完成 SQL 闭环，3/3 图表请求产生图表组件；2 条二层聚合请求在 Router/Catalog 阶段澄清、未生成 SQL。结构化报告位于仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-b/single-turn-safe-report.json`，人工审核模板位于同目录 `manual-review-template.json`。本次服务审计仍报告 `metric_version=0.1-draft`，与仓库当前离线指标目录存在版本一致性待确认问题；在完成 `metric_semantics`、`time_scope`、`answer_grounded`、`chart_semantics` 人工审核及服务版本确认前，不发布 B 语义准确率，也不进入 C。

### 2026-09-18：Project v1 Week 1 复盘完成

本周未启动新训练、DPO、protected final test 或大规模产品重构；重点是从真实代码、定向测试和仓库外脱敏证据恢复可演示项目基线。已完成请求路由、Semantic Catalog/QuerySpec、SQL Policy/reader role/ResultValidator、图表/表格、WorkingMemory、会话所有权、local Adapter 运行时边界以及 repair/fallback 的代码级审查；详细记录见 [`docs/architecture/project-v1-baseline-review.md`](docs/architecture/project-v1-baseline-review.md)，已确认缺口见 [`docs/architecture/project-v1-gap-map.md`](docs/architecture/project-v1-gap-map.md)。

同时冻结了“嵌入式 Copilot + artifact-backed memory”的 M1--M6 依赖路线：先宿主身份/workspace 合同，再会话隔离与版本、可信 ResultArtifact、产品壳、状态生命周期，最后才在真实超预算时增加可溯源历史摘要；该路线不采用“把全部聊天向量化后直接拼 Prompt”的模式。公开 Codex CLI、Aider、LangGraph、Mem0 的 revision-pinned 代码调研见 [`docs/architecture/open-source-agent-context-research-v1.md`](docs/architecture/open-source-agent-context-research-v1.md)。

后训练主事实统一收束为 Qwen2.5-Coder-1.5B Base 的 multi-schema SQL-only `adapter_best`：以 Olist/TheLook `6,000/1,500` 训练/validation、validation-best step 2700 为唯一候选，在两个各 750 条 protected final test 上完成统一治理与 Gold denotation 对照。Olist ordered Gold match 为 Base/Adapter `3/403`，TheLook 为 `41/396`；这证明冻结电商合同下的候选质量提升，不等于生产接入、开放域泛化或业务准确率。完整边界见 [`docs/post-training/experiments/project-v1-model-baseline.md`](docs/post-training/experiments/project-v1-model-baseline.md)。

DPO 当前不启动：train-only 与 validation-only 的两批各 48 次分层采样均为 Gold match，未出现可复核 A--C rejected SQL；不得手写坏 SQL、不得使用 holdout 错误凑 pair。证据与重新准入门见 [`docs/post-training/experiments/project-v1-dpo-decision.md`](docs/post-training/experiments/project-v1-dpo-decision.md)。Week 2--4 只作为待确认的单任务依赖路线，见 [`plan/process-project-v1-week2-4.md`](plan/process-project-v1-week2-4.md)。

### 2026-09-16：多 Schema 电商 SQL-only SFT v1 冻结

Olist v3.1 与 TheLook v3 r3 的 1.5B Base/Adapter matching 和 Gold denotation 对照已完成。Olist Adapter 在 750 条域内测试上达到 `750/750` ResultContract valid 与 `750/750` Gold ordered denotation；TheLook Adapter 在未见 Schema 的 750 条测试上达到 `368/750` ResultContract valid、`289/750` Gold bag denotation，说明 Olist-only SFT 的跨 Schema 能力仍有明显缺口。基于该证据，冻结 [`docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md`](docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md)：保留 Olist `3,000/750` train/validation，并已构建与 TheLook v3 r3 protected test 隔离的 TheLook `3,000/750` seed/Gold/admission release（`thelook-sql-only-sft-v1-r4`，`3750/3750` reader-role 准入）。新 SFT release 使用 1,000 行执行上限和独立 200 行预览预算，14 条 `201--829` 行训练结果仅标记展示截断、没有执行截断；TheLook v3 r3 750 条 test、Gold、错误结果不得回流训练。统一中文主问法、Prompt、token audit 与 Olist/TheLook 合并 `6,000/1,500` release 尚未开始，未启动下一轮训练。

### 2026-09-15：在线 SQL 执行上限与界面展示预算分离

修复在线查询因“200 行预算”提前失败的问题。`SecurePostgresRunner` 和在线
`QuestionRouter` 默认使用 1,000 行数据库执行上限；结果在 201--999 行时仍视为完整执行，
前端表格仅预览前 200 行并明确标记展示截断；真正取满 1,000 行才要求缩小查询范围。默认
`SqlPolicy()` 仍保持 200 行，以兼容受保护离线评测和历史合同；TheLook v3 继续显式使用
200 行完整 denotation 合同。图表展示预算不变，超过 200 行时要求先缩小范围。相关运行时
合同见 [`docs/architecture/runtime-contract-preflight-v1.md`](docs/architecture/runtime-contract-preflight-v1.md)，
回归测试覆盖 100 条，PostgreSQL 集成测试覆盖 7 条。

### 2026-09-15：TheLook v3 跨 Schema 评测覆盖合同 v2 与 seed 冻结

为避免把历史 TheLook v2 的 `family_id == query_spec_id` 误当成场景覆盖，新增机器可读
`thelook-v3-coverage-contract-v2`、独立 `scenario_family_id`、由 QuerySpec 派生的
`program_signature` 和 `risk_tags`。v3 目标为 750 个静态、可复现 seed；冻结事实域
`135/250/60/100/4/135/66`、shape `210/285/255`、日/周/月/季/年序列 `35/45/65/60/50`、
13 个安全分组维度、21 个 scenario family、1--4 指标配额、风险下限和八类纯中文主问法配额。
语义底座继续固定在 TheLook v2 的 20 项指标和 QuerySpec/renderer 版本。v3 明确允许
`inventory_unsold_snapshot` family 复用 v2 中 4 个合法 all-time snapshot QuerySpec/Gold，其他
QuerySpec、Gold hash 和规范化主问题仍由 release audit 检查，不再把合理复用误判为失败。
静态构造器生成 750 条 QuerySpec-backed seed，精确满足上述分布；seed 本身未渲染 SQL、访问
PostgreSQL、生成问题或调用模型。随后已完成 44/44、21/21 family 的小批准入，并物化
`thelook-cross-schema-final-test-v3-20260915-r3`：750/750 通过 Gold renderer、SqlPolicy、
`daa_thelook_reader`、ResultContract/ResultValidator 和 200 行展示预算。最终 release 的
`cases.jsonl` SHA-256 为 `bd1195c00dd68a884fc55eb8301184ef1492dd070f01fa26f96293ac13acef7c`，
release audit 状态为 `pass`。静态合同/seed/代码/测试见
[`docs/post-training/data/thelook-v3-coverage-contract-v2.md`](docs/post-training/data/thelook-v3-coverage-contract-v2.md)。
该 release 仅用于 Base/Adapter 的 final evaluation；TheLook v3 不进入 Olist 训练、Prompt 调优、
错误回流或模型选择。v2/v3 允许的 4 个 `inventory_unsold_snapshot` QuerySpec/Gold 复用已在
release audit 中显式记录，其他身份和规范化主问题不允许未经批准地重合。

### 2026-09-14：Olist v3 指标合同、平衡 SQL-only release 与全量准入

在不改写默认 `olist-catalog-v2 / 0.2-frozen` 运行时快照的前提下，新增隔离的
`olist-catalog-v3 / 0.3-proposal` 与 `OLIST_V3_WORKSPACE`，实现并注册 9 个 Olist 领域指标：
去重客户数、有效评价数、取消/送达/不可用订单数、平均每单商品件数、平均商品成交价、订单确认
时长和交承运商时长。离线 QuerySpec、deterministic PostgreSQL Gold renderer、SchemaLinkPlan 和
AST Policy 回归均保持 v2/v3 版本隔离；`average_items_per_order` 明确采用订单级二层聚合，客户
去重仅将敏感标识用于 `COUNT(DISTINCT)`，两项时长均排除空值和负区间。发现“取消订单数”被泛化
“订单数”同时召回的真实 alias overlap 后，Catalog Retriever 增加通用的长短受控别名覆盖消歧：仅当
泛化别名所有出现位置均被更长的已注册指标别名覆盖时才抑制；“有效订单数和取消订单数”仍保留为
两个指标。v3 专项静态/运行时测试 `126 passed, 1 skipped`，启用项目数据库后 `26 passed`；9 项
标量 Gold 也以 `daa_analytics_reader` 在本机 PostgreSQL 通过，聚合基线见
[`evals/results/olist-metrics-v3-proposal-golden.yaml`](evals/results/olist-metrics-v3-proposal-golden.yaml)。

同日将下一版数据设计从相互重叠的“family 桶”改为 8 个互斥主类别、300 个新增 family：单指标/多
指标标量、单指标/多指标维度分组、购买/评价单指标时间序列、多指标时间序列、结构难例。对历史
Release v2 的真实 QuerySpec 统计发现，它的 2,400 条 train **不均衡**：`JP09`/`JP10`/`JP11` 三类
合计 `2,347/2,400=97.8%`，多指标购买时间序列单类 `1,049/2,400=43.7%`，而商品品类在 test 为 0。
因此废弃“直接追加 v3 行后得到 6,000 行”的方案；最终 release 必须从 v2/v3 合法候选重新物化，
目标为 `train/validation/test=3,000/750/750`。新 release 同时以 family 覆盖保证稀缺能力，以行曝光
上限限制多指标时间序列约 25%，日期窗口与中文改写均不冒充独立能力。

v3 指标的真实 PostgreSQL Gold 回归已进一步覆盖客户州/商品品类分组、购买/评价时间序列、订单级
二层聚合和空时间窗口：`RUN_PROJECT_DB=1` 下 `37 passed`。空时间窗口的 `COUNT` 标量可展示为 0；
`AVG` 标量为 NULL 时必须拒绝；空分组/时间序列必须澄清，不能伪造结果。基于这些门，已冻结
[`olist_v3_coverage_family_seeds_v1.jsonl`](data/fixtures/olist_v3_coverage_family_seeds_v1.jsonl)：300 个
不含问题、Prompt、SQL 或结果的结构化 family seed，split 为 `200/50/50`。它们作为施工卡完成了小批
Gold 准入：12/12 在 `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator` 下 admitted。

随后以 v2 的**结构候选**（不是复制旧 SQL/Prompt/SFT 行）、v3 family seed 和有限补充模板，重新 pin、
校验、渲染并物化 v3.0 平衡 release。最终为 `3,000/750/750=4,500` 条 QuerySpec / canonical Gold SQL：
family、QuerySpec 与 canonical SQL hash 跨 split 均零交集，750 条 test 物理隔离为
`final_evaluation_only`。其中单指标维度分组的真实可用容量是 `122/33/33=188`，少于初始目标的 12 条
train 行已转给多指标维度分组，未用重复 SQL 或中文改写凑数。

2026-09-14 随后完成 v3.1 数据平衡与中文 surface 修复，旧 v3.0 外部资产保留而不重写。当前 release
仍为 `3,000/750/750=4,500` 条 QuerySpec / canonical Gold SQL，family、QuerySpec 与 canonical SQL hash
跨 split 均为零交集，750 条 final test 物理隔离为 `final_evaluation_only`。新 release 全部 4,500 条 Gold
SQL 已在实际 PostgreSQL 中通过 `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator`，结果为
`4,500 admitted / 0 needs_human_review / 0 rejected`；每条都保留 Gold SQL hash、Policy 最终 SQL hash、
reader-role 执行和结果摘要 hash。

v3.1 每个 QuerySpec 生成 8 个受控**纯中文**问法，共 36,000 条 runtime overlay，均重新经过
Router/Catalog/QueryPlan/ResultContract；正式 SFT 每实例仍只选一条主问法，但由 split×主 bucket 的稳定
SHA-256 排名分层精确配额选择，而非 `hash % 5`。train 的 3,000 条中每类 375；validation/final-test 的
750 条中 v1–v6 各 94、v7–v8 各 93。所有主问法过滤 ASCII Latin token，英文指标别名不混入中文主集。
新增 9 个 v3 指标在 train 均至少 230 行且至少 20 个 family；`category_grouped` train 由 9 提升至 17 行；
validation/final-test 的 day/week/month/quarter/year 时序均至少 70 行。最终 SFT JSONL 在 `3072` token
上限下无排除、无静默截断。

分层抽取的 48 条 DeepSeek advisory 语义复核中，45 条 pass、3 条为 `APITimeoutError`。三个 timeout 已在
审计中保留为非阻塞 provider-error advisory；它们不覆盖或替代 4,500/4,500 的确定性准入，且没有真实
semantic review block。完整 release、外部证据路径、hash 和准确解释见
[`docs/post-training/data/olist-v3-balanced-release-v1-1.md`](docs/post-training/data/olist-v3-balanced-release-v1-1.md)
与 [`docs/post-training/data/olist-v3-1-balance-and-surface-repair-contract.md`](docs/post-training/data/olist-v3-1-balance-and-surface-repair-contract.md)。

P1 审阅后，release audit 已进一步直接重算最终三个 SFT JSONL 的 SHA-256，并将每行的 split、family、
QuerySpec、runtime Prompt 和 canonical Gold SQL 与 admission 记录绑定，重算跨 split 零交集；复跑仍为 pass，
且不调用模型、GPU 或数据库。v3.1 SQL-only LoRA 的输入/标签模板、bf16 LoRA/optimizer、validation-best
checkpoint 与三阶段 matching Base/Adapter 协议已冻结于
[`docs/post-training/data/olist-v3-1-sql-only-training-evaluation-contract.md`](docs/post-training/data/olist-v3-1-sql-only-training-evaluation-contract.md)。

当前 v3.1 release **尚未启动 CPU/GPU smoke、训练或评测**，也未替换默认 Vanna/SiliconFlow 候选路径；它证明
受控训练输入和 Gold 执行链路完整，不证明模型提升、开放式业务语义准确率、跨 schema 泛化或生产接入资格。下一步
仅为 CPU preflight；之后仍需用户确认，才可做一次 GPU smoke。

### 2026-09-14：Text-to-SQL 训练组织方式与 Schema-aware Task B 调研

新增 [`docs/post-training/research/text-to-sql-training-organizations-v1.md`](docs/post-training/research/text-to-sql-training-organizations-v1.md)，基于 IRNet、RAT-SQL、RESDSQL、CodeS、PICARD、DAIL-SQL、MAC-SQL、CHESS、AutoLink、FINER-SQL、SQL-R1、SHARE 和 SLM-SQL 的论文/官方代码核对，区分了可编译中间表示、内部/独立 schema linking、约束解码、提示/多 Agent 分解和执行反馈训练。结论是当前 Task B 属于与 Task A 交错的辅助多任务 LM SFT，并没有形成模型可消费的 `Plan → SQL` 条件路径；Task B 目标更长且占约 59% 监督 token，并伴随 TheLook 输出协议漂移，因此暂不作为默认训练目标或生产能力。下一步优先保持 SQL-only 线上一致目标，研究输入侧 schema filtering/轻量结构 head，所有实验仍须 Olist train/validation-only 预注册，TheLook v2 只做最终后置验证。本轮未启动消融或重新训练，未修改生产运行时。

下一阶段平台计划与 Text-to-SQL 专项调研分别见：

- [`docs/AGENT_PLATFORM_NEXT_PLAN.md`](docs/AGENT_PLATFORM_NEXT_PLAN.md)
- [`docs/TEXT_TO_SQL_RESEARCH.md`](docs/TEXT_TO_SQL_RESEARCH.md)
- [`plan/feature-text-to-sql-reliability-v1.md`](plan/feature-text-to-sql-reliability-v1.md)
- [`plan/feature-text-to-sql-reliability-v2.md`](plan/feature-text-to-sql-reliability-v2.md)
- [`docs/verification-text-to-sql-v2.md`](docs/verification-text-to-sql-v2.md)
- [`docs/post-training/README.md`](docs/post-training/README.md)
- [`docs/post-training/learning/review-2026-08-28.md`](docs/post-training/learning/review-2026-08-28.md)
- [`docs/post-training/learning/walkthrough-v1.md`](docs/post-training/learning/walkthrough-v1.md)
- [`docs/post-training/learning/fundamentals.md`](docs/post-training/learning/fundamentals.md)
- [`docs/post-training/data/protocol.md`](docs/post-training/data/protocol.md)
- [`docs/post-training/experiments/log.md`](docs/post-training/experiments/log.md)
- [`docs/post-training/research/text-to-sql-training-organizations-v1.md`](docs/post-training/research/text-to-sql-training-organizations-v1.md)
- [`docs/post-training/archive/learning-notes-v1.md`](docs/post-training/archive/learning-notes-v1.md)
- [`docs/post-training-base-adapter-analysis-v1.md`](docs/post-training-base-adapter-analysis-v1.md)
- [`docs/post-training-spider-sft-v2-full-analysis.md`](docs/post-training-spider-sft-v2-full-analysis.md)
- [`docs/post-training-olist-business-transfer-evaluation-v1.md`](docs/post-training-olist-business-transfer-evaluation-v1.md)
- [`evals/manifests/post_training_forward_smoke_v1.yaml`](evals/manifests/post_training_forward_smoke_v1.yaml)
- [`evals/manifests/post_training_sft_smoke_v1.yaml`](evals/manifests/post_training_sft_smoke_v1.yaml)
- [`docs/post-training-base-adapter-evaluation.md`](docs/post-training-base-adapter-evaluation.md)
- [`evals/manifests/post_training_base_adapter_evaluation_v1.yaml`](evals/manifests/post_training_base_adapter_evaluation_v1.yaml)
- [`docs/sqlite-benchmark-adapter.md`](docs/sqlite-benchmark-adapter.md)
- [`docs/frozen-sqlite-baseline.md`](docs/frozen-sqlite-baseline.md)
- [`docs/spider-1.0-data-provenance.md`](docs/spider-1.0-data-provenance.md)
- [`docs/official-spider-test-suite.md`](docs/official-spider-test-suite.md)

| 日期 | 事项 | 结论 |
| --- | --- | --- |
| 2026-09-14 | Schema-aware Program Adapter 输出协议归一化与 TheLook v2 重评 | 新 Schema-aware Adapter 的 600 条冻结 completion 中有 480 条在 SQL 前多出短展示标题，旧的精确前缀处理使这些合法 SQL 在 AST 解析前失败。共享候选 SQL 归一化现只移除一条满足严格结构条件的首行标题（短、无 SQL 标记、下一非空行明确 `SELECT/WITH`），不搜索或修复 SQL，随后仍执行 SQL Policy、只读角色和结果合同。复用同一 Base/Adapter raw completion、marker 和 protected TheLook v2 阶段 C 重评后，Schema-aware 的 Policy/PostgreSQL/ResultContract/Gold ordered-or-bag 为 `457/296/284/205`，优于 Base `259/173/127/67`，但仍低于历史 SQL-only Adapter `467/335/313/225`；因此不替换默认候选模型、不接入生产、不将 TheLook case/错误回流训练。Gold 重放新增脱敏分类诊断和一次有限瞬态重试，305 个有资格的唯一 Gold case 全部通过，未复现历史 case 449 的无分类失败。原始 SQL、问题和结果继续留在仓库外。 |
| 2026-09-13 | Qwen2.5-Coder-1.5B Schema-aware Program SFT 完整训练完成 | 基于冻结 Olist Release v2 的 2,400 train / 600 validation query instance，训练期按同一 instance 交错 Task A runtime Prompt → canonical PostgreSQL SQL 与 Task B 紧凑结构上下文 → canonical SchemaLinkPlan JSON；共 4,800 / 1,200 event，但不计作额外业务问题。新 bf16 LoRA Adapter 在逻辑 CUDA 0 / 物理 GPU 3 RTX 4090 完成 2 epoch、2,400 optimizer step，耗时约 97.2 分钟；最终 train / aggregate validation / SQL validation / SchemaLinkPlan validation loss 为 `0.114698 / 0.100952 / 1.5486e-05 / 0.201889`，峰值 allocated/reserved 显存为 `8.60/18.80 GiB`。最终 71 MiB Adapter 已从全新 bf16 Base fresh reload，SHA-256=`87b4be23b50becf9d094fd34b5330138562d1427acd8998d57e7c32cb9bdffac`。SQL 低 loss 只表明共享 Olist 协议拟合强，不可写为业务准确率或跨 schema 泛化；Task B 较高 loss 反映结构 JSON 目标更复杂。step 2100 有最低 aggregate validation loss 但未保存，当前 final step 2400 不是 validation-best，下一轮需对齐保存/评估并启用 best-model 选择。生产默认路径未变；下一步严格按 Gold 后置隔离评测 Olist final test 和 protected TheLook v2 的 Base / SQL-only Adapter / Schema-aware Adapter 对照。 |
| 2026-09-11 | Qwen2.5-Coder-1.5B-Instruct TheLook v2 基座方案对照；Schema-aware Program SFT Phase 1 完成；Qwen3.5-4B Adapter 跨 schema 评测完成 | 官方 Instruct 无 Adapter 在 600 条 protected TheLook v2 上完成 generation-only 隔离、证据回读和生成后 Policy/reader/ResultContract/Gold 评测。相较历史 Qwen2.5-Coder 1.5B Base，Instruct 的 Policy/PostgreSQL/ResultContract 为 `259/173/127 -> 354/228/159`，但 ordered-or-bag Gold match 为 `67/600 -> 64/600`，且合同有效后的匹配率 `52.8% -> 40.3%`、column mismatch `5 -> 42`；Instruct 还使用官方 chat template，故不是严格单变量消融。没有证据支持替换基座，Schema-aware 实验仍从原始 Base 新建 Adapter。Schema-aware Program SFT Phase 1 已完成：Task A 保持真实运行时 Prompt -> canonical SQL；Task B 仅训练期从 QuerySpec/Catalog/static registry 确定性派生 SchemaLinkPlan，线上仍一次生成 SQL。Olist Release v2 train/validation 已物化为 `3,000` pair（`6,000` event），独立 audit 重派生全部 source 并确认字节、plan、pair/order、source hash、family/QuerySpec 隔离和零长度排除；随后 Olist-only review 确认当前十指标、四形态、十条 Join program 全覆盖，Task B 最大 `3,046/3,072` token。4B 的 Olist bf16 LoRA 在 600/600 no-Gold matching marker 后进入统一后评测：Base -> Adapter 的 Policy/PostgreSQL/ResultContract 为 `234/155/131 -> 518/389/389`，ordered-or-bag Gold denotation 为 `31 -> 340`；仍有 12 条 valid -> non-valid 回退、49 条 Adapter 合同有效但 Gold 不一致，不能宣称开放泛化或生产可用。未改变默认运行时。详见 [`docs/post-training/data/schema-aware-program-sft-contract-v1.md`](docs/post-training/data/schema-aware-program-sft-contract-v1.md)。 |
| 2026-09-11 | Qwen2.5-Coder-1.5B-Instruct TheLook v2 直接基座对照启动 | 为判断后续 Schema-aware Program SFT 是否值得改用同规模官方 Instruct 基座，冻结 `Qwen/Qwen2.5-Coder-1.5B-Instruct@2e1fd397ee46e1388853d2af2c993145b0f1098a`（官方 bf16 权重、无 Adapter、模型文件 SHA 已写仓库外 manifest）。这不是历史 1.5B Base / Olist Adapter 的严格 matching pair：服务器 Prompt 内容、600 条 protected TheLook v2 case、greedy decode、`4096/768`、seed、后验 Policy/reader/ResultContract/Gold 链路保持一致，但 Instruct 必须使用官方 chat template。因此结果只用于“同任务内容下的基座方案对照”，不能写为单变量参数消融。新增独立 generator / evaluator / launcher，生成器只读 `question + QuerySpec + ResultContract`，并绑定 server Prompt、chat template、chat prompt、模型 revision、raw completion 与 GPU UUID 哈希；评测器会在回读这些证据后才允许 Gold SQL 访问。专项 `8 passed`、Ruff、compile、shell syntax 和 diff 检查通过；阶段 A 已在逻辑 CUDA `3`/物理 GPU `1` RTX 3090 的空余显存启动，未抢占其他任务。TheLook 仍不进入 Olist 训练、Prompt、few-shot、checkpoint 选择或错误驱动数据构造；尚无质量结论。详见 [`docs/post-training/experiments/qwen25coder15b-instruct-thelook-v2-baseline-contract-v1.md`](docs/post-training/experiments/qwen25coder15b-instruct-thelook-v2-baseline-contract-v1.md)。 |
| 2026-09-11 | Qwen3.5-2B 完整 Olist bf16 LoRA 与 TheLook v2 跨 schema 对照 | Qwen3.5-2B 在冻结 Olist Release v2 `2,400/600` train/validation 上完成两 epoch、1,200 optimizer steps 的 bf16 LoRA：最后记录 train loss=`6.2284e-06`、validation loss=`6.5842e-06`，fresh bf16 Base + 磁盘 Adapter reload loss=`7.7904e-06`，最终 adapter SHA-256=`19b9a3f68f4494c38f9bcbef176b68336e9a625c8947b9324b03054fe599faf3`；训练耗时约 3 小时 20 分、峰值 allocated/reserved 约 `12.53/14.06 GiB`。该 loss 仅说明模型在共享 Olist Catalog/QuerySpec/ResultContract/renderer 的确定性 SQL 编译协议内高度拟合。TheLook v2 的 matching Base/Adapter 均 `600/600` 生成成功，并在 pair marker 验证后才运行 `SqlPolicy -> readonly PostgreSQL -> ResultContract -> Gold denotation`。Adapter 相对 Base 的 Policy accepted / PostgreSQL executed / ResultContract valid 为 `172/78/58 -> 388/206/196`，ordered-or-bag denotation match 为 `23/600 -> 118/600`；但仍有 `33` 条 valid -> non-valid 回退，且 Adapter 的 196 条合同有效结果中有 `71` 条值不匹配、`7` 条行数不匹配，不能写为开放式 SQL 正确率、无回退泛化或生产接入资格。TheLook 原始问题、SQL、Gold 和结果仍在仓库外，且不进入训练/Prompt。Qwen3.5-4B 的同一 Olist 两 epoch已在逻辑 CUDA `2`/物理 GPU `0` RTX 3090 的 UUID guard 下重新启动；不抢占其他 GPU 进程。 |
| 2026-09-10 | Qwen3.5-4B Instruct bf16 LoRA Trainer 与最小 GPU smoke | 新增独立 Qwen3.5-4B Instruct Trainer，冻结 Olist Release v2 的 `2,400/600` 输入、Qwen3.5 official chat template、SQL + EOT label 边界和 TheLook/in-domain test 隔离；Trainer 回读数据/模型/layout hash，只在实际枚举到的 128 个 `model.language_model.layers.*` projection 注入 LoRA。单步 smoke 在逻辑 CUDA `0`/物理 GPU `2` 的 RTX 4090 通过：一次更新、验证、checkpoint、`adapter_final`、fresh bf16 Base + Adapter reload 都完成，train/eval/reload loss 为 `0.503757/0.506993/0.504594`；未量化 `adamw_torch`、weight decay `0.01`、bf16、gradient checkpointing 和 `21,233,664`（总 4.56B 的 `0.4656%`）LoRA 参数均有外部 evidence。短序列 peak 为 `12,204.7/13,384.0 MiB` allocated/reserved，但首次使用 2,498-token validation 已因共享显存少 2.31 GiB 而 OOM，因此不能把 smoke 当作最长输入适配证明。正式两 epoch 启动器已冻结，并在物理 GPU 2 少于 `17,408 MiB` 空闲时 fail closed；本次实测仅 `14,715 MiB`，故未抢占其他任务或启动正式训练。TheLook 未读取、生产默认路径未改。详见 [`docs/post-training/experiments/qwen35-4b-olist-instruct-sft-smoke-v1.md`](docs/post-training/experiments/qwen35-4b-olist-instruct-sft-smoke-v1.md)。 |
| 2026-09-10 | Qwen3.5-4B Instruct Olist LoRA 训练前模板与长度准入 | 未启动 GPU 训练、未读取 TheLook v2 的前提下，新增 Qwen3.5 SQL-only SFT 模板转换层与 token 审计入口。既有 Olist Release v2 的 `2,400/600` train/validation 和 family-isolated split/hash 保持不变；完整 `rendered_prompt` 作为官方 chat template 的唯一 user turn，assistant 只监督 canonical PostgreSQL SQL 与 assistant EOT，`enable_thinking=False`，user/assistant 边界及模板插入的空 think 前缀均以 `labels=-100` mask，EOT 后模板换行不参与监督以与生成停止行为一致。实际 `3,000/3,000` 条 CPU token 审计通过：train `1116--2862`、validation `1086--2874`，3072 上限零触及、无静默截断；审计仅写仓库外聚合哈希，不保存问题/SQL。单元测试 `5 passed`、Ruff、compileall、diff check 通过。下一步是单独审阅 Qwen3.5 专用 Trainer 和 bf16 LoRA smoke；TheLook 继续不参与训练、模板调参或模型选择。详见 [`docs/post-training/data/qwen35-olist-sft-template-contract-v1.md`](docs/post-training/data/qwen35-olist-sft-template-contract-v1.md)。 |
| 2026-09-10 | TheLook v2 Base/Adapter matching 合同与执行入口冻结 | 为已冻结的 600 条 `thelook-cross-schema-final-test-v2` 建立独立 matching 协议，未改动 v1 的 206 条资产、Olist 训练数据或产品默认路径。生成被拆成三个强制阶段：A 只投影 `question + QuerySpec + ResultContract` 并以完整 v2 Catalog 生成 Base/Adapter raw completion，明确不读 Gold/数据库；B 在无 Gold、无数据库条件下校验两侧的 600 条 case 顺序、case/manifest hash、Prompt bundle hash、模型 revision、greedy decode、adapter 状态和 raw hash，写出 marker；C 仅在 marker 回读一致后才统一调用 `unwrap_sql_completion()`，经 `SqlPolicy -> daa_thelook_reader -> ResultValidator` 执行候选，并仅对 ResultContract valid 候选作 Gold denotation 比较。真实 Qwen tokenizer 对 600 条的只读预检为 `1948--2075` token，固定容量上限 `4096`、Prompt bundle SHA-256=`48bdc6763f8a7a5ead870d1ae9b25ba4dede6c99cf39fea0c5294cd4223ab123`，不发生截断。专项合同、v2 Catalog/QuerySpec/renderer/构造器回归 `22 passed`，Ruff、compile 和 launcher 语法均通过。尚未加载 Base/Adapter、未占用 GPU、未运行模型质量对照，故不产生任何跨 schema 泛化结论。详见 [`docs/post-training/data/thelook-v2-matching-evaluation-contract.md`](docs/post-training/data/thelook-v2-matching-evaluation-contract.md)。 |
| 2026-09-10 | TheLook v2 600 条 Base/Adapter matching 完成 | 同一 Qwen bf16 Base 与冻结 Olist Release v2 Adapter 的 600/600 raw completion 均已生成，并在不读 Gold/数据库的阶段 B 通过 Prompt、模型 revision、decode、case 顺序、raw hash 和 Adapter 状态配对验证。阶段 C 首次报告的 `0/600` ResultContract valid 被定位为 evaluator 假阴性：Policy 为无 LIMIT 候选自动添加的 `LIMIT 200` 被错误当作截断。修复为仅在候选显式超预算、被 Policy 收紧时保守标记，并保持实际返回 `>=200` 行的截断门；专项回归 `4 passed`、Ruff、compileall 和 diff check 通过。复用同一 raw completion 重跑阶段 C 后，Adapter 相对 Base 的 Policy `467/600 vs 259/600`、PostgreSQL `335/600 vs 173/600`、ResultContract valid `313/600 vs 127/600`、Gold ordered-or-bag match `225/600 vs 67/600`。这是一份冻结跨 schema 电商 holdout 上的正向迁移证据，不等价于开放泛化、生产准确率或运行时接入；TheLook 继续不进入 Olist 训练/Prompt。详见 [`docs/post-training/experiments/thelook-v2-matching-v1.md`](docs/post-training/experiments/thelook-v2-matching-v1.md)。 |
| 2026-09-10 | TheLook v2 指标合同与 600 条跨 schema final test 冻结 | 在 v1 的 206 条历史资产完全不变、Olist LoRA 保持冻结的前提下，新建独立 `thelook-catalog-v2` / `0.2-frozen`、`thelook-query-spec-v2`、deterministic PostgreSQL renderer v2 和 protected `thelook-cross-schema-final-test-v2`。20 项指标覆盖订单商品、订单/履约、库存入库/售出/快照、事件/会话和用户注册；多指标严格限制同一事实域，订单均值始终先以 `order_id` 聚合再外层平均。构造 600 个唯一 case / QuerySpec / family，标量/分组/时间序列为 `163/303/134`，20 项指标、152 条同事实域多指标 case、29 条多指标序列均覆盖。每条实际经过 `QuerySpec -> renderer -> SqlPolicy -> daa_thelook_reader -> ResultValidator`，`600/600 valid`、最大返回 `194` 行；`cases.jsonl` 位于仓库外 `evals/thelook-cross-schema-final-test-v2-20260910/`，SHA-256=`e942367df04b7ed6e52db8455e4de681827f8741989c19d81cd88bd0479b0fa5`。全量准入暴露了两项真实合同问题并已收紧而未截断：订单均值 renderer 漏了 `order_items` 内层关联；`event_state` 有 `228--231` 个分组，超过 200 行展示预算，故从 v2 Catalog/评测移除并以多指标时序场景替换。未读取 Olist 训练/Prompt、未运行 v2 Base/Adapter、未改变默认运行时；该资产只证明评测与 Gold 准入可复现，不构成模型质量结论。详见 [`docs/post-training/data/thelook-cross-schema-final-test-v2.md`](docs/post-training/data/thelook-cross-schema-final-test-v2.md)。 |
| 2026-09-10 | TheLook v2 场景与指标只读画像 | 为避免仅通过复制时间范围把 TheLook 评测集扩至约 600 条，使用 `daa_thelook_reader` 对七个脱敏 `analytics` 视图执行聚合画像，未导出原始行、未改 v1 资产、未构造问题/Gold/模型候选。证据表明订单/商品行可扩至购物篮、取消/退货数量和三段履约时间，但商品行 `created_at` 与订单创建时间 `181,748/181,759` 不一致，订单类指标继续只能按 `orders.created_at` 统计；库存单元的售出时间、10 个配送中心关联（`490,705` 条零错配）、事件/去重会话、注册用户均有独立可用事实和时间字段。候选范围从 v1 的 6 项扩至 20 项、五类业务场景，同时明确排除未经归因的事件转化率、库存周转率、毛利和订单配送中心绩效；高基数城市/品牌必须经逐 case 行数门。下一步须先冻结 v2 Catalog/指标合同，不直接造 600 条样本。详见 [`docs/post-training/data/thelook-v2-metric-scenario-candidates.md`](docs/post-training/data/thelook-v2-metric-scenario-candidates.md)。 |
| 2026-09-10 | Olist Domain SFT Release v2：600 条 Gold 语义对照完成 | 在 Base/Adapter matching 生成、safe report/raw-candidate hash 绑定和 `completed` 标志均已确认后，才读取 final test 的 deterministic Gold SQL。审计器用同一 `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator` 执行 Gold，并只重放原始 ResultContract 已通过的候选；按列、行数、数值（绝对/相对误差 `1e-6`）和顺序比较，必要时再作 bag 比较。Base 在 600 条中仅 20 条具备 Gold 对照资格，结果为 ordered match `1`、column mismatch `9`、denotation mismatch `10`，其余 580 条保持不具备资格；Olist LoRA Adapter 为 `600/600` ResultContract valid、`600/600` ordered Gold denotation match，且无有效样本退化。Adapter 的 600 条候选另经 SQL 文本哈希确认逐字等于各自 Gold，Base 为 `0/600`；Gold 未参与生成，因此这不是泄露，但说明该结果证明的是当前 Olist `Catalog + QuerySpec + ResultContract` 确定性编译协议的 in-domain 拟合，而不是开放式问答 100% 准确率、跨 schema 泛化或生产接入资格。完整脱敏报告位于仓库外 `.../qwen25coder15b-olist-domain-sft-release-v2-base-adapter-finaltest-v1-20260909/analysis/gold-denotation-audit-release-v2.json`。 |
| 2026-09-09 | Olist Domain SFT Release v2 正式数据集完成并启动训练 | 在 renderer 修复 grouped AOV 的 NULL customer_state 维度过滤后，重新完成 3,600 条 QuerySpec/Gold 物化与全量准入：3,600/3,600 通过 SqlPolicy、PostgreSQL reader role、ResultContract/ResultValidator，40 条 SiliconFlow advisory 抽检全部通过。基于五种中文 surface form 重建 18,000 条 runtime overlay，并按 `sha256(seed_id) mod 5` 稳定伪随机选择一条问法，正式产出 train 2,400、validation 600、`final_evaluation_only/in_domain_test` 600 三个 JSONL；五种问法仍按一个 query instance 计数，无长度排除，3072 上限下最大序列 2,921，family 与 QuerySpec 均无跨 split 重叠。正式产物位于仓库外 `evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2/`；bf16 LoRA 两 epoch 已在 screen `olist-domain-sft-v2-train-final` 的物理 RTX 4090（nvidia-smi GPU 2）启动。 |
| 2026-09-09 | Olist 中文 surface-form review pilot 完成 | 新增 `build_olist_surface_form_pilot.py`，从仓库外 2,371 条候选 QuerySpec 排除 Medium v1 和 protected family 后，构造 20 个全新 `review_pilot` family、每个 5 条受控中文表达，共 100 行；20 family 覆盖十项冻结指标、五种时间粒度和多指标 scalar/州分组/时间序列难例。当前没有新的合法 `category_grouped` family（均已在 Medium v1 出现），如实记入 manifest 而未重复旧 family。20/20 Gold 通过 deterministic renderer、SqlPolicy、`daa_analytics_reader`、ResultContract/ResultValidator 与 SiliconFlow `DeepSeek-V4-Flash` 辅助口径抽检；100/100 重建 Router、Catalog、QueryPlan、ResultContract 和 Candidate SQL Prompt，无候选模型/GPU 调用。多样性审计为完全重复 0、family 结构漂移 0、75 个归一化模板/100 行；“比较/趋势/变化”因无基线会进入澄清路由，明确不混入普通 SQL SFT。外部资产位于 `evals/olist-surface-form-pilot-v1-20260909-v4/`；该批仅供人工审阅，绝不直接训练，下一步是审阅问法后再决定正式 release。 |
| 2026-09-08 | TheLook Semantic Catalog v1 冻结 | 基于独立 `thelook_analytics.analytics` 7 个脱敏视图冻结 `thelook-catalog-v1` / metric `0.1-frozen`：8 条 Join、6 个首批指标（已完成成交额、已完成订单数、平均完成订单金额、平均履约天数、退货率、已完成客户数）。指标严格使用 TheLook 原始状态值（`Complete`/`Returned` 等）、`orders.created_at` 和已审计的订单/商品行粒度；来源没有币种字段，Catalog 不宣称 USD。事件漏斗、库存周转、毛利和配送中心指标暂缓，避免未审查的分母/跨事实归因进入 Gold。新增 `THELOOK_WORKSPACE` 仅用于 Catalog/Policy 离线验证，默认 Olist 运行时未切换；未构造 QuerySpec、自然语言问题、Gold SQL、训练数据或模型评测。专项 Catalog/Policy 回归 `41 passed`。 |
| 2026-09-08 | TheLook QuerySpec 合同 v1 | 新增独立 `thelook_queryspec.py`，以 `thelook-query-spec-v1` 固定版本快照、1--4 个指标、标量/单维度分组/订单创建时间序列、半开时间范围、固定 Join program 和结果列顺序，并通过 canonical JSON 生成稳定 `tqs_` ID。验证器拒绝未知/重复指标、敏感或不支持维度、形态/维度不匹配、非法时间、版本漂移、Join program/结果列篡改和 hash 不一致。TheLook Catalog/workspace/QuerySpec 回归 `48 passed`，另通过 compileall 与 Ruff；未生成自然语言问题、Gold SQL、训练数据或评测，默认 Olist runtime 未切换。下一步才是 deterministic PostgreSQL Gold SQL renderer。 |
| 2026-09-08 | TheLook deterministic Gold SQL renderer v1 | 新增 `thelook_renderer.py`，从已验证 QuerySpec 编译 6 项指标的固定 PostgreSQL SQL：每指标独立 CTE，多指标按公共键合并，AOV 先按订单粒度聚合，时间统一使用 `orders.created_at` 半开过滤；renderer 不解析问题、不调用模型、不执行数据库，artifact 带 renderer/QuerySpec/workspace 快照和 SQL SHA-256。单元与 Catalog/QuerySpec/workspace 回归 `55 passed`，Ruff、compileall、diff check 通过；标量、州分组、品类分组、月度序列、AOV 五个代表性 artifact 已通过 TheLook Policy 并以 `daa_thelook_reader` 在 `thelook_analytics` 执行成功。下一步才构造约 200 条测试问题，默认 Olist runtime 未切换。 |
| 2026-09-08 | TheLook 206 条跨 schema final test 冻结 | 新增可复现构造器，基于 TheLook QuerySpec/renderer 生成 206 条中文 final evaluation-only case：标量 60、低基数维度分组 66、时间序列 80，六项指标和四组多指标组合均覆盖。所有 case 的问题、family、QuerySpec 和 Gold SQL hash 唯一；逐条通过 `QuerySpec -> renderer -> SqlPolicy -> daa_thelook_reader -> ResultValidator`，206/206 valid、结果行数 1--194。`city`、`brand` 因完整分组可触及 200 行策略上限排除，`state` 限定为 2019、2020、2021 上半年窗口。产物在仓库外 `evals/thelook-cross-schema-final-test-v1-20260908/`，`cases.jsonl` SHA-256=`f10b4c...31576`。未读取 Olist 训练/验证数据、未调整 Prompt、未运行 Base/Adapter 或改变默认 runtime；下一步单独冻结 matching 生成/评测合同。 |
| 2026-09-08 | TheLook matching 生成合同与任务启动 | 新增 `run_thelook_matching_evaluation.py`（仅读取 206 条问题/QuerySpec/结果合同，不读取 Gold SQL）和独立 `evaluate_thelook_matching_outputs.py`（生成完成后才读取 Gold SQL，执行 Policy、`daa_thelook_reader`、ResultValidator 与 ordered/bag denotation 对照）。固定 cases、Prompt 版本、Prompt hash、bf16 基座、seed、greedy 解码、3072/768 token 上限和 TheLook workspace；Base/Adapter 唯一变量为是否加载同一 Olist LoRA Adapter。两个 screen 已于 19:04 启动，因其他 GPU 均有外部任务，暂时共享物理 GPU 3/RTX 4090，UUID=`GPU-10863af0-8588-7625-5609-640ba794f64b`；生成进行中，尚未产生质量结论。 |
| 2026-09-08 | TheLook Base/Adapter matching 生成与评测完成 | Base/Adapter 均完整生成 206/206，case ID、Prompt 合同和解码配置一致；原始候选与安全报告在仓库外 `experiments/qwen25coder15b-thelook-matching-v1-20260908/{base,adapter}/`。独立 evaluator 随后通过 TheLook `SqlPolicy -> daa_thelook_reader -> ResultValidator` 并读取 Gold SQL 做 ordered/bag denotation 对照：Base 执行 54/206、ResultContract 23/206、ordered/bag match 均 6/206；Adapter 执行 63/206、ResultContract 63/206、ordered match 37/206、bag match 41/206。失败分类：Base policy_rejected=94、PostgreSQL execution error=58；Adapter policy_rejected=132、execution error=11。Adapter 相对 Base 在跨 schema 上显著提高可执行性、结果合同通过率和 Gold 对照，但仍有大量安全策略拒绝，不能宣称已达到通用 TheLook 泛化；完整报告位于仓库外 `.../evaluation/evaluation-report.json`。 |
| 2026-09-08 | TheLook Adapter 错误归因完成 | 对 206 条候选按格式、schema/连接、指标公式/粒度、维度归因分层。发现 Adapter 的 132 条 Policy 拒绝中 129 条带 `Selection`/`Solution`/`Query` 前缀，当前 TheLook runner 未复用统一 `unwrap_sql_completion()`，应先修清洗契约再重评；剩余真实问题集中在 AOV 未做订单级二层聚合、traffic_source 错把 events 作为订单归因、额外连接导致重复计数、时间序列错误按 delivered_at 分桶以及少量 COUNT 未 DISTINCT。分析与下一批约 1,000 条 Olist 程序族覆盖建议见 `docs/post-training/experiments/thelook-matching-error-analysis-v1.md`。 |
| 2026-09-08 | TheLook Adapter 统一前缀清洗后重评 | TheLook evaluator 复用统一 `unwrap_sql_completion()`，支持 `Selection`/`Solution`/`Query` 单行或同行展示前缀；相关回归测试 42 passed。未重新调用模型，仅对原始 206 条候选重跑后续 Policy、`daa_thelook_reader`、ResultValidator 和 Gold 对照。Adapter 清洗后执行 174/206、ResultContract 通过 171/206、ordered match 100/206、bag match 111/206；剩余 `policy_rejected=7`（6 条不存在 `event_id`、1 条不存在 `orders_users`）、PostgreSQL execution error=25、ResultContract 失败=3。规范化候选和报告在仓库外 `experiments/qwen25coder15b-thelook-matching-v1-20260908/evaluation-normalized/`，这版作为最终 Adapter 测试结果；不再把前缀格式错误计入领域数据缺口。 |
| 2026-09-08 | Olist 领域 SFT 扩展方案 v2 | 基于 TheLook 206 条跨 schema final test 的规范化结果和 Olist Medium v1 中文问题模板审计，确认下一版需同时扩大语义程序覆盖与中文 surface-form 多样性。Medium v1 的 1,200 条问题归一化后主要句式仅 108 类，高频集中在“请统计 + 时间范围 + 指标列表”；真实缺口集中在最小 Join、`traffic_source` 订单归因、AOV 二层聚合、`COUNT(DISTINCT)`、时间字段和多指标 CTE。新增 [`docs/post-training/data/olist-domain-sft-expansion-plan-v2.md`](docs/post-training/data/olist-domain-sft-expansion-plan-v2.md)：先做 20 个 family/约 100 条可审阅小批，质量门通过后再考虑约 `1600/400/400` release；未生成新数据、未启动训练、未改变生产运行时。 |
| 2026-09-08 | Olist 中文问题 surface-form 多样性审计 | 新增只读审计器 `scripts/post_training/data/audit_olist_question_diversity.py`，不读取/改写 Gold SQL、不生成新样本。对 Medium v1 的 1,200 条 runtime candidates 运行后：完全重复为 0，但日期、指标和维度别名归一化后仅 38 类模板，归一化模板重复率为 96.83%，805 条以“请统计”开头，且 1,200 个 family 各只有一个语言变体；family 结构签名未发现漂移。报告保存于仓库外 `evals/olist-domain-sft-medium-v1/question-diversity-audit-v1/report.json`，专项测试 `2 passed`、Ruff 与 compileall 通过。结论是当前扩展必须补受控中文表达多样性；下一步是生成并审阅 20-family/约 100-row 小批，不直接开始训练。 |
| 2026-09-08 | TheLook 第二电商 schema 接入与 PostgreSQL 审计 | 为验证 Olist LoRA 是否只记住当前 schema，新建独立 PostgreSQL `thelook_analytics` workspace，绝不复用 Olist 的 `data_analysis_agent`、Catalog、业务表或 reader role。服务器缺少 Google Cloud 凭据与 `bq` 客户端，故不能宣称 direct BigQuery export；冻结的是 Kaggle `mustafakeser4/looker-ecommerce-bigquery-dataset` v1 CSV mirror，并记录 archive SHA-256、七个原始 CSV SHA-256、行数和 schema dump 于仓库外 `datasets/thelook/kaggle-mirror-v1-20260908/audit/`。成功导入 10 distribution centers、100,000 users、29,120 products、125,226 orders、181,759 order items、490,705 inventory items 与 2,431,963 events。原始表位于私有 `thelook_raw`；`daa_thelook_reader` 只可读 7 个剔除姓名、邮箱、地址、邮编、IP 和用户精确坐标的 `analytics` views，原始表读取和写入均实测拒绝。审计确认 events CSV 的 `.0` user ID 可无损转换、无事件用户孤儿、无订单项孤儿，且 `orders.num_of_item` 与 item 行数完全一致。此完成数据/权限接入，不构造 Catalog、QuerySpec、Gold SQL、问题或模型评测；TheLook 继续作为 Olist-only Adapter 的 protected zero-shot cross-schema holdout。详见 [`docs/post-training/data/thelook-cross-schema-workspace-v1.md`](docs/post-training/data/thelook-cross-schema-workspace-v1.md)。 |
| 2026-09-07 | Olist Medium v1 运行时边界修复与正式 matching 重跑 | 修复 `unwrap_sql_completion()` 对 `Query`/`Query Plan`/`Code` 精确单行展示前缀的安全归一化；补齐 `ResultContract.as_evidence()` 的 `result_time_column_aliases`，runtime prompt 资产升级 v3 并重建 1,200 条（final test 240 条）。正式生成阶段 Adapter `240/240` Policy accepted、PostgreSQL executed、ResultContract valid，Base 分别为 `104/240`、`32/240`、`13/240`；Adapter 生成后 Gold ordered denotation `240/240`，Base 原始有效候选 `0/13` match，其余 227 条按合同不进入 Gold。第一阶段 Olist Medium v1 训练测评闭环完成；下一步进入扩大领域训练/验证/测试集前的错误分类和数据覆盖设计，不将本轮结果表述为生产准确率或跨数据集泛化。报告位于仓库外 `qwen25coder15b-olist-medium-base-adapter-finaltest-v2b-20260907/`。 |
| 2026-09-07 | Olist Medium v1 Base/LoRA Gold denotation 对照 | 在 Base/Adapter 生成、SqlPolicy、PostgreSQL reader role 与 ResultContract 结果物化完成后，离线读取冻结 test 的 deterministic Gold SQL；Gold 与候选均通过同一受控 runner 执行，仅对原始 ResultContract 已通过的候选做结果集对照。Adapter 有 90 条可对照样本，90/90 为有序 denotation match；Base 有 12 条可对照样本，0/12 match（11 条 denotation mismatch、1 条 column mismatch）。另有 80 条从 Base 的非通过状态转为 Adapter Gold match，2 条 Base mismatch 在 Adapter 侧未通过 ResultContract。该结果支持“Adapter 显著改善当前 Olist Prompt 约定下的可执行性/结果合同与真实结果一致性”，但不等价于全量 240 条业务准确率、跨数据集泛化或生产接入结论；未改变默认运行时。脱敏报告位于仓库外 `.../qwen25coder15b-olist-medium-base-adapter-finaltest-v1-20260906/analysis/gold-denotation-audit.json`，审计入口与测试已提交。 |
| 2026-09-04 | Olist Medium v1 领域 SFT 数据集冻结 | 将 40 条 Pilot 纠正定位为工程验收集，构建首个中等规模 Olist Candidate SQL release。仓库外 `olist-domain-sft-medium-v1` 完成 1,200 个 unique family/QuerySpec：train/validation/final in-domain test 为 `720/240/240`。构造仅读取 protected-family hash summary，碰撞为 0；确定性 `QuerySpec -> PostgreSQL Gold SQL` 后，全量通过 `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator`。40 条分层 advisory semantic review 通过；生产 Router/Catalog/QueryPlan/ResultContract Prompt 以 1,200/1,200 重建，未调用候选模型。精确 `Prompt + SQL + EOS` 审计发现旧 Pilot `2304` 不够，Medium v1 冻结为 `3072`（最大 2,915），无截断、无长度排除；test 物理隔离于 `final_evaluation_only/`。family/QuerySpec 跨 split 重叠均为 0。数据已准备好用于单独的 matching Base/Adapter 生成评测和训练配置冻结；尚未训练、评测或接入运行时。 |
| 2026-09-04 | Olist protected-family summary 受限导出器 | 新增 `scripts/post_training/data/export_olist_protected_family_summary.py` 与合成测试。导出器只读取仓库外的人工批准结构输入：当前 WorkspacePin、protected source-manifest hash、review reference 和排序去重 family IDs；严格拒绝 case/question/Prompt/SQL/result/seed 等字段、空或未排序/重复 family、workspace 漂移及仓库内路径。输出为仓库外的最小 fingerprint summary 与不含 family 原文的 hash evidence，采用 staging + 原子 rename。真实 holdout 未读取、真实 summary 未导出，未调用 materializer/renderer/数据库/tokenizer/GPU。专项及 QuerySpec/materializer 回归 `59 passed`，ruff、compileall、diff check 与 CLI help 通过。下一项须单独设计 evidence 绑定的小批 Gold 准入/物化，不能直接生成训练 JSONL 或训练。 |
| 2026-09-04 | Olist 静态 QuerySpec coverage seed 清单冻结 | 新增 `data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl`，共 15 条仅结构化 seed，覆盖十项指标、四种允许结果形状和三类 split；配套设计说明、任务卡和结构测试。测试只验证字段白名单、QuerySpec/family 唯一性、Join program 不跨 split 与覆盖分布，不调用 renderer/materializer、不生成 Gold/Prompt/训练 JSONL、不执行 SQL、不读取 protected holdout。由于当前只有 11 个 Join program 且禁止跨 split，清单仅是结构合同审阅样本，不能作为最终训练/评测集或准确率证据。受限 protected-family summary 导出器已随后实现；真实 summary 导出并获批准后，下一项才是小批 Gold 准入/结构物化。 |
| 2026-09-04 | Olist QuerySpec 受控批量物化器实现 | renderer 审计先补齐一个 P0 快照门：显式传入的 Catalog 也必须与 QuerySpec 的 Catalog/dataset/metric/policy 版本一致，不能只按指标名称放行。新增 `scripts/post_training/data/materialize_olist_queryspecs.py` 与合成测试。脚本只接收显式 split 的结构化 coverage seed 和仅含 `family_id` fingerprint 的 protected summary；它用 `create_validated -> validate -> render -> hash/duplicate/family/protected 检查` 生成仓库外 `query_specs.jsonl`、`gold_sql.jsonl`、rejections 和 manifest。family 不含具体日期或措辞；同一 family/QuerySpec/SQL hash 禁止重复，Join program 允许同 split 复用但不能跨 split。输出使用 staging + 原子 rename，seed 中混入 question/未知字段、敏感维度、归因、重复、保护碰撞或 renderer hash 漂移均拒绝并审计。未运行任何真实 Olist seed，未读取 protected holdout、执行 SQL、构造 Prompt/训练 JSONL、加载 tokenizer 或启动 GPU。QuerySpec/物化器专项 `53 passed`，相关回归 `169 passed`，ruff/compileall/diff check 和 CLI help 通过。下一步只能先设计并审阅小型结构化 coverage seed 与 protected family summary，不能直接大规模物化或训练。 |
| 2026-09-03 | Olist QuerySpec 轻量加固与批量物化器设计 | 保留 `QuerySpec.create()` 与显式 validator 分离，新增 `create_validated()` 作为不易误用的构造入口；renderer 的时间序列校验现在要求所有指标共享同一 canonical 物理时间字段，分组键表达式集中为固定常量并测试明确的 NULL 排除。新增 `plan/feature-olist-queryspec-materializer-v1.md`，仅设计后续如何从 coverage seed 生成和审计 QuerySpec/Gold 中间产物：先 canonicalize -> create_validated -> validate -> render -> hash/key/duplicate/holdout-summary 检查 -> 原子写出；不得在本阶段物化数据、执行 Gold SQL 或启动训练。专项 `45 passed`，相关运行时回归 `161 passed`，ruff/compileall/diff check 通过。 |
| 2026-09-03 | Olist QuerySpec 与 deterministic PostgreSQL Gold renderer 实现 | 在已冻结的十指标合同和 coverage v2 上新增 `src/data_analysis_agent/olist_queryspec.py` 与 `tests/test_olist_queryspec.py`。离线 QuerySpec 以冻结 dataclass/canonical JSON hash 固定 ID；validator 对 workspace/prompt/version、指标数量与唯一性、结果形状、半开时间范围、Join 程序、派生结果列、归因和敏感维度 fail closed。只读注册表固定十项指标的事实粒度、过滤、时间字段和表达式；renderer 只编译静态片段，AOV 先订单内聚合，多指标先独立 CTE 再组合，证据仅保存版本、ID、指标、程序、列和 SQL hash。renderer 不调用 LLM、数据库、SqlPolicy、reader role 或 ResultValidator；后者仍是 Gold 的后续独立准入。专项 `43 passed`，相关运行时回归 `159 passed`，ruff/compileall/diff check 通过。尚未生成 QuerySpec/训练 JSONL、读取 protected holdout、执行 Gold SQL、运行 reader-role/ResultContract/人工语义审核或启动训练；下一步必须先由用户审阅实现与测试，不能直接物化数据。 |
| 2026-09-03 | Olist 跨层预检与 P0 合同对齐 | 在 Olist 领域 QuerySpec/Gold SQL 物化前完成 Catalog、Router、QueryPlan、SQL Policy、PostgreSQL runner 与 ResultContract 的运行时收口。`seller_id` 从 analyst 可展示维度删除，CatalogLoader 拒绝将敏感物理列声明为展示维度；十个指标声明基础值域，ResultValidator 现在拒绝合同外列、负金额/时长/计数、非整数计数、范围外比例或评分。Router 在调用模型前拒绝超过四项的指标请求、未支持展示维度、无绝对范围的按日序列和保守形状超过 analyst 200 行预算的请求，避免静默漏指标或生成无可验证结果。SecurePostgresRunner 默认启用结果校验；`PolicyDecision` 新增 `policy_limit_applied`，只有 Policy 实际补加/收紧并命中行上限才标记可能截断，修复了显式 `LIMIT 5` / `LIMIT 1` 被误拒绝的缺口。受保护 v1 `text_to_sql_v2` 不修改；以 v2 回放为 `57/60`，两条品类订单归因未冻结、一条州×月形状 `27×36=972` 超 200 行预算，均作为历史合同迁移差异而非 v2 分数。验证：确定性合同专项 `116 passed`，真实 PostgreSQL runner/run recorder `8 passed`，ruff、compileall、diff check 通过。仍不能由这些检查证明指标公式、任意 join 放大、归因规则或模型 SQL 语义正确；下一项才可单独实现并审阅 QuerySpec schema、验证器、指标表达式注册表和 deterministic renderer。 |
| 2026-09-02 | Olist 领域 Candidate SQL SFT v1 覆盖矩阵冻结 | 基于当前 4 指标 Catalog、7 条 Join、QuestionRouter、QueryPlan 和归因策略，冻结 `docs/post-training/data/olist-domain-sft-coverage-matrix-v1.md`。矩阵区分“Router 能路由”和“可形成稳定 Gold”：只将标量、确认时窗、每指标自身时间字段序列、客户州/城市、GMV 按品类/卖家，以及多指标 CTE 标量/州级/同购买时间序列纳入后续 renderer 设计；支付归因、订单/评价粒度穿过商品行的非天然归属、Top-N、自由筛选、相对时间、比较/因果和非 SQL 路由均保持排除。规划目标为 train/validation/in-domain test `3,000/460/460` 行、至少 162 个语义 family，行数不等于独立能力。未创建 QuerySpec、Gold SQL、训练 JSONL 或运行 GPU。下一项仅设计 QuerySpec schema 和确定性 PostgreSQL Gold renderer 的责任边界。 |
| 2026-09-02 | Olist 领域 Candidate SQL SFT v2 覆盖矩阵冻结 | 基于 `olist-catalog-v2` / 十项业务指标合同，新增 `docs/post-training/data/olist-domain-sft-coverage-matrix-v2.md`，不改写四指标 v1 历史快照。十项均纳入标量、确认时窗、自身时间字段序列和客户州；客户城市因高基数输出的排序/截断合同缺失而暂缓。该初版将 `gmv`、`item_count`、`freight_amount` 列为按品类/卖家可聚合；其中卖家部分随后在 2026-09-03 经现有 SQL Policy 敏感投影约束核对后撤回，当前矩阵只保留品类。订单和评价粒度在品类、卖家、支付方式下的归属/分摊问题仍需澄清或排除；多指标只纳入标量、州级、购买时间八指标序列或评价时间两指标序列，禁止混合时间字段的序列。初版的 `4,640/690/690` 与 340 family 配额也随卖家桶撤回而被后续 `4,460/665/665`、330 family 替代。未生成任何 QuerySpec、SQL、JSONL 或训练任务。 |
| 2026-09-02 | Olist 领域 Candidate SQL SFT 数据合同冻结 | 针对 CSpider Adapter 在当前中文、PostgreSQL、业务指标合同输入上的迁移退化，冻结 `docs/post-training/data/olist-domain-sft-data-contract-v1.md`：真实运行时 Prompt 是唯一训练输入；Gold 必须由受控 QuerySpec 和 Catalog 生成并通过 Policy、reader role、ResultContract 与人工口径审查；同一语义/程序族不得跨 train/validation/test，60 条 protected holdout（含当前迁移评测边界）不读原文、不作构造种子；`1536` 为无截断待审计上限。设计规模目标为约 3,000/400--500/400--500 materialized rows，但必须同时报告 QuerySpec、family 和行数，不能用模板改写虚增。未生成数据、未启动 GPU 或训练，也未变更生产 Vanna/SiliconFlow 路径。下一项仅盘点 v1 coverage matrix。 |
| 2026-09-03 | Olist QuerySpec 与 Gold renderer 设计冻结 | 新增 `docs/post-training/data/olist-queryspec-renderer-design-v1.md`，把十项指标从底层事实到训练目标的关系拆成 `QuerySpec -> deterministic renderer -> canonical PostgreSQL Gold SQL`。QuerySpec 是版本锁定、无自然语言/SQL/自由过滤/排序/Limit 的离线施工图；renderer 只从固定 Catalog 与指标表达式注册表编译 SQL，不执行、不调用 LLM、不修复。v1 支持标量、客户州、商品品类单指标和同时间字段序列，卖家维度因 `seller_id` 敏感策略保持排除；最终 Gold 仍须独立通过 Policy、reader role、ResultContract 和人工语义审核。更新十指标 coverage 最大规划为 `4,460/665/665` 行、330 family。未实现 renderer、未生成数据、未启动审计或训练。下一项仅可实现并审阅该确定性组件与单元测试。 |
| 2026-09-02 | CSpider LoRA Adapter Olist 业务迁移评测 | 在 12 条永久 protected Olist `answerable` holdout 上，只重跑最终 CSpider bf16 LoRA Adapter，并复用合同字段完全匹配的 2026-09-01 中文 bf16 Base 脱敏报告作为 matching Base。两侧模型 revision、bf16、提示 `olist-candidate-sql-v1`、greedy decode、seed、token 上限、案例 ID、Catalog/策略版本、PostgreSQL 方言和禁用 SQL repair 均一致。CSpider Adapter 结果为生成 `12/12`、Policy `2`、PostgreSQL executed `1`、ResultContract valid `0`；Base 为 `12/12`、`6`、`4`、`2`，变化为 `0/-4/-3/-2`。无 `non-valid -> valid`，两条 Base 合同有效结果分别变为 policy rejection 和 result-contract rejection。该结果表明 CSpider validation 的离线正向证据未迁移到当前中文 Olist/PostgreSQL 业务候选链路；不接入默认 Vanna/SiliconFlow 路径、不读取 CSpider final test、不启用 SQL repair。安全报告、对照报告、候选 SQL 和日志均在仓库外；外部数据库快照未单独 hash，因此结论仅对报告中冻结的工作区版本和合同负责。详见 `docs/post-training/experiments/cspider-bf16-lora-2epoch-v1.md`。 |
| 2026-09-02 | CSpider matching Base/Adapter 完整离线评测完成 | 两侧均完成 official validation `1,034/1,034` 生成并通过 matching verifier。Base（physical GPU 3，RTX 4090）SQLite 为 `911 executed / 7 policy rejected / 116 errors`；Adapter（physical GPU 0，RTX 3090）为 `932 / 0 / 102`。生成冻结后 bounded denotation exact-or-bag match 为 `525 -> 743`（`50.77% -> 71.86%`），严格有序为 `507 -> 725`；289 条不匹配/不可执行变匹配，71 条匹配退化，净增 218。Adapter 平均生成 token `108.29 -> 29.61`，但不同 GPU 使耗时不可比较。该结果是当前 CSpider validation SQLite 快照上的正向离线证据，保留明确回退和错误类别；不构成官方榜单、跨数据集泛化、Olist/PostgreSQL 业务正确性或生产接入结论。CSpider final test 未使用，生产运行时未修改；完整证据均在仓库外 pair 目录。 |
| 2026-09-02 | CSpider matching Base/Adapter 真实生成启动 | 经 GPU 检查后，按用户要求以两个独立 `screen` 并行启动完整 CSpider validation 的 Base/Adapter 生成：`daa-cspider-base-eval-v1` 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090（UUID `GPU-10863af0-8588-7625-5609-640ba794f64b`），`daa-cspider-adapter-eval-v1` 使用 logical CUDA `2` -> physical GPU `0` 的空闲 RTX 3090（UUID `GPU-24fc79b9-0cb5-8a75-17de-188e04e0690b`）。两侧均以同一 CSpider validation、基座 revision、bf16、prompt v2、greedy decode、seed 和 token 上限生成，均通过 acquisition 输入预检后开始写入仓库外 prediction JSONL；final test 没有传入。两卡差异只影响耗时/吞吐，本轮及后续仅比较冻结 SQL、SQLite 状态与 denotation，不把时间指标作为 Base/Adapter 对照结论。当前运行中，不报告 SQL 可执行性、语义、优劣、质量指标或生产接入结论；两侧结束后必须先运行 matching verifier。 |
| 2026-09-02 | CSpider matching Base/Adapter 生成与评测入口 | 将离线生成器、SQLite 诊断和生成后 denotation 显式扩展为 `cspider_validation` namespace。生成前强制校验 CSpider acquisition manifest（`dev=validation_only`、`dev.json`/`tables.json` hash、行数），生成不读取 gold SQL、SQLite 行、final test 或业务运行时。新增 matching verifier：Base/Adapter 必须有相同的模型 revision、bf16、prompt、input/new token 上限、greedy decode、源 hash、完整 1,034 source-order case coverage，且唯一变化为 adapter disabled -> enabled；验证报告不写问题、SQL、库标识或结果行。新增同卡顺序 screen launcher，在 verifier 成功后才执行 SQLite、paired analysis 和 denotation，且不调用 Spider Test Suite。15 项本地回归、ruff、CLI、shell syntax、diff check 通过；本轮未启动完整生成、未读取 CSpider final test，也没有 SQL 质量或生产接入结论。 |
| 2026-09-02 | CSpider bf16 LoRA 两 epoch 训练完成 | 冻结的 CSpider `8,574/1,034` train/validation 在 logical CUDA `1` -> physical GPU `3` 的 RTX 4090 上正常完成两 epoch（`4,288` steps），final test 未传给 runner。bf16 LoRA、batch `4`、AdamW `1e-4`、weight decay `0.01` 的峰值 allocated/reserved 为 `15.35/23.16 GiB`，无 OOM；末尾 full validation loss `0.318318`。最终 adapter 已保存并在独立进程 fresh reload，通过 UUID guard 且得到 finite validation forward loss。validation loss 最低点为 step `1,072` 的 `0.278697`，末尾高 `14.22%`，因此不能把两 epoch 或 loss 直接表述为 SQL 质量提升；CSpider matching Base/Adapter 生成、SQLite/denotation 与 final test 尚未运行。 |
| 2026-09-02 | CSpider 两 epoch bf16 LoRA 训练启动 | 合同提交 `24ea9dc` 后，实际检查 physical GPU `3` 空闲、UUID 为 `GPU-10863af0-8588-7625-5609-640ba794f64b`，以 screen `daa-cspider-bf16-lora-full2epoch-v1` 启动。日志确认 train/validation 输入与 logical CUDA `1` -> physical GPU `3` 映射，Trainer 已进入真实训练（启动后观察到 step `42/4,288`、约 16.3 GiB 显存、83% GPU 利用率），未发现 OOM、UUID 或数据合同失败。本项仅说明任务开始，完成前不报告 loss 趋势、SQL 质量或 Base/Adapter 差异；结束后先核验 reload/artifact，再单独实现 CSpider 成对评测入口。 |
| 2026-09-02 | CSpider 两 epoch 训练与 matching Base/Adapter 评测合同 | 冻结 `cspider_bf16_lora_2epoch_v1`：仅用长度物化 train `8,574` 做两 epoch（预期 4,288 optimizer steps），validation `1,034` 只作 loss 与成对生成评测，final test `2,147` 禁止训练、选择、prompt 示例和本实验。训练固定 bf16 LoRA、batch `4`、accumulation `1`、AdamW `1e-4`、weight decay `0.01`，并用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090 UUID guard。Base/Adapter 唯一变量是 adapter 是否加载；双方只看相同 validation schema/question，gold SQL 仅可在生成冻结后做受限等价性审计。当前合同已通过 YAML/CLI/shell 与 13 项数据训练回归检查，尚未启动训练；现有 Spider-only generation runner 必须先适配并测试 CSpider 后才执行成对质量评测。 |
| 2026-09-02 | CSpider bf16 LoRA batch-4 冒烟 | 在正式物化的 CSpider 8,574/1,034 train/validation 上完成 `max_steps=1` 的未量化 bf16 LoRA 冒烟：`batch=4`、`accumulation=1`、普通 AdamW、`weight_decay=0.01`。logical CUDA `1` -> physical GPU `3` 的 RTX 4090 完成真实 batch forward/backward、一次 optimizer update、全 validation loss、adapter 保存和新进程 PEFT reload；训练峰值 allocated/reserved 为 10.76/11.62 GiB，未 OOM。该结果只证明工程与显存可行，不说明 SQL 语义、泛化或业务迁移质量；完整两 epoch 及 Base/Adapter 成对评测仍需单独确认。 |
| 2026-09-02 | CSpider 正式长度物化与训练入口配置审阅 | 按 `cspider-token-length-v1` 在仓库外物化 `official-splits-length1536-v1`：train 8,574、validation 1,034、final-evaluation-only test 2,147；train 超长 82 条进入不含问题/SQL 的 exclusion manifest，test 完整保留且最大 996 token。新增物化器与训练入口回归。正式默认改为未量化 bf16 LoRA、普通 `adamw_torch`、`weight_decay=0.01`、`per_device_train_batch_size=4`、`gradient_accumulation_steps=1`（真实 batch 并行，effective batch 4）；QLoRA/8-bit optimizer 仅保留历史兼容入口。本轮只构造和审阅配置，未启动训练或读取 test 正文。 |
| 2026-09-02 | CSpider 训练长度合同 v1 | 新增只读 token 审计器，精确复用 `CausalSqlDataset` 的 `prompt + SQL + EOS`、`add_special_tokens=false` 计数布局，padding 不计入单样本预算。真实 CSpider train/validation 的 sequence p99 分别为 1,438/824，max 为 3,228/850；`1536` 保留 8,574/8,656 train（99.05%）与全部 1,034 validation。`2048`、`3072` 都不增加可入选行，因此冻结 `max_seq_length=1536`。脚本和合同禁止静默截断；后续必须单独物化外部派生 JSONL/exclusion manifest，并保持官方角色、hash、schema 隔离和 test 禁用。本轮 `12 passed`，未读取 test、加载权重、使用 GPU、训练或评测。 |
| 2026-09-02 | CSpider split-audit 契约适配 | Trainer 的 `validate_split_audit()` 从硬编码 Spider 两切分改为 fail-closed 的显式双策略校验。历史 Spider 继续要求 `spider_db_id` 与 v2 holdout；CSpider 仅接受 `official_cspider_train_dev_test`，并要求 `cspider_db_id`、官方 train/dev/test 角色、三组零 schema overlap、test 禁训标记、`final_evaluation_only` 路径、当前 train/validation 的输出路径、行数和 SHA-256，以及 SQLite `EXPLAIN` 通过数与 SFT 行数一致。专用环境 7 项 SFT 契约测试通过，真实 CSpider audit 已以 train/validation 成功验证；未加载 tokenizer/模型、未使用 GPU、未训练或评测。下一步应单独执行 token 长度分布审计，test 继续保持不可用。 |
| 2026-09-01 | CSpider 官方三切分 SFT 输入构造 | 新增可直接执行的 CSpider JSONL 构造器，核验 acquisition manifest、源文件/解压树 hash、官方角色/数量、字段/表元数据、schema 无交集，并对每条 gold SQL 只读 `EXPLAIN`。外部输出为 train `8,656`、validation `1,034`、final-evaluation-only test `2,147`，三份文件 hash 均与 audit 一致；官方 train 的 3 条 SQLite/schema 不一致 gold 被写入可审计 exclusion，不静默混入参数更新。专项测试 `3 passed`、ruff、py_compile 和 diff check 通过；未加载 tokenizer/模型、未使用 GPU、未训练或评测。下一步需单独审阅并适配 Spider-only Trainer 的 `cspider_db_id`/官方 test 隔离契约。 |
| 2026-08-31 | 项目进度与后训练学习文档职责分离 | 明确项目微调/后训练阶段、训练数据、GPU 任务、实验配置、评测结果、风险和下一步统一记录在本项目文档及 `PROJECT.md`；飞书《数据分析 Agent｜微调与后训练学习笔记》只记录后训练原理、代码审阅、用户问答和面试知识。同步更新 `AGENTS.md`、`docs/README.md` 与 `docs/post-training/README.md`，本轮不启动训练、不修改生产运行时。 |
| 2026-08-31 | 文档导航与后训练学习材料重组 | 新增 [`docs/README.md`](docs/README.md) 作为项目文档总导航，明确项目方案、运行时、评测证据、后训练学习/数据/实验和根目录兼容入口的职责。保留历史评测报告与旧链接，不为减少文件数批量移动长文；物理迁移须单独立项并做链接检查。飞书《数据分析 Agent｜微调与后训练学习笔记》重写为“项目进度”和“后训练学习”两部分，同步当前 Spider/Olist 结论、LoRA/PEFT 学习记录和 `forward -> loss` 的下一道审阅题。后续项目事实写入本文，后训练阶段写入 `docs/post-training/README.md`，学习问答写入 `learning/`，实验事实写入 `experiments/`。 |
| 2026-08-30 | 后训练数据边界与 SFT 入口加固 | 修复候选构建在 `selection-overfetch` 遇到被跳过的坏行时错误计算预期候选数的问题；SFT 入口新增 `validate_split_audit()`，在加载模型前核对 train/validation 文件的行数、SHA-256、`spider_db_id` 分组策略、通过状态和 holdout 标记，文件被替换或审计字段缺失时 fail closed。新增 2 个回归测试；数据/格式/切分专项 `7 passed`，SFT Dataset/审计专项 `3 passed`，ruff 与 compileall 通过。本轮不启动 GPU、不构造数据、不改变模型或生产运行时。 |
| 2026-08-30 | SFT 入口代码审阅回归 | 用户注释改动覆盖了 collator 的 `batch.items()` 和 split audit 完整校验；恢复两处行为契约，并修正 `CUDA_VISIBLE_DEVICES` 审计字段拼写。`7 + 3` 项后训练专项通过，ruff、compileall、diff check 通过。确认 padding 使用 `attention_mask=0`、`labels=-100`，EOS 是 SQL 目标结束并参与监督。本轮未启动 GPU、未构造数据、未训练或评测；下一步只审阅模型加载与 PEFT 生命周期。 |
| 2026-08-31 | 后训练代码问答同步与 LoRA 注入审阅 | 用户完成 `get_peft_model`/LoRA 配置复述：A/B 参数量为 `r * d_in + d_out * r`；`r` 控制 adapter 容量，`lora_alpha` 控制增量缩放，学习率控制更新步长；`target_modules` 命名不匹配可能造成未注入或部分注入。新增规则：每次学习问答同步本地学习记录和飞书，并保留用户复述、纠正点、代码入口与证据边界。本轮未修改训练逻辑、未启动 GPU。 |
| 2026-08-30 | AI 协作与工程主导权规则 | `AGENTS.md` 新增通用 AI 协作规则：用户负责问题定义、非目标、架构/数据/安全边界和验收结论；每项实质变更先写目标、非目标、输入、输出/接口影响、不变量、验收证据的最小任务卡；代码按模块地图、契约、风险升级逐层审阅，不要求无差别逐行通读。AI 的实质变更必须交付可审计变更包（影响、测试空白、失败/回滚条件），测试/loss/单次成功不替代业务语义、泛化或上线结论。本轮只更新协作规则，不运行训练、评测或生产链路。 |
| 2026-08-30 | 项目目录地图与后训练代码审阅门 | 新增 [`docs/architecture/repository-map.md`](docs/architecture/repository-map.md) 区分上游 Vanna、本项目 `data_analysis_agent`、Web Component、数据/评测/基础设施、离线后训练脚本和 Git 忽略调研产物；新增 [`docs/post-training/learning/code-review-guide-v1.md`](docs/post-training/learning/code-review-guide-v1.md)，要求后训练从 Spider prompt/候选构建/切分开始按“真实文件 + 配套测试 + 用户复述 + 审阅证据”推进。`AGENTS.md` 增加代码审阅门：概念讲解或测试通过不等于理解/审阅，不在共同审查接口、依赖与回归范围前为了目录美观迁移模块。本轮不构造领域数据、不训练、不评测、不改变生产运行时；`github-research-output/` 是 Git 忽略的大型本地调研产物，后续另行确认是否迁出仓库根目录。 |
| 2026-08-28 | 后训练学习问答、目录分层、领域数据合同与基础覆盖矩阵 | 将截至本轮的共同学习整理为 [`docs/post-training/learning/review-2026-08-28.md`](docs/post-training/learning/review-2026-08-28.md)：明确候选 SQL 与执行边界、gold SQL/holdout 隔离、schema-disjoint、labels/EOS、forward smoke、梯度累积、validation/test、LoRA/QLoRA、PEFT 生命周期及 Olist/PostgreSQL 领域数据合同。后训练文档以 [`docs/post-training/README.md`](docs/post-training/README.md) 为规范入口，按 learning/data/experiments/archive 分类；旧根目录路径保留兼容页。离线后训练脚本的实现归入 `scripts/post_training/{data,training,inference,evaluation,launchers}`，根目录保留兼容入口；生产运行时模块、原始数据、模型、Adapter 和实验产物不迁移、不提交。新增 [`docs/post-training/data/olist-pilot-coverage-v0.1.md`](docs/post-training/data/olist-pilot-coverage-v0.1.md) 作为待确认设计：v0.1 只做四个核心指标的单指标、安全维度、单轮显式时间覆盖，排除多指标、排名、多轮、支付归因和按品类的履约/好评率。先用 18 个覆盖单元和约 120--160 个 materialized rows 审查数据合同；用户确认后才考虑扩到约 300--500 条基础 pilot。新数据构造、新训练、完整评测与运行时接入继续暂停。 |
| 2026-09-04 | Olist Pilot v1 正式领域 SFT 数据集 | 将 40 个 QuerySpec family 的完整 Gold admission、中文问题、生产运行时 Prompt 重建和 token 审计连续收束为第一版正式外部数据资产：train/validation/in-domain-test 为 `24/8/8`，family 与 QuerySpec 跨切分 overlap 均为 0，in-domain test 物理隔离且禁训。40/40 Gold 均通过 `SqlPolicy -> daa_analytics_reader -> ResultValidator/ResultContract`；两个 DeepSeek advisory 口径疑点均依据冻结十指标合同，以 QuerySpec ID 与 Gold SQL hash 绑定后人工批准。实际 Olist `Catalog + QueryPlan + ResultContract` Prompt 的最大精确序列长度为 2,076，故独立冻结 `2304` 无静默截断合同（CSpider 的 1,536 不适用）；最终长度排除为 0。Trainer 在加载模型前校验 Olist PostgreSQL 执行证据、运行时 Prompt 与 SQL 的精确拼接、family 隔离、test 存储和 2,304 合同。专项测试、外部审计回读、`compileall`、ruff 与 diff check 通过；未启动 GPU 或训练。详细数据合同见 [`docs/post-training/data/olist-pilot-v1-sft-data.md`](docs/post-training/data/olist-pilot-v1-sft-data.md)。该 Pilot 只有 24 个训练 family，只证明数据工程与后续显存 smoke 前提，不证明模型能力、业务准确率或电商泛化。 |
| 2026-08-27 | 后训练学习审查协作门 | 暂停扩充通用 Spider、启动新训练和 Olist 运行时接入。新增 [`docs/post-training-learning-walkthrough-v1.md`](docs/post-training-learning-walkthrough-v1.md)，将环境、模型冻结、数据构建、schema 切分、tokenizer/labels、LoRA/QLoRA、SFT、Adapter 重载、候选生成和多层评测按真实代码逐节解释。`AGENTS.md` 新增先讲解、先读后跑、每次只推进一个学习单元、用户确认后才执行长任务的协作门；后续不再自动连续启动 GPU 实验。 |
| 2026-08-26 | Olist PostgreSQL 业务迁移候选 SQL 质量门 | 新增受控离线 Base/Adapter runner、12 条永久 holdout manifest、脱敏 paired analyzer 与 detached screen 启动器。每条模型候选均复用 QuestionRouter、Semantic Catalog、QueryPlan、ResultContract、SqlPolicy、PostgreSQL reader role 和 ResultValidator；禁用 SQL repair，不改 Vanna/SiliconFlow 默认模型。最终 Base/Adapter 均为 12/12 generation、Policy `6 -> 6`，PostgreSQL executed `4 -> 2`，ResultContract valid `2 -> 0`，0 条无效变有效、2 条有效变无效。结论是 Spider SQLite 的离线提升未迁移到当前中文 PostgreSQL 业务上下文，业务迁移子门未通过；下一步先构建与 v2 60 条 holdout 严格隔离的 Olist-domain PostgreSQL train/validation 资产，不盲目扩大通用 Spider 或接入运行时。截断 SQL 的 `sqlglot.TokenError` 现与 `ParseError` 一样 fail closed 为可审计 PolicyViolation；QLoRA Python 3.11 环境还修正了不可变映射 dataclass 默认值兼容性。完整结论见 [`docs/post-training-olist-business-transfer-evaluation-v1.md`](docs/post-training-olist-business-transfer-evaluation-v1.md)，原始候选 SQL、结果行、模型、Adapter 和日志继续留在仓库外。 |
| 2026-08-26 | Spider SFT v2 bf16 LoRA 全量 Base/Adapter 诊断闭环 | 官方 Spider 1.0 dev 的 Base 与 3,600 条 train-only 候选训练出的 bf16 LoRA Adapter 均完成 `1,034/1,034` 生成、只读 SQLite diagnostics、固定 commit Test Suite bridge 和生成冻结后的全量 bounded denotation audit。SQLite executed 为 `950 -> 961`，Test Suite internal all 为 `0.507 -> 0.667`，denotation exact-or-bag 为 `570 -> 708`；213 条非匹配变匹配、75 条匹配变非匹配。结论为离线候选生成质量门通过、运行时接入延后；不宣称官方排行榜或生产业务准确率，不替换 Vanna/PostgreSQL 运行时安全边界。新增非 UTF-8 SQLite 文本审计回归修复与完整分析文档 [`docs/post-training-spider-sft-v2-full-analysis.md`](docs/post-training-spider-sft-v2-full-analysis.md)，原始 SQL、问题、数据行、模型和日志仍在仓库外。 |
| 2026-08-26 | Spider SFT v2 训练完成与独立 smoke 质量合同 | 3,048 条官方 train-only 样本的 2 epoch bf16 LoRA 已完成 `1,524` optimizer steps，训练/eval loss 为 `0.141485/0.222317`，74 MB adapter fresh PEFT reload 的 validation loss 为 `0.235857`；这些仅证明训练工程。官方 dev 前缀 100 条在相同 v2 prompt 下，SQLite executed 为 Base/Adapter `94 -> 89`、`no_such_column` 为 `5 -> 11`，按预设执行护栏失败；但输出冻结后、只读 gold 的 bounded denotation audit 为 `56 -> 69` exact-or-bag match，说明 SQLite 可执行不等于查询语义。因为该前缀只覆盖 3 个 schema，未据此放行全量评测。新增可复现 schema-stratified smoke 构造器与无原始 SQL 输出的 denotation 审计器；独立复验排除前 100 条及其出现的全部 schema，以固定 seed 从剩余 17 个未观察 schema 取 164 条，并预先冻结“语义净提升 + executed 回退不超过 5% + no_such_column 不超过 Base 两倍”的质量门。任一失败则优先评估通用 identifier repair 或训练改造，不进入 Test Suite/生产接入。 |
| 2026-08-26 | Spider SFT v2 训练与 Base smoke 启动 | 在数据门通过后，启动两条隔离 `screen` 任务：`daa-qwen15b-spider-sft-v2-train` 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090，对 3,048/552 schema-disjoint split 运行 2 epoch bf16 LoRA（effective batch 4、`lr=1e-4`、LoRA r16/alpha32/dropout0.05），结束后执行 fresh PEFT reload；`daa-qwen15b-spider-sft-v2-base-smoke` 使用 logical CUDA `0` -> physical GPU `2` 的 RTX 4090，在同一 v2 prompt/greedy decode 下对官方 dev 前 100 条做只读 SQLite diagnostics，并显式跳过 Test Suite。两个进程均通过 UUID guard，未抢占 3090 或其他任务。该记录仅表明任务已启动，完成并比较 Adapter 100-case smoke 前不产生训练效果或质量结论。 |
| 2026-08-26 | Spider SFT v2 规模化数据与训练基线 | 为避免沿用 102 条 v1 smoke 的覆盖不足和不完整 FK 表达，新增版本化 `spider-sft-schema-question-sql-v2`：Schema 以 `table.column`、列类型、PK 和 fully-qualified FK 序列化；v1 保持不变以保护历史评测可复现。官方 train-only 构造出 3,600 条样本，先做只读 `EXPLAIN`（排除 3 条不兼容 SQL），再做 Qwen 1.5B tokenizer 的 1,536 token 硬预算（排除 29 条，最大保留 1,443），得到 3,048 train / 552 validation、118/21 schema-disjoint split。6 个通用 SQL text shape 跨 schema 重叠仅记录，不误当 schema leakage。主实验固定为单张 RTX 4090 上的 2 epoch bf16 LoRA，先做 100-case SQLite smoke，再决定是否运行完整 1,034-case/Test Suite；不进入 DPO/GRPO 或生产接入。详细计划见 [`docs/post-training-spider-sft-v2-plan.md`](docs/post-training-spider-sft-v2-plan.md)，外部资产哈希见 `evals/manifests/post_training_candidates_v2.yaml`。 |
| 2026-08-26 | 官方 Spider release Base/Adapter 成对评测完成与质量门判定 | 官方 `dev.json` 1,034/1,034 的 Base 与 26-step QLoRA Adapter 均完成生成、只读 SQLite 诊断、固定 Test Suite bridge 和安全 paired analysis。Base/Adapter SQLite executed 为 `829 -> 671`，execution error 为 `201 -> 360`，Test Suite internal all 为 `0.433 -> 0.376`；`no_such_column` 为 `182 -> 322`，其中限定列引用为 `15 -> 296`。Adapter 生成 token 从 `123.86` 均值降至 `36.9`，但规范化 SQL 中位长度由 `86` 增至 `121`。结论：本配置质量门失败，不能进入 DPO/GRPO 或生产接入；下一步使用官方 `train_spider.json` 做 1,000--3,000 条 schema-stratified train-only 扩展，并先升级 fully-qualified Schema prompt v2。完整聚合报告留在仓库外，专属分析见 [`docs/post-training-official-base-adapter-analysis-v1.md`](docs/post-training-official-base-adapter-analysis-v1.md)。原始预测、SQL、gold、结果行和日志未提交 Git。 |
| 2026-08-26 | 官方 Spider release Base/Adapter 重新评测启动 | 评测脚本新增显式 `DATASET_VERSION` 和 `MODEL_ID`，保留历史 Kaggle 默认值以保证旧实验可复现。官方数据 2-case smoke 通过：`dev.json`/`tables.json` 哈希与外部 manifest 一致，Qwen 1.5B 4-bit NF4 在逻辑 CUDA `1` -> 物理 GPU `3`、UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` 上通过守卫。随后在物理 GPU `2` 的 RTX 4090（逻辑 CUDA `0`、UUID `GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52`）启动已 fresh-reload 验证的 26-step QLoRA Adapter。Base screen 为 `daa-qwen15b-base-official-v1`，Adapter screen 为 `daa-qwen15b-adapter-official-v1`，两者使用官方数据和同一评测合同，输出分属独立仓库外目录；完成前不发布分数。 |
| 2026-08-25 | 官方 Spider 数据包获取与版本隔离 | 通过用户本地 VPN 临时代理直接下载官方 Google Drive `spider_data.zip`，归档为 **205,800,266 bytes / SHA-256 `00636695...5b121b`**，`unzip -tq` 和路径安全校验通过；只解压 `spider_data/` 到新的仓库外目录。官方核心哈希为 `dev.json=30d64a...758a4`、`tables.json=61bb20...9e2ed`，数据库树为 166 files / `67d29c...a5aba`，20 个 dev 数据库树为 `deef29...175ca`。官方 dev/train 虽与历史镜像同为 1,034/7,000 条，但仅 933/1,034 与 6,327/7,000 有序记录完全相同，历史预测与分数不得迁用。Test Suite 的官方 dev 库 20/20 存在且 schema mismatch 为 0；尚未为官方包运行新的完整生成或 evaluator，因而没有官方包分数。原始数据、SQLite、gold SQL、预测、模型与日志均未提交 Git。 |
| 2026-08-25 | 26-step LoRA/QLoRA matching Base/Adapter 全量评测启动 | 在启动前完成 bf16 LoRA adapter 的 2-case fresh-load 验证：`base_weight_mode=bf16_lora`、PEFT adapter 挂载和 physical GPU 3 UUID guard 均通过。随后启动两条独立 `screen`：`daa-qwen15b-qlora26-eval-v1` 在 logic CUDA 0 -> physical 2 RTX 4090 上复用已冻结的 4-bit Base、生成 26-step QLoRA adapter 的 1,034 条候选；`daa-qwen15b-bf16lora26-eval-v1` 在 logic CUDA 1 -> physical 3 RTX 4090 上先生成 bf16 Base、再生成 bf16 adapter。每条会顺序运行只读 SQLite diagnostics、固定 commit 的未修改 Spider Test Suite bridge 和无 SQL/问题/库名的 paired analyzer；原始预测、报告、模型和日志全部留在仓库外。进程内 UUID guard 防止逻辑 CUDA 编号误映射，未停止或抢占其他用户 GPU 进程。当前仅记录“评测运行中”，不产生质量、执行率或语义结论；完成后才判定 non-regression 并抽查 changed cases。 |
| 2026-08-25 | 后训练材料重组与 26-step LoRA/QLoRA 覆盖度实验 | 新增后训练总览、独立学习指南和实验台账，明确将产品运行时的 Vanna/FastAPI/PostgreSQL 可信链路与 Spider SQLite 离线候选模型研究分离；历史笔记保留为参考，不再混入实时结论。SFT runner 与 adapter reload validator 同时支持 `qlora_4bit` / `bf16_lora`，均在外部目录记录 GPU UUID、物理映射、量化和结果。固定 Qwen 1.5B、102/26 schema-disjoint split、seed、lr、effective batch、LoRA r16/alpha32/dropout0.05，仅改 base storage mode，分别在 logical 0 -> physical 2 和 logical 1 -> physical 3 的 RTX 4090 运行 26 step。QLoRA train/eval loss `0.427482/0.290193`、allocated peak 4.35 GiB；bf16 LoRA 为 `0.426504/0.309192`、5.19 GiB；两个 adapter 都 fresh PEFT reload 得到 finite validation loss。该事实只闭合训练工程/显存对比，不构成质量改进；下一步必须对每个精度独立跑 matching base/adapter 的 1,034-case SQLite/Test Suite 对照、状态迁移和人工 changed-case 语义核验。 |
| 2026-08-25 | Base/Adapter 负向结果诊断与安全分析器 | 新增可复用的离线 paired diagnostics analyzer，只读取仓库外 SQLite 报告并输出状态迁移、错误类别、token/归一化长度分布、展示包装模式和有限 case ID；测试断言输出不含 SQL、问题、数据库标识或原始错误文本。对冻结结果，**253** 条 Base 可执行候选在 Adapter 变为 error/policy rejection，仅 **88** 条失败候选变为可执行；20 个开发数据库中 0 改善、17 回退、3 不变。Adapter 虽把 raw completion 改为 1,033 条直接 query-shaped、将 token cap hit 从 342 降至 43，但归一化 SQL 中位长度由 87 升至 158，`no_such_column` 从 180 升至 289，其中带限定引用由 16 升至 254。训练状态确认 8 step、global batch 4 最多仅覆盖 32 次样本暴露（epoch `0.313725...`）。对 3 条回退、4 条 apparent recovery 的非随机人工核验显示 recovery 仍可能错误 Join/粒度、缺要求列或错指标，不能称语义提升。下一步仅建议保持其他变量冻结、将 SFT horizon 从 8 调至 **26 step**（约完整一轮 102-row 数据）后重新全量对照；在 non-regression 前不扩数据、不进 DPO/GRPO。 |
| 2026-08-25 | Qwen 1.5B Base vs Adapter 完整对照 | 在唯一变量为是否加载 74 MB LoRA adapter 的冻结合同下，Base 与 Adapter 都完成 **1,034/1,034** Spider dev 候选、共享完成文本规范化、受限 SQLite 诊断和未修改 Test Suite evaluator。Adapter 为 **61,796 token、4,797,725 ms、666 executed、29 policy rejected、339 execution error、0 timeout、Test Suite all=0.215**；对应 Base 为 **128,957 token、4,631,685 ms、831/4/199/0、all=0.427**。Adapter 的外部 prediction/report SHA-256 分别为 `8babd2e...406dd` / `346bead...7d82a`，Test Suite input/raw-output hash 为 `d67b185...7de8` / `6e97ace...cc242b8`。在 102 条 train-only、8-step schema-disjoint QLoRA SFT smoke 下，adapter 生成 token 减少但 SQLite 与 Test Suite 均回退；这是该配置的负向 ablation，不代表 QLoRA/SFT 普遍无效，也不是官方 Spider 分数或业务语义准确率。下一步先做可复现的状态迁移、错误类别、输出长度和 changed-case 分析，期间不直接扩大训练或进入 DPO/GRPO。 |
| 2026-08-25 | Qwen 1.5B Base 完整评测证据 | 保留 Base 原始 1,034 条外部 prediction JSONL（SHA-256 `8978b8...29cc0e`）不改写，在固定共享完成文本规范化后得到 SQLite **831 executed、4 policy rejected、199 execution error、0 timeout**，报告 SHA-256 `433cf3...15d071b`。此前未规范化 SQLite 报告（SHA-256 `320f68...a101ef`）作为外部审计留档，不进入公平比较。未修改 Test Suite evaluator 完整执行 1,034 条，raw internal output 为 easy/medium/hard/extra/all = `0.748/0.448/0.241/0.094/0.427`；它是当前 2020-01 mirror/asset 组合的内部证据，不是 current official leaderboard。SQLite 可执行性与 Test Suite 行为一致性分别记录，不能由前者推导后者。Adapter 已在同一 GPU/Prompt/Decode/Normalizer/Evaluator 合同下开始全量生成，完成前不声称 SFT 提升。 |
| 2026-08-25 | Text-to-SQL 完成文本规范化 | Base 恢复评测发现原始模型 completion 中有 806/1,034 条在 SQL 后续接 `### Answer`/Markdown 表格，旧官方 bridge 把表格分隔线的 `--` 误作 SQL 行注释并安全停止。新增共享规范化器：原始外部 JSONL 保留，只在 SQLite Adapter 与官方 bridge 前一致移除明确代码围栏、SQL 前缀与答案/解释 heading 后的展示尾部；它不修复 SQL 语法、表列、Join、过滤或聚合。真实 SQL 行注释转为等价块注释后再折叠到官方的一行输入，保持注释作用域。专项 **32 passed, 1 skipped**；待用该固定规范化合同重新产出 Base 诊断/官方 bridge，再顺序开始 Adapter。 |
| 2026-08-25 | QLoRA Base 评测容错与恢复 | Base Qwen 1.5B 已完成 1,034 条外部 Spider dev 生成（128,957 token、4,631,685 ms；预测 SHA-256 `8978b8...29cc0e`），但 SQLite 诊断遇到模型输出的未闭合引号，旧代码仅捕获 `ParseError`，漏掉 `sqlglot.TokenError` 并提前退出。现统一将 `SqlglotError` 归为 `policy_rejected`，报告保留该条并继续后续 case；新增两条 case 回归覆盖坏 SQL 拒绝和下一条正常执行。顺序 driver 支持显式 `BASE_REUSE_GENERATION=1`，仅复用已有 generation evidence + JSONL，已存在官方评测证据时拒绝恢复，避免重跑约 77 分钟的 base 生成或混用成品。SQLite/bridge 相关 **26 passed**，ruff、compileall、shell 语法、diff 检查通过；adapter 尚未启动，尚无质量提升结论。 |
| 2026-08-25 | QLoRA SFT 工程 smoke 与 adapter reload | 128 条外部 Spider train-only 候选按数据库 schema 切为 102 train / 26 validation，66/19 个 `db_id` 完全不重叠、SQL shape 交集 0、v2 holdout 未使用。Qwen 1.5B 以 4-bit NF4 + `r=16` LoRA、batch 1、accumulation 4、8 个 step 在逻辑 CUDA 1/物理 GPU 3 运行，train/eval loss 为 `0.556203/0.466989`，allocated peak 为 3,557,137,920 bytes。训练保存仓库外 74 MB LoRA adapter 和 checkpoint，并从冻结基座 fresh reload 得到有限 validation loss `0.309798`。该结果仅证明单卡训练、恢复和 adapter 复载闭环，不是 EX/Test Suite/语义准确率，也没有改动生产 Vanna/PostgreSQL。 |
| 2026-08-25 | Qwen 1.5B 冻结与 QLoRA forward smoke | 通过 `HF_ENDPOINT=https://hf-mirror.com` 下载、固定并逐文件哈希 `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67...`（`model.safetensors` SHA-256 `535485...fa210`），模型文件留在仓库外。逻辑 CUDA 1/物理 GPU 3 空闲 RTX 4090 上，以 4-bit NF4 + double quant + bf16 加载并注入 `r=16` LoRA；单个 train-only 样本 125 个 prompt token 全 mask、47 个 SQL/EOS token 被监督，172 token forward 得到有限 loss `0.884486`，allocated peak 2,085,800,448 bytes。没有 backward、optimizer、adapter 保存或生产链路改动，不能表述为已微调或效果提升。 |
| 2026-08-25 | Spider train-only 后训练候选与学习笔记 | 新增 `scripts/build_spider_sft_candidates.py`，仅读取 7,000 条 Spider train、`tables.json` 和只读 SQLite schema，按固定 seed/SQL shape 轮询抽取 128 条。128/128 通过 `EXPLAIN QUERY PLAN`，v2 60 条 holdout 碰撞 0；候选 JSONL、训练文本、审计和 SHA-256 均在仓库外，manifest 状态更新为 `prepared_external_only`。新增 [`docs/post-training-learning-notes-v1.md`](docs/post-training-learning-notes-v1.md)，解释 SFT/LoRA/QLoRA/DPO/GRPO、Text-to-SQL 评测和面试考点；候选阶段没有启动 SFT、DPO 或 GRPO。 |
| 2026-08-24 | QLoRA/SFT 隔离环境 bootstrap | 新建仓库外 Conda 前缀 `data-analysis-agent-qlora`（Python 3.11），使用与宿主 NVIDIA 535.54.03/CUDA capability 12.2 兼容的 `torch==2.5.1+cu121`，而非需要更高驱动的 CUDA 12.4。直接与完整传递依赖分别固定在 `requirements-qlora-v1.in` / `.lock`；`pip check` 通过。`CUDA_VISIBLE_DEVICES=1` 仅暴露一个进程内 `cuda:0`，其 UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` 对应物理 GPU 3 的空闲 RTX 4090。未下载基座权重、未创建训练数据、未读取 Spider gold SQL、未启动训练，60 条 v2 holdout 仍永久隔离。 |
| 2026-08-24 | Spider dev 完整冻结小模型基线 | 以临时、项目专用 Ollama 服务在 `CUDA_VISIBLE_DEVICES=1`（逻辑 1、物理 GPU 3/RTX 4090）完成 **1,034/1,034** 有序且唯一的 primary SQL 候选生成，服务随后关闭。预测 SHA-256 为 `9459b8262d62983c6e40c92b8bcc3be756bc4f2afd19562c8c9b1b5f06d572b0`；受限 SQLite Adapter 为 **884 executed、150 execution_error、0 policy_rejected、0 timeout**，诊断 SHA-256 为 `43561e69f6420bfc9f12a5d4822b65e0a96ff838e115eba8d5511e1e5f237744`。生成累计 466,667 ms、32,662 token；执行错误以 138 `no_such_column` 为主。没有读取 gold SQL/答案、没有下载 Test Suite 数据库、没有运行官方 evaluator，因此结果仅是本地可执行性/Schema-linking 基线，绝不是 EX/EM/Test Suite Accuracy 或语义准确率。 |
| 2026-08-24 | 冻结生成可恢复性与全量预检 | 修复长批次的通用数据管道缺口：生成器每成功一条便将最小候选记录追加并 `fsync` 到仓库外 JSONL，后续模型失败不会丢失已完成前缀，`--resume` 只补缺失 primary case。新增第二条模型模拟失败回归。对 Spider dev 的 20 个数据库只读 DDL 预检显示最大 schema 为 3,012 字符，0 个超过 20,000 字符预算，可以安全启动 1,034 条完整冻结生成；没有读取表数据、gold SQL 或答案。专项 34 passed、ruff/compileall/diff check 通过。 |
| 2026-08-24 | 官方 Spider Test Suite 防误报桥接 | 固定官方 `taoyds/test-suite-sql-eval` code commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`（Apache-2.0 仅覆盖代码），新增仓库外 bridge：先校验 commit/干净 worktree、完整且有序的一对一 primary 预测和测试库文件，再以 `--etype exec` 调用未修改 `evaluation.py`，gold/pred/raw output/evidence 均不进入 Git。上游 Spider evaluator 对单 session 使用 `zip(predictions, gold)`，短预测会静默变更分母；真实 20 条 smoke 已以 exit 2 在此 guard 前拒绝，未写评测输入或输出。独立 Test Suite 数据库资产的条款、release/hash 与 2020-01 Spider 镜像兼容性仍未核验，故未下载、未运行，也没有 Test Suite Accuracy。专项 33 passed、ruff/compileall/diff check 通过。 |
| 2026-08-24 | Spider 原生 20 条冻结基线 smoke | 使用冻结 `qwen2.5-coder:3b`、固定 prompt/解码和原生 Spider 前 20 条仅在内存中的问题/DDL生成外部预测 JSONL；SQLite Adapter 的本地只读诊断为 **15/20 executed、5/20 execution_error、0 policy_rejected、0 timeout**。五条错误均为 `sqlite_operational_error/no_such_column`，表明当前小模型的 schema linking 是后训练前的可量化缺口；没有读取 gold SQL/答案、没有运行官方 evaluator，不能称为 EX/EM/Test Suite Accuracy 或语义准确率。模型生成总耗时 13,123 ms、594 token；预测/诊断 SHA-256 与外部路径已写入文档。Ollama 自动实际使用物理 GPU 0（逻辑 CUDA 2，3090）为主、物理 GPU 2（逻辑 CUDA 0，4090）为辅，说明后续训练必须显式隔离设备而不能依赖自动分配。 |
| 2026-08-24 | Spider 镜像获取与完整性冻结 | 由于官方 Google Drive 在本机网络层持续无首字节，改用明确引用官方 Spider 页面的 Kaggle v1 镜像；它自身 license metadata 为 `unknown`，项目仍保留官方 CC BY-SA 4.0 署名并明确其为 2020-01 镜像，不能对标官方 2020-08 修订或排行榜。仓库外 `archive.zip` 100,663,520 bytes 的 SHA-256 为 `ed2a34b84e9665606da73497f4166b1c8d94056517614c33f9dcdca45346be0f`，ZIP 完整性和 398 条安全路径均通过；解压后原生 `dev.json` 1,034 case、20 个开发库、166 个 SQLite 文件齐全，`dev.json`、`tables.json`、完整数据库树和开发数据库树哈希均已写入 manifest/provenance。尚未生成真实预测或运行官方/本地评分。 |
| 2026-08-24 | Spider 1.0 原始数据许可核验 | 官方 Spider 1.0 任务页的 "Getting Started" 明确说明其下载数据集按 **CC BY-SA 4.0** 分发，`taoyds/spider` 的 Apache-2.0 仍只覆盖代码，不能替代数据许可。因官方 Google Drive 在本服务器网络中不可达，实际获取固定为公开 Kaggle v1 镜像；该镜像明确引用官方来源但自身 metadata 为 `unknown`，所以仍保留原始 CC BY-SA 署名并将镜像发布日期、来源、存储位置、检索日期与代码 commit 单独记录。它早于官方 2020-08 修订，下载后才补 archive、`dev.json`、`tables.json` 和 SQLite database tree 哈希，且不用于官方榜单对比。官方当前排行榜指标为 Test Suite Accuracy，SQLite Adapter 仍只做本地安全执行诊断，不计算或声称任何官方分数。 |
| 2026-08-24 | GPU 设备映射冻结 | 明确本机逻辑 CUDA `0/1/2/3` 分别映射到 `nvidia-smi` 物理 GPU `2/3/0/1`，即 4090、4090、3090、3090。`AGENTS.md` 要求后续推理、离线基准与后训练任务同时记录逻辑/物理编号，并在每次分配前复查 `nvidia-smi`；不假定四卡可同时使用，也不抢占其他项目进程。 |
| 2026-08-24 | 冻结小模型 SQLite 基线生成侧 | 新增 `frozen_sqlite_baseline.py` 与 `run_frozen_sqlite_baseline.py`，在原生用例顺序下仅将问题和 SQLite DDL 留在内存中，以无工具 Ollama 请求生成单候选 SQL；输出 JSONL 只含 case ID、候选 SQL、token 和生成耗时，强制留在 Git 外。Schema 读取使用 `mode=ro` 和 `query_only`，超出字符预算直接拒绝，不静默截断；模型输出仅去除 Markdown/`SQLQuery:` 包装，不截断分号后的文本或多语句，仍完整交由 Adapter 的 AST/只读执行诊断。冻结 Q4_K_M `qwen2.5-coder:3b` 量化包的完整 manifest/blob digest、Qwen Research License（非商业研究/评测）和 Ollama server `0.13.1` / CLI `0.31.2` 已写入实验清单。合成真实模型烟测经 Adapter 为 1/1 executed，预测文件不含问题，官方 EX 为 not_run；最终相关测试共 **35 passed**，包含多语句候选抵达 Adapter 后被策略拒绝的端到端回归，`ruff`/`compileall`/YAML 解析通过。Spider 代码 commit 已记录，但其原始数据许可未获得显式核验，未下载 Spider/BIRD 原始数据，未跑真实基线、官方 evaluator 或任何训练。 |
| 2026-08-24 | SQLite benchmark Adapter 基础设施 | 新增独立 SQLite 离线执行器和 `run_sqlite_benchmark.py`：以 `sqlglot` SQLite AST 限制单条只读查询，拒绝 DDL/DML、事务、`ATTACH`/`DETACH`、危险 `PRAGMA` 和扩展加载；以 SQLite `mode=ro`、`query_only`、authorizer、progress-handler 超时与服务端 `LIMIT` 形成纵深保护。BIRD/Spider 原生 `dev.json` 只用于生成稳定 case/database locator，问题、证据和 gold SQL 不进入报告；外部 JSONL 预测记录候选 SQL、token/生成耗时和执行证据，结果行不落盘。报告明确将本地执行诊断与官方 EX 分开，官方 EX 只能挂载未修改官方评测器生成的带版本摘要。本轮 **19 passed**、`ruff` 与 `compileall` 通过；未下载 BIRD/Spider、未下载模型、未创建训练环境、未更改 PostgreSQL/Vanna 生产链路。 |
| 2026-08-24 | 后训练准备第一阶段 | 新增服务器拥有的 `ChartContract`，从用户问题、`QueryPlan` 和 `ResultContract` 派生受控 bar/line 图表边界，并固定横轴、指标列、系列、标题与当前 SQL 结果工件；图表层拒绝模型自选类型/标题/文件、额外列、缺失或非数值字段、重复横轴和二次聚合。`DimensionPolicy` 升级为 `safe_direct` / `requires_attribution` / `server_owned_rule`，后者仅在实际服务器规则注册后才可放行，当前 Olist 没有归因 SQL compiler，继续 fail closed。新增后训练数据协议、60 条 v2 golden holdout manifest 与完整性测试；训练候选永远不能替代 AST、PostgreSQL reader role、Result/Chart Contract 或服务器归因规则。本轮专项 **96 passed**、v2 golden **60/60 passed**、`ruff`/`compileall`/diff check 通过；未调用在线模型、未创建训练环境、未执行微调。 |
| 2026-08-19 | SSE 澄清流状态收尾 | 修复嵌入式窗口在 SQL 前澄清/归因阻断只返回 `status_card` 时，SSE 已正常结束但状态栏仍停留在 `Sending message...` 的问题。前端将服务端 `status_bar_update` 同步到 Lit 状态，避免后续渲染恢复旧 loading；对未发送终态状态更新的普通用户请求，在正常 SSE 或 polling 结束时清空临时 loading，同时保留 starter UI 和服务端 error/success/warning 终态。新增确定性 Playwright 回归，验证澄清卡片可见、状态回到 `idle`、输入可继续使用；修正历史结果预览测试的 Shadow DOM 读取断言。Web Component 构建通过，嵌入窗口回归 **10 passed, 1 skipped**，服务已重启至 `127.0.0.1:32010`。 |
| 2026-08-19 | 可信结果呈现与归因边界 | 已验证的分组结果由服务器展示中文概览、样例行和完整表格提示，替代机器式审计结尾，且不重新引入自由模型总结；支付方式 GMV 与品类履约/好评率的未冻结一对多归因均在 SQL 前阻断，并明确要求配置 Catalog 归属/分摊规则而非承诺自然语言追问即可执行。确定性专项 77 passed、v2 golden 60/60，Playwright 直接渲染验证表格语义和桌面无横向溢出。 |
| 2026-08-02 | 项目立项 | 确定为 Python 可信数据分析 Agent，不继续以 Java 本地生活平台作为主项目。 |
| 2026-08-02 | Agent 选型 | 选择 Vanna，而非 PandasAI。 |
| 2026-08-02 | 前后端基座 | 使用 Vanna + FastAPI；前端只保留可嵌入的 Vanna Web Component 与原生宿主页，不再建设 TailAdmin。 |
| 2026-08-02 | 基础设施 | v1 使用 PostgreSQL；Redis 延后到出现异步任务/缓存/限流需求时。 |
| 2026-08-02 | 数据策略 | Chinook 用于回归，Olist 用于主展示，中文电商数据用于后续扩展。 |
| 2026-08-02 | 项目基线同步 | 已创建飞书项目文档；GitHub SSH 认证已恢复，首个项目基线提交已推送至 `main`。 |
| 2026-08-02 | 开发计划与目录边界 | 冻结完整阶段计划；自有项目仅在本仓库开发，上游 Vanna/TailAdmin 收纳为 `/disk2/gengnan/_upstream/` 参考缓存。 |
| 2026-08-02 | 单仓库 Vanna-first | 将 Vanna 源码合并到本仓库并保留 `upstream` 远端；先用原生 FastAPI + `<vanna-chat>` 垂直验证，再建设数据合同和自有平台。 |
| 2026-08-02 | Phase 1 冒烟验证 | Conda 环境、SiliconFlow `DeepSeek-V4-Flash`、SQLite 合成 fixture、SSE API 和浏览器页面均已验证；服务端口为 `32009`。 |
| 2026-08-02 | Phase 2 数据合同草案 | 确定 Olist 为主展示案例、Chinook 为回归候选、中文数据和天池 O2O 为后续候选；新增 manifest、字段字典、指标目录、分析模型草案、合成 fixture 和 20 条评测草案，暂不下载原始数据。 |
| 2026-08-02 | 嵌入式产品路线 | 确认 Vanna Web Component 可嵌入任意网页并支持最小化/最大化；停止独立 Next.js/TailAdmin 方向，后续以宿主页和 Vanna 原生组件为唯一前端基座。 |
| 2026-08-02 | 嵌入式基线审计 | 新增 `/embedded-demo` 无框架经营宿主页和 Playwright E2E；已验证窗口状态、真实 SSE 表格、移动端无横向溢出及 CSV 不落仓库根目录。图表、真实认证、SQL 策略和审计仍未实现。 |
| 2026-08-03 | Markdown 表格兼容性修复 | 修复 Vanna RichText 原解析器不支持 Markdown 表格而原样显示符号的问题；根页面和宿主页改用本地构建 bundle，E2E 已验证语义化表格渲染。 |
| 2026-08-03 | Olist 来源与文件版本核验 | 通过 Kaggle 公开元数据与文件列表 API 核验 version 2、CC BY-NC-SA 4.0、9 个 CSV 清单；原始数据仅下载到仓库外目录，已记录 ZIP 与逐文件 SHA-256、行数和源列，未提交 Git。 |
| 2026-08-03 | CI 质量门收敛 | 将上游遗留的全量 `tox` 外部集成矩阵替换为 Python 3.12 核心单元测试、示例编译和 Web Component 构建；本地验证 56 个测试通过，真实模型 E2E 仍只在本机显式运行。 |
| 2026-08-03 | Olist analytics 转换基线 | 新增 PostgreSQL analytics DDL、8 表确定性 CSV 转换器和合成关联测试 fixture；全量源文件 checksum 核验后转换成功。评价使用 `(review_id, order_id)` 复合键，原始评价文件物理行数不等于逻辑记录数。PostgreSQL 尚未启动或加载。 |
| 2026-08-03 | PostgreSQL 本地加载工具 | 新增仅绑定 loopback 的 PostgreSQL Compose 编排、事务化加载脚本与核心指标 golden SQL。当前账户无 Docker daemon 权限，故只完成静态校验，未启动容器或执行真实加载。 |
| 2026-08-03 | 本地 PostgreSQL 实例 | 确认其他项目分别占用 35432/35433，随后以用户态 PostgreSQL 12.20 在本项目专属仓库外目录启动 `data_analysis_agent`，仅监听 `127.0.0.1:35434`。本轮未加载 Olist 数据。 |
| 2026-08-03 | Olist 真实入库与 golden 基线 | analytics 8 表已真实加载到项目专属 PostgreSQL，行数与转换报告一致、关联质量违规为 0；固化 GMV 13,494,400.74、有效订单 98,207、平均履约 12.558702 天、好评率 0.770680 的技术回归基线。业务口径仍标记为草案。 |
| 2026-08-03 | SQL 安全内核与数据库角色 | 新增 `sqlglot` AST 策略与 PostgreSQL 双角色：Agent 查询角色仅可读取 `analytics`，应用写角色仅可写审计表。策略已覆盖单语句、只读 AST、Schema/表/列白名单、敏感投影限制、函数拒绝与角色化 LIMIT；尚未接入 Vanna 运行入口。 |
| 2026-08-19 | 真实业务质量评测 | 新增 20 条脱敏在线业务评测和 5 条图表意图；20/20 权限合规，识别支付归因、评价粒度、模型超时和图表类型未遵守四类后续加固项。 |
| 2026-08-03 | 可信 Olist 查询闭环 | 新增 `trusted_olist_web_demo.py`、受控 PostgreSQL Runner、指标上下文和持久审计；真实 SSE 已验证中文州订单问题、策略归一化 SQL、结果表、中文结论及数据/指标版本记录。`analyst` 无法访问 `app`，查询账号与审计写账号已分离；身份仍为演示请求头。 |
| 2026-08-03 | 嵌入式窄浮窗修复 | 将 Vanna Web Component 的窄布局从视口媒体查询改为组件容器查询，修复宽屏宿主页中 440px 浮窗被进度栏挤压的问题。宿主页改为展示 Olist 真实 golden 指标和州排名，不再展示合成华东/华南数据。 |
| 2026-08-03 | PostgreSQL 信任边界集成测试 | 新增显式 `RUN_PROJECT_DB=1` 集成测试，验证受控查询、允许/拒绝审计以及 reader/writer 跨 Schema 权限拒绝；默认测试环境安全跳过，不依赖本地数据库。 |
| 2026-08-03 | 审计证据宿主页 | 宿主页新增数据/指标版本、演示角色和最近查询卡片；后端将数据库审计记录映射为页面 DTO，只展示最终 SQL、策略状态、版本、行数和耗时，不暴露异常详情或原始 SQL。浏览器验证 analyst 页面无控制台错误。 |
| 2026-08-03 | 受控 Plotly 图表 | 注册仅可读取当前用户 `RunSqlTool` 输出文件的图表工具，限制 `query_results_<id>.csv`、200 行和 3 列。真实 SSE 已验证州订单聚合返回表格、柱状图与中文结论；任意文件名被策略拒绝。 |
| 2026-08-03 | 第一轮确定性评测 | 新增 60 条 v1 版本化用例和可执行评测器：26/26 安全/边界 SQL 与 AST 策略预期一致，本地 PostgreSQL golden SQL 通过。报告明确区分确定性策略/数据评测与在线 LLM 语义准确率，后者不伪造百分比。 |
| 2026-08-03 | 嵌入式分析窗口交互修复 | 修复 `/embedded-demo` 浮窗固定 440px/500px、不能移动或缩放的问题。宿主页新增桌面拖拽条、右下缩放柄、视口边界限制和本地位置/尺寸记忆；小屏维持自适应固定布局。`vanna-chat` 仅在 `data-hosted-window` 属性下接受宿主页提供的高度，内部聊天布局随外壳同步，不影响独立组件页面。Playwright 已验证拖动、缩放、最小化/恢复、尺寸同步、390px 无横向溢出及无控制台错误。 |
| 2026-08-03 | 嵌入式首屏与本地化打磨 | 将受信 Olist 演示的默认英文欢迎卡替换为项目专属中文 workflow：首屏说明受控只读边界，并提供州前五、品类前十和指标概览三个真实问题入口。AgentConfig 新增可配置的空闲状态和输入提示，避免 starter SSE 将宿主页中文输入框回退为英文；组件补充可配置空态、窗口控制和发送按钮文案。经截图复查，三个入口、状态条和输入区均位于 440px 嵌入窗口首屏；workflow 单元测试和 390px/桌面 Playwright 回归均通过。 |
| 2026-08-03 | 嵌入式图表自适配修复 | 对抗性浏览器检查发现 Plotly 在嵌入窗口中未显式接收容器宽度时会回退到 700px 默认画布，导致 438px 窗口横向溢出。`plotly-chart` 改为以组件宿主的实时宽度构造与重布局，并监听宿主而非 Plotly 内部可变节点；富结果图表容器补充最小宽度与裁剪边界。固定受控柱状图 fixture 的 Playwright 回归已覆盖普通、手动缩放、最大化与 390px 窄屏，确认 SVG 画布宽度始终等于容器、无页面横向溢出且无控制台错误。在线模型请求的时延独立于此布局验收，本轮不据此虚称其端到端图表成功率。 |
| 2026-08-03 | 演示级角色会话与权限可见性 | 用标准库 HMAC-SHA256 签名的短期 cookie 取代 `X-Demo-Role` / `X-Demo-User` 请求头。角色仅能为固定映射的 `demo-analyst` 或 `demo-admin`，且同一解析器被 SSE、审计 API 和 SQL 策略使用；未签名、篡改或过期的 cookie 默认回退分析员。宿主页新增分段角色选择和“演示会话，非密码登录或生产认证”说明。单元测试覆盖签名、篡改、过期与请求头提权拒绝；Playwright 覆盖伪造 header 仍为 analyst、页面切换 admin 后签名会话刷新。该能力证明角色化策略与审计范围，不代表真实身份认证或组织权限。 |
| 2026-08-03 | 固定演示场景与 Golden 验证 | 将“州前五”“品类前十”“指标概览”从工作流按钮提升为 `evals/cases/demo_scenarios.yaml` 中的版本化场景契约，固定问题、允许角色、指标口径、来源表、排序/行数、图表要求、展示证据和预期结果。新增 PostgreSQL 断言脚本验证州 Top 5、品类 Top 10 和四项指标概览不发生数据或口径漂移；本地真实库执行成功。CI 在无数据库环境校验契约结构与 starter action 一致性，数据库 golden 只在 `RUN_PROJECT_DB=1` 显式执行。该资产不测量在线 LLM 语义准确率。 |
| 2026-08-03 | 项目级验证审查 | 对嵌入图表、演示会话、固定场景、对外文档和发布回归进行逐项审查：浏览器 E2E 5/5、策略/场景相关确定性测试 30 passed 1 skipped、项目 PostgreSQL 测试 6/6、60 条安全预期 26/26、3 条场景 golden 均通过；最近 GitHub Actions 通过。审查记录明确未覆盖真实认证、组织级 RLS、批量在线 LLM 准确率和生产部署。 |
| 2026-08-03 | Agent 平台与 Text-to-SQL 专项调研 | 明确下一阶段优先做 PostgreSQL 持久会话、分层上下文、请求级工具/token 预算、歧义澄清、一次受限执行修复和结果级校验；对照 Vanna、Dataherald、WrenAI 及 2025--2026 Text-to-SQL 论文，暂缓多 Agent、Best-of-N、RL/专用训练、向量库和任意 Python 执行。详见两份专项文档。 |
| 2026-08-03 | 第二轮 P0 基础设施骨架 | 新增 PostgreSQL `ConversationStore`、`conversations/messages/agent_runs` 表、`run_id` 审计关联、请求级工具/SQL/图表/输入/上下文/输出预算、上下文轮次裁剪和历史 API。11 项预算/上下文确定性测试、8 项 PostgreSQL/SQL runner/路由/run recorder 集成测试、run/audit 真实回链与应用装配检查通过；GitHub Actions run `30806496513`（commit `0a04c7d`）成功。 |
| 2026-08-03 | P0 会话交互与空会话修复 | 宿主页完成历史列表、点击恢复、刷新恢复、新建会话、删除失败状态和角色切换隔离；嵌入窗口 E2E 6 条通过。starter UI 不再提前持久化零消息会话，历史 API 过滤遗留空记录；历史恢复只回放安全文字，不伪造 SQL/图表/DataFrame。 |
| 2026-08-03 | Text-to-SQL 可靠性计划冻结 | 新增 `plan/feature-text-to-sql-reliability-v1.md`，把调研落成 5 个可执行阶段：在线基线、结构化 Catalog、确定性澄清、一次受限修复/结果校验、回归评测；后续实现严格按可验证的最小闭环推进。 |
| 2026-08-03 | Text-to-SQL 二次源码调研 | 按 `github-research` 六阶段流程核验 OpenChatBI、PremSQL、BIRD-INTERACT、Lumen、PandasAI、Dash、test-suite-sql-eval、SQL-R1 和 MAC-SQL；确认下一步核心是 Catalog/Schema linking、可回答性澄清、一次执行修复、结果 denotation/校验和分维度评测。研究缓存位于本地 `github-research-output/` 且已忽略，不进入 Git。 |
| 2026-08-03 | Text-to-SQL 第二轮论文与实现核验 | 直接复核 arXiv API 与 GitHub Public API：ABISS、RBAC Text-to-SQL、Schema retrieval、Context Compression、On-Prem self-correction、GATE 和 DataClawEval 支持“先检索/澄清/验证，再优化模型”的路线；新增 `plan/feature-text-to-sql-reliability-v2.md`。同时修复 YAML 裸 `on` 被 `safe_load` 转为布尔键的问题，Catalog smoke load 已通过（9 表/4 指标/7 Join）。 |
| 2026-08-03 | Text-to-SQL 第二轮开发 | 按 v2 计划落地角色化 Catalog 检索与 trace、短安全系统提示、`QuestionRouter`、PostgreSQL working memory、零 SQL 澄清边界、一次 Policy 二次校验修复契约、数据库错误脱敏和 `ResultValidator`；Catalog/Router/working memory/ResultContract 已接入 Trusted Demo SSE，结果校验合同和 `prompt/catalog/dataset/metric/policy` 版本进入 ToolContext、run evidence 与 SQL 审计。确定性专项测试 **68 项通过**，项目 PostgreSQL 会话/Runner/路由/run recorder 集成测试 **9 项通过**；模型驱动修复的完整生命周期、在线模型语义评测和浏览器多轮回归仍未完成。详见 `docs/verification-text-to-sql-v2.md`。 |
| 2026-08-06 | 通用工作区与可信修复生命周期 | 新增 `WorkspaceProfile`，将数据集/指标/Catalog/Policy 版本、PostgreSQL Schema/角色、对象白名单和 Catalog 路径从通用核心中抽出；Olist 保留为当前 adapter 和展示案例。新增 `TrustedRunSqlTool`，在不修改 Vanna Agent 核心循环的前提下，将一次 SQL 修复接入原生工具生命周期：错误脱敏、候选二次 Policy、reader role 重执行、ResultValidator 二次校验和可信拒答均已闭环。`app.query_audits` 与 `app.agent_runs` 新增 `repair_evidence` JSONB，预算记录同步修复状态；新增四类生命周期测试、WorkspaceProfile 测试和浏览器多轮澄清回归。专项回归 **84 passed**，PostgreSQL runner/run recorder **4 passed**，多轮浏览器测试 **1 passed**；ruff、compileall 和 diff 检查通过。尚未做真实 SiliconFlow 批量修复成功率、第二数据集和生产认证。 |
| 2026-08-06 | 嵌入式可靠性与延迟证据 | 修复多 CTE 输出列与 scalar subquery 的 SQL Policy 边界；`QuestionRouter` 对纯指标定义问题直接返回 Catalog Markdown（`catalog_answered`），避免无意义的 SQL/LLM 重试；assistant 历史消息接入与流式文本一致的安全 Markdown renderer，支持标题、表格、强调、列表、引用、代码块和分割线；宿主页通过双 RAF + `ResizeObserver` 同步最小化恢复、拖拽和缩放后的聊天高度。新增阶段耗时证据并确认代表性多指标请求约 77.7 秒，其中 LLM 约 77.2 秒、PostgreSQL 约 0.39 秒。确定性专项 **63 passed, 1 skipped**，Playwright 集成 **7 passed**，Web Component `npm run build` 通过。后续优先做模型调用超时/流式可观测性和 v2 评测集，不把单次时延写成 P95 或准确率。详见 [`docs/verification-2026-08-06.md`](docs/verification-2026-08-06.md)。 |
| 2026-08-06 | 证据路由、QueryPlan 与结果记忆 | 将 `QuestionRouter` 扩展为 `intent`/`requires_database`/`evidence_mode` 路由；通用业务/知识和结果追问复用模型但不暴露 SQL/图表工具，帮助与指标定义走确定性回答。新增服务器拥有的多指标 `QueryPlan`，将指标、维度、时间和合法聚合策略写入 Prompt、ToolContext 与运行证据，并把分组维度纳入 `ResultContract`。通过 `ResultValidator` 的结果生成最多 8 行、1200 字的可信摘要，持久到 WorkingMemory 供结果追问使用；无摘要的追问先澄清。新增路由、计划、结果摘要和会话记忆回归后，专项集合 **88 passed**；ruff、compileall、`git diff --check` 通过。全量 Vanna 上游测试仍有缺少可选依赖/外部 fixture 的既有失败，不纳入本项目质量门。下一步是版本化 v2 评测和真实模型核验。 |
| 2026-08-06 | v2 确定性评测与嵌入窗口加固 | 冻结 `evals/cases/text_to_sql_v2.yaml` 60 条路由/QueryPlan golden，新增脱敏离线 runner，结果 **60/60 passed**；该数字只代表确定性合同，不代表在线模型准确率。宿主页支持四边四角八方向缩放，非法/最小化尺寸不再污染恢复缓存；Web Component 补齐窄窗口 flex 约束、历史消息安全 Markdown 和空行表格解析。完整 `tests/e2e/test_trusted_embedded_window.py` **9 passed**，Web Component 构建成功。一次 SQL 修复仍默认最多 1 次（最多 2 次 SQL 执行），是否扩展到 2 次留待真实 SiliconFlow 人工标签评测比较恢复率、语义风险、延迟和 token 成本。 |
| 2026-08-18 | 真实 SiliconFlow 人工标签评测 | 新增 24 条在线代表性清单、真实 SSE 运行器、人工标签合同和脱敏本地报告。24/24 Agent Run 均记录路由/澄清、SQL、指标语义、结果合同、权限、回答有据、修复、耗时和 token 状态；路由 23 pass/1 fail，权限 24 pass，回答有据 17 pass/7 fail。评测暴露 BRL 被误写为人民币、合同通过后继续发出无关 SQL、Catalog slice 不完整、支付归因未冻结与一次 180 秒未完成。所有修复次数为 0；22 条 provider usage 为 unknown，未把它们写成零。该单次小样本不发布在线总体准确率、P50/P95 或修复恢复率。 |
| 2026-08-19 | 敏感关联键投影误拒绝修复 | SQL Policy 区分内部 CTE 与最外层结果阶段：CTE 可保留敏感关联键维持事实粒度，最外层仍拒绝敏感结果列/别名、`GROUP BY`、`ORDER BY`。QueryPlan/Catalog Prompt 增加顶层结果列白名单。专项 50 passed、真实 PostgreSQL 11 passed、v2 golden 60/60；真实 `data_005` 从 2 SQL/2 LLM 轮次/1 rejected audit 收敛为 1 SQL/1 LLM 轮次/0 rejected audit，结果合同和确定性收口保持通过。 |
| 2026-09-01 | Olist 英文候选 prompt 语言敏感性对照 | 以当前代码、当前 PostgreSQL 工作区和同一 RTX 4090 刷新中文 Base/Adapter，再冻结 Catalog/Router/QueryPlan/ResultContract/审计的中文 grounding，仅将候选生成器问题替换为仓库外、哈希固定的英文 overlay。英文直接进入当前中文语义层时仅 8/12 可查库、4/12 保持相同指标；隔离生成对照中 Base 从 `12/12 generated, 6 policy accepted, 4 executed, 2 contract-valid` 变为 `12/12, 6, 2, 1`，Adapter 从 `12/12, 6, 2, 0` 变为 `12/12, 4, 2, 0`。该实验分离了产品多语言缺口与模型候选语言敏感性，不构成业务准确率；Adapter 在任一语言下均未通过业务迁移门，生产默认 Vanna/SiliconFlow 路径不变。详见 `docs/post-training/experiments/olist-english-prompt-transfer-v1.md`。 |
| 2026-09-04 | Olist Medium v1 训练前配置与 bf16 LoRA smoke | 训练入口兼容新的 `olist_family_isolated_v1` split audit；冻结 Medium v1 的 bf16 未量化基座、LoRA `r=16/alpha=32/dropout=0.05`、`adamw_torch`、学习率 `1e-4`、weight decay `0.01`、真实 batch `2`、梯度累计 `2`（有效 batch `4`）、两 epoch和 `max_seq_length=3072`。1-step smoke 在逻辑 GPU `1`/物理 GPU `3` RTX 4090 完成，global step=1、train loss `0.2078`、validation loss `0.2198`，峰值显存约 14.65 GiB allocated / 22.99 GiB reserved，adapter 成功写出；这些仅证明训练工程链路可运行，不代表 SQL 质量。配置与成对评测合同见 [`docs/post-training/data/olist-medium-v1-training-evaluation-contract.md`](docs/post-training/data/olist-medium-v1-training-evaluation-contract.md)，外部证据见 `/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-medium-bf16-lora-smoke-v1-20260904/`。下一步才是用户确认后启动完整两 epoch，并在冻结 test 上做 matching Base/Adapter 生成评测。 |
| 2026-09-04 | Olist Medium v1 正式两 epoch 训练已启动 | 通过独立覆盖审查后，在 screen `daa-olist-medium-full2epoch-v1-20260904` 使用逻辑 CUDA `1`（物理 GPU `3`，RTX 4090）启动冻结配置：720 train、240 validation、有效 batch `4`、预计 360 global steps、bf16 未量化 LoRA、`adamw_torch`、weight decay `0.01`、3072 长度。训练过程稳定，初始 15/360 steps 未 OOM。审查确认各 split 内及跨 split 的 family、QuerySpec、问题、Prompt 和 canonical SQL 均无重复；已知限制是 category_grouped 仅 train/validation 的 3/2 条，in-domain test 没有该形态，因此不对品类分组做本轮测试结论。外部日志与最终 adapter 位于 `/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-medium-bf16-lora-full2epoch-v1-20260904/`。训练完成后才运行 matching Base/Adapter 生成和 240 条冻结 test 的质量评测。 |

### 2026-09-21：M3 ResultArtifact presenter 合同与预览边界完成

冻结 [`docs/architecture/result-artifact-presenter-v1.md`](docs/architecture/result-artifact-presenter-v1.md)：
结果呈现现在明确区分 `total_row_count`（已验证结果总行数）与
`preview_row_count`（实际发送给表格的预览行数），并以
`display_truncated`、列标签/单位、workspace、dataset/metric version 和不含 SQL 的
`audit_ref` 作为服务器拥有的 presenter metadata。250 行结果会展示“预览 200 / 共 250 行”，
隐藏行不会进入 UI 或模型上下文。未建立认证、workspace 隔离和审计下载路由前，
`preview_export_allowed`、`full_export_allowed` 和通用表格 `exportable` 均为 false，
避免把预览误认为完整 CSV；现有 `ResultArtifactStore` 仍只作为内部完整 artifact 存储。
新增 presenter、trusted SQL tool、通用 SQL table 和 250 行预览回归，focused 测试 13 passed，
结果验证/artifact/runner/router 回归 61 passed、7 skipped，compileall、ruff 和 diff check 通过。
前端 DataFrame renderer 已消费这份合同：真实 `embedded-demo` 浏览器回归中，250 行结果实际显示
`200 / 250 rows` 与 `Showing 200 of 250 rows`，且没有通用 Export 按钮、没有 console error；npm build
通过。本阶段没有修改 SQL 生成、Policy、reader role、ResultValidator 或 1,000 行执行上限。

### 2026-09-21：M4 EmbeddableCopilotShell 产品壳完成

新增独立 [`/embedded-copilot`](examples/embeddable_copilot_shell.html) 产品入口和
[`embeddable-copilot-shell-v1.md`](docs/architecture/embeddable-copilot-shell-v1.md)。它只显示服务端
bootstrap 得到的当前 workspace、可信聊天面板、`新分析` 和按需 `查看依据`；不显示演示角色切换、全局
审计列表或原始 SQL 面板。原 `/embedded-demo` 保留完整开发/面试/安全回归控制面，未被替换或删除。

产品壳通过 M1 `/api/copilot/bootstrap` 获取 workspace、dataset/metric version，浏览器不自报用户、角色、
权限或 SQL；查看依据只展示版本和“服务器只读校验”。`新分析`通过新的 conversation ID 清空 active analysis
state，不删除旧历史。移动端采用全宽、无横向滚动布局，桌面端保留紧凑的嵌入式 surface；TheLook 仍未接入默认在线页面，
跨域 iframe/postMessage 和真实 SSO 仍不在本任务范围。

真实服务端口 32013 的 Playwright 回归结果：产品壳隔离/依据/新分析 **2 passed**，M3 结果预览、旧 Demo 表格和移动端回归
**3 passed**；前端 `npm run build`、定向 Ruff、compileall 和 diff check 通过。M4 没有修改 SQL 生成、Policy、reader role、
ResultValidator 或模型 provider。

### 2026-09-21：M1–M4 联合验收完成

后端定向回归 **83 passed, 12 skipped, 0 failed**；skip 仅对应未启用的外部 PostgreSQL/集成环境。M1–M4 浏览器回归在
`http://127.0.0.1:32013/embedded-copilot` 通过 **13 passed, 1 skipped**，覆盖嵌入式产品壳、依据面板、新分析、ResultArtifact
预览（250 行结果显示为 200/250 且不提供导出）、旧 Demo 表格、移动端无横向溢出以及多轮澄清。该验收确认四阶段的接口边界
可以联通；M5 的会话生命周期（清空上下文、版本失效、并发产品合同）、真实 SSO/跨域宿主和 full export 仍未实现。

### 2026-09-22：M5 会话分析上下文生命周期完成

同一会话现在可以清空分析上下文而保留聊天记录。`POST /api/project/conversations/{id}/analysis-context`
只清除 WorkingMemory 中的指标、时间、维度和 active ResultArtifact 引用，保留消息与 history summary，
不删除审计 artifact 文件。数据版本或指标版本与当前 Olist workspace 不一致时，下一轮请求丢弃旧分析状态
和语义历史摘要，并返回“分析上下文已失效”；版本匹配时仍复用已确认状态。同会话 stale `state_version`
返回“会话状态已更新”，并正式记录 `termination_reason=state_conflict`，不再在冲突处理中因未知终止原因抛错。
产品壳 `/embedded-copilot` 增加“清空分析上下文”，发送中的聊天输入由组件锁定为单次 in-flight。
定向测试 `tests/test_analysis_lifecycle.py` 与上下文过滤器回归通过。真实 SSO、跨域宿主、完整导出和
TheLook 在线接入仍未实现，后训练资产未改。

### 2026-09-22：Router 排名、指标别名与会话指标继承修正

「最好 / 最高 / 最低」不再被当成缺少同比或环比基线。上一轮指标只在未完成澄清（`pending_question` 或 `pending_missing`）时继续使用；新问题若未命中 Catalog 指标，会重新询问，不再静默沿用 GMV。运行时 Olist Catalog 为「商品件数」增加「销量」别名。定向测试 `tests/test_question_router.py`、Catalog 与生命周期回归 86 passed。这不改变品类归属未冻结时的澄清，也不把口语筛选值自动写成 SQL 条件。

### 2026-09-22：问法对齐从别名补丁改为查询形状加受约束对齐

排名词不再进入对比规则。「各州 / 按州」才是按州汇总，「SP州」只作为筛选，不拆成每个州一行。「哪种商品」按品类排名。别名没有命中时，才额外调用一次模型，并且只能返回当前 Catalog 里的指标和维度 id；未知 id 丢弃，失败则仍澄清。归因、行数预算、SqlPolicy 和 ResultValidator 仍在这条对齐之后。相关测试 57 passed。

### 2026-09-22：产品页合并为单一 Copilot 界面

`/embedded-copilot` 现在包含会话列表、恢复和删除。演示角色与最近查询放在「管理」开关后面，默认用户界面不显示。`/embedded-demo` 重定向到该页面。旧的 `examples/embedded_analyst_host.html` 已删除。浮动窗口拖拽回归不再对应当前界面。

### 2026-09-22：Router 只决定是否生成 SQL

`QuestionRouter.classify` 现在只拒绝不安全或当前做不到的请求，并区分帮助、指标定义、经营建议、结果解释和需要查库。指标是否命中、分组归属、相对时间、比较基线和结果行数形状改由 `admit_sql_request` 在生成 SQL 前检查。别名未命中时，仍先做一次受约束的 Catalog 对齐，再进入这道验收。定向测试 63 passed。

### 2026-09-22：在线 Catalog 扩到 v3，并冻结可复用电商角色

在线演示改为读取 `olist_catalog_v3`，工作区 id 仍是 `olist-demo`，指标版本为 `0.3-proposal`。商品成交额、商品件数和运费金额增加 `seller_state`。新增 [`data/catalog/ecommerce_semantic_profile_v1.yaml`](data/catalog/ecommerce_semantic_profile_v1.yaml)，把 Olist 与 TheLook 中可共用的指标和维度收成角色绑定。客户城市和卖家城市改为只返回该指标最高的 20 个分组。订单按品类或支付方式统计使用多归属，金额按支付占比分摊。卖家原始编号仍不能出现在结果里。

### 2026-09-22：本轮产品优化停在偏好、诊断、路由探针和结果摘要

当前轮交给模型的结果优先使用已验证摘要；没有摘要时，CSV 在完整行边界截断，并仍带上图表文件名。产品页可以保存语言、时区、默认图表和数字格式，也可以把当前已校验结果按数据版本和指标版本保存下来。这两类记录不进入下一问的 SQL。管理开关后面增加按终止原因和修复成功次数的计数，分析员只看自己的会话。运行时路由探针冻结在 `evals/cases/runtime_router_probe_v1.yaml`，其中两条口语问法记为已知缺口。Phase B 安全模板仍是 `pending_manual`，没有回答正文，所以没有填写语义通过或失败。5–8 分钟演示脚本、架构图和模型实验卡留到后面。后训练资产未改。

### 2026-09-22：本地登录、跨域嵌入令牌和完整结果下载

产品页在没有本地会话或显式演示 cookie 时要求登录。账号密码只存在服务端；浏览器提交的 `user_id` 或角色会被拒绝。跨域宿主使用允许列表里的 origin 换取 5 分钟嵌入令牌，再由 `postMessage` 交给 iframe。完整 CSV 下载的是当前用户已校验的结果文件，下载前重算校验和，并写入 `app.export_audits`。界面里的通用预览导出按钮仍然关闭。这不是外部身份提供商，也没有放开 1,000 行执行上限。

### 2026-10-08：周计划补记本地登录三项，演示脚本成为全量功能测试

`plan/process-project-v1-week2-4.md` 补上 2026-09-22 的本地登录、跨域令牌和完整结果下载。当前入口文档里仍把已删除宿主页或「SSO/导出未做」写成现状的句子已改掉。历史变更记录和实验报告保留。W4-04 的演示脚本在 [`docs/demo-script.md`](docs/demo-script.md)，自动步骤在 [`evals/cases/w4_04_product_demo_v1.yaml`](evals/cases/w4_04_product_demo_v1.yaml)。同一天补了架构图、模型实验卡和面试问答，数字仍指向既有实验记录，没有重跑评测。

### 2026-10-08：行数上限、结果追问和偏好说明

策略把查询收到 1,000 行时，会多读一行判断是否还有数据，然后丢掉这一行。正好 1,000 行且没有多出来的行，不再说成可能截断。结果追问若写出摘要里没有的数字，回答会改成只复述已验证摘要。展示偏好仍会保存，页面写明它们还不会改变本轮的语言、时区、图表或数字格式。

### 2026-10-10：当前实现提交前核验与对外口径修正

核验从 2026-09-21 基线之后积累的产品与离线后训练改动。对已修改及新增的定向 Python 用例运行无在线模型回归：`369 passed, 30 skipped, 1 deselected`；跳过项有数据库/集成前置条件，主动排除了会创建本地账号的登录路由用例。另补 CI 基础组回归 `87 passed, 1 skipped`。Web Component 的 TypeScript/Vite build 通过。静态检查发现的字典推导和多语句写法已做等价修正，定向 Ruff 通过；相关复验 `15 passed, 4 skipped`。这不是全仓测试或在线语义准确率结论。

当前产品页的四条 Playwright 用例最终 `4 passed, 11 deselected`，覆盖登录不能被演示角色/请求头提权、演示脚本控件、移动端不溢出和 200/250 行数提示且关闭预览导出。首次检查暴露 presenter 注入与 starter SSE 并行的用例不稳定、移动端旧“宽度必须等于视口”断言不适用；已将 presenter 用例与 starter 输出隔离，并按现有浮动窗边距验证视口边界，复验通过。没有据此宣称完整在线语义验收。

README 已更新为当前本地登录、来源绑定嵌入令牌、完整 CSV 下载、五种服务端图表与会话链路。产品解析器只接受本地账号会话或有效 embed token，不接受演示角色 cookie；偏好仍仅保存、尚未应用展示。

重读既有主实验原始终测汇总后，纠正两项对外口径：最新多 Schema 1.5B 主模型的训练集已包含 TheLook，因此其 TheLook final test 是已见 Schema 的留出评测；Olist Base 的 3 条 Gold 匹配由 ordered 2 条和仅 bag 1 条构成，不能写成 ordered 3 条。历史 Olist-only 迁移实验不受此结论替换。本轮没有启动新训练或重跑 protected final test。

### 2026-10-10：后端与 Agent 实习方向的四周完善与学习路线

当前代码基线 `8bdb1de` 已推送到 origin/main。结合用户简历的两个项目和第二轮学习计划，新增 [`plan/process-agent-internship-improvement-v1.md`](plan/process-agent-internship-improvement-v1.md)，默认四周、每次一个具体单元。第一周掌握真实链路并完成产品质量基线；第二周围绕前三类真实错误改善业务回答；第三周补隔离环境部署、可配置 workspace、权限与并发证据；第四周审阅既有 SFT 并形成开源交付与面试口径。企业规模交付不作为四周承诺。

本项目突出结构化数据、指标 grain/口径、SQL 信任边界和结果证据；另一知识 Agent 项目继续承担 RAG、通用工具与 Durable 主题。计算机基础不重复安排。后训练保留为小比例、可解释的已有实验专题；本地候选接入必须先验证离线 QuerySpec 与在线 QueryPlan 输入关系，默认 provider 不自动切换。新一轮大规模数据扩展、完整训练、DPO/RL 与 protected test 重跑均未启动。路线当前为 Planned，下一单元是第一周 Day 1 的请求链追踪与用户复述。当前事实、核验范围、口径修正及四周路线已同步飞书项目文档 revision 253。

### 2026-10-10：四周路线 Day 1 开始，请求追踪与分段读代码

用户授权开始 Day 1。按当前代码修正 [`产品请求架构`](docs/architecture/product-request-architecture-v1.md)：Catalog 检索先于 Router.classify，查库准入后由服务端生成 QueryPlan/结果及图表合同，再交给在线模型生成 SQL 工具调用；拒绝、澄清、定义及受控结果解释有不查库分支。补充各环节输入输出和代码/模型职责。

对“按客户州统计有效订单数前五名，并生成柱状图”执行一次真实 HTTP/SSE 请求：run `55193f08-9fd4-453f-b9f1-414a0885f9f5`、SQL audit `31913`，一次 SQL、策略 allowed、结果合同满足、一个图表块，五个州的结果及顺序与固定 Gold 一致。原始证据留在仓库外 `evals/day1-request-trace-4OupjSaN/`。临时核验器最初未适配中文展示列，复用既有产品解析函数后通过，没有重发模型请求。既有路由/脚本用例 `3 passed, 3 deselected`；未重跑浏览器验收，不能视为在线整体准确率。

[`Agent 代码学习记录`](docs/learning/agent-project-walkthrough-v1.md) 从 create_app 和 chat_sse 两个入口开始；用户尚未完成复述，TASK-001 仍未完成。QueryPlan 的 top_n 未覆盖本次“前五”数量，实际 SQL LIMIT 和返回行数正确；结构化排行约束覆盖列入后续计划审阅。不启动新训练、终测或功能扩展，不切换默认模型。本次事实已同步飞书项目文档 revision 254。

### 2026-10-11：候选 SQL 训练/运行时输入对齐，共用入口与数据回放

Day 1 用户已复述入口、Router 与 QueryPlan/QuerySpec 的主要职责，并提出复用既有数据重建输入、重新训练验证。新增 [`runtime 候选 SFT 对齐计划`](plan/refactor-runtime-candidate-sft-alignment-v1.md)：小模型保留 SQL-only 候选职责，服务端负责工具包装与既有治理；统一真实请求构造器，不从离线 Spec 注入答案。先修复查询要求错配，再小批、物化、GPU smoke 和 1.5B LoRA；默认 provider 不切换。

新增单轮无 grounder 诊断 `runtime_alignment_audit.py` 与脚本 `audit_runtime_input_alignment.py`，回放原 `multi-schema-ecommerce-sql-only-v1-r1` 的 6,000 train / 1,500 validation。源文件 hash 与原 manifest 一致。Olist train/validation 结构含列序一致为 1480/338，列序差异为 1493/404，路由未准入为 27/8；TheLook 为一致 1494/132、列序差异 500/97、未准入 23/5，另有维度差异 775/455、粒度差异 208/61、结果列集合差异 983/516，后三类有重叠。记录位于仓库外 `evals/runtime-candidate-alignment-20261011-r1/{summary.json,records.jsonl}`，不含 raw SQL 或题目。

实现 `candidate_sql_generator.py:CandidateSqlRequest/build_runtime_candidate_request/render_runtime_candidate_prompt` 的 `runtime-candidate-sql-v2` 共用入口，输入来自实际 CatalogSelection、QueryPlan、ResultContract、workspace 与问题；没有 QuerySpec/Gold/摘要参数。`BudgetedChatHandler` 的查库准入分支调用同构造器，仅记录候选 Prompt 版本/hash；不替换默认 API Agent，不声称其完整工具调用 Prompt 已变成 SQL-only 模板。离线 `replay_single_turn(workspace=...)` 调用同一入口。v1 renderer 与完成清洗合同保持可回查，AGENTS 改为约束新实验必须复用版本化构造器，旧实验不迁移覆盖。

共用 v2 回放证据为仓库外 `evals/runtime-candidate-alignment-20261011-v2-r1/{summary.json,records.jsonl}`；7,500 行记录与初始审计的来源 hash、各组差异统计一致，生成 7,437 个不重复候选输入 hash，63 条未准入。两组定向测试共 119 passed、7 skipped（数据库集成未启用），Ruff/compileall/diff check 通过；覆盖 runtime/offline 输入文本及 hash 相同、版本/列/指标漂移拒绝、拒绝路由不得构造输入、摘要排除、客户端假 hash 被服务端覆盖。该诊断只比较规则推导的结构，并非完整输入准入或模型准确率；没有验证端点语义或全部表列依赖。TheLook 是现有规则的假设复用，不代表线上已接入。没有读取 final test、调用数据库/LLM/GPU、生成新正式 release 或启动训练。下一项是必要的维度、时间、结果列合同修复，随后一次物化、smoke 与正式训练；不会把缺月粒度的问题靠注入 Gold 计划包装为合格训练行。本次事实已同步飞书项目文档 revision 255。

### 2026-10-11：候选输入对齐第二步，运行时合同修复与旧问题歧义识别

完成对齐计划 TASK-003/004：`WorkspaceProfile.DimensionBinding` 将 TheLook 13 个逻辑维度绑定真实列，Catalog/QueryPlan 共用；明确不支持的维度进入澄清，不能忽略后变成标量查询。基于冻结 TheLook 数据库的只读聚合导出维度基数和五个事实字段的每日未过滤行数，用作结果上界预检；只保存聚合元数据、不放入候选 Prompt。Quarter 明确措辞、周一分桶、不同 workspace 年数边界均修正；只有“时间变化”而无粒度时澄清，单纯按季度看变化不误要求比较基线，真实比较请求仍检查基线。

`query-plan-v2` 规范列序为维度、时间、排序指标。`query_time.py` 统一绝对区间，明确 start-inclusive/end-exclusive：未排除结束日时转为下一日上界；单年为下一年首日上界。旧未标记会话状态不改写、需重新确认；ResultContract/Runner/Validator 显式传递排除上界标记，共用候选构造器拒绝端点漂移。保留历史离线模板和评测默认语义。回放修正为线上 1,000 行执行预算，而非旧默认 200 行预览预算。

最终 7,500 行回放为仓库外 `evals/runtime-candidate-alignment-20261011-fixed-r2/{summary.json,records.jsonl}`；源文件 hash 未变，中间 `fixed-r1` 仅留作诊断。TheLook train/validation 已准入 2,857/714 条，已准入行的指标/维度/粒度/端点/列集合均匹配原规格，仅列序差异 1,237/254。剩余缺粒度 123/36，train 20 条品牌分组超保守执行上界。Olist 已准入 2,973/742，但其中 2,902/728 条问题未声明排除结束日，而 Gold 使用排除上界；该旧问题歧义直到显式端点审计才被暴露。Olist 列序差异 2,026/545；未准入 27/8，原因是缺明确查询动作 24/4 和被判定义问题 3/4。各类有重叠，不相加，不作为 SQL 准确率。

相关 Python 回归 250 passed、7 skipped（数据库集成未启用），定向 Ruff、compileall、diff check 通过。覆盖真实维度列、预算、非法/闰日/同日区间、周/月/季度/年粒度、旧状态确认、列序、端点合同漂移与原工具/工件/图表链；尚未做新 Gold 小批执行准入。下一项直接在 train/validation 小批物化中补清可追溯的问法歧义、规范 Gold 顶层投影、核对实际 Catalog 依赖，再生成新 release。未读取 final test、调用 LLM/GPU或启动训练，默认 provider 与在线 Olist workspace 不变。本次事实已同步飞书项目文档 revision 256。

### 2026-10-11：候选输入对齐第三步，32 条小批物化与只读 Gold 准入

完成对齐计划 TASK-005。新增 `runtime_candidate_materializer.py` 和 `scripts/post_training/data/materialize_runtime_candidate_release.py`，从冻结的 train/validation 中确定性选取 Olist/TheLook × train/validation 各 8 条。先修正问题端点/粒度歧义并保存父问题与修改原因，再经实际 Catalog/Router/QueryPlan/ResultContract 共用入口生成 `runtime-candidate-sql-v2` 输入。模型输入是 rendered_prompt，监督目标是 candidate_sql 加 EOS；Spec、family、父 hash、执行结果仅为审计信息。Gold 仅规范顶层投影列序，不改公式、过滤或 Join。

最终小批证据在仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-pilot-20261011-r3/`：32/32 准入、0 排除，train/validation 各 16 条；覆盖 35 个不同指标、5 种结果形态、8 个逻辑维度和日/周/月/季度/年粒度。16 条补清排除结束日，2 条补清时间粒度。`review-preview.md` 展示原/新问题、实际 QueryPlan、输入与 SQL 标签；`selection-manifest.jsonl` 冻结来源，`evidence.jsonl` 留下每条问题/SQL/输入 hash、真实表列依赖、reader 角色、结果状态和 denotation，`manifest.json` 留下来源和输出文件 hash。r1/r2 是诊断中间产物，以 r3 为最终小批证据。

每条原/新 Gold 均在同一 REPEATABLE READ 只读事务执行，使用 `daa_analytics_reader` / `daa_thelook_reader`；经过计划 Guard、SqlPolicy、1,000 行执行预算和真实 ResultValidator，结果状态均为 valid，按既有数值容差协议均为 ordered_denotation_match。该证据证明迁移保持父 Gold 的结果，不是模型准确率或指标口径的独立业务证明。修复两处实际跨层问题：v2 Guard 只接受计划声明的 top_n，避免误套 Olist 旧城市 Top20；不能从可用翻译 Join 推断返回品类语言，展示标签改为“商品品类”。历史无版本计划仍保留旧 Guard 行为。

小批含 EOS token 长度为 1,410–3,463：23 条不超过 3,072，9 条标记 requires_length_decision；未截断，manifest 明确不是正式训练 release。下一项全量重建原 6,000 train / 1,500 validation，报告实际准入、排除、覆盖和长度分布后再冻结上限；4,096 暂为候选。本轮相关回归 218 passed，定向 Ruff、compileall 和 diff 检查通过；没有读取 final test、调用 LLM/GPU 或启动训练。本次事实已同步飞书项目文档 revision 257。

### 2026-10-11：候选输入对齐第四步，正式 train/validation 重建与 4,096 长度冻结

完成 TASK-006：同一物化入口新增 release 模式，绑定已完成的 32 条 pilot 与固定 source manifest/hash，在独立 screen `daa-runtime-release-20261011` 中以最多 4 路只读任务处理 7,500 条来源。原/新 Gold 经实际计划 Guard、SqlPolicy、各 workspace reader role、ResultContract/ResultValidator 与结果对照；先写 staging，源文件与运行时/Catalog hash、split/重复审计通过后发布正式目录 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-sql-v2-r1/`。完整日志在外部 `evals/runtime-candidate-materialization-20261011-r1.log`，未覆盖旧 release。

正式 train/validation 为 **5,980/1,500**：Olist **3,000/750**、TheLook **2,980/750**，共 **7,480** 条。20 条排除均为原 TheLook 品牌分组的保守 `result_row_budget_exceeded`（4 program × 5 window），不换问题填数、不放宽预算。全部准入行结果状态 valid，按既有数值容差全部 ordered_denotation_match。3,824 条问题有可追溯修改：补清排除结束日 3,630 次、粒度 159 次、查询动作 35 次，原因可重叠；保留原八问法主变体，train 各类 744–750 条，validation 186–188 条。

相对原来源没有丢失指标、场景类别或时间粒度。Olist train/validation 场景 family 为 455/113，program 类别 11/10；TheLook 场景类别 16/5，独立 program 596/150。Olist 两组覆盖 19 指标；TheLook train 18 指标、validation 3 指标，后者集中 event_count、registered_user_count、unique_session_count，仍有原分布的事实域偏斜。Olist 49 train + 16 validation 原 risk_tags 为空，coverage-audit 如实保留并报告；此前补标签在 DPO collector 的选择 metadata 中，不改写冻结 source，且风险标签不进入本次 SQL-only Prompt。此轮是输入协议迁移，不主张全面覆盖扩展。

正式输入为实际问题派生的 Catalog/QueryPlan/ResultContract；目标为规范顶层列序后的 PostgreSQL SQL 加 EOS，公式、过滤和 Join 不变。来源与输出的样本、QuerySpec、Gold、规范化问题和 Prompt 无重复或跨 split 交集。Olist 按原 family、TheLook 按独立 program 的 split_family_id 也为零交集；TheLook 共享 scenario 类别仅为覆盖标签，不作为隔离单位。`coverage-audit.json` 保存四组源/新指标、维度、grain、shape、风险和主问法分布；`evidence.jsonl` 保存每条溯源及只读执行证据。

train 长度 1,265–3,538（p95=3,300），validation 1,165–3,592（p95=3,339），合计 862 条超过旧 3,072，均完整保留；正式 manifest 冻结 **max_seq_length=4,096**，零截断、零长度排除。train SHA-256 `372254d553248ce7f5068b966f3ee99e805d9b78b0e0ed9d4cc1bab482b10b31`，validation `161e02be0e2b2c12cae4cf1e99d990a6b4370370e82b174b00e2e2622e7cc4a6`。独立回读逐条重算输入/标签、父记录/hash 和 token 长度通过，报告在外部 `evals/runtime-candidate-release-verification-20261011-r1.json`。相关回归 **230 passed**，Ruff、compileall、diff 检查通过。本轮没有读取 final test、调用 LLM/GPU 或启动训练；下一项为 Trainer v2 manifest/标签兼容、训练配置审阅与最小 GPU smoke。本次事实已通过 lark-doc 同步飞书项目文档 revision 258。

### 2026-10-11：候选输入对齐第五步，Trainer v2 兼容与最小 GPU smoke

完成 TASK-007 的审阅/smoke 小单元，完整训练尚未启动。`run_post_training_sft_smoke.py` 支持新 release：校验固定 manifest SHA-256、文件 hash/行数、实际逐条准入、运行时资产与 tokenizer 身份及 4,096 上限；全量 5,980/1,500 输入重新 tokenize 并确认记录长度、Prompt/completion 分界无漂移。v2 推理前缀精确停在 `### SQL`，分隔换行与 SQL/EOS 一起监督，Prompt 和 padding 不计 loss；旧格式标签布局不改。末步额外 eval/save，避免非周期末步不能进入 validation-best 选择。

新增有界 smoke 参数和 `scripts/post_training/launchers/start_runtime_candidate_sft_smoke_screen.sh`：校验完整 release 后，各 workspace 选择最长样本，实际只训练 8 条、评估 4 条，运行两次更新。逻辑 CUDA 1 / 物理 GPU 3 RTX 4090、UUID `GPU-10863af0-8588-7625-5609-640ba794f64b`，screen `daa-runtime-smoke-b2-20261011` 正常退出。batch=2、累积=2、有效 batch=4 无 OOM；峰值分配/预留 17.89/22.98 GiB，实际最长样本 3,592，未宣称验证任意恰好 4,096 的序列。

smoke 的平均 train loss 0.215921、4 条 validation loss 0.150052；392 个 LoRA 张量实际更新，Base 冻结，AdamW m/v 为 fp32。adapter_best 在 step 2，保存权重逐张量与 checkpoint-2 一致；重新加载 Base+best 后单条 loss 0.137117 有限。证据在外部 `experiments/runtime-candidate-sft-smoke-20261011-b2-r1/sft_smoke.json`（SHA-256 `79854c528ed8ffad6d34431f17228e2b428c10217bec0a35bfe58d418bb5fee2`），日志在 `evals/runtime-candidate-sft-smoke-20261011-b2-r1.log`。这些值仅证明工程流程可用，不能作为全量 validation 或生成质量提升。

下一步正式训练配置在原对齐计划中冻结：从原 1.5B Base 新训 bf16 LoRA，r16/alpha32/dropout0.05；非量化 AdamW、lr1e-4、constant scheduler、weight_decay0.01；train/eval batch2、累积2、两 epoch、seed20261011、max_seq_length4096。每250 optimizer step及最后一步对完整validation评估/保存，最小eval_loss选择adapter_best，adapter_final仅诊断；预期2,990 optimizer step。未使用 smoke adapter 续训，未读取 final test，默认在线 provider 未变。定向训练/旧格式回归42 passed、共用候选/物化回归28 passed，Ruff、compileall、bash语法和diff检查通过。本次事实已通过 lark-doc 同步飞书项目文档 revision 259。

### 2026-10-11：候选输入对齐第六步，正式两 epoch 训练已启动

20:05（Asia/Shanghai）按上一步冻结配置，从原 Qwen2.5-Coder-1.5B Base 新训 bf16 LoRA。使用已冻结的 5,980 train / 1,500 validation，batch2、累积2、两 epoch、4,096 上限、lr1e-4、weight_decay0.01、seed20261011，预计2,990 optimizer step。逻辑CUDA1 / 物理GPU3 RTX4090、UUID `GPU-10863af0-8588-7625-5609-640ba794f64b`；screen `daa-runtime-sft-20261011-r1` 已 detached，关闭SSH不影响运行。没有使用smoke adapter、旧adapter或final test。

新增 `scripts/post_training/launchers/start_runtime_candidate_sft_training_screen.sh`，代码基线 `ad5179a`：启动前核对release/smoke/model manifest hash、GPU空闲状态，记录完整argv、代码hash、git commit与开始时间，结束时保存exit状态；重复调用拒绝覆盖，未重复开训练。实际命令用Trainer parser核对冻结配置通过，bash语法、diff检查通过；Trainer代码复用上一轮已通过70条回归的版本，未改训练实现。

首个step250完成全量validation，`eval_loss=0.0004893963341601193`，耗时159.6秒，checkpoint保存后继续训练。首checkpoint只读回读确认LoRA配置、2,990总步数、fp32 AdamW m/v及optimizer/scheduler/RNG/trainer_state齐全，审计通过；loss/grad_norm有限、没有OOM/CUDA错误。此值仅用于训练期checkpoint选择，不能推出生成质量改善；正式训练尚未结束，最终adapter_best尚未确定。

仓库外运行目录为 `experiments/runtime-candidate-qwen25coder15b-bf16-lora-2epoch-20261011-r1/`，日志为 `evals/runtime-candidate-qwen25coder15b-sft-20261011-r1.log`，同名前缀下保存 `-launch.json` 与 `-first-checkpoint-audit.json`，结束后保存 `-exit.json`；外部根目录为 `/disk2/gengnan/data-analysis-agent-data/`。完整结束后Trainer会写运行目录的 `sft_smoke.json`、`adapter_best/`和`adapter_final/`。按当前速度整轮粗估约两小时；下一项等待完成后审查训练/validation-best证据，再准备matching Base/Adapter生成与Gold评测。本次事实已通过 lark-doc 同步飞书项目文档 revision 260。

### 2026-10-11：runtime candidate v2 两 epoch 训练完成与 best 冻结

完成对齐计划 TASK-007。训练于 22:17:59（Asia/Shanghai）正常退出，exit 0，完整两 epoch / 2,990 step；Trainer 记录含验证/复验耗时约 2 小时 11 分钟，平均 train loss 0.0012747832。12 个预先指定的完整 validation 点中 step 2,500 最优，eval_loss 为 0.000019297187463962473。末步权重的 loss 为 0.000027163852792000398；恢复 best 后完整复验回到最优值，两次 step 2,990 日志已经分开审计。

训练结束回读确认 release/runtime/Base 文件 hash 不变，保存的超参符合合同；best 精确匹配 checkpoint-2500，final 精确匹配 checkpoint-2990，两者 392 个 LoRA 张量均不同且有限。两个保留 checkpoint 的 optimizer/scheduler/RNG/trainer_state 齐全，AdamW m/v 为 fp32。独立进程重新加载原 Base + best，复用训练 Dataset，在两个 workspace 最长 validation 样本上通过权重、标签/EOS 和有限 loss 检查；没有再次评估全量 validation 或读取 final test。

完整结论见 [`训练结束验收`](docs/post-training/experiments/runtime-candidate-sft-v2-r1.md)。仓库外 eval 同名前缀新增 `-completion-audit.json`、`-best-checkpoint-freeze.json` 和 `-validation-curve.csv/.html`；后续只使用 `adapter_best`，权重 SHA-256 `926d16e4218fb9f83dc99cb1ef79094dac90adc397e3c5e0ad9526c7eb834c9e`。低 loss 不等于自由生成 SQL 的业务准确率；当前受控模板和 TheLook validation 三指标偏斜仍是限制。既有 Olist 403/750、TheLook 396/750 headline 成绩不变，默认在线 provider 不变。

下一项为 v2 matching 入口实现与共同解码合同冻结：共用真实候选构造器，保留原问题，非准入/超长记录仍计入各 750 条总分母。旧 QuerySpec 评测模板不能直接套用这轮 Adapter。本轮没有启动生成终测。本次事实已通过 lark-doc 同步飞书项目文档 revision 261。

### 2026-10-11：runtime candidate v2 matching 入口、共同合同与 validation smoke

完成 TASK-008 的入口部分，正式终测尚未运行。新增 `runtime_candidate_matching.py` 与统一 CLI `scripts/post_training/evaluation/run_runtime_candidate_matching.py`，按 freeze → prepare → generate(Base/Adapter) → evaluate 执行。生成侧只投影原问题与 ID，调用真实 Catalog/Router/QueryPlan/ResultContract 及共同候选 builder；不从 QuerySpec/Gold 反向补计划，也不修改 protected 问法。两侧完整输出的模型身份、输入、数量和 hash 核对后才读取 Gold；拒绝、超长、空输出和长度停止都留在固定分母。

解码固定 bf16、greedy、beam1、seed20261011、输入4096/输出1024、EOS，无采样或 SQL 修复。后置复用 Plan Guard、SqlPolicy、各 workspace PostgreSQL reader 的只读事务、1,000 行多读探测、ResultValidator 和容差 1e-6 的 ordered/bag 对照；只按预存合同规范时间结果列别名、按冻结列序重排 Gold 顶层投影，不改公式、Join 或过滤。safe report 保留原/规范 Gold 与候选 hash、生成/EOS、reader/合同/denotation；问题、SQL、错误原文和结果行不进入报告。原问题/Spec 的语义差异在生成后另报，不能一律算模型错误。

最终 validation smoke 使用两个 workspace 各一条固定验证样本，同一输入 hash。在物理 GPU 0/1 的两张 RTX3090 分别加载 Base/step2,500 best：Adapter 两条均 EOS、valid、ordered Gold 匹配；Base 一条 1,024 token 无 EOS，另一条 SQLSTATE42702 字段歧义。证据位于仓库外 `evals/runtime-candidate-v2-matching-validation-smoke-20261011-r3/`，结果为 `evaluation/safe-report.json`。生成耗时约29/37秒，不比较跨卡速度。此前 r1/r2 保留作中途诊断，以 r3 为最终版本证据。两条只能证明真实生成、受控执行和判分走通，不作为整体质量提升或新 headline 成绩。

新入口30项定向测试通过；共用候选/回放、统一清洗、Gold 比较、SqlPolicy、Plan Guard 和 ResultValidator 另70项回归通过，Ruff、compileall、diff check 通过。正式合同为仓库外 `evals/runtime-candidate-v2-matching-final-20261011-r1/contract.json`，SHA-256 `f68fdeda3aff58e492f50a9935b23b7e52cb12550c8f6b7193e6709ec1ba97a3`。冻结时只计算两个 final 来源的文件 hash，没有读取问题/Spec/Gold 字段、物化正式输入或启动完整生成。下一小单元按此合同处理原 Olist750 + TheLook750，screen 生成 Base/Adapter，完成后统一 Gold 对照。完整规则和代码地图见 [matching 合同](docs/post-training/data/runtime-candidate-v2-matching-contract-v1.md)。

本次事实已通过 lark-doc 追加飞书项目文档 revision 262，并局部回读确认成功。

### 2026-10-11：runtime candidate v2 正式终测输入封存与后台编排

用户确认后进入原 Olist750 + TheLook750 正式 matching。首次 prepare 发现入口误以为历史 Olist SFT 内含 question/Spec，实际问题按主问法ID保存在独立surface档案、Spec在structural档案；程序停止，未产生完整输入或任何正式模型输出。补存档适配及六项回归：prepare只恢复原主问题，拒绝旧Prompt/标签访问；Spec仍在两侧完成核验后读取并核对身份。没有改题、解析器、模型、解码或Gold语义。r1合同/失败日志保留，使用记录supersedes/reason的新r2合同，SHA-256 `c639a1fd71368e7b2e10ce0af199052a72bb69cb36f6b3ec83ea6e0eea46408c`。

仓库外 `evals/runtime-candidate-v2-matching-final-20261011-r2/inputs/inputs.jsonl` 已封存1,500条，SHA-256 `b96a1a268498b1c572071927aea9ae6c36840591e7cd3202deb2ecd80a870bb8`。Olist736 ready/14路由拒绝；TheLook717 ready/33路由拒绝。合计拒绝为12条缺查询动作、2条指标定义问题、33条缺时间粒度，保留总分母。已构造输入1,180–2,884token，没有超长或截断；不据此修protected问题或规则。

新增一次性 `scripts/post_training/launchers/start_runtime_candidate_matching_screen.py`：两个screen独立生成；逻辑CUDA0/物理GPU2给Base，逻辑CUDA1/物理GPU3给best Adapter，均RTX4090且记录UUID。独占launch防重复启动、原子status发布、实际argv/hash回读；Adapter成功后等待Base退出及两侧完整标识/hash核验，再自动调用统一evaluate。生成失败、worker失联或等待超过24小时均阻断Gold，不覆盖输出或补跑失败题。定向入口/启动编排45项、相关回归70项通过，Ruff、compileall、diff check通过。实际启动及首段运行证据随后记录；此时暂无正式质量成绩。

实际启动验收通过：2026-10-11 23:55（Asia/Shanghai），代码基线 `b82381a`，screen `daa-runtime-v2-base-20261011-r2` / `daa-runtime-v2-adapter-20261011-r2` 已detach；两个worker和generate子进程均存在，两侧首条已写记录，GPU运行正常。外部r2根目录的 `launch.json` 保存argv/GPU/自身hash，`screens-started.json` 记录成功启动；`base-generation.log` / `adapter-generation.log` 每条更新，worker log记录编排。完成时各写 `base-exit.json` / `adapter-exit.json`，后置执行写 `evaluation.log`、`evaluation-exit.json` 与 `evaluation/safe-report.json`。关闭SSH不影响screen。此状态仅为生成运行中，不能据首条或进程存活宣称质量改善；最终结果待生成和Gold全部完成。

实际重复启动检查被拒绝，未产生额外screen或生成进程。本次启动事实已通过lark-doc追加飞书项目文档revision263，并回读确认。

### 2026-10-12：runtime candidate v2 matching 评测修复与完整结果

上一轮 r2 的 Base/Adapter 生成均已完整（各 1,500 / 1,500），但 Gold 后置评测在第 713 条附近遇到 PostgreSQL 5 秒 `statement_timeout`，旧逻辑把 `QueryCanceled` 错判为数据库连接基础设施故障，导致没有安全报告。修复 [`runtime_candidate_matching.py`](src/data_analysis_agent/runtime_candidate_matching.py) 后，`QueryCanceled` / SQLSTATE `57014` 作为单条候选的 `postgres_execution_error` 记录并关闭连接继续；普通连接/接口错误、权限错误和 Gold 执行失败仍然阻断。新增超时回归测试，runtime matching 定向测试 46 passed，Ruff 通过。

为保留生成证据，新增 r3 后置合同并显式记录 `generation_contract_sha256`，不重新生成模型输出。r3 screen `daa-runtime-v2-eval-r3` 使用 r2 的 inputs、Base raw completion 和 Adapter raw completion，完成 `1,500 / 1,500` 条 Gold 对照。报告位于仓库外 `evals/runtime-candidate-v2-matching-final-20261011-r3/evaluation/safe-report.json`，修复与结果详见 [`runtime-candidate-matching-v2-r3.md`](docs/post-training/experiments/runtime-candidate-matching-v2-r3.md)。

固定 1,500 条分母中，Olist 750 条（736 ready、14 route rejected），TheLook 750 条（717 ready、33 route rejected）。Adapter 在 1,453 条 ready 输入上全部 EOS、全部通过 SQL Policy、reader-only 执行和 ResultContract；Gold ordered denotation 匹配为 **1,147 / 1,453（78.9%）**。其中 Olist **430 / 736（58.4%）**，剩余 177 条 denotation mismatch、129 条 row_count mismatch；TheLook **717 / 717（100%）**。Base 只有 1,041 条 EOS，412 条 length-stopped，最终 ordered match 为 41 条（另有 13 条仅 bag match）。

五项只读审计发现，Olist 704 条带时间范围的原始问题都存在 `time_range_mismatch`：运行时解析器因问题没有显式写“不含结束日”，把结束日加了一天；原 QuerySpec/Gold 却已经把原日期作为 `end_exclusive`。其中 Adapter 采用 Gold 边界的 266 条全部匹配；采用运行时边界的 438 条中 132 条结果仍匹配，剩余 177 条 denotation mismatch 与 129 条 row_count mismatch 不能直接归因于模型结构错误。Olist 端点一致的 32 条和 TheLook 端点一致的 717 条全部匹配，source-semantics-consistent 子集为 749/749，但不能替代固定分母成绩。另一个风险是 266 条虽然匹配旧 Gold，却偏离了输入中的 QueryPlan 时间上界；现有 Guard 未精确校验时间谓词，ResultContract valid 不等于遵循计划。

Olist 388 条、TheLook 222 条时间序列的 DATE_TRUNC 粒度均符合请求；Olist 736 条候选在统一 Gold 最终列序、遮住日期字面量后，完整 PostgreSQL AST 736/736 一致。129 条行数差异全部恰好多一行（日43、周14、月36、季度27、年9），与结束日多纳入一个时间桶一致；未发现遗漏状态过滤、重复 JOIN 或错误聚合层级。Olist train/validation/test 的时间序列是1,511/417/388，structural_hard 主桶468/118/119，19项指标全部覆盖，family455/113/285且三对split交集均为零；没有证据支持整体时间序列训练不足。实际薄弱项是品类分组仅17/8/11，低曝光的商品/评论 scalar 程序，以及 TheLook validation 只覆盖事件/会话/注册三个指标的事实域偏斜。训练2,902条、验证728条时间样本都已显式补清排除结束日，与保持原问法的protected评测不同。

本轮只读诊断没有更改来源、输入、生成、Gold或checkpoint。详细方法、分布与限制见上述实验报告，聚合hash证据在仓库外 `evals/runtime-candidate-v2-matching-final-20261011-r3/evaluation/diagnostic-audit-20261012.json`。下一项应先冻结时间语义一致的诊断合同，明确评估原标签还是实际QueryPlan编译；原76.5%固定分母结果保留并标注704条历史端点不一致，不作为干净语义准确率，不将306条受保护失败题回流SFT/DPO。

已完成下一步决策：冻结 [`runtime-queryplan-aligned-evaluation-contract-v1.md`](docs/post-training/data/runtime-queryplan-aligned-evaluation-contract-v1.md)，后续以实际运行时 QueryPlan 的 deterministic Gold 作为主评测目标，原 QuerySpec/Gold 作为辅助兼容性指标。新合同要求显式 `[start, end)`、单独报告 `plan_adherent` 与 `gold_disagreement`，并保留 route rejected/ambiguous 的固定分母；r3 原始输入、输出、Gold 和结果不变。当前只冻结合同，尚未重新生成 GPU 结果；下一步实现并测试 QueryPlan → deterministic Gold SQL renderer。

已完成 aligned renderer 第一步：新增 [`runtime_queryplan_renderer.py`](src/data_analysis_agent/runtime_queryplan_renderer.py)，只从运行时 QueryPlan、ResultContract、Catalog 和 Workspace 生成 Gold SQL，并保存 QueryPlan/ResultContract hash、版本、边界和结果列证据；不把内部桥接 QuerySpec ID 暴露到 runtime artifact。它支持标量、客户州/品类分组、时间序列及维度+时间序列，遇到 Top-N、比较基线、未冻结 attribution 或未支持维度会 fail closed。既有 Olist renderer 已补上 runtime workspace identity 与维度+时间复合 join 支持。54 个定向测试通过，定向 Ruff 通过；三条 CPU 样例通过 SqlPolicy。尚未执行 PostgreSQL 小批准入、GPU 生成或训练，下一步是 aligned validation 小批的 renderer→Policy→reader→ResultContract/ResultValidator 准入。

已完成 Olist aligned Gold 小批准入：脚本 [`admit_runtime_queryplan_gold_batch.py`](scripts/post_training/evaluation/admit_runtime_queryplan_gold_batch.py) 从冻结 `runtime-candidate-sql-v2-r1/validation.jsonl` 按 scalar、customer_state、product_category_name、time_series 各 4 条选择 16 条，不读取候选模型输出或 protected test。16/16 通过 runtime renderer、SqlPolicy、`daa_analytics_reader` 的 read-only `REPEATABLE READ` PostgreSQL 执行及 ResultValidator；所有结果低于 1,000 行执行上限和 200 行展示预算。证据目录为仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-gold-admission-20261012-v2/`，只保存 QueryPlan/ResultContract/SQL hash、版本、reader 身份、行数和验证状态。首版小批发现并修复了离线 renderer 的 metrics-first 与 runtime time-first 时间序列列序差异。下一步是按 scenario family 扩大 CPU 小批，并明确未冻结 Top-N、比较基线、attribution 的拒绝分类；当前仍未启动 aligned GPU 生成。

已完成第二轮 family/risk 小批准入：同一脚本以 `--max-families 32` 从 Olist validation 做风险标签、结果形态和时间粒度的确定性 greedy set-cover，覆盖 32 个不同 `scenario_family_id`、9 类风险标签及 day/week/month/quarter/year；32/32 通过 renderer、SqlPolicy、reader-only PostgreSQL 和 ResultValidator。证据目录为仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-gold-admission-20261012-v3/`。该轮按 family 自然分布为 19 条时间序列、6 条客户州、6 条标量、1 条品类，上一轮四形态各 4 条的平衡 smoke 仍保留；当前尚未物化完整 aligned Gold 或启动 GPU 生成。

已完成 Olist validation 全量 Gold 准入：同一脚本以 `--all-validation` 处理冻结的 750 条 Olist validation，仓库外证据目录为 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-gold-admission-20261012-v4/`。结果为 `750/750 admitted`，其中时间序列 417、标量 183、客户州分组 142、商品品类分组 8；全部通过 renderer、SqlPolicy、`daa_analytics_reader` 的 read-only `REPEATABLE READ`、ResultValidator，未发生截断，最大 92 行，750 条 `plan_adherent=true`。SQL hash 有 724 个唯一值，重复只表示相同确定性程序。当前尚未生成 Base/Adapter 候选或计算模型准确率；TheLook 仍需先完成自己的 runtime QueryPlan renderer 与同等级 Gold 准入。

已完成 TheLook runtime QueryPlan Gold：新增 [`runtime_thelook_queryplan_renderer.py`](src/data_analysis_agent/runtime_thelook_queryplan_renderer.py)，复用 TheLook v2 已审阅的 21 项指标公式，但入口只接收运行时 QueryPlan、ResultContract、Catalog 和 Workspace；TheLook 小批 32/32、5 个 family 小批 5/5 通过。随后对冻结 TheLook validation 全部 750 条执行准入，证据目录为 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-thelook-queryplan-gold-admission-20261012-v3/`，结果 750/750 admitted：时间序列 290、事件浏览器/事件流量来源/事件类型各 65、注册用户国家 100、注册用户州 65、注册流量来源 95、标量 5；5 个 family、5 类风险标签及五种时间粒度均覆盖。全部通过 TheLook renderer、SqlPolicy、`daa_thelook_reader` read-only `REPEATABLE READ`、ResultValidator，最大 125 行、无截断、低于 1,000/200 行预算，750 个 SQL hash 唯一。两个 workspace 的 aligned validation Gold 分母已准备好，下一步才是构造双 workspace 对齐输入并开始 Base/Adapter generation。
已物化双 workspace aligned release：脚本 [`materialize_runtime_queryplan_gold_release.py`](scripts/post_training/evaluation/materialize_runtime_queryplan_gold_release.py) 生成仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-aligned-v1-20261012/`，包含 `inputs/inputs.jsonl` 1,500 条模型输入与独立 `gold/gold.jsonl` 1,500 条主 Gold。输入严格剥离 QuerySpec、candidate/Gold SQL、training text 和历史 execution evidence；Olist/TheLook 各 750 条，source_id 唯一，输入与 Gold hash 和顺序一致，Gold SQL hash 共 1,474 个唯一值。接下来只需审阅统一 Base/Adapter generation 入口，才能启动 GPU 评测。
已完成 aligned Base/Adapter 生成入口与 smoke：`generate_runtime_queryplan_aligned.py` 只读取运行时 `inputs.jsonl`，不读取 Gold 或数据库，使用 bf16、greedy、beam=1、EOS、最大 1,024 新 token，并固定 batch=4；`evaluate_runtime_queryplan_aligned.py` 在生成后统一执行 `unwrap_sql_completion -> QueryPlan guard -> SqlPolicy -> reader-only PostgreSQL -> ResultContract/ResultValidator -> Gold denotation`。4 条 Base 与 4 条 Adapter smoke 分别命中物理 3090；matching smoke 走通，但不作为全量质量结论。完整 1,500 条成对生成已在 screen `runtime-aligned-base-r2-20261012` / `runtime-aligned-adapter-r2-20261012` 后台运行，输出位于 aligned release 的 `generation-base-full-r2/`、`generation-adapter-full-r2/`；完成后以 runtime QueryPlan Gold 的 ordered denotation 为主指标，旧 QuerySpec Gold 仅作辅助诊断。
已完成 aligned runtime Gold 的 Base/Adapter 正式 matching：报告位于仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-queryplan-aligned-v1-20261012/matching-full-r1/`，固定分母 1,500。Adapter-best 全部 1,500 条通过 Policy、reader-only PostgreSQL 和 ResultContract；ordered denotation 为 1,469/1,500（97.93%），另有 30 条 bag match，真实 mismatch 1 条。Olist 为 ordered 719/750（95.87%）+ bag 30，TheLook 为 ordered 750/750（100%）。唯一真实 mismatch 是 Olist 周时间序列多指标的二层订单聚合，Adapter 将 AOV 的“按订单汇总后取平均”写成直接平均商品价格。Base 仅 131 条 valid，1,005 Policy rejected、238 PostgreSQL execution error、126 ResultContract rejected；ordered 17/1,500（1.13%），ordered-or-bag 23/1,500（1.53%）。这证明 Adapter 在当前 aligned 合同上的可执行和语义结果显著优于 Base，但不能把 `plan_adherent` 单独当作语义正确；完整说明见 [`runtime-queryplan-aligned-evaluation-contract-v1.md`](docs/post-training/data/runtime-queryplan-aligned-evaluation-contract-v1.md)。
已新增 `summarize_runtime_queryplan_aligned.py` 生成仓库外 `aligned-diagnostic-v1.json`，对 workspace、scenario family、risk tag、状态和 Base→Adapter 转移做脱敏聚合。诊断确认 Adapter 的非满分主要是 30 条行顺序不同和 1 条 Olist 二层订单聚合公式错误；Base 的失败主要集中在 `time_bucket`、`multi_metric_cte_merge`、列/关系不存在和字段歧义。该结果仍是 validation-only，未回流训练或改变 protected final test 合同。

### 2026-10-12：冻结分层结果预算合同并建立 perf_shop 性能夹具

针对“所有查询统一 1,000 行”不适合大型电商数据的问题，冻结 [`result-budget-v2`](docs/architecture/result-budget-v2.md)。合同将查询分为 `aggregate`、`detail_page` 和 `export` 三种模式，分别定义数据库执行、业务结果基数、模型摘要、前端预览、分页与导出边界。当前在线全局 1,000 行仍是兼容性兜底，没有直接放宽；前端预览继续 200 行，模型摘要继续最多 8 行/1,200 字符。新的大结果异步导出还未实现；现有 CSV 路由已经具备用户/workspace 鉴权、工件完整性验证和审计，只下载已有的已验证有界结果。

新增独立 synthetic `perf_shop` workspace，不进入 Olist/TheLook Catalog，也不接入默认在线路由。构建入口为 [`scripts/infra/build_perf_shop.sh`](scripts/infra/build_perf_shop.sh)，使用确定性 `generate_series` 生成 100,000 或 1,000,000 条订单及两倍明细，独立 `perf` schema 和只读 `perf_shop_reader` 角色；manifest/schema 位于仓库外 `data-analysis-agent-data/perf/perf-shop-v1-{100000,1000000}/`。1,000,000 订单 smoke 的 aggregate、36 行月序列、200 行明细、keyset 下一页和 100,000 组高基数分组均成功；当前记录在 `perf-shop-v1-1000000/benchmark.json`，总耗时约 683ms，`EXPLAIN` 估算高基数分组约 100,945 行、总成本 68,814。该结果只证明性能/结果预算夹具可运行，不代表模型语义准确率。

下一步是基于夹具测量 `EXPLAIN` 成本、分页、结果字节数和异步导出边界，再实现 QueryPlan 的分层预算字段；不会先把全局 1,000 改成更大的固定值，也不会为了性能测试再引入第三个业务数据集。相关合同 manifest 为 [`data/manifest/result-budget-v2.json`](data/manifest/result-budget-v2.json)，定向回归为 `tests/test_result_budget_contract.py`。

### 2026-10-13：perf_shop 结果预算基线完成

同一 `benchmark_perf_shop.py` 在 100,000 和 1,000,000 订单规模复测，证据分别位于仓库外 `perf/perf-shop-v1-100000/benchmark-v2.json` 与 `perf/perf-shop-v1-1000000/benchmark-v2.json`。1,000,000 订单下标量聚合返回 1 行、月序列 36 行、明细页 200 行并通过 sentinel 确认仍有下一页；高基数客户分组返回 100,000 行、CSV 约 889 KB，导出模拟约 346 ms。`EXPLAIN` 估算标量 1 行/成本 15,468.57，高基数分组约 100,282 行/成本 68,759.2。

这组结果支持“扫描规模、业务结果基数、前端预览和模型上下文必须拆开”的设计，但不直接给出生产阈值，也不证明任意 Olist/TheLook Join 都具有相同性能。新增 [`result-budget-v2-performance-baseline.md`](docs/architecture/result-budget-v2-performance-baseline.md)；下一步仍先把 `query_mode`/预算以服务器字段接入 QueryPlan 与 ResultContract，再决定哪些聚合可完整返回。

### 2026-10-14：模式与执行预算接入 QueryPlan / ResultContract

完成本轮 [`任务卡`](plan/feature-result-budget-runtime-v2.md) 的代码与验收。
新增 `result_budget.py:ResultBudget` 不可变 DTO，composition root 按实际 PostgreSQL settings
生成 aggregate 预算；在线 Handler 清除客户端模式/预算/计划后，将同一预算传给
QueryPlan 和 ResultContract，并存入工具 metadata 与 Agent Run 的计划/合同证据。

Runner 在 Policy 和 reader 查询连接前核对两份预算及实际执行/validator 配置，拒绝
缺失配对、非法字段、预算不一致、配置漂移和未启用的 detail_page/export。拒绝通过
应用审计角色写 query_audits 的 `result_budget:...` 原因，不调用模型修复。当前只启用
Catalog 指标 aggregate，默认仍执行 1,000 行/5,000ms、前端预览 200 行、模型摘要
8 行/1,200 字符；aggregate_result_rows 暂与执行兜底一致，不宣称已经完整返回高基数聚合。

冻结 runtime-candidate-sql-v2 构造器先核对预算，再显式投影原 SQL 语义字段。同一
请求的新旧 CandidateSqlRequest 字节与 Prompt SHA-256 相同；旧离线调用不带双方
预算时保持旧协议，不重建数据/训练/历史成绩。修正了设计和性能记录中的导出表述：
现有鉴权/workspace 隔离/完整性核验/审计 CSV 接口下载已有的有界验证结果；新的
大结果异步 export 模式仍未实现，通用 DataFrame 导出开关也未放开。

验收为合同/执行链 207 项、历史候选/评测/运行时回归 138 项、真实本地 PostgreSQL
集成 8 项，共 353 项通过；定向 Ruff、任务卡 ID 声明唯一性与 diff check 通过。
执行边界测试证明拒绝不调用 Policy、不连接 reader 且保留审计，工具级测试确认不
调用 SQL repair。测试未包含新分页/异步导出/EXPLAIN 成本门或新 GPU 评测。

学习记录写入 [`agent-project-walkthrough-v1.md`](docs/learning/agent-project-walkthrough-v1.md)
的结果预算小单元，阅读顺序为预算创建、在线传递、执行校验；不将 Day 1 自动标为学完。
下一小单元按 aggregate 结果形态/维度基数区分完整统计、明确 Top-N 与需澄清的高基数分组，
再用 perf_shop 验证实际返回行为。

本轮事实已用 lark-doc 同步飞书项目文档 revision 269，并局部回读确认；同时定向修正
上一条性能基线记录对已有 CSV 下载能力的过时描述。

## 2026-10-14｜结果预算收尾，回到四周学习计划

在线完整 aggregate 不再统一卡在 1,000 行。Router/QueryPlan 按维度基数、时间桶、
明确 Top-N 派生本次预算；进程兜底默认 100,000 行、32 MiB 估算 JSON 字节、5,000ms。
SqlPolicy 保留 AST/表列/敏感投影检查，替换模型附加的顶层 LIMIT/OFFSET；明确 Top-N
由服务端计划决定。Runner 使用 PostgreSQL named cursor，每批最多 1,000 行，
资源 LIMIT 多读一行判断真实溢出；完整聚合超界返回 needs_clarification，不把部分结果
当作正常统计。冻结历史离线协议与数据不迁移。

完整验证结果保存在 CSV 工件，新增每 200 行的稀疏索引及鉴权 rows 接口；网页
“更多 → 分页查看完整结果”显示行范围/总行数，支持上一页和下一页，默认 200、接口最多
500 行。分页和现有审计 CSV 下载都读取同一验证工件，不重新执行 SQL、不调用模型。
保持初始 Presenter 200 行、模型摘要最多 8 行/1,200 字符，权限/版本/完整性隔离不变。

百万订单 perf_shop 的生产 Runner 六场景通过：汇总 1 行、日统计 1,095 行、候选自带
LIMIT 1000 的分组完整返回 100,000 行、明确 Top-5 返回 5 行、基数或字节超界拒绝。
10 万行结果 CSV 为 988,920 字节，估算 JSON 为 4,188,897 字节，摘要仅 338 字符；
完整分组含工件保存和首尾页核验约 2.14 秒。单次本机测量不代表并发 SLA 或模型准确率。
性能夹具使用真实只读登录、内存审计；产品审计/下载另由真实 PostgreSQL 路由测试覆盖。
原始证据位于仓库外 `perf/perf-shop-v1-1000000/runtime-v2-final.json`，
可复跑脚本为 `scripts/infra/benchmark_perf_shop_runtime.py`。

验收为 212 项 Python 定向回归通过，包含 SQL/结果合同/历史候选协议/工件/真实 PostgreSQL/认证下载与隔离；
桌面和手机的新增浏览器分页 2 项通过，当前壳移动端布局另 1 项通过。定向 Ruff 与
diff check 通过。旧常驻页面综合浏览器套件的菜单与旧宽度断言不作为本轮通过证据，
本轮用隔离服务测试实际新增页面，未改动无关 UI 基线或停止用户常驻服务。

合同与 manifest 已更新为 implemented，AGENTS 只记录稳定在线/离线边界，未记录进度。
飞书项目文档已同步至 revision 271。未创建任意原始敏感明细 SQL、异步导出平台、
EXPLAIN 成本门，未启动新模型训练/终测。本专题结束，按照
`plan/process-agent-internship-improvement-v1.md` 回到 Day 1 的工具执行、结果校验和
可信依据；学习记入 `docs/learning/agent-project-walkthrough-v1.md`，不自动标记学完。

## 2026-10-14｜自然语言入口 Phase 0 / Phase 1

默认在线入口已用 LLM 结构化 SemanticIntent 替代固定句式的 classify/admit。模型理解
请求类型、指标、维度、时间、筛选和图表要求，服务器校验 Catalog/归属/角色/资源能力
后生成同一份 QueryPlan 与结果合同；不从历史摘要提取 SQL 条件。WorkingMemory 不再
用旧正则重解读这类在线请求，解析失败也不回退旧关键词。SQL 安全和 reader 权限保持。

32 条自然问法旧基线仅 6 条准入，新入口真实模型调用 32 条解析成功、20 条查询准入；
不是 SQL 正确率。真实认证、SSE、数据库、模型六场景通过，覆盖查销售额、按州追问、
继续不查库、画曲线、口径解释和写库拒绝。243 项 Python 定向回归通过，8 项数据库
测试跳过；真实 HTTP smoke 单独验证数据库。定向 Ruff 和 diff check 通过。

语义调用中位延迟约 3.73 秒；仍有过度澄清/多选城市维度。指标继续受 Catalog 定义
约束，额外筛选仅编译单客户州，不宣称任意分析/增长率/归因能力已完成。
详见 `plan/feature-semantic-intent-phase01.md`；证据保存于仓库外
`evals/semantic-intent-phase01-20261014/`。现有 32013 产品服务加载新入口，页面路径不变。
本轮未启动训练；继续原四周 Day 1 的模块学习，问答记入 agent-project-walkthrough。
飞书项目文档已用 lark-doc 追加同步至 revision 272，并回读确认。

## 2026-10-14｜Phase 2/3：语义范围修复与在线验收

针对 Phase 0/1 暴露的两类问题做了小范围修复：明确按州分析时，语义提示和回归合同要求
只生成 `customer_state` 分组，不再擅自增加 `customer_city`；“只看圣保罗州的订单”
按 Catalog 默认客户地域编译为 `customer_state=SP`，只有用户明确提到卖家才选择卖家维度。
新增结构化期望检查，把“JSON 能解析”与“指标/维度/筛选/是否应查库”分开统计。

Phase 2 的 8 条真实语义回归全部通过，包括两个原问法的改写、明确城市、明确卖家州、
比较缺少时间基线和不支持利润。Phase 3 的两个真实认证 SSE + PostgreSQL scope probe
均通过：每条一次 SQL、ResultContract 满足、ResultArtifact 可回读；服务端 32013 重启后
页面返回 200。语义解释器对 provider 异常只重试一次，最终失败不进入 SQL。

本轮定向回归 `246 passed, 8 skipped`，Ruff 与 diff check 通过。证据位于仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/semantic-intent-phase23-20261014/`，
任务卡为 `plan/feature-semantic-intent-phase23.md`。这些结果只说明语义范围和在线接线
符合合同，不是 SQL 业务准确率、吞吐或生产 SLA；下一步回到四周计划继续 Day 1 小单元学习。

## 2026-10-15｜上下文与记忆 v2：Phase 0 基线完成

按 [`architecture-analysis-context-memory-v2.md`](plan/architecture-analysis-context-memory-v2.md)
完成 TASK-001～003：核对线上请求、会话历史、WorkingMemory、摘要、执行审计和结果工件的
实际读写与失败边界；新增 [代码基线](docs/architecture/analysis-context-memory-v2.md)，
统一 ConversationHistory、AnalysisState、ExecutionMemory、ResultArtifact、ReferenceBinding
和 ContextCompaction 术语。本轮仅修改文档，未改变在线行为、SQL 权限或预算，未启动训练。

默认在线入口已有语义解释器，不调用旧 classify/admit；QuestionRouter 仍承载 Catalog 等辅助
职责及旧离线兼容。确认待改缺口：语义解释器没有读取近期对话/可信实体引用；历史全量读写；
持久摘要未用于增量续接且来源范围可能大于实际摘要输入；当前计划与上一成功结果缺少 task/
execution/state 关联。各项已归入后续阶段，未标为已修复。

现有定向回归 68 passed / 6 skipped，随后启用项目 PostgreSQL 独立运行会话存储测试
6 passed，合计覆盖 74 项不同测试。该段是 Phase 0 完成时的历史记录；后续已按同一计划
完成 ConversationHistory 与 AnalysisState 的 Phase 1 改造，当前结果见下方 2026-10-15
Phase 1 记录。

已通过 lark-doc 追加到飞书项目文档 revision 274，并局部回读确认。

## 2026-10-15｜上下文与记忆 v2：Phase 1 完成（M1–M4 联合验收）

在不新增第二份权威状态、不改变认证、SQL 安全、reader 权限和结果合同的前提下，完成
`AnalysisState` 与 ConversationHistory 增量读写改造。`AnalysisState` 现在统一保存 active
analysis task 的 task/turn/state version、workspace 与数据/指标版本、指标、时间、维度、筛选、
结果形态、当前计划、最后成功计划和可信结果工件指针；`WorkingMemory` 只做兼容映射，在线
QueryPlan 不再从两份状态中择一。

会话存储复用既有 `message_id`、`message_index`、metadata、JSONB、事务和 `state_version`：
新增近期完整 turn 读取、cursor 消息分页和增量消息追加；消息带服务端 turn/run/task 关联，
失败状态与既有 run 审计关联。更新仍按 workspace/用户隔离并使用 `SELECT ... FOR UPDATE`，
过期版本返回冲突而不是静默覆盖。清空分析只创建新的 active task、清除当前计划和工件指针，
保留聊天历史；刷新只恢复同一会话，不重新激活旧任务条件。

M1 宿主上下文与 Bootstrap、M2 workspace/会话版本、M3 ResultArtifact Presenter、M4 可嵌入
Copilot Shell 已通过联合验收。Python 定向回归 **116 passed, 15 skipped**；定向 Ruff、
compileall、diff check 通过。真实浏览器通过 8 项组合回归，并额外通过真实认证、SSE、
PostgreSQL reader、SqlPolicy、ResultContract、ResultArtifact 的“查询 → 工件清单 → 刷新恢复 →
消息分页 → 清空分析 → 清空后新查询”链路。真实模型偶发生成不符合 QueryPlan 的候选时，后端
仍拒绝并标记失败；这说明安全边界生效，不代表模型业务准确率。

当前仍未完成：ExecutionMemory 独立视图、上一轮排序实体的可信引用解析、跨会话长期记忆、
真实 SSO/跨域 iframe、完整导出入口以及生产 SLA。下一步按计划进入 Phase 2，先让每次执行与
当前 task/state/artifact 的复用关系可回放、可判定；不要把本轮浏览器通过写成多轮业务语义已经
完成。

## 2026-10-15｜上下文与记忆 v2：Phase 2 完成

实现逐次执行的 `ExecutionMemory` 视图，复用 `query_audits.audit_id` 与既有 `agent_runs`
及 JSONB trace，不新增表、数据库迁移或 Plan/SQL hash。一次 SQL 尝试关联自己的
run/task/turn、父/当前 state version、计划和查询范围、验证状态与 artifact；同一轮先失败后成功
保持两条独立证据，不把整轮最终成功套在失败尝试上。新增所属会话的执行记录读取 API，
不返回原始 SQL、数据库原始错误或完整结果行。

`ResultArtifact` 保存查询范围、真实结果列、可由 SQL AST 证明的排序、最多 8 行分组实体及
执行关联。结果解释在复用前核对范围、用户/workspace、版本和文件完整性；条件变化或工件失效
不能用旧结果回答。展示/分页继续读取原工件，范围相同的解释不重复查库。没有新加兼容层，
也没有额外增加 LLM 调用。禁止无必要兼容层的协作约束已写入 `AGENTS.md`。

真实测试修复两处实际缺陷：保存工件读取错误的嵌套字段，导致数据/指标版本成为 `unknown`；
页面启动遍历历史的回退及组件注册时序，导致新分析或刷新后可能被旧会话覆盖。现直接读取
服务端平铺版本字段，只恢复选定会话，等待组件就绪并忽略已过时的加载响应；旧 unknown 工件
不补推为有效，不增加旧格式兼容分支。

验收：项目数据库开启后的定向 Python 回归 **127 passed**；浏览器组合回归 **9 passed,
9 deselected**。真实模型/PostgreSQL 链路覆盖查询、执行证据、刷新、解释结果不重复 SQL、
清空及新查询；其他浏览器用例覆盖历史恢复、移动端、预览和受控 SSE。Ruff、compileall、
diff check 通过。截图：仓库外 `evals/analysis-memory-phase2/browser/`。

下一步按计划进入 Phase 3：从可信结果实体解析“这个州/上一轮第几名”并生成新查询条件。
Phase 2 尚不等于实现跨轮实体下钻、持久摘要增量续接或跨会话长期记忆。

## 2026-10-15｜上下文与记忆 v2：Phase 3 可信结果实体引用完成

新增 `ReferenceRequest` 和 `result_reference.py`，把“这个州/上一轮第几名”限制为服务端可验证的
排名实体引用。语义模型只能提交引用类型、维度、排名和匹配语义，不能提交实体值；服务端从当前
已通过 `ResultValidator` 的 `ResultArtifact` 读取有界 `ranked_entities`，校验用户/工作区、数据与
指标版本、结果列、排序方向、排名范围和并列情况。第一行与唯一最高分开处理：第一行可以按
`first_row` 引用，唯一最高必须是降序第一行且与第二行不并列。

成功绑定后，运行时将解析出的值写入 `AnalysisState` 的服务器拥有筛选，再继续原有
Catalog → QueryPlan → ResultContract → SqlPolicy → PostgreSQL reader → ResultValidator 链路。
值不从助手自然语言、HistorySummary 或客户端字段提取；引用失败只产生可解释澄清，不进入 SQL。

本轮同时清理了 10 个静态无引用导入、1 个无用局部变量和 1 个重复类型字面量；QuestionRouter、
WorkingMemory、历史离线评测路径和 `/embedded-demo` 经引用审计仍在使用，未误删。定向联合回归
**193 passed**，定向 Ruff、compileall、diff check 通过。浏览器真实验收已通过：第一轮排名查询、
跨轮实体下钻生成新 SQL 的独立测试 **1 passed**，既有浏览器组合 **9 passed**；台账验证引用成功
绑定到 `customer_state=SP`，且后续继续经过既有 SQL 治理链。一次完整重跑遇到在线模型提供方超时，
未输出未经验证结果。Phase 3 不将单次链路写成任意业务语义准确率。

## 2026-10-16｜上下文与记忆 v2：Phase 4 持久增量摘要与请求预算

完成实施计划 TASK-018～022。新增 `AnalysisContextBuilder`，在语义理解之前装配当前任务的
最近完整问答、持久叙事摘要、未解决澄清、分析状态及可信结果引用元数据；当前问题、Catalog 和
可用工具分别由实际调用方加入，最终在 `ObservedLlmService` 检查完整请求预算。
近期历史上限 6 turns/6,000 tokens，摘要 1,000、state/reference 2,000；
总体 64K、history ceiling 48K，包含输出预留。使用本地 Llama tokenizer 代理估算，
不宣称在线 DeepSeek 精确计数；无 tokenizer 时明确标记字符估算。

摘要从同一 owner/workspace 的消息原文按完整 turn 增量读取，输入为旧摘要与新增省略问答，
保存实际消息 watermark 和 task/版本 scope；复用原有 JSONB、事务和 state_version。
删除旧进程内缓存、旧 Filter 摘要生成入口及 Handler 后置摘要写入，不增加兼容分支或新表。
旧工具调用与工具结果不再作为历史进入模型；当前工具轨迹仍配对保留。
摘要失败保留上一有效版本、不推进覆盖位置；超过 30 秒也不终止本轮正常分析。
safe trace 新增摘要状态、失败类型、耗时及各模型调用的预算来源，不写原始提示词或异常文本。

清空/版本变化后旧摘要不进入新任务；编辑/撤回清掉派生 memory。结果实体继续由可信 artifact
绑定，不从摘要生成筛选。线程内 recall 仅提供最多 3 条完整叙事/800 tokens 的显式接口与来源
索引，没有接入自动召回、向量库或跨会话长期记忆。单 turn 超过摘要输入预算时不截断并假称完成，
记录 source_turn_too_large；该极端情况暂不做分段摘要。

验证：开启项目 PostgreSQL 的定向联合回归 **274 passed**；定向 Ruff、compileall、diff check
通过。新版服务浏览器组合 **10 passed、1 failed、9 deselected**，唯一失败为摘要未在 30 秒内
生成，SQL/状态/结果复用正常。没有放宽断言；长对话独立复验 **1 passed、19 deselected，
59.58 秒**，完成真实查询 → 追加长对话 fixture → 刷新 → 生成持久摘要 → 追加历史 → 刷新 →
增量续接；两次解释均无新增 SQL，artifact、task 和筛选保持一致。组合同时验证真实实体下钻、
清空与新查询、页面/移动端、历史恢复和预览。Provider 延迟仍影响交互和摘要生成，
不能将成功复验写成无条件的长对话准确率或稳定性结论。

截图复核还发现结果解释入口仅收到数值摘要，没有已验证时间/维度，因此会误称查询范围未知。
已将 artifact 的可信查询范围与有界叙事接到无工具解释入口，删除原 1,000 字符截断；
数值约束只允许可信摘要/范围中的数字，叙事历史不能扩充数字来源。补充两项确定性回归，
并在浏览器长会话用例中要求解释真实 2017 年范围且仍不新增 SQL。补齐后的最终浏览器
独立验收 **1 passed、19 deselected，49.59 秒**，确认日期解释与持久摘要续接同时正常。

本轮证据与截图：仓库外 `evals/analysis-memory-phase4-20261016/`。
新版服务运行于 screen `data-analysis-agent-phase4`、`http://127.0.0.1:32015/embedded-copilot`；
既有 32013/32014 服务未替换。架构说明已更新到
[`analysis-context-memory-v2.md`](docs/architecture/analysis-context-memory-v2.md) 第 10 节。
下一步为计划 Phase 5 的多轮产品验收与指标化，不提前引入长期记忆或训练。

## 2026-10-16｜上下文与记忆 v2：Phase 5 多轮产品验收

完成 Phase 5 的真实多轮验收准备、代码修复和证据整理。`AnalysisContextBuilder` 现在在持久摘要
watermark 已经覆盖近期窗口时不再读取反向范围，避免摘要复用过程中出现无效的历史区间；语义解释
提示明确要求 `catalog_definition` 也填写对应 `metric_ids`，定义问题不会因为只写在 assumptions
而被错误澄清。SQL 计划提示补充了二层聚合应使用具名 CTE 的约束，避免模型用内联子查询触发敏感
标识列策略拒绝；策略本身没有放宽。

确定性 Python 定向回归 **244 passed**，上下文/意图补充回归 **58 passed**。真实浏览器多轮矩阵
覆盖任务切换、增删改筛选、指标切换、时间粒度、结果解释、图表复用、排名实体下钻、刷新恢复、
空结果、过期工件、未知指标、写入请求和恶意摘要隔离；最新代码下功能矩阵 9 组通过，恶意摘要
场景修正为保留合法来源证据后单独复验通过。既有浏览器组合 11 个选中用例中 10 个通过；唯一
失败是在线语义提供方返回 `semantic_service_unavailable`，未进入 QueryPlan/SQL 执行，不能归因
为状态或引用实现错误。此前完整矩阵已成功将排名第一州绑定为 `customer_state=SP` 并生成新查询。

本轮每个 run/audit 关联、状态快照、模型调用次数、延迟和脱敏 SSE/截图均保存于仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/analysis-memory-phase5-20261016/`；safe report 不含
完整 SQL 或结果行。当前仍没有自动历史召回，召回命中率记为 N/A；Phase 5 通过的是组件和链路
行为，不是任意自然语言业务语义准确率。Phase 6 跨会话长期记忆继续暂缓。

### 2026-10-16｜V3.2 切换与 Phase 5 补验

在线 PostgreSQL 与 SQLite 示例、SQL 审计的默认模型及 README 示例统一改为
`deepseek-ai/DeepSeek-V3.2`；历史模型评测和训练记录不改写。补验服务在
screen `data-analysis-agent-phase5`、`http://127.0.0.1:32016/embedded-copilot`。

真实多轮测试发现并修复：可信排名实体只更新会话 filters，却没有同步进入有效语义意图，
导致 QueryPlan 缺少该筛选。现在引用绑定后只生成一份合并后的有效意图，Catalog、QueryPlan、
SQL 候选和会话状态都使用它，删除后置重复注入 filter 的分支。确定性 Handler 回归和浏览器
断言均检查引用值真正进入 QueryPlan，不能只凭状态页出现州代码就判为成功。

OpenAI 兼容客户端此前漏发 `LlmRequest.temperature`，导致语义入口的 `temperature=0`
没有生效；现流式/非流式的公共请求构造器都传递该字段，并用异步客户端回归验证参数和让出
事件循环。语义提示补齐合法 JSON patch、清除条件、指标切换、保持年份、引用来源与目标
分组的区别；保持模型语义理解，不添加关键词路由或解析失败的隐式回退。分页浏览器 fixture
补齐当前会话接口的 `conversation_id`，没有为旧测试数据增加产品兼容层。

开启项目 PostgreSQL 的确定性联合回归 **256 passed**，定向 Ruff、compileall、diff check 通过。
V3.2 完整浏览器矩阵 **10 passed，36 轮无断言失败，耗时 16 分 16 秒**，覆盖条件增删改、
指标切换、排名引用、刷新恢复、图表复用、澄清、空结果、失效工件、未知指标、写入拒绝及摘要污染。
最终补充“引用来源维度不等于目标分组”的语义提示后，排名第一和第二的真实下钻再次独立复验
**2 passed，3 分 24 秒**；确认州筛选进入 QueryPlan、原年份保留、目标改为商品品类。

既有网页套件首次 **15 passed、3 failed、2 skipped**：两项分页 fixture 和一项排名下钻
分别修复后复验 **2 passed** 与上述排名组合 **2 passed**，18 个有效场景均有通过证据，
并非同一次完整套件全部通过。两项 skip 为已移除的浮动宿主拖拽/缩放场景，不属于现产品页。
原有长会话摘要生成与增量续接用例在该网页套件中通过。最终提示相关定向回归 **45 passed**。
证据位于仓库外 `evals/analysis-memory-phase5-20261016/v32-r4/` 与 `v32-r5/`。
这些测试验证状态、引用、结果合同和产品行为，未做逐条 Gold denotation，不代表任意业务问题
100% 正确；跨会话长期记忆和自动历史召回仍未接入。

## 2026-10-16｜当前任务按 token 记忆与回答后后台摘要

完成三阶段改造：取消在线六轮历史截断，数据库按完整轮次游标分批读取，语义解释与后续
Agent 调用复用同一历史范围。统一 `RequestBudget`：完整请求 64K、历史 48K；未压缩原文
超过 24K 后后台压缩，目标保留约 16K 原文；摘要目标 2K/上限 4K，输入含提示最多 24K，
单次摘要调用限时 60 秒；状态/引用 4K 是软目标。计数记录本地估算方式，不宣称等于供应商窗口。

普通请求只读已有摘要，消息与结果保存后再启动压缩。同一会话一个任务、每服务进程最多两个
并发；积压串行处理，异常停止本次尝试，后续请求可重新调度。摘要未成功覆盖的原文继续参与
上下文；只有完整请求确实装不下时才等待或尝试一次同步压缩，仍不足则明确报错。超长单轮
分片生成临时摘要，整轮成功才推进覆盖位置。摘要专用写入只修改派生字段，行锁事务核对用户、
workspace、任务/数据/指标版本、旧覆盖位置和源消息；不会拿旧会话对象覆盖新消息。
前台普通写入反向保留数据库最新摘要；清空、编辑、切换任务及版本变化使旧后台结果失效。

摘要固定保留分析目标、用户纠正、未解决事项与表达偏好。SQL、工具返回和完整表格不进入摘要；
指标、时间、筛选和数值仍来自 AnalysisState、QueryPlan 和可信工件。后台使用独立计量上下文，
摘要调用不写入已结束用户请求的调用次数。关闭服务取消任务，数据库原文保留供重启后续接。

最新代码定向 Python 回归分两批 **216 + 65 = 281 passed**，包含真实 PostgreSQL 事务与并发
写入回归。Ruff、compileall、diff check 通过。此前扩大到继承的 `tests/test_agents.py` 时，
存在未定义 `agent` fixture 和缺少可选 `ollama` 依赖的问题；未修改无关测试，也不宣称全仓通过。
浏览器真实联合矩阵 **9 passed**，覆盖两次增量摘要、用户纠正/待办、条件增删改、排名下钻、
只换图不重查、摘要污染隔离、分页及移动端。另一个隔离服务用例 **1 passed**，注入摘要失败后
继续使用原文，实际重启、重新认证后恢复同一会话摘要和工件，全程没有新增 SQL。
最终在更新后的主端口独立复验长历史与排名引用 **2 passed，311 秒**；两次摘要覆盖位置
从消息 23 推进到 51，用户纠正与待办仍在，前台摘要调用数为 0。

长历史由持久化夹具注入后运行真实模型，不是几十轮人工自然对话的统计结论。首个有效内容、
整轮耗时、摘要耗时、覆盖位置、token 和失败原因保存在仓库外
`evals/background-context-compaction/{final,restart-final,main-final}/`；没有相同负载的新旧受控
延迟 A/B，因此不承诺固定提速比例。当前页面更新至 `http://127.0.0.1:32016/embedded-copilot`。
本轮没有引入队列服务、新表、额外 hash、训练或跨任务自动召回。

实现说明见 [后台摘要任务记录](plan/feature-background-context-compaction.md) 与
[上下文架构第 10 节](docs/architecture/analysis-context-memory-v2.md)。下一轮再讨论同一会话更早
分析任务与结果的召回，本轮不延伸该范围。

## 2026-10-16｜短对话状态连续性与 SQL 修复收尾

针对用户报告的“哪个州销售额最高 → 这个州哪些商品卖得好 → 前十商品品类”排查，
确认主要问题不是 token 窗口不足：换分组沿用了旧 Top-1；编辑最后一问会清空整个状态；
澄清分支丢失已知字段；四表 Catalog 无法完整提供品类与州筛选来源；SQL 修复仅预留
320 tokens 且输入按字符截断，出现过遗漏条件和半截 SQL。

已完成以下修复：分组改变时移除旧排名数量，明确新数量优先；澄清保留已验证的草稿；
用户消息保存服务端轮前状态，编辑在原事务内恢复前缀并使摘要失效，清空/版本失效不能被撤销；
失败保留当前意图和上次成功结果的独立归属。在线 Catalog 放宽到六表并保护必要关联路径；
修复完整接收 QueryPlan/Catalog，输出复用统一预算（当前 4,000 tokens），截断响应不执行。

真实验收还发现图类型自造后缀和金额自造加权：解释器现在获得共享 ChartType 的规范 ID；
SQL 提示明确事实粒度、多对一维度关联和计划归因的区别，禁止无归因要求的金额重新分摊。
这只是明确已有指标口径，SQL 语义仍需数值对照，不能把提示视为完备的语义证明。
浏览器测试也改为匹配当前问题的 SSE 请求，避免欢迎流被错认成答案。

最新代码定向回归 **250 passed**，包含实际 PostgreSQL 状态恢复、版本/归属、摘要竞态、
修复输出、计划完整性、结果复用及图表回归；Ruff、compileall 和 diff check 通过。
最终真实模型浏览器分两批 **4 + 1 组通过，共 18 轮**，覆盖三组不同措辞的三问、
澄清补充与七轮编辑/重试/刷新/换图/取消筛选。失败状态续接由受控状态夹具安排，随后调用
真实模型与数据库；另有 **1 项真实 SQL 修复验收通过**，漏掉 SP 的候选被拦截，修复补齐客户关联
与 SP 筛选后返回十行。九次品类排名对照独立 reader SQL 的类别、顺序和金额，误差阈值 0.01。

有效证据为仓库外 `evals/task-continuity-20261016/final-r3/` 中的
`category-ranking-continuity`、`partial-clarification-continuity`、`gold-comparisons`，
以及 `final-r4/` 中的编辑/重试完整链路和逐轮 Gold 对照；独立修复证据为
`final/live-sql-repair.json`。早期失败、模型 stream-idle 超时和欢迎流误匹配证据保留，
没有把它们计入最终通过。上述回归不等于任意电商问题或任意 SQL 的语义准确率。

当前服务为 screen `data-analysis-agent-continuity`，地址仍是
`http://127.0.0.1:32016/embedded-copilot`。没有改摘要预算、模型、数据库权限或后训练数据。
旧消息不补写轮前快照；没有快照的历史编辑无法据此恢复，不能从助手文本猜回业务条件。
代码阅读见 [修复任务卡](plan/fix-analysis-turn-continuity.md) 和
[上下文架构第 12 节](docs/architecture/analysis-context-memory-v2.md)。

## 2026-10-17｜Catalog 组合筛选与连续下钻

本轮只处理连续分析中的维度组合筛选。用户三问中的实体引用已经能识别，真正的阻碍是旧入口
只允许一个客户州条件，州与品类一起出现时会在生成 SQL 前被拒绝。现在根据当前可见 Catalog
绑定多个等值条件，将条件和实际表列来源贯穿意图、QueryPlan、SQL 检查与结果工件；修改或撤销
一个条件保留其他条件。无法访问的维度仍拒绝，未知筛选值允许正常返回空结果。

英文显示品类与葡萄牙语原始品类通过 reader 参数化查询确认值来源，不把英文直接用于原始列。
纠正意图提示中“撤销条件就清空全部筛选”的旧示例；所有分组键均已等值固定时，不再强制冗余
Top-N LIMIT。SQL 检查接受可追溯别名、CTE、IN、EXISTS 和合法 COALESCE，同时识别漏条件、
错字段、无效 OR 和未使用 CTE 中的伪筛选。权限、敏感列、只读角色与执行预算保持原边界。

真实数值对照发现商品行序号误去重、品类过滤只限制订单而未限制商品行、支付筛选被误当金额
分摊，已明确 Catalog 与生成提示。主页面复验还发现按非唯一品类名重新关联原始商品表，使
477,794.20 的 GMV 被放大到 1,447,238,631.80；新增基于已有 Catalog 多对一关联键的定向检查，
并验证正常主键、翻译表、CTE 及聚合结果关联可以通过。这是对实际错误的约束，不是完整 SQL
语义证明；其他公式错误仍需独立数值核对。

最终相关 Python 联合回归 **393 passed**，包含实际 PostgreSQL 用例；定向 Ruff、compileall、
diff check 通过。更新后的主端口真实 Playwright + 模型 + 数据库验收 **4 组通过、共 9 轮查询，
耗时 257.25 秒**，九轮均读取实际工件并与独立 reader SQL 比较类别、排名、计数及金额。
覆盖州排名后下钻品类、继续看销售额、只改州、只撤销州、英文/原始品类及州+支付方式组合。

最终证据为仓库外 `evals/catalog-filters-20261016/main-final-r2/` 下的 SSE、截图、turns.json
和九份逐轮 Gold 对照。早期失败尝试的日志保留，不计入最终通过；清理本轮临时测试会话，保留
最终成功验收会话及用户原对话。主服务仍为 `http://127.0.0.1:32016/embedded-copilot`，隔离
测试服务 32019 已停止。没有改模型、摘要预算、训练数据或数据库权限。

本轮不宣称整体流畅度已经解决。失败后的结果复用目标、模型八行预览以外的排名引用，以及
两次串行模型调用的延迟仍需后续分别处理。实现见 [任务卡](plan/fix-catalog-dimension-filters.md)
与 [上下文架构第 13 节](docs/architecture/analysis-context-memory-v2.md)。

## 2026-10-17｜连续 SQL 生成流程约束审计与裁剪

本轮把用户报告的九轮场景作为端到端验收，而不是只测单个函数：

`哪个州销售额最高` → `这个州哪些商品种类卖得好` → `排名前十的商品种类都给一下` → `只保留前五名` → `只换图` → `改看商品销量` → `州换成 RJ` → `取消州限制` → `解释刚才的结果`。

另外验证了两组边界：已验证排名结果之外的第十行引用；当前范围失败后明确展示上一份成功结果，再重试当前失败范围。每轮都记录语义意图、路由、AnalysisState、QueryPlan、SQL 审计、结果合同、结果复用目标和终止原因，原始 SQL 只写到仓库外的受限审计目录。

这次实际复现并修复了四类会影响体验的约束：

- 实体引用不再把“模型上下文只保留 8 行”误当成“最多只能引用前 8 名”；服务端从已验证完整 CSV 按游标读取目标行，唯一最高额外读取相邻行确认并列。
- 当前查询失败时，用户明确要求上一份成功结果仍可解释或重绘；`current_task` 与 `last_successful_result` 分开，旧结果不会覆盖当前失败状态。
- 在线聚合的外层 Top-N 由执行策略统一生成，移除模型必须重复写 `LIMIT` 的重复检查，避免无意义的修复重试。
- 只换图直接使用已验证工件的结果形状构造图表合同，不再重新构建 SQL 计划；图表仍受列、类型、空值、重复键和 200 行展示预算约束。

同时修复了英文品类缺失翻译时名称变为 `nan` 的问题（用 `COALESCE` 回退原始名），提示禁止未请求地删除真实 `NULL` 分组；计划拒绝和修复拒绝现在在执行前写入受限 SQL 审计。权限、workspace/版本隔离、敏感字段、SqlPolicy、PostgreSQL reader、只读事务、超时、结果行/字节预算和 ResultValidator 均保留。

验收结果：定向 Python 回归 **337 passed**；隔离服务真实 Playwright + SSE + PostgreSQL 三组流程 **9 passed**；主端口 `32016` 的嵌入网页回归 **5 passed**。主服务当前地址为 `http://127.0.0.1:32016/embedded-copilot`，screen 为 `data-analysis-agent-fluency`。详细清单与逐轮证据见 [连续分析流程约束审查](plan/fix-analysis-flow-constraints.md)；证据目录在仓库外 `evals/analysis-memory-phase5-20261016/flow-{continuous,rank-reference,failed-scope}/`。

仍未扩展的能力边界：多维或按时间的 Top-N、结果解释中的数字证据判定、提供方超时处理以及跨任务历史召回。这些应作为独立任务评估，不能通过删除权限和数据安全约束来换取流畅度。

## 2026-10-17｜分区 Top-N 与结果解释数字判定

补齐了前一节列出的两个在线能力缺口。语义意图现在允许带业务分组维度的分区 Top-N，最后一个维度作为排名目标，前置维度和时间粒度作为分区键：例如“每个州前五个品类”按州分区，“2017 年每月前五个品类”按月份分区。没有分区键的单维度 Top-N 保持原行为；只有时间粒度而没有排名维度仍会澄清。

`QueryPlan` 保存 `top_n_partition_dimensions`，结果预算按分区基数乘以 N 计算。分区 SQL 必须通过 AST 检查包含 `ROW_NUMBER`/`RANK`/`DENSE_RANK`、计划分区和不超过 N 的外层排名过滤；时间分区允许模型在 CTE 中使用 `month_start` 等明确时间字段映射到结果列 `time`。执行策略不再给分区 Top-N 追加全局 Top-N `LIMIT`，但原有 SqlPolicy、只读 reader、超时、字节/行预算和 ResultValidator 均保留。

结果解释数字检查现在忽略行首 `1.`/`1）`、`第 10 名`、`排名第 2`、`Top 10`、`前 5 项`、`共 10 行` 等展示编号；金额、比例、年份和其他业务数字仍必须出现在可信结果摘要中。

确定性回归：**147 passed、10 skipped**，补充测试覆盖全局、州分区、时间分区、时间+多维分区、错误分区字段和数字展示编号。真实 `Playwright + SSE + PostgreSQL` 浏览器验收：分区 Top-N **2 个场景通过**（每州前五个品类、2017 年每月前五个品类），原九轮连续分析流程 **9 轮通过**。证据位于仓库外 `evals/flow-constraints-20261017-final/`，服务仍为 screen `data-analysis-agent-fluency`、`http://127.0.0.1:32016/embedded-copilot`。
