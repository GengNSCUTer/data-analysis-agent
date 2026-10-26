---
goal: 用两周掌握 Data Analysis Agent 的核心实现，并形成可用于后端/Agent 实习面试的项目表达
version: 2.0
date_created: 2026-10-26
owner: 李耿楠
status: In Progress
---

# 两周学习与面试准备计划

这份计划是当前生效版本，压缩自 [`process-agent-internship-improvement-v1.md`](process-agent-internship-improvement-v1.md)。旧文件保留为历史路线和已完成事项的来源，不再按四周阶段继续排任务。

目标不是继续堆功能或反复训练模型，而是让你能独立说明：一次业务问题如何进入系统、模型和服务端分别负责什么、SQL 为什么被允许或拒绝、结果怎样被验证和展示、连续追问如何继承状态，以及当前系统的真实边界。后训练保留为面试差异化专题，安排一个半天复盘，不再作为主线阻塞 Agent 项目。

## 学习方式

每天只学习一个小单元：先看真实代码和对应测试，再由你用自己的话复述输入、输出、关键不变量和一个失败路径，最后做一次小范围测试。发现真实缺陷才修复；修复后只重跑受影响链路。学习问答继续记录在 [`agent-project-walkthrough-v1.md`](../docs/learning/agent-project-walkthrough-v1.md)，项目事实写入 `PROJECT.md` 和飞书项目文档。

每个主题按“业务场景与功能 → 关键设计和边界 → 输入输出与关键函数 → 用户亲自审阅 → 少量复述题”推进；同一天的主题可以分成数轮小节，不一次讲完全部文件。Day 1–3 先做同一场景的整链复盘，补齐薄弱环节，复述后进入 Day 4，不将工程测试通过当作学习完成。

## 第一周：掌握请求链和可信执行边界

| 天 | 主题 | 代码入口 | 当天产出 |
| --- | --- | --- | --- |
| Day 1 | 从用户问题到 SemanticIntent、AnalysisState、QueryPlan | `server.py`、`chat_runtime.py`、`semantic_intent.py`、`analysis_state.py`、`query_plan.py` | 画出一次请求链；解释新任务、修改任务、结果追问的区别 |
| Day 2 | Catalog、指标粒度、Join 和计划检查 | `semantic_catalog.py`、`metric_context.py`、`plan_sql_guard.py` | 用 GMV 和客单价说明事实粒度、重复关联和分母；指出规则检查的边界 |
| Day 3 | SQL 安全与数据库执行 | `sql_policy.py`、`postgres_runner.py`、`resources/sql/security.sql`、`trusted_sql_tool.py` | 解释 AST 白名单、敏感字段、reader 只读事务、超时和结果预算；复述拒绝/一次修复及失败结果禁止回答的路径 |
| Day 4 | ResultContract、ResultValidator、ResultArtifact 和结果证据 | `semantic_catalog.py:ResultContract`、`result_validator.py`、`result_artifact.py`、`result_artifact_store.py`、`result_evidence.py` | 从合同如何生成开始，串联验收、完整结果保存、授权读取、模型证据与网页预览；说明为什么可执行不等于业务正确 |
| Day 5 | 上下文、短期记忆、连续追问和图表 | `working_memory.py`、`analysis_lifecycle.py`、`context_builder.py`、`chat_runtime.py` | 走通“最高州→该州品类→改 Top-N→换图→撤销筛选”；列出仍存在的体验缺口 |

第一周退出条件：能不看笔记讲清“身份/状态/计划/模型 SQL/安全执行/结果工件/回答”的主链；能指出一个模型错误、一个服务端约束和一个尚未覆盖的业务语义问题。

## 第二周：质量证据、部署边界和面试表达

| 天 | 主题 | 代码/证据入口 | 当天产出 |
| --- | --- | --- | --- |
| Day 6 | 真实质量评测与失败归因 | `scripts/verify_workspace_onboarding.py`、`evals/cases/`、`evals/results/` | 按路由、SQL、口径、结果合同、多轮和图表分类失败；不把测试通过率包装成业务准确率 |
| Day 7 | 近期真实修复复盘 | `join_evidence.py`、`plan_sql_guard.py`、`result_analysis.py` | 解释 JOIN 探针为什么误拦、如何修复、连续下钻如何验证；复述一个安全不放宽的修复取舍 |
| Day 8 | 部署、工作区接入和故障恢复 | `deployment.py`、`runtime_workspace.py`、`local_auth.py`、`infra/postgres/`、`docs/templates/workspace/` | 按文档从空环境启动；说明新业务库需要补充的 Catalog、指标、Join、权限和版本配置 |
| Day 9 | 后训练专题复盘 | `scripts/post_training/`、`docs/post-training/learning/`、既有 SFT/Adapter 报告 | 讲清 LoRA、SFT、validation-best、Base/Adapter matching；解释为什么当前不继续 DPO/训练 |
| Day 10 | 面试模拟与项目收口 | `docs/interview/project-release-facts-v1.md`、`README.md`、`PROJECT.md` | 完成 30 秒、2 分钟、5 分钟三种介绍；回答 SQL 安全、指标粒度、多轮状态、结果证据和模型效果追问 |

第二周退出条件：有一份可回查的质量报告、一条完整代码链路、一次部署/恢复证据和四条面试深挖答案。没有证据的能力标为“未验证”，不写成已上线、企业试用、SLA 或开放泛化。

## 当前进度

- Day 1：已完成主要请求链复述，仍需按新计划做一次闭卷回顾。
- Day 2：已完成指标粒度、Join 放大和计划检查复述。
- Day 3：2026-10-26 已完成。覆盖 SQL Policy、reader、结果预算、ResultValidator、服务端结果分析、JOIN 证据与一次 SQL 修复；收尾确认合同失败不存可信工件、已有范围可复用而新范围需查库。
- Day 1–3 整链复盘：待复述。小节已学，仍需补足非 SQL 分支、逻辑维度到物理表列的绑定，以及各检查层与失败处理的分工；不会因此重做已完成工程。
- Day 4：待开始。整链复盘后先看 `ResultContract.from_selection()` 在 SQL 生成前如何规定结果要求，再看验收与工件；当前任务与上次成功结果的状态区别留到 Day 5。
- Day 5：待开始。已有工程实现和历史测试证据，按新计划学习上下文与多轮，不重复大规模评测。
- Day 6–7：已有 TheLook/Olist 真实扩测和 JOIN/连续下钻修复记录，下一步以代码审阅和面试表达为主。

## 明确不纳入本两周主线

不启动新的大规模 SFT、DPO、GRPO 或基座切换；不为面试堆叠多 Agent、MCP、Redis、Kubernetes；不把已有 Olist/TheLook 同域或受控评测结果写成开放式泛化。只有出现明确模型侧错误、独立 train/dev 来源、变量和停止条件时，才另立后训练实验。

