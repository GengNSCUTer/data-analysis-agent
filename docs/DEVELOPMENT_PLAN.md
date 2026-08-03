# Data Analysis Agent｜开发计划

> 状态：Phase 1 和 Phase 2 已完成；Phase 3 可信查询后端已跑通首个真实 Olist 闭环。
> 最近更新：2026-08-03

## 1. 开发原则

项目采用“先跑通真实底座，再逐层增加自己的工程价值”的顺序。第一步不是先搭完整
平台、下载大数据集或冻结数据合同，而是验证 Vanna 的真实 API、工具调用、SSE 和原生
页面。原型通过后，再把数据集、指标语义、安全策略、审计和作品集包装逐步加回去。

每个阶段都必须有可复现的退出条件。计划中的能力不视为已实现；实现状态以代码、命令
输出、测试和迭代记录为准。

## 2. 仓库和运行边界

唯一自有仓库：

```text
/disk2/gengnan/data-analysis-agent/
├─ src/vanna/                  # 已合并的 Vanna 上游源码
├─ frontends/                  # 已合并的 Vanna 原生前端资源
├─ examples/                   # 本项目运行入口和小型合成 fixture
├─ tests/                      # Vanna 现有测试；后续增加项目回归测试
├─ docs/                       # 计划、ADR、数据和评测文档
├─ PROJECT.md
└─ AGENTS.md
```

Git 远端约定：`origin` 是 `GengNSCUTer/data-analysis-agent`，`upstream` 是
`vanna-ai/vanna`，用于后续同步和差异审查。`/disk2/gengnan/_upstream/` 下的克隆仅
作参考，不能作为第二个项目仓库或开发位置。

禁止提交：`.env`、API Key、数据库密码、原始第三方数据、生成的 SQLite/数据库文件、
浏览器截图和缓存。第三方数据只提交 manifest、许可证/署名、下载说明、DDL、转换脚本
和小型 fixture。

## 3. 目标架构（原型之后）

```text
既有业务网页 / 静态宿主页 + Vanna <vanna-chat>
                 │ HTTPS/SSE
                 ▼
FastAPI：身份/角色、指标上下文、Vanna Agent、SQL Policy、证据和审计
                 │ 只读查询
                 ▼
PostgreSQL：analytics（业务数据）+ app（会话/审计/指标/评测）
```

当前保留 SQLite 合成原型作为上游回归入口，并新增真实 Olist 可信原型：FastAPI、原生
Web Component、PostgreSQL、Vanna 受控 SQL 工具与 SiliconFlow。项目不再计划创建独立
`frontend/`、Next.js 或 TailAdmin；自有查询内核放在 `src/data_analysis_agent/`。

## 4. 阶段计划

### Phase 0｜仓库和决策基线（已完成）

交付：确定 Python/Vanna 路线、建立唯一项目仓库、合并 Vanna 源码、保留 upstream 远端、
创建 `PROJECT.md`、`AGENTS.md`、飞书项目文档和本计划。

退出条件：不再存在“自建 wrapper 仓库”和“Vanna 独立开发目录”两个事实来源；`.env`
和第三方原始数据均不进入 Git。

### Phase 1｜Vanna 原生垂直原型与嵌入审计（已完成）

目标：验证 Vanna 2.0.2 的最小真实闭环，不修改 Vanna 核心。

实施：

1. 用 Conda 环境 `/disk2/gengnan/conda_envs/data-analysis-agent` 运行 Python 3.12；
2. 从根目录 `.env` 读取 `SILICONFLOW_API_KEY` 和 `SILICONFLOW_BASE_URL`；
3. 使用模型 `deepseek-ai/DeepSeek-V4-Flash`；
4. 创建 `examples/data/vanna_demo.sqlite`（仅合成 `sales_daily` fixture）；
5. 通过 Vanna `Agent`、`ToolRegistry.register_local_tool`、`RunSqlTool`、
   `SqliteRunner` 和 `register_chat_routes` 提供原生服务；
