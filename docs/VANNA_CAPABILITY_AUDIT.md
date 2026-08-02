# Vanna 能力审计（2026-08-02）

本审计以仓库内 Vanna 2.0.2 的 `README.md`、`frontends/webcomponent/` 和 FastAPI
路由实现为依据。状态分为：已端到端验证、源码已确认但本项目未启用、后续需实现。

| 能力 | Vanna 原生支持 | 本项目状态 | 结论 |
| --- | --- | --- | --- |
| 任意页面嵌入 | `<vanna-chat>` 是 Lit Web Component，可配置 `sse-endpoint` | 已端到端验证 `/embedded-demo` 经营宿主页、中文属性和 SSE 查询 | 可作为项目唯一交互前端，无需 Next.js |
| 最小化/最大化 | `allowMinimize` 默认开启；组件有 `normal`、`minimized`、`maximized` 三态 | 已在 Chrome Playwright 中验证最小化、恢复、最大化和移动端 64px 入口 | 最小化后固定在右下角 64px 浮动入口，不是完整侧栏模式 |
| SSE 流式聊天 | `POST /api/vanna/v2/chat_sse` | 已验证 | 当前主通信方式 |
| 轮询降级 | `POST /api/vanna/v2/chat_poll` | 路由存在，未单独验证 | 作为网络降级通道，后续补 API 测试 |
| WebSocket | `/api/vanna/v2/chat_websocket` | 路由存在，未使用 | 不进入首批范围，SSE 足够 |
| 进度和任务面板 | 状态栏、任务跟踪、输入状态组件 | 已随 SSE 在页面展示 | 保留，后续用业务化状态文本替代通用文本 |
| 结果表 | `DataFrameComponent` | 已端到端验证 | 后续补列别名、格式化、下载和大结果截断策略 |
| Plotly 图表 | `VisualizeDataTool` + `ChartComponent` + `plotly-chart` | 渲染器和工具存在；当前 Agent 未注册工具，未验证 | Phase 3 优先接入并测试，而不是自建图表框架 |
| SQL 结果文件 | `RunSqlTool` 默认写 `query_results_*.csv` | 已注入 `LocalFileSystem`，默认写至 `/tmp/data-analysis-agent-vanna-query-results/`，可由 `VANNA_QUERY_RESULTS_DIR` 覆盖；E2E 已验证仓库根目录无结果文件 | 当前仅为原型文件隔离，后续图表工具复用该受控目录并定义保留/清理策略 |
| 自定义工具 | `Tool` / `ToolRegistry.register_local_tool` | `RunSqlTool` 已注册 | 项目应以自定义安全 SQL 工具和指标上下文工具作为核心扩展点 |
| 认证上下文 | `UserResolver` 可读 Cookie、Header、JWT | 当前 resolver 固定返回演示用户 | 必须实现本地演示身份和后续真实认证；原生登录页仅为前端 cookie 演示 |
| 工具权限 | 注册工具时按 `access_groups` 限制 | 当前只有 `analyst` 演示组 | 只保护工具入口，不自动保证表/列/行级 SQL 安全 |
| 会话存储 | `ConversationStore` 抽象，内存/本地文件实现存在 | 当前使用内存默认行为 | 需接 PostgreSQL 或文件型开发实现，才能支撑可回放历史 |
| 审计 | `AuditLogger` 抽象和事件模型存在 | 未配置持久化 logger | 需由本项目实现 PostgreSQL 审计落库 |
| 生命周期钩子 | `LifecycleHook` 支持配额、日志、过滤 | 未配置 | 后续用于请求 ID、限流、成本和策略记录 |
| 本地 LLM | OpenAI-compatible 服务可配置自定义 Base URL | 当前使用 SiliconFlow | 保留 vLLM/Ollama Base URL 配置，不在此阶段部署模型 |

## 结论

Vanna 已经解决了嵌入式聊天、流式富结果、表格、图表渲染和窗口交互这些前端基础能力。
本项目不再建设独立管理后台或重新实现聊天界面。工程价值应集中在：业务语义约束、
安全 SQL 执行、数据和指标版本、图表触发策略、审计、权限和可回归评测。

当前原生组件提供的是右下角浮动最小化，而非网页左/右侧停靠抽屉。`/embedded-demo` 已用
宿主页 CSS 和 `window-state-changed` 事件完成第一层组合验证；后续在接入真实数据和证据
对象后，再把正常态收束为右侧分析面板。继续避免修改 Vanna 组件核心源码，优先使用属性、
CSS Custom Properties 和宿主层事件编排。
