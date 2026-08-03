# 简历项目条目草稿

以下文案只引用当前仓库、数据库 golden、浏览器回归和 GitHub Actions 能证明的事实。

## 一段版

> 可信数据分析 Agent｜Python、FastAPI、Vanna、PostgreSQL、sqlglot、Plotly  
> 基于 Vanna 构建可嵌入既有网页的经营分析副驾：通过 Web Component + SSE 流式返回中文结论、表格、图表、最终 SQL 与数据/指标版本证据；在执行链路中引入 `sqlglot` AST SQL Policy、PostgreSQL 只读查询角色与应用审计写角色、签名演示会话及受控图表工具，将自然语言分析限定在可解释、可审计的只读业务查询范围内；基于 Olist 公开数据建立 60 条确定性评测用例和 3 条固定 Demo 场景，并以 PostgreSQL golden SQL、Playwright 嵌入式回归和 GitHub Actions 作为项目证据。

## 三点版

- 设计并实现受控 Text-to-SQL 执行链路：使用 `sqlglot` AST 校验单语句只读 SQL、Schema/表/列白名单、角色化 LIMIT 与危险函数拒绝，并通过 PostgreSQL 双角色隔离查询与审计写入。
- 在 Vanna 原生 `<vanna-chat>` 上构建可嵌入经营页面的分析副驾，支持 SSE 流式结果、SQL 证据、受控 Plotly 图表、桌面拖拽缩放和移动端自适应；浏览器回归覆盖窗口交互、图表适配和演示角色切换。
- 基于 Olist 公开数据建立可复现评测体系：60 条确定性策略/数据用例、3 条固定面试 Demo 场景、PostgreSQL golden 指标与场景校验，并以 CI、数据库断言和本地 E2E 作为量化证据来源。

## 可引用的量化事实

- 60 条确定性策略/数据用例。
- 3 条固定 Demo 场景及其数据库 golden 校验。
- 当前确定性测试集：88 passed。
- 嵌入式浏览器回归：5 passed。

## 不要写进简历的内容

- 不要写“生产级认证”“OAuth 登录”“组织级 RLS”。
- 不要写“模型准确率 xx%”或“平均时延 xx ms”，除非后续新增并保存了对应评测报告。
- 不要把 Olist 说成中国真实业务数据。
