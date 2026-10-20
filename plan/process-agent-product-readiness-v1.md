---
goal: 补齐数据分析 Agent 的业务质量、可部署性、业务库接入和运行保障
version: 1.0
date_created: 2026-10-19
last_updated: 2026-10-20
owner: 李耿楠
status: In progress
tags: [Agent, 产品完善, 部署, 泛化, 测试, 面试]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In_progress-yellow)

本计划落实 2026-10-19 代码审阅提出的六项改进，作为
[四周学习计划](process-agent-internship-improvement-v1.md)的工程执行清单。
四周计划继续负责学习节奏；本文件负责下面六项交付的顺序和完成记录，不重开一条后训练主线。
首发目标是可自托管、单实例、接入一个受控 PostgreSQL 业务库的分析 Copilot。
“可泛化”指配置新库的 schema、指标与权限后复用核心流程，不承诺从裸 DDL 自动推导业务口径。

当前状态：前三项已完成，第三项以全新虚拟环境和独立 PostgreSQL 的真实链路验收为证据。
第二项九个遗留场景中有五项定向复测通过，更新后混合证据为56/60，独立数值对照23/24；
这不是修复后完整重跑的准确率。Docker Compose 配置通过检查，但本机无 Docker daemon
权限，未实际运行容器。第四项已完成：TheLook以独立在线工作区复用产品链路，真实浏览器
11/11轮通过、独立数值8/8，Olist九轮连续分析回归通过；详见阶段4验收记录。下一项是多人使用、安全和故障处理。

## 1. Requirements & Constraints

- **REQ-001**: 按下表六项顺序交付；每项包含实现、针对实际失败的回归、必要浏览器验证和事实记录。完成一项再进入下一项，不把全部后续功能一次写完。
- **REQ-002**: 业务评测分别记录数值/口径、状态继承、澄清、图表与导出；执行成功或 ResultContract 通过不能代替业务正确。
- **REQ-003**: 新工作区接入使用表列名、Join和指标定义不同于Olist的TheLook；保留其实际业务Schema，通过服务端配置选择连接、Catalog、权限和维度绑定。补充一个小型自构造Schema的确定性测试，检查配置接口没有退化为Olist/TheLook二选一分支；不另建第三套大型数据工程。
- **REQ-004**: 其他开发者能够根据说明在全新环境启动；没有外部试用时只记录隔离环境证据，不声称真实企业使用。
- **SEC-001**: 保留认证、用户/workspace 隔离、SqlPolicy、数据库只读角色、结果校验、执行预算与审计。
- **SEC-002**: 结果已被判定不可信时，任何未经替换的数字回答不得先进入前端；正常结果、帮助和普通对话不因本次修复全量关闭流式。
- **CON-001**: 复用现有类型、数据库事务、测试和日志；不新增 hash、冻结合同、基线平台或准入层，不引入 Redis、Kubernetes、多 Agent、第二套前端或队列。
- **CON-002**: 默认模型和后训练资产不变，不启动新训练，不重跑旧 protected final test，不将其问题或错误改写成新的开发题。
- **GUD-001**: 学习仍按“宏观目的 → 1–3 个关键函数 → 测试/失败路径 → 用户复述”推进；工程完成不自动标记用户已经掌握。学习记在 docs/learning/agent-project-walkthrough-v1.md，进度记在 PROJECT.md。

## 2. Implementation Steps

阶段依赖为 1 → 2 → 3 → 4 → 5 → 6。测试和相关说明随当项更新，阶段 6 负责最终汇总，
不把新增关键测试留到最后才接入 CI。对应原审阅六点的顺序是 5 → 1 → 3 → 2 → 4 → 6。

