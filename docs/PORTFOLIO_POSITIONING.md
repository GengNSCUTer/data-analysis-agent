# 作品集定位与简历表达

## 项目定位

项目定位为“嵌入式可信数据分析副驾”，不是独立 BI 平台，也不是仅能生成 SQL 的聊天 Demo。
它可嵌入已有经营数据页面：用户边看指标和订单数据，边通过浮动分析组件追问；后端把
自然语言问题约束为可解释、可审计、只读的业务查询，并返回表格、图表、口径和 SQL 证据。

## 简历应突出什么

1. **后端与 Agent 工程**：基于 Vanna Agent/Tool Registry 设计受控工具链，而不是调用
   一个聊天 API。
2. **正确性和安全性**：用指标目录、Schema 上下文、SQL AST 策略、只读数据库角色和
   查询预算约束模型输出。
3. **嵌入式产品形态**：使用 Web Component 接入任意现有网页，SSE 推送进度、表格、
   Plotly 图表和结论，不另起一个割裂的数据后台。
4. **可追溯性和评测**：记录模型、Prompt、数据/指标/策略版本和最终 SQL；用版本化
   用例评估业务正确性与安全拦截。

## 目前可以诚实表述的事实

- 将 Vanna 2.0.2 合并进单一 Python 项目仓库，使用 FastAPI/SSE 和原生 Web Component，
  运行一个嵌入式经营分析副驾。
- 接入 SiliconFlow `DeepSeek-V4-Flash`，完成中文问题、受控 SQL、表格、图表、最终 SQL 和
  审计证据的代表性真实链路验证。
- 建立 `sqlglot` AST SQL Policy、PostgreSQL 双角色（只读查询 / 应用写审计）和持久
  `app.query_audits`。
- 使用 Olist 公开数据完成真实 PostgreSQL 加载、golden 指标校验和 60 条确定性策略/数据
  评测；同时为 3 条面试 Demo 场景建立版本化契约和数据库 golden 验证。
- 演示级 analyst/admin 会话已可在宿主页切换，并真实影响 SQL 限制与审计范围；它明确不是
  生产认证。

不要声称已经实现真实身份系统、组织级行权限、在线 60 题语义准确率、生产部署 SLA 或商业化场景的权限治理。

## 完成后可使用的简历描述模板

> 可信数据分析 Agent｜Python、FastAPI、Vanna、PostgreSQL、sqlglot、Plotly
>
> 面向经营分析场景构建可嵌入既有网页的分析副驾：通过 Web Component + SSE 流式呈现
> 查询进度、表格、图表、SQL 与指标证据；在 Agent 工具层接入指标语义、AST SQL Policy、
> PostgreSQL 双角色和版本化审计，将自然语言分析限定在可解释的业务数据范围内；基于
> Olist 构建 60 条确定性评测集和 3 条固定 Demo 场景，并以保存的 golden SQL、浏览器回归
> 与 CI 结果支撑项目表述。

上面文字必须在对应能力和评测报告落地后使用；最终简历的数字只从已保存的评测报告中提取。