6. 用 API、curl、Chrome/Playwright 浏览器检查页面、SSE、工具结果和中文总结。

已验证：

- `GET /` 返回 Vanna 原生页面；
- `POST /api/vanna/v2/chat_sse` 实际调用 SiliconFlow 并完成 SQL 查询；
- 浏览器可看到结果表和中文解释，测试值为华东 `113130`、华南 `94500`；
- 页面唯一控制台提示是原生页面缺少 favicon 的 404，不影响聊天链路；
- 当前服务端口为 `127.0.0.1:32009`，后台 screen 名为 `data-analysis-agent`。
- `/embedded-demo` 用无框架经营宿主页嵌入 `<vanna-chat>`，初始最小化，提供中文文案并记录窗口状态事件；
- Playwright 已验证桌面端最小化、恢复、最大化、真实 SSE 表格结果以及移动端无横向溢出；
- `RunSqlTool` 的 CSV 结果写入 `/tmp/data-analysis-agent-vanna-query-results/`（可由 `VANNA_QUERY_RESULTS_DIR` 覆盖），不会写入仓库根目录。
- 修复 Vanna RichText 的 Markdown 表格解析并以语义化 HTML table 展示；根路径和宿主页均改由同一 FastAPI 进程提供本地 bundle。

运行该 demo 前，先在 `frontends/webcomponent/` 执行一次 `npm install --package-lock=false && npm run build`。
`node_modules/` 和 `dist/` 都是本地生成物，不进入 Git；服务启动时会在 bundle 缺失时明确报错，避免悄然回退到未修复的远程 CDN 版本。

退出条件：上述闭环可重复运行；API Key、SQLite 文件和测试产物不提交；记录 Vanna
当前缺口（原生认证演示、图表触发、SQL 写操作默认行为等）。

### Phase 2｜数据和领域合同（已完成）

目标：把合成表替换为可解释、可复现的数据资产，并冻结第一版业务口径。

任务：

1. 选择 Olist 主展示案例，Chinook 作为小型回归案例，中文数据作为可选扩展；
2. 提交数据 manifest、许可证/署名、下载和加载说明，不提交原始大文件；
3. 设计 6–10 张分析表、字段字典、主外键和可访问范围；
4. 冻结 GMV、订单量、客单价、取消率、履约时长、好评率等指标的公式、粒度、时间字段
   和允许维度；
5. 将 20 个代表性问题写入版本化评测草案，并为核心问题生成 golden 结果。

当前交付：`data/manifest/datasets.yaml`、`docs/data-dictionary.md`、
`docs/metric-catalog.md`、`docs/architecture/data-model.md`、
`data/fixtures/sales_daily.csv` 和 `evals/cases/draft.yaml`。这些是版本化草案，
尚未代表生产口径已冻结。Olist 的 Kaggle version 2、许可证、9 个 CSV 清单、原始列名、
行数和 SHA-256 已于 2026-08-03 记录到 manifest；原始 ZIP/CSV 仅保存在仓库外目录，未提交 Git。
analytics 8 表已真实加载并以 golden SQL 复核核心指标。

退出条件：每个指标都能由人工依据字典写出明确 SQL；数据可从空环境重建；没有把
“让模型自行猜口径”当作设计；至少完成一次原始数据许可、质量和 golden 结果复核。

### Phase 3｜受控查询后端（进行中）

目标：在真实数据上建立安全、可审计的自有查询内核。

任务：

1. 创建 FastAPI 应用和配置模型，保留 Vanna 原生路由作为兼容基线；
2. 建立 PostgreSQL `analytics` / `app` Schema 和独立只读查询角色；
3. 使用 `sqlglot` 实现单语句、只读、对象白名单、LIMIT、超时和行数策略；
4. 按角色提供受控 Schema/指标上下文；
5. 记录候选 SQL、最终 SQL、策略决策、模型配置、指标/数据版本、耗时和结果摘要；
6. 编写正常、歧义、无权限、越权和恶意 SQL 集成测试。