| 阶段 | 交付 | 对应四周计划 | 完成证据 |
| --- | --- | --- | --- |
| 1 | 可信回答的流式展示时序 | Day 3 的结果边界 | 拦截状态不泄漏分片；正常流式保留；浏览器实际消费 SSE |
| 2 | 业务连续分析与质量报告 | Day 5–10 | 60 条开发用例逐项有结论，关键数字有独立口径对照 |
| 3 | 全新环境部署 | Day 11–12 | 应用/数据库可安装启动，重启后会话和工件可恢复 |
| 4 | 第二业务库在线接入 | Day 13 | TheLook至少5类业务和2类越权请求、连续追问及独立数值核对；小型配置fixture验证可复用 |
| 5 | 多人使用、安全与故障处理 | Day 14–15 | 1/5/10 并发各 20 请求；故障/断连/隔离有明确结果 |
| 6 | CI、说明与开源交付收口 | Day 19–20 | 核心 CI、安装/接入/限制说明、外部试用状态与面试事实一致 |

### Implementation Phase 1

- **GOAL-001**: 修复已知不可信结果的输出时序，避免先展示错误数字再替换。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | 在 src/vanna/core/middleware/base.py 与 core/agent/agent.py:_handle_streaming_response 接入可显式决定是否即时展示分片的中间件方法；默认继续流式。src/data_analysis_agent/budget.py:BudgetSafetyMiddleware 复用现有结果失败/预算失败判断决定是否暂存，完整响应仍经过原有 after_llm_response。 | 是 | 2026-10-19 |
| TASK-002 | 在 tests/test_budget_streaming.py 验证 degraded、SQL 修复失败、结果校验失败、工具预算耗尽的 rich/simple/SSE 事件不含候选数字；正常可信回答仍在模型流结束前显示；工具调用及 usage 不丢失；异常中断不泄漏暂存内容。将测试加入 .github/workflows/tests.yml。 | 是 | 2026-10-19 |
| TASK-003 | 使用真实浏览器消费上述实际 Agent 生成的 SSE，检查中间 DOM 与最终内容；再对现有产品页运行相关回归。更新 PROJECT.md 和产品架构说明，记录测试分母；提交并推送已审阅变更。 | 是 | 2026-10-19 |

阶段 1 证据：先用新测试复现 5 项失败（四种拒答状态的分片泄漏，以及流中断前的分片泄漏），
修复后相关 Python 回归 **225 passed、5 deselected**。浏览器联合回归 **12 passed、25 deselected**，
其中 5 项以可控 provider 经实际 Agent 生成 SSE 并回放给真实 Web Component，观察中间 DOM；
另 7 项覆盖现有产品壳、预览、移动端和澄清。真实模型 + PostgreSQL + 浏览器的三轮连续追问
**1 组通过**（76.69 秒），末轮品类 Top 10 与独立 reader SQL 对照通过。
原始证据在仓库外 `evals/product-readiness-stage1-20261019/`。本项验证已知拒答状态的展示时序，
不证明任意模型回答业务语义正确；其他五阶段仍未实施。

### Implementation Phase 2

- **GOAL-002**: 明确产品可回答范围，用独立业务证据评估连续分析。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-004 | 完成 60 条独立开发用例：单轮10、澄清10、两窗口/趋势10、多轮10、安全/范围10、图表/下载/空结果10；共73轮。不复用 protected final，保留原题并记录预期修正。 | 是 | 2026-10-19 |
| TASK-005 | 复用 SSE/认证/审计和浏览器助手完成全量及定向复测；24项独立数值对照。修复结果可用性/展示、指标单位/精度、最小澄清三类，后者仍有未解决样本。 | 是 | 2026-10-19 |
| TASK-006 | 完成 evals/results/product_release_dev_v1.md，逐项来源/失败及数值对照可回读，回归和浏览器验证完成；未支持的半年度对比、多值误拦截等仍保留失败。 | 是 | 2026-10-19 |

阶段 2 结果：原全量按最终标准复核为 45/60，使用受影响题目定向复测后为 **51/60**，不是修复后
全量新跑的准确率。独立数值对照 **23/24**；剩余一项是月度表未完成半年度对比。详见
[逐类质量报告](../evals/results/product_release_dev_v1.md)。Python/数据库回归 231 passed、5 skipped，
既有产品页面回归 7 passed；真实浏览器验证三轮排名下钻及品类/履约图。下一项部署尚未开始。

### Implementation Phase 3

- **GOAL-003**: 形成不依赖当前服务器目录和 Conda 的可部署版本。

