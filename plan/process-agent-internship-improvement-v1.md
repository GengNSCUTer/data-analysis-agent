---
goal: 可信数据分析 Agent 的产品完善、代码学习与后端及 Agent 实习面试准备
version: 1.0
date_created: 2026-10-10
last_updated: 2026-10-21
owner: 李耿楠
status: In Progress
tags: [Agent, 后端, 电商分析, Text-to-SQL, 后训练, 开源, 面试]
---

# Introduction

![Status: In Progress](https://img.shields.io/badge/status-In_Progress-blue)

本计划以 2026-10-10 的仓库实现为起点，参考用户的《后端实习简历_v3》和《第二轮面试准备计划_2026-09-28》。目标是在四周内形成可解释、有业务质量证据、能在其他机器复现的实习项目版本；企业级通用产品作为后续方向，不把四周计划写成商业交付承诺。代码基线已经提交并推送为 `8bdb1de`。

两个简历项目分别承担不同主题：知识 Agent 平台解释非结构化检索、工具工作流和 Durable；本项目解释结构化数据分析、指标口径、只读执行、结果证据和小模型候选生成。计算机基础由另一个项目安排，这里只讨论它们在真实代码中的应用。

**当前事实（2026-10-20）。** 在线使用SiliconFlow DeepSeek-V3.2，Olist与TheLook已分别通过配置
接入同一产品链路；六项工程补缺、隔离安装、业务开发场景、容量与CI已有记录，详见
[工程清单](process-agent-product-readiness-v1.md)。偏好仍仅保存，历史Phase B标注不由后续
新报告自动补齐。主模型是Qwen2.5-Coder-1.5B Base的bf16 LoRA，双workspace训练/validation
为6,000/1,500，各750条已见Schema留出的ordered Gold为403/750、396/750。Adapter未接入
在线页面，DPO未训练。工程与测试完成不等于用户已经掌握实现。

**时间安排。** 默认每周五个主题单元、每单元 90–120 分钟，另外预留每周半天处理必要的小改动。遇到面试或另一项目占用时间，按完成条件顺延，不赶工。建议本项目时间约 60% 用于业务质量与工程补缺，25% 用于代码理解和面试复述，15% 用于后训练学习与有限验证；比例是执行建议，不是岗位统计结论。

**每日方式。** 一个真实场景 → 用户先口述 → 阅读 1–3 个关键函数 → 跟踪输入/状态/输出 → 做一个测试或小改动 → 记录证据与边界 → 再次口述。发现缺陷立即列明影响；只修本单元需要的问题。通用学习材料不复制另一个项目，后训练概念沿用既有学习目录。2026-10-10 用户授权开始 Day 1；后续功能任务未自动启动。

2026-10-19 补充：[六项产品完善执行清单](process-agent-product-readiness-v1.md)按“可信流式回答 → 业务质量 → 部署 → 新库接入 → 多人/故障 → 开源收口”逐项落实本计划。工程修复不自动标记学习完成，代码阅读与复述仍记录在既有学习文档。

## 1. Requirements & Constraints

- **REQ-001**: 四周交付包括一张真实请求链路图、一份在线产品质量报告、一个可在隔离环境启动的版本，以及 30 秒/2 分钟/5 分钟介绍和四次专项口述记录。
- **REQ-002**: 默认服务一个部署实例中的受控电商分析库，首发业务包括成交/订单/客单价概览、州或品类排行、时间趋势与履约表现；清楚列出不能回答的退款、广告投放或因果归因问题，不凭缺失数据推断。
- **REQ-003**: 首发开源形态是自托管分析 Copilot。接入方配置 PostgreSQL 只读凭据、Catalog、指标合同、workspace 和允许嵌入来源；不要求接入方使用 Olist 表名，也不要求先微调模型。
- **REQ-004**: 每阶段同时交付代码理解与实际证据。用户能够解释模型和代码的职责、一次失败路径及一个设计取舍，才将该能力加入面试事实表。
- **REQ-005**: 分别记录路由正确、SQL 执行、结果合同、Gold 一致、回答有依据、图表及多轮状态的质量；不把单元测试数量包装成线上准确率。
- **REQ-006**: 后训练保留已有 SFT 实验为差异化专题，近期重点是理解和验证产品适配。新的完整训练必须有独立开发集、可量化假设和资源上限，并单独取得用户启动指令。
- **SEC-001**: 保留 AST Policy、数据库只读角色、结果合同、查询预算和审计；Catalog、摘要、客户端参数和模型输出不能赋予权限。
- **SEC-002**: 原始业务数据、模型权重、工件、密码与 API Key 留在仓库外；仓库中的演示数据使用自己构造的合成样本。发布说明分别列明上游代码、数据及派生资产的许可。
- **CON-001**: 不重复安排操作系统、网络、数据库原理等基础课程；不为简历改写 Java、增加多智能体、任意 Python 执行、Kubernetes 或 Redis，除非实测缺陷需要它们。
- **CON-002**: 既有 final test 的问题、Gold、失败 SQL 和派生改写不得进入训练、开发挑战集或模型选择。新产品开发用例单独建来源记录；旧 final test 只作为历史结果展示。
- **CON-003**: 单轮只执行一个具体任务。局部测试通过后继续既定范围，不引入重复准入与多层人工审批；仅对新增权限、数据污染、对外发布等实质边界做额外审查。
- **GUD-001**: 项目实时进度写入 PROJECT.md 和飞书项目文档，稳定协作约束才写 AGENTS.md；每日学习记录写入学习目录，面试记录不包含私人联系方式或未经证实的成果。

## 2. Implementation Steps

阶段间按 1 → 2 → 3 → 4 推进。阶段内 TASK 编号顺序表示依赖；每个 TASK 完成一次短复验即可，不重复跑大规模模型终测。第四周的模型服务实验有单独准入条件，不阻塞其他开源交付。

2026-10-20说明：下面的完成列记录用户学习与复述情况，不代表工程尚未实现。原计划中的
60题、安装恢复、新库接入和容量测试已有交付，分别回读`evals/results/`与工程清单即可；
新库接入以已完成的TheLook配置实例为准，不再另造mini-shop或重复冻结用例。仅发现新的
具体缺陷时做相应修改，正常学习以阅读、追踪与口述为主。

### Implementation Phase 1

- **GOAL-001**: 第一周掌握现有系统，复核核心边界，产出面向产品的最小质量基线。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | Day 1：从 src/data_analysis_agent/server.py:create_app、ManagedChatHandler、SemanticIntentInterpreter 和 BudgetedChatHandler 跟踪“客户州有效订单前五”请求。理解当前自然语言理解、状态和执行的分工；旧 classify/admit 只供历史离线协议。用户闭卷画链路，说明QuerySpec/QueryPlan区别与一次拒绝路径。 | 是 | 2026-10-21 |
| TASK-002 | Day 2：阅读 semantic_catalog.py、query_plan.py、plan_sql_guard.py 和十项/十九项指标合同，核对 GMV、订单数、客单价的 grain、状态与 join 放大风险。只为发现的真实口径缺陷补测试，不一口气读全部训练构造脚本。 | 否 | — |
| TASK-003 | Day 3：阅读 sql_policy.py、SecurePostgresRunner 和 ResultValidator，复核只读角色、敏感列、空窗口、计划结果预算与多读一行，以及预览200行/模型8行/完整工件的区别；说明执行与合同通过不能证明全部SQL业务语义。旧1,000行协议只供历史离线评测。 | 否 | — |
| TASK-004 | Day 4：阅读 conversation_store.py、working_memory.py、context_builder.py、analysis_lifecycle.py 与 ResultArtifactStore。跟踪“查结果→结果解释→换维度→重载→清空分析”的会话，验证摘要不进入 SQL 条件、状态冲突可恢复、下载不能跨用户/workspace。 | 否 | — |
| TASK-005 | Day 5：复用 scripts/run_product_capability_online.py 和 conversations.py 的结果记录，读取仓库外回答证据，完成已有 Phase B 的人工标注；若旧输出缺失，明确标缺失。另冻结 evals/cases/product_release_dev_v1.yaml 的 60 条新开发用例：单轮业务10、澄清10、两窗口比较/趋势10、多轮10、安全与范围10、图表/下载/空结果10。来源不复写既有 SFT 或 final test；输出当前 baseline 和第一周五分钟口述。 | 否 | — |

第一周退出条件：能画出并解释请求链、身份链、结果链、会话链和离线模型链；已有 Phase B 审核有可回查状态；60 条新开发用例的目标行为、业务口径和来源记录齐全。没有证据的能力明确标记待测。

2026-10-21完成Day 1：用户已分段阅读并复述语义理解、状态更新、QueryPlan、候选SQL、执行
安全、结果保存与回答的主链；能说明遗漏SP筛选应拒绝、QuerySpec与QueryPlan的来源差异，
以及服务端身份、角色白名单与数据库只读权限的区别。完成依据是代码阅读与复述，不是工程
测试数量。Day 2开始核对GMV的商品行粒度、状态和品类筛选口径；TASK-002仍待完成。
问答继续集中在[`agent-project-walkthrough-v1.md`](../docs/learning/agent-project-walkthrough-v1.md)，
不启动新训练或重复终测。

### Implementation Phase 2

- **GOAL-002**: 第二周解决有数据支持的真实业务体验问题，建立从错误到修复的质量闭环。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-006 | Day 6：运行 TASK-005 的开发集并按 Router/grounding、SQL 结构、时间/状态、指标 grain、ResultContract、回答/图表、多轮分类。保留 sample/request/model/prompt/SQL/artifact 标识；raw 内容在仓库外，脱敏统计进 evals/results/product_release_dev_v1.md。仅选择影响最大的前三类进入本周修复。 | 否 | — |
| TASK-007 | Day 7：复盘已复现的指标/维度/过滤误识别，阅读 SemanticIntentInterpreter、resolve_state_intent() 和 QueryPlan 的处理。相对时间使用显式时间锚点和 workspace 时区/数据范围；不能把 Olist 历史数据冒充当前销售。先复现当前行为，仅有新缺陷才修复，不恢复旧 classify/admit 语言闸门。 | 否 | — |
| TASK-008 | Day 8：针对实际 join、grain、分母或结果列错误选择一项修复。优先使业务合同、Catalog 和候选校验表达一致；如需增强 plan_sql_guard，使用 sqlglot AST 进行该项验证并增加等价正确 SQL 与错误 SQL 的对照，不能声称规则检查证明所有业务语义。 | 否 | — |
| TASK-009 | Day 9：完成已支持指标的两窗口比较与受控维度拆解演示；比较数值从验证结果确定性计算，回答标明口径与缺失数据。若当前工具链不支持两次查询，在 RequestBudget 与执行计划中明确上限后再扩展。展示已定义的时间、排行和比例图表；不能为“原因是什么”伪造因果。偏好是否应用展示，仅在不改变 SQL 语义的情况下作为一个独立小项。 | 否 | — |
| TASK-010 | Day 10：仅重跑受影响类别并完成一次 60-case 最终开发回归，输出前后相同模型/Prompt/数据条件的统计、回退和限制；组织一次“为什么 SQL 可执行还会算错”的模拟追问。未达到首发能力的场景从支持清单移到澄清/不支持，不通过改题目隐藏失败。 | 否 | — |

第二周退出条件：前三类问题各有可复现样例和处理结论；60 条均有最终状态。将每类正确数/样本数与实际错误保留为发布证据，不预先宣称某个准确率。安全越权、未验证数值展示和跨用户下载必须为零；其余未解决场景清楚收口。

### Implementation Phase 3

- **GOAL-003**: 第三周让其他开发者能部署并接入自己的小型业务库，补齐可运维性。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-011 | Day 11：审查 server.py、local_auth.py、runtime_paths.py、pyproject.toml 和运行环境依赖，解释现有产品/后训练依赖分离、环境配置及路径解析。按 .env.example 追踪必填项、默认值和启动失败提示；工程已有交付，不为学习重复改造。 | 否 | — |
| TASK-012 | Day 12：复用 infra/postgres/compose.yaml 建立合成小电商数据和 app schema 初始化/迁移流程；新增应用容器和可执行 quickstart。在空白目录/隔离环境验证源码安装或容器启动、健康检查、重启后会话和工件恢复。现有服务器数据库不删除、不重建。 | 否 | — |
| TASK-013 | Day 13：用自己构造、表列名不同于 Olist 的 mini-shop workspace 做接入 smoke。整理 Catalog、WorkspaceAccessRegistry、readonly role、ResultContract、版本与来源允许列表的配置方法；通过五条固定业务请求和两条越权请求，证明无需修改 Router 的 Olist 专用分支才可完成最小接入。先支持 PostgreSQL，不新增方言。 | 否 | — |
| TASK-014 | Day 14：核对 AccountStore/ProductIdentityResolver、embed token、ExportAudit、会话 CAS 与取消处理。补测试会话过期/退出、来源不匹配、工件篡改、两用户隔离、同会话并发、断连及数据库超时。给单实例部署加有界模型并发与登录/请求速率限制；共享多实例限流与企业 SSO另列边界，不使用进程内 limiter 冒充跨实例方案。 | 否 | — |
| TASK-015 | Day 15：按实测瓶颈处理数据库连接、同步阻塞、模型超时与工件生命周期；执行固定回放的 1/5/10 并发，每档20次请求，记录排队/模型/SQL/绘图延迟、错误数、首事件与完成时间、token估算。mock provider 测容量，在线 provider 单列真实时延，不能混报 p95 或承诺企业 SLA。产出部署/故障排查说明和工程口述。 | 否 | — |

第三周退出条件：隔离环境可启动；mini-shop 接入可复现；重启和权限边界有测试；并发数据有配置、样本数和测量定义。开源首发承诺单实例、一个受控业务库，不承诺组织级行权限、跨实例协调或大规模租户管理。

### Implementation Phase 4

- **GOAL-004**: 第四周掌握已有后训练证据、完成一个可选候选服务验证，并形成可投递的开源版本。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-016 | Day 16：沿现有 scripts/post_training/training/run_post_training_sft_smoke.py 小单元审阅 tokenizer、prompt/labels mask、EOS/padding、get_peft_model、AdamW、梯度累积与 validation-best checkpoint；用 tests/test_post_training_sft_smoke.py 验证。用户能指出实际更新的参数与推理加载所需文件；本任务不启动完整训练。 | 否 | — |
| TASK-017 | Day 17：核对模型卡与 matching runner 的生成隔离、前缀归一化、Policy/结果合同/Gold 分层和 ordered/bag 区别。检查离线训练 QuerySpec 与在线 QueryPlan 输入是否能无损对齐；输出 docs/post-training/experiments/product-candidate-readiness-v1.md 的“已验证/缺口/停止条件”。复盘 Task B 未提升和 DPO 无真实 pair 的结论，不制造新成绩。 | 否 | — |
| TASK-018 | Day 18：可选门：只有 TASK-017 的输入/权限/资源合同明确且用户愿意实际验证时，接入 opt-in 本地候选 provider；使用新开发集中的24条预注册问题，对比在线候选与现有1.5B Adapter，记录语义、格式、延迟和资源。默认产品 provider 不自动切换，不为SQL小改造重跑旧750终测。若不满足门或无收益，写停止结论，继续开源包装。 | 否 | — |
| TASK-019 | Day 19：形成首发交付包：README 安装与接入、demo script、架构、指标语义、许可证来源、故障处理、CONTRIBUTING 和CI。CI覆盖纯契约与应用smoke；数据库集成和在线模型测试独立。让另一位开发者按文档部署并记录问题；若无人试用，明确该项未验证。发布 tag/release 作为后续单独动作，不由本计划自动发布。 | 否 | — |
| TASK-020 | Day 20：输出 docs/interview/project-release-facts-v1.md，组织第四次口述与一次完整模拟；准备30秒定位、2分钟链路、5分钟深挖，以及指标口径/SQL安全/多轮与工件/模型效果四条追问。用代码和测试证据检查简历条目，再由用户确认个人简历修改；不把离线Adapter写成线上替代、不把同Schema留出写成未见Schema泛化、不写不存在的用户或QPS。 | 否 | — |

第四周退出条件：核心功能能演示、另一环境可复现、质量指标范围清楚、用户能回答失败路径。后训练至少完成 TASK-016/017 的理解；TASK-018 可以以“无充分收益，暂不接入”结束。项目达到可投递版本后转为维护，不因某个训练指标不够高无限延期。

**后训练的后续门。** 投后端/Agent应用岗时，能讲清一次 SFT 与效果评测即可；有明确算法训练岗位机会时，再设单独学习/实验计划。一次新实验至少需要：独立新 train/dev 来源、已复现的模型侧错误、一个训练变量、固定资源预算和 stop 条件。线上输入/列别名规范/展示错误优先工程修复；schema linking/指标推导错误才考虑数据或训练。DPO 继续遵守已有 pair 门；没有真实 rejected 就不训练。DPO、GRPO、分布式训练原理可作为扩展学习，不以新增生产能力名义强行启动。

## 3. Alternatives

- **ALT-001**: 继续扩大合成数据并更换更大模型。当前缺乏统一产品质量基线，且训练模板与在线输入未证明相同，暂缓；先区分数据分布、结构规范与模型能力问题。
- **ALT-002**: 为区别另一个Agent项目加入多智能体、MCP与Durable全套。两项目的业务与信任边界已经不同，本轮不重复建设；仅在经营分析任务确需后台执行时另立需求。
- **ALT-003**: 直接定位通用企业SaaS。组织隔离、行级授权、SSO、数据接入运营和 SLA 均有额外成本，本轮以可自托管、可配置的单实例产品为目标。
- **ALT-004**: 彻底放弃后训练。已有可复核SFT资产可以形成面试差异，因此保留受控专题；减少计算实验不等于放弃理解训练原理和应用。

## 4. Dependencies

- **DEP-001**: 用户每天为本项目保留90–120分钟；与另一个项目并行时按完成条件顺延，不复制其4.5–5.5小时每日总安排。
- **DEP-002**: 在线评测需要既有服务和模型额度，数据库用当前只读角色；不停止其他项目服务或复用其凭据。
- **DEP-003**: 本地候选实验依赖已冻结Adapter、独立开发数据和空闲GPU；三者缺一就跳过 TASK-018，不阻塞发布。
- **DEP-004**: 企业真实业务接入由用户后续协调；无人外部试用时只报告本地与隔离环境证据，不声称电商企业验证。

## 5. Files

- **FILE-001**: src/data_analysis_agent/server.py、runtime_control.py、semantic_intent.py、query_plan.py、catalog_grounding.py：当前请求/身份/语义入口；question_router.py只按当前调用职责阅读，不把旧语言闸门当作在线入口。
- **FILE-002**: src/data_analysis_agent/sql_policy.py、postgres_runner.py、result_validator.py、plan_sql_guard.py：受控执行与正确性边界。
- **FILE-003**: src/data_analysis_agent/conversation_store.py、working_memory.py、context_builder.py、analysis_lifecycle.py、result_artifact_store.py：状态、摘要和工件。
- **FILE-004**: examples/embeddable_copilot_shell.html、frontends/webcomponent/、infra/postgres/、pyproject.toml：交互与部署接入。
- **FILE-005**: evals/cases/product_release_dev_v1.yaml、evals/results/product_release_dev_v1.md、evals/results/thelook_product_queries.md、docs/interview/project-release-facts-v1.md：已有开发评测、结果和面试事实，学习时审阅证据与失败，不因文件存在标记已掌握。
- **FILE-006**: docs/post-training/learning/、experiments/project-v1-model-card.md、project-v1-dpo-decision.md、scripts/post_training/training/：后训练学习与已冻结实验。

## 6. Testing

- **TEST-001**: 一条用户请求在 Router/QueryPlan/SQL/ResultArtifact 的标识与版本能关联；澄清、拒绝和可信结果追问不额外查库。
- **TEST-002**: 新60-case开发集每条有目标、人工口径和来源；业务候选同时验证执行、结果合同与人工/独立Gold；UI只发预览，模型只收受控摘要。
- **TEST-003**: 两用户/workspace隔离、匿名访问、token来源/过期、完整工件下载和篡改阻断；数据库集成使用隔离fixture且清理自身创建的数据。
- **TEST-004**: 同会话状态冲突、清空与新会话区别、摘要失败回退、断连/超时/取消、版本变化后的旧结果失效均有可复现案例。
- **TEST-005**: 隔离环境从源码/容器启动、重启恢复、mini-shop受控接入；1/5/10并发各20请求，报告请求类型、provider和统计定义。
- **TEST-006**: 可选Adapter对照使用相同新开发问题、输入语义、decode和验证链；训练检查只在对应环境验证，不将运行环境测试变成GPU依赖；新增训练与旧final test重跑另行授权。

## 7. Risks & Assumptions

- **RISK-001**: 代码已有大量能力而人工尚不熟悉，继续自动增加代码会使面试不可控。按每日小单元阅读与复述执行，先解释再扩展。
- **RISK-002**: 极低validation loss和高合同通过率容易掩盖列别名、排序、grain或数值错误。结果报告保留分层指标与Gold口径，避免只报最好数字。
- **RISK-003**: Olist数据页面标明CC BY-NC-SA 4.0；其数据与派生权重不作为默认商业部署资产分发。自构造合成数据与接入方自身业务库分别处理，保留上游MIT版权声明。
- **RISK-004**: 个人四周无法证明企业规模可靠性。开源首发限定部署形态；实测并发与外部试用多少，就报告多少。
- **RISK-005**: AI修复可能固化开发题目。采用场景口径和等价SQL测试，记录来源；既有final test不回流，不为凑数复制日期与问法。
- **ASSUMPTION-001**: 目标仍为后端与Agent应用实习。若后续明确转为训练算法岗，再调整时间比例与训练专题，不在当前主线自动扩张。
- **ASSUMPTION-002**: 首发接入方愿意定义自己业务的指标口径和访问范围；不能从裸数据库DDL自动推导收入、订单和退款的正确业务定义。

## 8. Related Specifications / Further Reading

本计划参考用户本地简历和第二轮计划，仅引用其工作流，不把私人简历复制进公开仓库。旧 Week 2–4 路线保留为已经实施阶段的历史说明；新的学习和交付节奏以本计划为准。

- [当前项目事实](../PROJECT.md)
- [当前产品请求架构](../docs/architecture/product-request-architecture-v1.md)
- [产品能力基线与待人工审核项](../docs/architecture/product-capability-baseline-v1.md)
- [既有 Week 2–4 路线](process-project-v1-week2-4.md)
- [模型卡](../docs/post-training/experiments/project-v1-model-card.md)
- [DPO停止证据](../docs/post-training/experiments/project-v1-dpo-decision.md)
- [腾讯：混元AI应用开发工程师（数据飞轮）](https://careers.tencent.com/jobdesc.html?postId=2090359354707591168)：应用交付与训练/评测业务理解的岗位实例；不是实习录取标准。
- [阿里：算法工程师-AI Agent](https://campus-talent.alibaba.com/campus/position/199903260012)：SFT/RL/偏好对齐属于该算法岗位要求；该招聘批次不作为用户毕业年份适配的投递建议。
- [百度：AI开放平台研发工程师实习生](https://talent.baidu.com/jobs/detail/INTERN/ef30e579-2d3d-4539-ad90-884521b815c9)：应用后端重视服务化、鉴权、模型调用与工程交付的官方例子。
- [Olist公开数据来源与许可](https://www.kaggle.com/olistbr/brazilian-ecommerce/home/)

岗位来源于2026-10-10检索，具体是否在招和毕业年份资格需投递时再核实。上述少数官方岗位仅说明岗位方向有区别，不能推出所有Agent岗位都要求后训练，也不能推断某公司的统一面试权重。
