---
goal: Establish a staged Product Capability Baseline v1 for the trusted data-analysis Agent
version: 1.0
date_created: 2026-09-18
last_updated: 2026-09-20
owner: gengnan
status: Planned
tags: [feature, evaluation, agent, runtime, memory, frontend, regression]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

本计划先量化当前可信数据分析 Agent 的用户可见能力边界，再根据实际失败做产品、记忆、展示或模型优化。它把 A 路由、B 单轮问答、C 多轮会话、D 浏览器和 E 汇总拆成独立阶段；每次只执行一个阶段，完成报告并经用户审阅后才进入下一阶段。

## 1. Requirements & Constraints

- **REQ-001**: 建立 `product-capability-baseline-v1`，独立报告 A 路由/澄清、B 单轮业务问答、C 短期记忆/会话、D 浏览器展示、E 汇总能力，不生成单一“项目准确率”。
- **REQ-002**: 每个 case 必须有稳定 `case_id`、类型、用户输入、预期 Router/数据库路径、预期 ResultContract 或图表形态、验收字段和风险标签；在线回答不使用逐字文本匹配。
- **REQ-003**: 失败必须归因到路由、Catalog/QueryPlan、候选 SQL、Policy、reader role、ResultContract、回答、图表、会话状态或浏览器交互之一；无法分类的记录为 `unknown`，不得静默计为 pass。
- **REQ-004**: A--E 每阶段都必须生成独立 safe report，并在用户审阅后才允许开始下一阶段；本计划不自动触发代码修复、训练、DPO、protected final test 或全量在线评测。
- **REQ-005**: B/D 的真实在线调用只能走 `examples/trusted_olist_web_demo.py` 的 SSE 路径与 `BudgetedChatHandler.handle_stream()`，记录 provider/model、运行版本、审计引用和终端状态；禁止直连数据库或绕开 Agent。
- **REQ-006**: C 必须测量 `WorkingMemory`、`PostgresConversationStore`、`ContextBudgetFilter` 的真实行为；workspace 隔离、state version 或压缩未实现时只能报 `not_implemented`。
- **REQ-007**: D 必须分开稳定的 stubbed SSE 回归与少量真实 SSE；澄清、拒绝、错误和 timeout 都必须验证 loading 最终收束。
- **REQ-008**: E 只汇总已执行阶段，显式列出测试空白、P0/P1/P2 与唯一下一任务；不把 SQL 可执行、ResultContract valid 或图表组件存在写成业务语义正确。
- **SEC-001**: 所有 SQL 仍必须经过 `SqlPolicy -> PostgreSQL readonly role -> ResultContract / ResultValidator`；评测不授权写操作、敏感列、多次 repair 或新的数据库路径。
- **SEC-002**: API key、cookie、原始 SSE、完整 SQL、结果行、completion、trace 和截图只写入 `/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/`；仓库只保存 case 合同、脚本和脱敏汇总/hash。
- **SEC-003**: 新 suite 的 `usage=runtime_regression_only`，永久禁止 SFT train/validation、DPO source、few-shot、local-model checkpoint selection 和 protected-final-test 替代；任何未来模型开发用途必须新建 split/usage 合同。
- **CON-001**: Web runtime 使用 `/disk2/gengnan/conda_envs/data-analysis-agent/bin/python`，不能用 QLoRA/训练环境启动产品服务。
- **CON-002**: 当前仅有 Olist demo workspace 和演示 cookie 身份；报告不得把它写成真实 SSO/RBAC 或多 workspace 隔离已验证。
- **CON-003**: 当前线上默认模型是 SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`；本计划不接入、训练或比较 local Adapter。
- **CON-004**: 在线模型有非确定性和 timeout；自动标签只验证结构性证据，`metric_semantics`、`time_scope`、`answer_grounded`、`chart_semantics` 必须经版本化人工审核。
- **GUD-001**: 可以复用 `evals/cases/text_to_sql_v2.yaml`、`text_to_sql_online_*.yaml`、`demo_scenarios.yaml` 的格式和既有测试辅助，但不得重命名/改写历史 suite 代替新基线。
- **GUD-002**: v1 固定最小覆盖量为 A=12、B=16、C=10 个会话脚本、D=8 个浏览器 flow；不得用换日期、同义改写或 UI 文案复制凑数量。
- **PAT-001**: 离线模型评测、确定性合同、在线 Agent 行为和浏览器交互分别出报告；E 仅关联分析，不相加不同层的分数。

## 2. Implementation Steps

### Implementation Phase 0 — 冻结公共合同与隔离边界

- **GOAL-001**: 不调用在线模型、不改运行时行为，冻结 case schema、safe artifact schema、数据隔离和阶段入口。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 新建 `evals/cases/product_capability_v1.yaml`。固定 `suite_id=product-capability-baseline-v1`、`workspace_id=olist`、`usage=runtime_regression_only` 和完整 `forbidden_uses`；顶层只能有 `routing_cases`、`single_turn_cases`、`conversation_scripts`、`browser_flows`。不写 Gold SQL、结果行或模型答案。 | Yes | 2026-09-18 |
| TASK-002 | 新建 `src/data_analysis_agent/product_capability_contract.py`，实现 frozen `CapabilityCase`、`ConversationStep`、`BrowserFlow`、`CapabilityEvidence` 和 `validate_product_capability_suite()`。验证唯一 ID、A/B/C/D 配额、期望字段、禁止用途、无 SQL 泄露和无 protected case 标识。 | Yes | 2026-09-18 |
| TASK-003 | 新建 `tests/test_product_capability_contract.py`，断言重复 ID、错误配额、缺 Router 期望、SQL/Gold 泄露、非法 usage、protected 标识和不完整会话脚本均 fail closed。 | Yes | 2026-09-18 |
| TASK-004 | 新建 `docs/architecture/product-capability-baseline-v1.md`，固定 A--E 范围、自动/人工标签边界、safe report 字段、环境和报告模板；只链接架构文档，不复制敏感资产。 | Yes | 2026-09-18 |

### Implementation Phase A — 路由、澄清与拒绝基线

- **GOAL-002**: 用 12 条确定性 case 测量“是否应该查库”的边界，不调用在线模型。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | 在 `product_capability_v1.yaml` 写入 12 条 `routing_cases`：3 answerable database、2 definition/help/non-database、3 clarification、3 security refusal、1 无可信结果的 result follow-up。每条固定 `expected_intent`、`expected_requires_database`、`expected_evidence_mode`、`expected_terminal_state`。 | Yes | 2026-09-19 |
| TASK-006 | 新建 `scripts/run_product_capability_routing.py`，复用 `QuestionRouter.classify()`，生成不含用户原文/SQL/结果行的 `routing-safe-report.json`；明确不构造或执行 SQL。 | Yes | 2026-09-19 |
| TASK-007 | 新建 `tests/test_product_capability_routing.py`，覆盖 answerable、clarification、refusal、definition/help、无可信结果 follow-up，并验证 runner report schema。 | Yes | 2026-09-19 |
| TASK-008 | 运行 A runner，审阅报告并追加 A 结论。12/12 通过，安全拒绝与相对时间 case 均未进入 database path；不触发 P0 停止条件。 | Yes | 2026-09-19 |

### Implementation Phase B — 单轮真实业务问答与图表基线

- **GOAL-003**: 在真实受控 SSE 上测量 16 条单轮 Olist 业务请求的路由、SQL、结果合同、回答和图表；不用于本地模型训练或选择。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | 在 `product_capability_v1.yaml` 写入 16 条 `single_turn_cases`：2 scalar、3 dimension-grouped、3 time-series、3 multi-metric、2 two-stage aggregate、3 chart request。每条固定期望 metric IDs、时间要求、结果 shape 和 chart type/axis 或澄清；不保存 SQL/数值。 | Yes | 2026-09-19 |
| TASK-010 | 新建 `scripts/run_product_capability_online.py`，只能访问已启动 trusted demo SSE。记录结构化 Router/工具/Policy/PostgreSQL/ResultContract/组件/终端证据；raw artifact 写外部目录，safe report 不写问题、SQL、结果行或回答全文。 | Yes | 2026-09-19 |
| TASK-011 | 新建 `src/data_analysis_agent/product_capability_review.py`，实现 `build_single_turn_review_template()` 和 `validate_manual_review()`。审核字段固定为 `metric_semantics`、`time_scope`、`answer_grounded`、`chart_semantics`、`review_status`、`review_notes_redacted`。 | Yes | 2026-09-19 |
| TASK-012 | 新建 `tests/test_product_capability_online_contract.py`，覆盖人工审核字段、未知 case、原始证据泄露和 pending/complete 边界；合同与 runner safe report 校验通过。 | Yes | 2026-09-19 |
| TASK-013 | 用户确认后运行 16 条在线 case。旧服务实例的首跑仅作历史证据；服务重载当前 Catalog 后的 2026-09-20 重跑为 16/16 AgentRun、16/16 SQL/Policy/结果合同结构闭环，二层聚合 2/2 完成。图表 2/3 首跑成功；第三条暴露模型跳过图表工具后，产品改为验证 SQL 后服务端直接绘图，针对性真实回归为 1 SQL 调用、0 模型图表调用、1 模型回合且含图表组件。外部证据 hash 已冻结；人工语义审核尚未完成，因此 B 尚不能发布语义通过率。 | Yes（待人工审核） | 2026-09-20 |

### Implementation Phase C — 多轮短期记忆、会话恢复与预算基线

- **GOAL-004**: 用 10 个会话脚本测量短期状态、持久会话与 ContextBudget；未实现能力不伪装为通过。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | 在 YAML 写入 10 个 `conversation_scripts`：2 相对日期后补绝对日期、2 仅改维度/排序、2 有可信结果的解释追问、1 无可信结果 follow-up、1 新会话不继承、1 刷新/恢复、1 长历史 budget。每步声明期望状态差异，不保存 SQL/结果全文。 | Yes | 2026-09-20 |
| TASK-015 | 新建 `scripts/run_product_capability_conversations.py`，通过 trusted demo SSE（其内部为 `BudgetedChatHandler.handle_stream()` 与 `PostgresConversationStore`）执行脚本。每步只记录 conversation ID hash、route、WorkingMemory 允许字段投影、tool-free、可信结果摘要存在性、ContextBudget 来源；原始事件只写仓库外。 | Yes | 2026-09-20 |
| TASK-016 | 新建 `tests/test_product_capability_conversation_contract.py`，覆盖 carry-forward、无可信结果澄清、新会话清空、刷新恢复和完整 turn budget；workspace/state-version/压缩未实现时报告 `not_implemented`。 | Yes | 2026-09-20 |
| TASK-017 | 运行 C 并完成结构性审阅。canonical safe report 为仓库外 `phase-c/rerun-v2/conversation-safe-report.json`：10 个脚本、26 个 turn、4/10 脚本严格通过；发现 WorkingMemory 不记录新维度、结果追问路由误入新数据库分析、部分真实 SQL turn 未完成、刷新后的可信结果依赖上一轮成功。workspace/state-version、clear action、ResultArtifact 回放和历史压缩仍未实现/未测，不在本阶段直接实现 M2--M7。 | Yes（待用户审阅） | 2026-09-20 |

### Implementation Phase D — 浏览器、流式状态、表格与图表基线

- **GOAL-005**: 用 8 条 browser flow 验证用户可见闭环，将稳定 DOM 回归和真实 SSE 分开。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-018 | 在 YAML 写入 8 条 `browser_flows`：2 stub normal stream、2 stub no-SQL clarification/refusal、1 stub provider/error、1 stub history/new conversation、1 live 201--999 行 preview/export、1 live chart resize/mobile。每条包含 `transport_mode=stub|live`、预期 loading 终态和 DOM evidence。 |  |  |
| TASK-019 | 新建 `tests/e2e/test_product_capability_browser.py`，复用嵌入窗口辅助函数和 `vanna-chat.ts` 的 `settleCompletedStream()`；stub flow 断言 loading 结束、错误可见、历史/表格/图表状态正确。 |  |  |
| TASK-020 | 新建 `scripts/run_product_capability_browser_live.py`，只执行 YAML 中最多 2 个 `live` flow，外部保存 trace/截图，仓库内写脱敏 `browser-safe-report.json`；SSE 结束后存在 `working` 状态必须失败。 |  |  |
| TASK-021 | 先运行 stub flow，再在用户确认后运行最多 2 条 live flow；将结果映射到 GAP-UX-01/03/04。D 阶段不重构产品壳。 |  |  |

### Implementation Phase E — 汇总能力边界与优化入口

- **GOAL-006**: 汇总 A--D 的证据，输出不混淆层次的能力地图和唯一下一任务。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-022 | 新建 `scripts/summarize_product_capability_baseline.py`，只读取 A--D safe report 和人工审核，核对 suite/version/hash 后生成 `capability-baseline-summary-v1.json`，按阶段、失败层级、case 类别、未执行阶段汇总，不读取 raw SQL/问题/结果行。 |  |  |
| TASK-023 | 新建 `tests/test_product_capability_summary.py`，验证 missing phase 为 `not_run`、不同 suite/hash 拒绝合并、缺人工审核不能显示语义 pass、异质指标不生成总准确率、safe summary 无 SQL/结果。 |  |  |
| TASK-024 | 更新 `docs/architecture/product-capability-baseline-v1.md` 和 `docs/architecture/project-v1-gap-map.md`，列出 verified capability、failure taxonomy、test gaps、P0/P1/P2 与唯一下一任务；该任务必须来自最高优先级的已验证失败。 |  |  |

## 3. Alternatives

- **ALT-001**: 先实现产品壳/M2/M3/M5，最后测试。拒绝：尚无证据区分问答、状态、展示和流式交互的主要失败源。
- **ALT-002**: 用 Olist/TheLook protected final test 充当产品 runtime suite。拒绝：它们是 local-model final evaluation，不能成为在线 Agent 调优/产品回归来源。
- **ALT-003**: 只运行单测并当作产品能力。拒绝：单测不测在线中文问答、回答有据、真实 SSE 与人工语义。
- **ALT-004**: 直接运行数百条在线问题。拒绝：当前先定位系统性失败，规模化在线调用不能代替分层归因。
- **ALT-005**: 浏览器全部 mock，不跑 live SSE。拒绝：仍需要少量真实流式/工具/审计闭环证据。

## 4. Dependencies

- **DEP-001**: `examples/trusted_olist_web_demo.py`、本地 PostgreSQL 和 SSE API 可用；若服务并非当前 worktree revision，B/D 标记 blocked，不记录为基线。
- **DEP-002**: Web runtime `/disk2/gengnan/conda_envs/data-analysis-agent/bin/python` 与 `RUN_PROJECT_DB=1` 集成环境可用。
- **DEP-003**: `QuestionRouter`、`WorkingMemory`、`BudgetedChatHandler`、`PostgresConversationStore`、`ContextBudgetFilter`、`TrustedRunSqlTool`、`ChartContract` 的当前代码保持可读。
- **DEP-004**: B/C/D 真实调用需要用户的逐阶段确认、有效 SiliconFlow 配置和可用配额；没有这些条件时只做 Phase 0/A 和 stub D。
- **DEP-005**: Playwright、`tests/e2e/` fixture、Web Component build 可用；缺依赖时 D 报环境阻塞，不能以 HTTP 200 替代浏览器验收。

## 5. Files

- **FILE-001**: `evals/cases/product_capability_v1.yaml` — A--D case 合同。
- **FILE-002**: `src/data_analysis_agent/product_capability_contract.py` — schema、隔离与 safe-evidence validator。
- **FILE-003**: `src/data_analysis_agent/product_capability_review.py` — B 人工审核模板/校验。
- **FILE-004**: `scripts/run_product_capability_routing.py` — A runner。
- **FILE-005**: `scripts/run_product_capability_online.py` — B SSE runner。
- **FILE-006**: `scripts/run_product_capability_conversations.py` — C runner。
- **FILE-007**: `scripts/run_product_capability_browser_live.py` — D live browser runner。
- **FILE-008**: `scripts/summarize_product_capability_baseline.py` — E 汇总器。
- **FILE-009**: `tests/test_product_capability_contract.py`、`tests/test_product_capability_routing.py`、`tests/test_product_capability_online_contract.py`、`tests/test_product_capability_conversation_contract.py`、`tests/test_product_capability_summary.py` — 合同/runner 回归。
- **FILE-010**: `tests/e2e/test_product_capability_browser.py` — D browser 回归。
- **FILE-011**: `docs/architecture/product-capability-baseline-v1.md` — 方法、结果、人工审核和能力结论。
- **FILE-012**: `docs/architecture/project-v1-gap-map.md` — E 阶段优先级入口。

## 6. Testing

- **TEST-001**: Phase 0 运行 `tests/test_product_capability_contract.py`，验证 schema、配额、usage 隔离、无泄露和错误拒绝。
- **TEST-002**: Phase A 运行 `tests/test_product_capability_routing.py` 和 A runner；12 条均有 route/evidence/terminal-state，安全拒绝与相对时间不得进入 database path。
- **TEST-003**: Phase B 先跑 `tests/test_product_capability_online_contract.py`；用户确认后跑 16 条真实 SSE，验证外部 raw artifact、safe report 和人工审核完整性。
- **TEST-004**: Phase C 先跑 `tests/test_product_capability_conversation_contract.py`；用户确认后跑 10 个会话脚本，验证状态投影和 ContextBudget 来源。基线后的定向修复只重跑受影响的小批：维度替换/清空、结果解释与明确下钻区分、客户端重载的可信 ResultArtifact 摘要回放，以及一个 SQL 未完成的仓库外诊断工件。safe report 只能记录诊断 SHA-256，原始 SQL 与安全错误文本只能留在权限受限的仓库外路径。
- **TEST-005**: Phase D 跑 6 条 stub browser flow；用户确认后最多跑 2 条 live flow，验证 loading、错误、表格范围和图表尺寸。
- **TEST-006**: Phase E 跑 `tests/test_product_capability_summary.py` 与汇总器；缺阶段、hash 不同、缺人工审核或 raw 泄露必须失败。
- **TEST-007**: 每阶段结束运行 `git diff --check`；只有改动 Python 时运行 `python -m compileall` 和当期 pytest，不重跑训练或 protected final test。

## 7. Risks & Assumptions

- **RISK-001**: 在线 provider 非确定或 timeout。缓解：独立分类，自动结构证据与人工语义审核分开，不把 timeout 写成语义失败/成功。
- **RISK-002**: 新 case 泄露到后训练。缓解：`forbidden_uses`、合同测试、外部 artifact 根；任何模型用途另建合同。
- **RISK-003**: 把执行成功/ResultContract valid 当业务正确。缓解：B 强制人工审核口径、时间、依据和图表语义。
- **RISK-004**: 单 workspace demo 无法测真实隔离。缓解：C 记 `not_implemented`，把结果作为 M2 输入。
- **RISK-005**: 浏览器/网络脆弱。缓解：D 的主要门是 stub；live 仅做少量 E2E 并区分环境阻塞。
- **ASSUMPTION-001**: Olist trusted demo 是当前唯一产品 runtime workspace；TheLook 仍主要用于离线跨 Schema 模型评测。
- **ASSUMPTION-002**: A=12、B=16、C=10、D=8 用于发现首批系统性问题，不构成统计总体准确率。
- **ASSUMPTION-003**: B/C/D 的真实在线运行均需要用户阶段性确认；Phase 0/A/contract/stub 测试不需要额外授权。

## 8. Related Specifications / Further Reading

- [Project v1 基线审查记录](../docs/architecture/project-v1-baseline-review.md)
- [Project v1 缺口与优化地图](../docs/architecture/project-v1-gap-map.md)
- [嵌入式 Copilot 与对话记忆设计 v1](../docs/architecture/embedded-copilot-and-memory-design-v1.md)
- [Week 2–4 路线](process-project-v1-week2-4.md)
- [主模型证据](../docs/post-training/experiments/project-v1-model-baseline.md)
- [DPO 停止证据](../docs/post-training/experiments/project-v1-dpo-decision.md)
