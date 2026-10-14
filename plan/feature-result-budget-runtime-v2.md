---
goal: 将 result-budget-v2 的模式与预算接入在线查询计划和执行边界
version: 1
date_created: 2026-10-14
last_updated: 2026-10-14
owner: data-analysis-agent
status: 'Completed'
tags: [feature, runtime, result-budget]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-green)

本轮任务卡：输入是已准入的指标请求与服务端 PostgreSQL 配置；输出是带模式/预算的
QueryPlan、ResultContract、完整聚合、工件分页/CSV 下载及真实运行证据。
用户追加授权要求一次收尾：已完成返回行为迁移，不重写历史离线 Prompt，
不建立异步 export 平台，不启动训练。

## 1. Requirements & Constraints

- **REQ-001**: 服务端创建不可变 ResultBudget，传给 QueryPlan 和 ResultContract；记录 query_mode 和独立的执行、结果、预览、模型摘要预算。
- **SEC-001**: 清除客户端提交的模式/预算/计划；Runner 在查询连接前拒绝缺失配对、字段不合法、计划/合同不一致、配置漂移及未启用模式。
- **CON-001**: 在线完整聚合按结果形态派生预算；资源兜底为 100,000 行、32 MiB、5,000ms；预览 200，摘要 8 行/1,200 字符。
- **CON-002**: 分页/下载复用已有工件，不实现异步导出、EXPLAIN 成本门或新训练；执行字段不加入冻结 runtime-candidate-sql-v2 Prompt。
- **CON-003**: 历史离线调用保持原协议；鉴权 CSV 下载不重跑 SQL。

## 2. Implementation Steps

- **GOAL-001**: 元数据从服务端计划流到执行边界，行为和旧实验保持兼容。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | 新增 src/data_analysis_agent/result_budget.py 的 ResultBudget 及配对/配置校验；QueryPlan.from_selection、ResultContract.from_selection 接受同一预算，序列化支持旧协议投影。 | ✅ | 2026-10-14 |
| TASK-002 | chat_runtime.BudgetedChatHandler 删除客户端保留字段，注入服务端 aggregate 预算；examples/trusted_olist_web_demo.py 用 runner settings 显式配置；postgres_runner.SecurePostgresRunner 在 policy/查询连接前校验，拒绝写审计。 | ✅ | 2026-10-14 |
| TASK-003 | candidate_sql_generator.build_runtime_candidate_request 校验配对预算，保持 v2 SQL 语义输入和 Prompt hash；补充真实协议边界测试及 PostgreSQL 集成。 | ✅ | 2026-10-14 |
| TASK-004 | 更新设计/manifest、PROJECT 和学习记录，明确已实现/未实现边界；检查 diff 后提交推送并追加飞书项目事实。 | ✅ | 2026-10-14 |

- **GOAL-002**: 按用户追加授权一次收尾结果预算问题，然后回到学习主线。输入为实际查询形态与现有工件；输出为完整聚合、分页浏览、完整 CSV 与真实性能证据。不建立新异步任务平台，不重跑模型终测。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-005 | 在线 Router/QueryPlan 用形态与维度基数派生结果预算；SqlPolicy 使用每请求预算，完整聚合去掉模型附加的顶层分页 LIMIT，明确 Top-N 保留业务含义；旧离线调用保留 1,000 行合同。 | ✅ | 2026-10-14 |
| TASK-006 | Runner 用 PostgreSQL named cursor 分批读取，按结果行数/32 MiB JSON 字节/超时限制资源；实际超界不展示不完整统计。 | ✅ | 2026-10-14 |
| TASK-007 | ResultArtifactStore 增加 CSV 稀疏索引和有界页读取；鉴权 API 与网页支持 200 行一页，最多 500；完整 CSV 复用现有鉴权下载，不重新执行 SQL。 | ✅ | 2026-10-14 |
| TASK-008 | 用百万订单 perf_shop 跑生产 Runner 的标量、超过 1,000 行的聚合、100,000 组、Top-N、超界，验证工件分页/下载；补浏览器分页及权限回归，记录测量而不推导 SLA。 | ✅ | 2026-10-14 |
| TASK-009 | 更新预算合同、AGENTS 稳定边界、项目/学习文档与飞书，提交推送；明确本专题结束，继续原 Day 1 结果链讲解。 | ✅ | 2026-10-14 |

## 3. Alternatives

- **ALT-001**: 直接放大 LIMIT 会把不完整聚合当答案；本轮不采用。
- **ALT-002**: 任意明细 SQL 与异步导出平台扩大范围；采用不可变工件分页与现有鉴权 CSV 下载。

## 4. Dependencies

- **DEP-001**: result-budget-v2 设计和 perf_shop 性能基线已存在；定向测试使用 data-analysis-agent Conda 环境，本地 PostgreSQL socket /tmp:35434。

## 5. Files

- **FILE-001**: 核心变更为 result_budget.py、query_plan.py、semantic_catalog.py、chat_runtime.py、postgres_runner.py、candidate_sql_generator.py 和 examples/trusted_olist_web_demo.py。
- **FILE-002**: 测试为 test_result_budget_contract.py、test_runtime_candidate_request.py、test_question_router.py、test_postgres_runner.py；说明为 result-budget-v2.md、PROJECT.md 和 agent-project-walkthrough-v1.md。

## 6. Testing

- **TEST-001**: 测试三个模式的合法表示、非法数值/字段、缺失配对和不一致；假连接证明拒绝路径不访问 reader；拒绝审计有可复核原因。
- **TEST-002**: Handler 测试提交伪造 export/大预算/QueryPlan 后仍得到服务端 aggregate；候选输入字节和 hash 与未带预算的同一请求完全相等。
- **TEST-003**: 真实 PostgreSQL 标量/分组运行与拒绝路径回归；已有 policy 多读、validator、presenter 回归；定向 Ruff 和 diff check。

## 7. Risks & Assumptions

- **RISK-001**: 新字段改变旧 Prompt 会失去实验可比性；使用显式旧协议投影并测试。
- **ASSUMPTION-001**: 当前 SQL 准入只支持 Catalog 指标聚合；不把任意明细/下载请求猜成新的可执行模式。

## 8. Related Specifications / Further Reading

- ../docs/architecture/result-budget-v2.md
- ../docs/architecture/result-budget-v2-performance-baseline.md
- ../plan/process-agent-internship-improvement-v1.md
- ../docs/learning/agent-project-walkthrough-v1.md
