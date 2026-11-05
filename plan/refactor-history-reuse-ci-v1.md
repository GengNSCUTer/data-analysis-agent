---
goal: 修复历史结果复用的审计遗漏并恢复完整浏览器CI
version: 1
date_created: 2026-11-05
owner: Data Analysis Agent
status: Completed
tags: [bug, ci, history-reuse]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-green)

目标：修复提交a06754a在完整结果追问回归中的首个已确认失败，并逐轮检查后续真实失败。
非目标：扩展历史复用能力、放宽SQL治理、调整训练或改动现有业务数据。
输入：GitHub Actions运行41965072147及对应源码；输出：修复、隔离验证记录和通过的Actions。
用户已批准修复、追加完整对话测试和通过后推送；实施证据更新于PROJECT.md及验收记录。

## 1. Requirements & Constraints

- **REQ-001**: 历史复用与现有结果检查记录一致的审计语义，包括原始行数、选中行数、进入模型证据的行数、覆盖情况及选择操作；数值必须来自实际工件和生成的证据。
- **REQ-002**: requested_answer缺指标是结构错误，允许已有的一次结构纠错；未知/越权指标仍为权威拒绝。无当前指标时交由原准入澄清，不因内部空需求抛异常。
- **SEC-001**: 保留身份/workspace隔离、覆盖证明、SQL策略、关联拒绝与文件完整性检查，不删除失败断言或填入虚假默认值。
- **CON-001**: 依照gh-fix-ci技能，用户明确批准后才实施；复现只能使用明确标记synthetic-olist-v1的隔离测试库。
- **GUD-001**: 从完整浏览器回归逐轮验证；区分已确认审计遗漏与尚未运行到的后续断言不一致。

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: 修复审计遗漏并恢复现有完整浏览器场景，保留可复核数值和SQL调用次数。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | 在隔离合成库复现tests/e2e/test_result_evidence_flow.py::test_browser_result_evidence_continuity_and_refresh第九/十名场景的KeyError；证据ci-history-6g4oetl3。 | 是 | 2026-11-05 |
| TASK-002 | 统一_answer_history_result与_inspect_previous_result的result_inspection构造，补查也记录analysis_acquisition；源10行选中2行断言通过，不删除检查。 | 是 | 2026-11-05 |
| TASK-003 | 26轮完整浏览器用例通过；十二月查询明确LOCAL_COMPUTE、0次SQL；重复关联负例/正确重查显式refresh，仍执行并审查SQL。 | 是 | 2026-11-05 |
| TASK-004 | 899项联合回归、5组64轮完整对话通过；修复5331ece已推送，Project Quality Checks运行41973195118的build与postgres-and-browser均通过。diff、定向Ruff检查通过，项目记录已更新。 | 是 | 2026-11-05 |
| TASK-005 | 真实模型季度追问暴露的空requested_answer改为Pydantic结构校验；测试一次纠错后保留指标/月粒度、未知指标不重试以及无指标交原准入。 | 是 | 2026-11-05 |

## 3. Alternatives

- **ALT-001**: 删除source_row_count断言或将缺字段默认成10；拒绝，因为会掩盖真实审计缺失。
- **ALT-002**: 为通过旧SQL次数预期禁用历史复用；拒绝，因为会撤回本轮已实现的功能。

## 4. Dependencies

- **DEP-001**: 当前gh身份可读取GengNSCUTer/data-analysis-agent的Actions；本次失败日志已获取，无需新增GitHub授权。
- **DEP-002**: 测试环境安装产品依赖与Playwright，具有独立synthetic-olist-v1 PostgreSQL及管理员测试DSN；不对Olist/TheLook业务库执行合成数据fixture。

## 5. Files

- **FILE-001**: src/data_analysis_agent/chat_runtime.py：统一实际检查证据的审计字段来源。
- **FILE-002**: tests/e2e/test_result_evidence_flow.py：审计、复用、强制SQL负例及连续追问断言。
- **FILE-003**: tests/test_execution_memory.py或新增对应定向测试：覆盖新旧入口的检查审计语义。
- **FILE-004**: PROJECT.md与evals/results/historical_artifact_reuse_20261105.md：追加本次故障、修复及真实验收结果；工作流仅在发现确有配置缺陷时调整。
- **FILE-005**: src/data_analysis_agent/semantic_intent.py、artifact_reuse_planner.py及对应测试：真实追问暴露的结构校验与原准入职责边界。

## 6. Testing

- **TEST-001**: 完整结果10行，询问第9/10行时source_row_count=10、selected_row_count=2、included_row_count=2，且回答引用真实数值。
- **TEST-002**: 全年月度结果可覆盖十二月查询时0次SQL；缺基期、缺总体或不可加指标不错误复用。
- **TEST-003**: 强制重查的关联放大SQL实际执行并被拒绝；正确查询仍通过；保持一次修复及原安全预算。
- **TEST-004**: 完整浏览器回归跑到最后一轮、刷新与CSV检查；相关历史状态隔离测试通过；新Actions运行实际通过后才标记CI修复完成。

## 7. Risks & Assumptions

- **RISK-001**: 本次CI在首个KeyError停止，后续可能还有审计或复用断言问题，不能仅补字段后宣称全通过。
- **ASSUMPTION-001**: 缺失的是新复用路径的审计字段；目前日志没有证明该问题同时造成数值错误。

## 8. Related Specifications / Further Reading

- docs/architecture/historical-artifact-reuse-v2.md
- .github/workflows/tests.yml
- GitHub Actions运行41965072147，postgres-and-browser任务，Test complete result explanations and artifact follow-ups步骤。