本轮任务卡：修复有复现证据的多值筛选误拦截与语义状态继承；将产品装入 wheel，提供持久化的合成数据启动流程。输入为现有 Catalog、应用和安全 SQL；输出为可安装包、容器配置和从空目录执行的部署说明。保留只读角色、账号与 workspace 隔离，不改训练或新增业务库。验收以离仓库 wheel 安装、独立 PostgreSQL 初始化、真实浏览器查询/图表/分页/下载及重启后恢复为准；当前用户无 Docker socket 权限，容器配置检查与实际运行证据分别记录。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-007 | 将产品入口移至 src/data_analysis_agent/server.py，wheel 同时打包两个 Python 包和生成的产品资源；product/browser-test/post-training 依赖分离。runtime_paths.py 统一状态目录，.env.example 提供配置。安装至仓库外全新环境，不设置 PYTHONPATH。 | 是 | 2026-10-20 |
| TASK-008 | Dockerfile 与 Compose 提供 db/init/app；复用既有 SQL，独立管理员初始化、受限 reader/writer passfile、合成数据、持久化卷、签名密钥与健康检查。配置检查通过；容器实运行因本机权限限制尚未验证，数据库初始化已在独立 PostgreSQL 实测。 | 是 | 2026-10-20 |
| TASK-009 | 全新环境的已安装 wheel + PostgreSQL 18.3 完成真实登录、模型查询/图表、285行独立数值核对、分页/下载，重启应用与数据库后恢复登录/会话/工件及追问。更新 README 与 docs/deployment.md，保留原始证据。 | 是 | 2026-10-20 |

阶段3实际证据：仓库外 `evals/product-readiness-stage3-20261020/isolated-ju4eff6t/report.json`。
两次初始化均为1,200订单；未修改现有真实分析库。应用运行于空目录，模块来自新环境
`site-packages`，使用安装包资源，未安装训练框架。243项定向回归通过、5项跳过；另外显式运行
44项 PostgreSQL/认证/工件路由集成测试通过。页面回归5项及真实模型三轮排名下钻通过。
早期测试脚本错误和启动失败记录保留，不作为成功证据。说明和限制见
[部署文档](../docs/deployment.md)。下一阶段不在本轮启动。

### Implementation Phase 4

- **GOAL-004**: 证明工作区和业务语义可配置接入，而非只支持 Olist 演示。

本轮任务卡（2026-10-20）：选择现有TheLook本地PostgreSQL库作为第二在线业务库，中文提问与
中文指标说明保持不变。目标是配置不同业务Schema后复用在线分析链路，不是再次评测本地
Adapter，也不把“国内数据来源”“中文表列名”“中文提问”“数据库引擎”混为一个目标。
输入为现有7张安全视图、20项指标、维度绑定和reader权限；输出为可配置的产品装配、独立
TheLook在线实例、接入说明及真实浏览器证据。原始快照是Kaggle镜像，非直接BigQuery导出，
本任务不重新分发原始数据。现有protected final题目/结果不作为产品开发题或提示示例。

实际前置检查：`thelook_analytics`可由`daa_thelook_reader`连接；订单125,226行、商品订单行
181,759行、行为事件2,431,963行；Catalog为20指标、7表、13个维度绑定。尚有四类在线接入障碍：
`server.py`固定装配Olist；前端bootstrap固定请求`olist-demo`；系统/帮助/部分语义示例绑定
Olist；TheLook的writer仍为`daa_thelook_writer_unconfigured`，缺少在线会话/审计的元数据配置。
上述是接入前的差距记录；本轮实施及真实验收结果见下表，不以离线WorkspaceProfile或reader连接成功代替在线接入证明。