当前已完成：`SqlPolicy`、双 PostgreSQL 角色、受控 `SecurePostgresRunner`、版本化指标上下文、
持久 `app.query_audits` 与 `examples/trusted_olist_web_demo.py`。真实 SSE 已验证“按客户州
统计有效订单数前五名”返回 5 行表格与中文结论，审计保存请求 ID、问题、原始/最终 SQL、版本、
耗时和行数。受信服务为 `127.0.0.1:32010`，screen 名为 `data-analysis-agent-trusted`。
`tests/test_postgres_runner.py` 以 `RUN_PROJECT_DB=1` 显式运行，覆盖真实查询、允许/拒绝审计
和双角色跨 Schema 拒绝；CI 不运行该测试，避免依赖开发机数据库。
宿主页已通过 `/api/project/evidence`、`/api/project/session` 和角色过滤的 `/api/project/audits`
展示版本、演示身份与最近查询证据；页面只使用专门的审计 DTO，不输出原始 SQL 或异常详情。

退出条件：任何 Agent SQL 都经过策略层和数据库只读权限；失败不编造结论；关键事件
可由 request ID 回放。

### Phase 4｜嵌入式交互和作品集包装

目标：在 Vanna 原生 UI 已验证的基础上，增加适合简历展示的产品外壳。

任务：

1. 创建无框架宿主页，把 `<vanna-chat>` 作为浮动或右侧分析面板嵌入；
2. 展示流式状态、表格、SQL、图表和指标证据，并验证最小化/最大化状态；
3. 用后端审计 API 和静态证据页呈现查询历史、指标口径、数据集版本和评测报告；
4. 补齐登录态、角色状态、错误/空结果/加载态和响应式布局；
5. 通过浏览器 E2E 验证“业务页 → 提问 → 查看证据 → 查看历史”。

退出条件：前端不绕过后端策略；一次回答可以追溯到口径、SQL、数据来源和版本；页面
展示的是可验证能力而非静态 mock。

### Phase 5｜评测、加固和交付

目标：形成可诚实写入简历的量化证据。

任务：

1. 将评测扩展到至少 60 个业务、复杂关联、歧义和安全用例；
2. 记录 SQL 执行成功率、口径正确率、安全拦截率、图表适配率和 P50/P95 时延；
3. 固化模型、Prompt、Schema、指标、数据和策略版本；
4. 补齐运行手册、开源声明、演示脚本、架构图和已知限制；
5. 从评测报告反推简历项目描述，只写有运行记录支撑的数字。

退出条件：陌生开发者可按文档启动；安全用例全部阻断；核心业务问题有人工核验；
没有把合成 fixture 描述成真实生产数据。

## 5. 测试和迭代闭环

每轮迭代必须记录目标、实施、命令、结果、限制和下一步，并同步 `PROJECT.md`、本计划
和飞书文档。代码变更完成后检查 `git diff` / `git status`，确认无密钥和生成物，再用
Conventional Commit 提交并推送 `origin`。

测试层级按风险增加：

| 层级 | 当前/后续重点 |
| --- | --- |
| 冒烟 | Python 编译、SQLite fixture、`create_app()`、页面和健康检查 |
| API 集成 | SSE 事件顺序、工具调用、错误和 request ID |
| SQL 安全 | AST、越权、写操作、多语句、无界查询和超时 |
| 数据质量 | 主外键、空值、指标 golden 结果和转换校验 |
| Agent 评测 | 标准问题、歧义问题、失败行为、模型/Prompt 对比 |
| E2E | 宿主页嵌入、窗口状态、提问、流式结果、证据和历史 |

## 6. 近期下一步

1. 为可信服务补 PostgreSQL 集成测试、真实认证替换接口与角色行范围策略；
2. 在宿主页中展示受角色保护的最终 SQL、审计历史和指标证据；
3. 设计受控 Plotly 图表工具并补图表/证据 E2E；
4. 将评测扩展到至少 60 个用例，并运行可复现的安全与语义回归报告。
