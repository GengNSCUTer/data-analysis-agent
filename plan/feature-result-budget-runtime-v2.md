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

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

本轮任务卡：输入是已准入的指标请求与服务端 PostgreSQL 配置；输出是带模式/预算的
QueryPlan、ResultContract、运行证据和执行前一致性校验。本轮只启用 aggregate，
保持现有执行行为，为下一步按模式调整结果返回做准备。

## 1. Requirements & Constraints

- **REQ-001**: 服务端创建不可变 ResultBudget，传给 QueryPlan 和 ResultContract；记录 query_mode 和独立的执行、结果、预览、模型摘要预算。
- **SEC-001**: 清除客户端提交的模式/预算/计划；Runner 在查询连接前拒绝缺失配对、字段不合法、计划/合同不一致、配置漂移及未启用模式。
- **CON-001**: 默认执行 1,000 行、预览 200 行、模型摘要 8 行/1,200 字符、超时 5,000ms；实际执行配置由 composition root 注入，不提高阈值。
- **CON-002**: 不实现分页、异步导出、EXPLAIN 成本门或新的模型训练；不将新执行字段加入冻结 runtime-candidate-sql-v2 Prompt，不改变历史 hash。
- **CON-003**: 无新字段的历史离线调用保持原协议；已有鉴权 CSV 下载仅下载已验证有界结果，不等于新的 export 模式。

## 2. Implementation Steps

- **GOAL-001**: 元数据从服务端计划流到执行边界，行为和旧实验保持兼容。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | 新增 src/data_analysis_agent/result_budget.py 的 ResultBudget 及配对/配置校验；QueryPlan.from_selection、ResultContract.from_selection 接受同一预算，序列化支持旧协议投影。 | ✅ | 2026-10-14 |
| TASK-002 | chat_runtime.BudgetedChatHandler 删除客户端保留字段，注入服务端 aggregate 预算；examples/trusted_olist_web_demo.py 用 runner settings 显式配置；postgres_runner.SecurePostgresRunner 在 policy/查询连接前校验，拒绝写审计。 | ✅ | 2026-10-14 |
| TASK-003 | candidate_sql_generator.build_runtime_candidate_request 校验配对预算，保持 v2 SQL 语义输入和 Prompt hash；补充真实协议边界测试及 PostgreSQL 集成。 | ✅ | 2026-10-14 |
| TASK-004 | 更新设计/manifest、PROJECT 和学习记录，明确已实现/未实现边界；检查 diff 后提交推送并追加飞书项目事实。 | ✅ | 2026-10-14 |

## 3. Alternatives

- **ALT-001**: 直接放大 LIMIT 会把不完整聚合当答案；本轮不采用。
- **ALT-002**: 立即同时实现分页和异步导出会扩大审阅单元；本轮用明确的未启用拒绝代替。

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