泛化边界：本阶段验证不同PostgreSQL业务库、物理表列、Join与指标口径的接入。直接连接
MySQL、BigQuery等其他引擎还需要对应driver、方言、权限和超时实现及真实验证，不能由本轮
跨Schema结果推导。国内业务数据源在后续有明确字段/场景需求时补充，语言或产地不代替覆盖度。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-010 | runtime_workspace.py加载配置，Catalog驱动系统提示/帮助/示例；应用Store、runner、账号与审计使用配置角色与版本。TheLook新建独立writer和app元数据；bootstrap不指定数据集。第三个工单Schema fixture通过，无数据集分支。 | 是 | 2026-10-20 |
| TASK-011 | TheLook真实浏览器11/11轮、8/8独立reader数值对照，五类业务及下钻/取消筛选/换图复用/两类敏感拒答/跨workspace隔离。修复会话默认workspace和库存Join放大；Olist九轮真实连续分析及5项页面回归通过。 | 是 | 2026-10-20 |
| TASK-012 | docs/workspace-onboarding.md说明配置、权限、metadata-only初始化和边界；打包TheLook资源，离仓库wheel可加载两套Catalog，独立PG18.3初始化两次通过。详细证据见evals/results/workspace_onboarding_thelook.md。 | 是 | 2026-10-20 |

阶段4证据：仓库外 `evals/product-readiness-stage4-20261020/browser-r4/report.json` 与
`trace-audit.json`；失败轮次保留，不混入最终11轮分母。Python联合回归293 passed、1 skipped；
实际数据库/认证/工件路由24 passed。第三Schema为确定性配置测试，没有真实新业务库上线。
本轮不训练、不重跑protected final，也不修改旧离线工作区与实验数据。下一阶段尚未开始。

### Implementation Phase 5

- **GOAL-005**: 限定资源与失败行为，让多人使用不依赖运气。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-013 | 在应用入口和模型调用处增加单实例有界并发、排队等待上限、登录/请求速率限制，返回明确的排队/过载状态；验证登录过期、退出后凭证策略、嵌入来源与跨用户下载。只实现首发需要的本地账号能力，企业 SSO/组织行权限另列范围。 | 否 | — |
| TASK-014 | 跟踪用户断连、主动取消、模型超时、数据库超时、同会话并发及后台摘要停止；按实际调用链释放资源。给工件配置保存/清理策略并测试恢复；根据实测决定是否需要数据库连接池，不能因组件名而添加。 | 否 | — |
| TASK-015 | 使用可控 provider 回放运行 1/5/10 并发每档 20 次，记录排队/模型/SQL/图表时长、首个有效响应、完成时间、错误和资源；真实 provider 时延单列。复用现有 run/audit 标识与观测，不新增监控平台，不把小样本压测承诺为 SLA。 | 否 | — |

### Implementation Phase 6

- **GOAL-006**: 整理可维护、可试用、可准确讲述的开源交付。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-016 | 将语义解释、分析状态、历史/摘要、执行/结果工件等关键纯测试纳入 CI；数据库集成、浏览器和在线模型测试分别运行。README、PROJECT.md 与 docs 导航对齐真实入口、预算、支持范围、部署和已知限制；只合并重复当前说明，不删除历史实验证据。 | 否 | — |
| TASK-017 | 补接入/贡献/排错说明和代码/数据许可清单，保留 Vanna 版权；演示使用合成数据。邀请一名开发者独立部署并记录反馈；没有参与者时保留“未验证”，不虚构用户或企业案例。正式 tag/release 是另一个动作，不在本计划中自动发布。 | 否 | — |
| TASK-018 | 更新 docs/interview/project-release-facts-v1.md，整理实际测量、局限、失败修复和架构取舍；与用户逐段复述。后训练保留已有 SFT/评测及停止结论，只有线上质量/成本证据支持时再另议训练。 | 否 | — |

## 3. Alternatives

- **ALT-001**: 所有模型回答都等待结束后显示。未采用；只对现有安全逻辑已判定应拦截的响应延后，保留正常流式体验。
- **ALT-002**: 先扩大训练或增加模型规模。未采用；当前优先补齐业务质量、交付和可复用接入，训练另需明确错误与授权。
- **ALT-003**: 同时建设多租户 SaaS 与全套分布式基础设施。未采用；首发限定单实例受控业务库，按实测瓶颈增加必要能力。

## 4. Dependencies

