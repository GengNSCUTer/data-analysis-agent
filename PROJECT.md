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

### 5.0 当前运行基线（Phase 1）

当前先验证 Vanna 本身的真实能力，不提前实现自建平台。唯一运行入口是
`examples/siliconflow_sqlite_web_demo.py`：它创建本地合成 SQLite fixture，使用
Vanna 的 `Agent`、`ToolRegistry`、`SqliteRunner`、`OpenAILlmService` 和原生 FastAPI
路由，页面由 Vanna 原生 `<vanna-chat>` 提供。模型通过 `.env` 中的
`SILICONFLOW_API_KEY` / `SILICONFLOW_BASE_URL` 调用
`deepseek-ai/DeepSeek-V4-Flash`，服务监听 `127.0.0.1:32009`。

同一 FastAPI 进程还提供 `/embedded-demo`：这是一个无框架的经营总览宿主页，加载 CDN
版 `<vanna-chat>`，以中文标题、提示词和 `window-state-changed` 事件组合原生组件。组件
初始为最小化入口；桌面端已验证最小化、恢复、最大化及真实 SSE 结果，390px 宽移动端已
验证没有页面横向溢出。`RunSqlTool` 使用项目注入的 `LocalFileSystem`，默认将下游 CSV
写到 `/tmp/data-analysis-agent-vanna-query-results/`，可通过 `VANNA_QUERY_RESULTS_DIR` 覆盖，
不再写入仓库根目录。

这一步的目标是确认“中文问题 → LLM 工具调用 → SQL → 结果表 → 中文总结 → SSE/UI”
闭环，而不是冻结最终数据模型、SQL 安全策略或 Next.js 页面。SQLite fixture 只用于
可重复的冒烟验证，不代表最终业务数据。

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

上图是通过原生 Vanna 基线后再逐步建设的目标架构，不是当前已完成的实现。当前没有
独立 `frontend/` 应用或 PostgreSQL 业务应用；后续代码以 `src/data_analysis_agent/` 为
项目扩展层，宿主页只负责嵌入和样式，不引入新的前端框架。

### 5.1 后端模块职责

| 模块 | 职责 |
| --- | --- |
| API 层 | 鉴权、请求校验、SSE、错误语义和 OpenAPI。 |
| Agent 编排层 | 绑定 Vanna Agent，限制工具集合和最大工具循环次数，记录模型调用。 |
| Context 层 | 按角色提供经过筛选的 Schema、指标定义、样例问题与业务约束。 |
| SQL Policy 层 | 基于 `sqlglot` 解析 AST，执行语句类型、单语句、对象白名单、LIMIT、范围过滤和预算校验。 |
| Query Runner | 使用 PostgreSQL 只读角色执行 SQL，设置 `statement_timeout`、最大行数和取消机制。 |
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
  → 表格 / 图表 / 结论 / 证据对象
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
| 图表 | Vanna Plotly 结果 | 通过受控 `VisualizeDataTool` 生成图表，不另引入图表前端框架。 |
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

### Phase 2：数据与领域建模（当前）

- 确定 Olist 主案例的表范围；
- 编写数据集清单、许可证/署名约束、分析 Schema、数据字典；
- 起草第一批指标定义、最小合成测试 fixture 和 20 条评测问题；
- 暂不下载原始数据，不创建 PostgreSQL，不将草案口径描述为已冻结生产能力。

本阶段文档入口：[`data/manifest/datasets.yaml`](data/manifest/datasets.yaml)、
[`docs/data-dictionary.md`](docs/data-dictionary.md)、[`docs/metric-catalog.md`](docs/metric-catalog.md)、
[`docs/architecture/data-model.md`](docs/architecture/data-model.md) 和
[`evals/cases/draft.yaml`](evals/cases/draft.yaml)。

### Phase 3：可信查询后端

- 创建 FastAPI 应用、认证/角色占位、PostgreSQL 双角色配置；
- 接入 Vanna Agent 与受控工具；
- 实现 `sqlglot` SQL Policy、超时/行数限制、审计与证据对象。

### Phase 4：嵌入式交互与证据呈现

- 提供可嵌入既有网页的宿主页示例，控制 Vanna Web Component 的浮动/右侧面板状态；
- 打通 SSE、表格、SQL、图表、指标证据和角色化展示；
- 不创建独立 Next.js/TailAdmin 应用。

### Phase 5：评测、加固与作品集

- 建立评测集、回归测试和安全测试；
- 完成部署说明、演示脚本、架构图、数据署名和项目 README；
- 形成可量化且可诚实写入简历的项目成果。

## 10. 当前决策与待确认项

已确认：单仓库 Vanna-first、Python/Conda、Vanna 原生 Web Component、SiliconFlow
开发模型、SQLite 合成冒烟 fixture、Olist 主展示案例草案、后续再引入 PostgreSQL 和
v1 不引入 Redis。

待在 Phase 2 合同冻结后确认：数据加载方式、PostgreSQL 角色、认证方案、组织/行级
权限的演示粒度、首批指标的人工 golden 结果和评测题标准答案。独立 Next.js/TailAdmin
外壳不再作为候选默认方案。

## 11. 变更记录

| 日期 | 事项 | 结论 |
| --- | --- | --- |
| 2026-08-02 | 项目立项 | 确定为 Python 可信数据分析 Agent，不继续以 Java 本地生活平台作为主项目。 |
| 2026-08-02 | Agent 选型 | 选择 Vanna，而非 PandasAI。 |
| 2026-08-02 | 前后端基座 | 使用 Vanna + FastAPI；使用 TailAdmin + Vanna Web Component。 |
| 2026-08-02 | 基础设施 | v1 使用 PostgreSQL；Redis 延后到出现异步任务/缓存/限流需求时。 |
| 2026-08-02 | 数据策略 | Chinook 用于回归，Olist 用于主展示，中文电商数据用于后续扩展。 |
| 2026-08-02 | 项目基线同步 | 已创建飞书项目文档；GitHub SSH 认证已恢复，首个项目基线提交已推送至 `main`。 |
| 2026-08-02 | 开发计划与目录边界 | 冻结完整阶段计划；自有项目仅在本仓库开发，上游 Vanna/TailAdmin 收纳为 `/disk2/gengnan/_upstream/` 参考缓存。 |
| 2026-08-02 | 单仓库 Vanna-first | 将 Vanna 源码合并到本仓库并保留 `upstream` 远端；先用原生 FastAPI + `<vanna-chat>` 垂直验证，再建设数据合同和自有平台。 |
| 2026-08-02 | Phase 1 冒烟验证 | Conda 环境、SiliconFlow `DeepSeek-V4-Flash`、SQLite 合成 fixture、SSE API 和浏览器页面均已验证；服务端口为 `32009`。 |
| 2026-08-02 | Phase 2 数据合同草案 | 确定 Olist 为主展示案例、Chinook 为回归候选、中文数据和天池 O2O 为后续候选；新增 manifest、字段字典、指标目录、分析模型草案、合成 fixture 和 20 条评测草案，暂不下载原始数据。 |
| 2026-08-02 | 嵌入式产品路线 | 确认 Vanna Web Component 可嵌入任意网页并支持最小化/最大化；停止独立 Next.js/TailAdmin 方向，后续以宿主页和 Vanna 原生组件为唯一前端基座。 |
| 2026-08-02 | 嵌入式基线审计 | 新增 `/embedded-demo` 无框架经营宿主页和 Playwright E2E；已验证窗口状态、真实 SSE 表格、移动端无横向溢出及 CSV 不落仓库根目录。图表、真实认证、SQL 策略和审计仍未实现。 |
