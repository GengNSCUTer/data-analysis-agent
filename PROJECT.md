# Data Analysis Agent（可信业务数据分析智能体）

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
当前仅支持柱状图和折线图；不支持或缺少必要横轴的请求会在 SQL 前澄清/拒绝，不能由模型或
前端静默改图。图表层仅渲染已经通过 `ResultValidator` 的当前结果，不补值、去重、聚合或重算。
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
- 已实现服务端 `ChartContract`：由用户意图、`QueryPlan` 和 `ResultContract` 固定 bar/line、横轴、指标列、系列、标题与当前 SQL 结果工件；不支持的图形和缺少横轴的请求在 SQL 前终止，图表层不做二次聚合。
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

### Phase 4：嵌入式交互与证据呈现（基础能力已完成）

- 提供可嵌入既有网页的宿主页示例，控制 Vanna Web Component 的浮动/右侧面板状态；
- 打通 SSE、表格、SQL、图表、指标证据和角色化展示；
- 已完成历史会话列表、恢复、刷新恢复、新建会话和受控删除交互；完整嵌入浏览器回归当前 9 条通过；
- 不创建独立 Next.js/TailAdmin 应用。

### Phase 4.5：平台基础收敛（进行中）

- 已完成 PostgreSQL 会话/消息存储、Agent Run 台账、请求级工具/SQL/图表/输入/上下文/输出预算；
- 已完成 starter 空会话生命周期修复，避免页面刷新制造零消息历史记录；
- Text-to-SQL 第二轮运行时合同已完成：WorkspaceProfile、Catalog/路由/working memory/结果合同、TrustedRunSqlTool 一次修复、repair evidence 和可信拒答均已落地；浏览器多轮澄清、八方向缩放和长历史 Markdown 回归已补齐。
- 已完成后训练准备第一阶段：`ChartContract` 与归因三态接入运行时，`QueryPlan`/`ResultContract`/审计记录携带归因需求证据；`docs/post-training-data-protocol.md` 固化样本字段、来源、脱敏、评测和训练边界，`evals/manifests/post_training_holdout_v1.yaml` 将 v2 60 条 golden 永久隔离。下一项是在不接触 holdout 的前提下，单独采集和人工复核训练候选样本，再设计可复现实验基线。
- 已完成后训练依赖环境 bootstrap：独立 Conda 前缀 `data-analysis-agent-qlora` 使用 Python 3.11，冻结 CUDA 12.1 的 `torch==2.5.1+cu121`、accelerate、bitsandbytes、transformers、PEFT、TRL 与 datasets 的直接/传递依赖。宿主 535.54.03 驱动只支持 CUDA 12.2，故选择 CUDA 12.1 wheel 而非需更高驱动的 CUDA 12.4；`pip check` 通过。`CUDA_VISIBLE_DEVICES=1` 的最小探针只见进程内 `cuda:0`，其 UUID 确认为物理 GPU 3/RTX 4090。没有下载基座模型、创建训练语料、读取 Spider gold SQL 或启动训练；v2 60 条 holdout 继续永久隔离。详见 [`docs/qlora-environment.md`](docs/qlora-environment.md) 和 [`evals/manifests/qlora_environment_v1.yaml`](evals/manifests/qlora_environment_v1.yaml)。
- 已完成后训练研究的 SQLite benchmark Adapter 基础设施：离线 `ReadOnlySqliteExecutor` 使用 SQLite 方言 AST、只读 URI、SQLite authorizer、超时和行数上限执行外部候选 SQL；`scripts/run_sqlite_benchmark.py` 仅消费 BIRD/Spider 原生 `dev.json` 和外部预测 JSONL，输出版本化模型/Prompt/候选/执行证据报告。它不接入 Vanna/前端/生产 PostgreSQL，也不自行计算官方 EX；BIRD/Spider 原始数据、模型预测和报告都继续留在仓库外或忽略目录，许可与 checksum 在下载前再冻结。详情见 [`docs/sqlite-benchmark-adapter.md`](docs/sqlite-benchmark-adapter.md)。
- 已完成冻结小模型基线的生成侧与合成端到端烟测：`frozen_sqlite_baseline.py` 只在内存中读取原生问题与 SQLite DDL，以无工具、固定解码的本地 Ollama 调用生成一条 SQL 候选，并且只向仓库外 JSONL 写入 Adapter 合同字段；脚本拒绝向 Git 工作树写入预测，Schema 超预算 fail closed，模型 SQL 不在生成侧执行或修复。当前冻结目标为 `qwen2.5-coder:3b` 的本地 Q4_K_M 量化分发，manifest/blob digest、Qwen Research License 与 Ollama server/CLI 版本均已记录。真实模型在合成 SQLite fixture 上生成后经 Adapter 得到 1/1 `executed`；随后原生 Spider 镜像的许可、哈希和布局门槛、20 条 smoke 与完整 1,034 条冻结生成均已完成。QLoRA/SFT 仍继续暂停，详情见 [`docs/frozen-sqlite-baseline.md`](docs/frozen-sqlite-baseline.md)。
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

已确认数据加载方式、PostgreSQL 双角色和首批核心指标 golden 结果。Olist 只是当前展示数据集，
通用链路通过 `WorkspaceProfile` 组织，尚未用第二个真实数据集验证。待确认真实认证方式、组织/行级
权限的演示粒度、评测题标准答案与图表受控生成方案。独立 Next.js/TailAdmin 外壳不再作为候选默认方案。

## 11. 变更记录

下一阶段平台计划与 Text-to-SQL 专项调研分别见：

- [`docs/AGENT_PLATFORM_NEXT_PLAN.md`](docs/AGENT_PLATFORM_NEXT_PLAN.md)
- [`docs/TEXT_TO_SQL_RESEARCH.md`](docs/TEXT_TO_SQL_RESEARCH.md)
- [`plan/feature-text-to-sql-reliability-v1.md`](plan/feature-text-to-sql-reliability-v1.md)
- [`plan/feature-text-to-sql-reliability-v2.md`](plan/feature-text-to-sql-reliability-v2.md)
- [`docs/verification-text-to-sql-v2.md`](docs/verification-text-to-sql-v2.md)
- [`docs/post-training-data-protocol.md`](docs/post-training-data-protocol.md)
- [`docs/sqlite-benchmark-adapter.md`](docs/sqlite-benchmark-adapter.md)
- [`docs/frozen-sqlite-baseline.md`](docs/frozen-sqlite-baseline.md)
- [`docs/spider-1.0-data-provenance.md`](docs/spider-1.0-data-provenance.md)
- [`docs/official-spider-test-suite.md`](docs/official-spider-test-suite.md)

| 日期 | 事项 | 结论 |
| --- | --- | --- |
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