- **DEP-001**: 阶段 1 使用现有产品 Conda 环境、pytest、Playwright 和已构建的 Web Component；不依赖 GPU 或在线模型返回特定错误。
- **DEP-002**: 阶段 2 的真实业务评估需要 PostgreSQL 和现有模型额度；原始问题/SQL/回答/截图留在仓库外授权目录，Git 内保存脱敏汇总。
- **DEP-003**: 阶段3使用隔离数据库和合成数据；阶段4复用独立TheLook库的安全视图，只补应用元数据和专用writer，不重建或覆盖业务数据，不操作Olist业务表。
- **DEP-004**: 阶段 6 的外部试用依赖实际参与者；其缺失不阻止记录其他已完成事项。

## 5. Files

- **FILE-001**: src/vanna/core/agent/agent.py、core/middleware/base.py、src/data_analysis_agent/budget.py：阶段 1 流式可见性与已有结果拒答。
- **FILE-002**: tests/test_budget_streaming.py、tests/e2e/test_trusted_embedded_window.py、.github/workflows/tests.yml：流式、浏览器与 CI 回归。
- **FILE-003**: evals/cases/product_release_dev_v1.yaml、evals/results/product_release_dev_v1.md：阶段 2 开发用例和业务质量汇总。
- **FILE-004**: pyproject.toml、Dockerfile、infra/postgres/compose.yaml、src/data_analysis_agent/server.py、deployment.py、runtime_paths.py、local_auth.py、workspace.py、metric_context.py：部署、配置与工作区接入。
- **FILE-005**: src/data_analysis_agent/postgres_runner.py、conversation_store.py、result_artifact_store.py、llm_observability.py：按实际瓶颈修改执行/资源/生命周期和观测。
- **FILE-006**: README.md、PROJECT.md、docs/README.md、docs/architecture/product-request-architecture-v1.md：当前事实；docs/learning/agent-project-walkthrough-v1.md：用户学习记录。

## 6. Testing

- **TEST-001**: 阶段 1 断言所有中间事件而非只断言最终回答，覆盖 rich/simple 两种文本、SSE 和浏览器 DOM；正常流式测试在模型流结束之前收到文本，不能仅检查最终字符串。
- **TEST-002**: 阶段 2 的语义正确由独立业务口径核对；保留失败和 provider 状态，不用执行成功、快照或同一个 renderer 自证业务正确。
- **TEST-003**: 阶段 3 验证安装产物和全新环境，不从当前源码目录导入掩盖漏包；数据库迁移和重启持久化分别验证。
- **TEST-004**: 阶段 4 对新表列名、新指标名运行同一链路，记录修改量；跨 workspace 与用户访问拒绝必须验证。
- **TEST-005**: 阶段 5 分别测容量和在线真实时延，验证断连/超时后资源收敛、工件清理与恢复，不把前端取消视为后端已取消。
- **TEST-006**: 阶段 6 对照发布说明检查自动测试、人工验证、外部试用和未支持项，各自如实记录。

## 7. Risks & Assumptions

- **RISK-001**: 当前 worktree 含上一轮未提交修复和用户文件；只提交明确归属于项目迭代且已审阅的变更，不覆盖或顺手提交用户的上下文设计原稿。
- **RISK-002**: 流式显示发生在最终中间件之前，最终回答测试无法发现短暂泄漏；阶段 1 必须验证事件顺序，并保留正常增量输出测试。
- **RISK-003**: Olist-only 提示/装配与旧文档可能掩盖新库接入成本；以实际TheLook在线请求和独立数值为证据，不用接口命名证明泛化。
- **ASSUMPTION-001**: 目标仍为后端/Agent 开发实习和小范围自托管；实施不承诺企业级 SLA 或任意数据库零配置接入。

## 8. Related Specifications / Further Reading

- [四周学习与实习项目计划](process-agent-internship-improvement-v1.md)
- [当前产品请求架构](../docs/architecture/product-request-architecture-v1.md)
- [分析上下文与记忆设计](architecture-analysis-context-memory-v2.md)
- [项目事实](../PROJECT.md)
- [代码学习记录](../docs/learning/agent-project-walkthrough-v1.md)
