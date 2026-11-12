# Data Analysis Agent：当前项目情况

更新于2026-11-12。本文是当前项目事实入口；详细实验和旧决策集中在
[历史记录](docs/archive/project-history-through-20261111.md)，不再把完整流水账堆在首页。

## 项目目标与定位

面向电商经营分析的可嵌入网页Copilot。用户用中文连续提问，系统理解指标、时间、分组
和筛选，生成只读SQL，返回表格、图表、自由组织的中文解释和数据依据。

基于Vanna扩展，核心为Python 3.12、FastAPI/SSE、PostgreSQL、sqlglot与Plotly，以及
原生`<vanna-chat>`。当前是可自托管的个人开源原型；没有企业使用、生产SLA或任意数据库
零配置接入的主张。Olist为默认工作区，TheLook作为独立配置的第二实例验证。

## 当前运行与请求链

- 安装入口：`data-analysis-agent`；代码装配点：`src/data_analysis_agent/server.py:create_app()`。
- 页面：`/embedded-copilot`；本机Olist端口32013，TheLook端口32104；服务和前端同源。
- 在线模型：官方DeepSeek `deepseek-flash`，OpenAI兼容接口、`OPENAI_*`配置、关闭thinking。
- 身份：本地账号会话或来源绑定嵌入令牌，服务端决定角色与workspace；旧演示Cookie不授权。

```text
用户问题 + 已选历史 + 当前分析状态
→ SemanticIntent：模型提出意图和StatePatch，代码验证并应用
→ 历史覆盖证明：足够则读取授权CSV；不够则生成QueryPlan
→ 模型生成候选SQL
→ PlanSqlGuard → SqlPolicy → PostgreSQL reader → ResultValidator
→ 完整ResultArtifact、预览与服务端图表、受控计算证据
→ 模型自由回答 → 代码检查 / 按疑点复核 / 至多一次修订
→ 会话与分析状态保存；需要时后台压缩历史
```

### 已实现的关键能力

| 能力 | 当前行为 |
| --- | --- |
| 连续业务分析 | AnalysisState明确继承/修改/清空；可信排名实体下钻；有效补丁在已有调用次数内纠正 |
| 查询治理 | Catalog指标/关联/权限、计划对照、AST白名单、敏感列检查、只读角色及超时 |
| 业务结果检查 | 结果列、数值范围、时间、完整分组键唯一性；已覆盖指标来源/公式及JOIN放大检查 |
| 数据呈现 | 默认查询上限5000、预览200，用户可配置并受进程上限约束；预览不截断完整工件 |
| 解释与取证 | 服务端计算、模型表达；错误/疑点/缺证据分别处理，最多四次解释调用，不授SQL权限 |
| 历史复用 | 同会话单工件覆盖证明；显式两个已验证来源可作标量/可加汇总比较，不自动JOIN |
| 上下文管理 | token预算历史、回答后后台摘要；24K触发、约16K近期原文；摘要不成为执行条件或结果事实 |
| 存储与下载 | 会话、状态、索引事务保存；工件校验归属、版本、TTL与完整性；完整CSV有界内存流式下载 |
| 图表 | 服务端生成柱、横向条形、折线、面积、环形图；换图可复用结果，无需模型再调用绘图工具 |
| 运行与部署 | 单进程排队、取消及超时；wheel/合成库初始化、重启恢复、分层CI与真实浏览器验证 |

详见[当前请求架构](docs/architecture/product-request-architecture-v1.md)、
[历史复用](docs/architecture/historical-artifact-reuse-v2.md)、
[接库模板](docs/templates/workspace/README.md)和[部署说明](docs/deployment.md)。

## 最新质量证据

2026-11-11在代码`14ab842`与官方`deepseek-flash`非思考模式下，完整连续运行原60项、
122轮，最终**60/60通过，Olist和TheLook各30/30**。六类覆盖基础、公式、比较、连续
修改、历史复用和解释边界；122轮CSV独立对照、19份图表重放通过，34轮复用0业务SQL。
115轮解释accepted、7轮仅换图，无API错误或解释超时。整轮P50 5.01秒、P95 6.77秒。

没有选择性重发、改评分或拼接定向成绩；旧53/60和历史失败原样保留。语义审阅由助手
结合独立数据完成，不是第三方盲审。有限开发题集100%不等于任意问题100%，耗时也不是
生产SLA。复核有成本：本批96次复核、2次修订、213次解释调用，不声称降低总调用成本。

报告：[完整60项收尾](evals/results/agent_business_quality_closeout60_20261111.md)；
清单：[逐项结论](evals/results/agent_business_quality_closeout60_20261111.json)。
原始SSE、run、CSV、参考、截图及图表位于仓库外
`data-analysis-agent-data/evals/agent-business-closeout-full60-20261111/`。
飞书项目文档已同步该次结果（revision 337）。

## 当前边界与后续主线

SQL业务语义只做已覆盖检查，不证明任意公式等价；维度组合唯一仍不能代替独立数值对照。
解释复核可能漏判。历史比较限同会话且来源有效，不提供任意JOIN或跨会话事实召回。
企业SSO、组织行权限、多实例协调、外部独立部署与生产SLA尚未完成；偏好保存仍以回看为主。

后训练是离线研究：已有SFT、LoRA、validation-best和Gold对照证据，Adapter未接入默认
在线链路，DPO因没有足够真实可核验负例暂停。历史模型结果只从
[后训练总览](docs/post-training/README.md)读取，本轮不启动训练或新一轮业务优化。

接下来遵循[两周学习计划](plan/process-agent-internship-improvement-v2-2week.md)，学习
记录保留在[Agent代码学习记录](docs/learning/agent-project-walkthrough-v1.md)。Day 4已阶段
收尾、Day 5上下文和连续分析进行中；工程验收不等于用户已掌握代码。重点是完整链路
审阅、复述和面试表达，然后进入Day 6质量证据复盘。

## 本轮整理（2026-11-12）

任务：输入为现有源码、引用和文档；输出为无用代码清理、统一文档入口和历史归档。
不改业务决策、权限、数据、模型、训练或评分。验收使用引用/链接检查、确定性测试、
隔离PG登录/工件回归和安装资源检查；不为文档整理重复消费完整在线模型评测。

删除无生产调用方的演示身份签发/解析和旧摘要、回放文字辅助方法，保留产品登录与
伪造Cookie拒绝测试。后训练7个跳转页直接改为规范链接，重复简历草稿合并到面试事实页，
独有报告与失败证据保留。
当前架构、预算、模型与面试事实同步更新；HTML源码与生成资源不是两套源码，不清理
正在运行的工件、数据库、模型或用户未提交设计稿。

验收：CI同范围确定性测试1,034通过、30未选；隔离PostgreSQL登录/会话/工件/路由
27通过；定向Ruff、compileall、diff检查通过。所有受影响Markdown链接有效，归档正文
与原PROJECT的2,176行一致（仅调整链接目标）。安装检查发现本地旧build缓存会夹带已删
模块，已移到仓库外可恢复归档后干净构建，确认wheel包含页面/JS且不含旧身份模块。
旧生成目录归档于`data-analysis-agent-data/cleanup-20261112-GwbNNpLD/build-output/`。
本轮没有重跑付费模型评测，60/60仍引用11月11日固定代码的原始报告。
整理记录已同步飞书项目文档（revision 338）。
