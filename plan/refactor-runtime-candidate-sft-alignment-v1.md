---
goal: 复用已准入电商样本，将小模型 SQL-only 训练与实际候选生成输入对齐
version: 1.0
date_created: 2026-10-11
last_updated: 2026-10-11
owner: 李耿楠
status: In progress
tags: [Text-to-SQL, SFT, QueryPlan, runtime, alignment]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In_progress-yellow)

用户在 Day 1 代码学习中提出复用现有数据，改造训练/验证/测试输入后重新验证。本计划落实该请求。当前已完成全量结构回放与共用候选请求入口：对既有 `multi-schema-ecommerce-sql-only-v1-r1` 的 6,000 train / 1,500 validation 执行无模型、无数据库的单轮运行时规则回放，运行时与离线可以调用同一 v2 构造器。发现 Olist 主要有结果列顺序差异，TheLook 还有维度和时间粒度差异，不能直接用新 Prompt 包住旧 Gold 开始训练。

任务卡：目标是同一服务端候选请求在数据构建、matching Base/Adapter 生成及 opt-in 接入时得到一致文本；输入是既有 train/validation、当前 Catalog/Router/WorkingMemory/QueryPlan；输出是版本化构造器、可追踪的新 release、一次 1.5B SFT 和匹配评测。保持指标口径、权限、数据快照和 SQL-only 目标；不引入 Task B、DPO、新指标、全量 Agent 对话训练或默认 provider 切换。先修已发现的输入错配，再小批、全量、smoke、正式训练，逐步审阅。

## 1. Requirements & Constraints

- **REQ-001**: 小模型仍只生成候选 SQL。统一请求由服务端构造，包含当前问题、workspace/version、Catalog、QueryPlan 与 ResultContract；服务端把 SQL 包装为 RunSqlToolArgs，调用既有受控执行链。小模型不负责权限、路由、历史压缩、工具选择、绘图或全流程对话。
- **REQ-002**: 扩展现有 `candidate_sql_generator.py`，保留已冻结的 v1 renderer，新增 `runtime-candidate-sql-v2` 版本。请求中不能出现 QuerySpec、join_program_id、Gold SQL、SQL hash、结果行或任意语义摘要；QuerySpec 只在构建/准入证据中使用。Catalog 的业务字段和合法关联仍可进入模型。
- **REQ-003**: 在线运行时应调用同一候选请求构造器并记录 request/prompt hash；默认在线 API 的完整 Agent 协议仍是工具调用。SQL-only 小模型输出由服务端适配到同一执行工具，无需为了工具调用外壳训练完整 Agent。不能把候选输入共用描述成现有完整 API Agent Prompt 已完全相同。
- **REQ-004**: 仅允许从自然语言问题和真实可用的受控状态推导查询要求。不能从 QuerySpec 把正确维度/时间/指标偷偷填进模型输入。Catalog 检索或路由失败记录为解析失败，不能包装为正常 SQL 监督样本。
- **REQ-005**: 复用既有 6,000/1,500 的语义身份、中文主问法、split、program 与 Gold 来源。确有问题的问法允许依据 train/validation 规格修正歧义，必须记录原问题 hash、修正原因和新 hash，不能只为通过匹配将输入改成结构化答案。配额按实际准入统计，不强凑原数量。
- **REQ-006**: 结果列使用一个稳定规范：维度、时间列（若需要）、按 metric_id 排序的指标列。结构化请求、Gold 顶层投影、结果合同与评测遵循同一规范；仅重排顶层投影时保留父 Gold hash 并记录新 hash，指标公式/过滤/关联不改变。历史文件与历史 ordered 结果不覆盖。
- **REQ-007**: 时间必须表达确定的区间及端点含义。新候选请求采用 start-inclusive / end-exclusive；用户表示含结束日时转换为下一日上界。既有 WorkingMemory/ResultContract 的字段兼容必须明确实现和测试，不能把同一个 end 字符串未经转换就解释为任意端点语义。
- **SEC-001**: 不读取 protected final test 来设计解析规则、改问法、训练或挑 checkpoint。旧两套各 750 条 final test 保持冻结；新实验的 test JSONL 是新的输入视图，不是新的语义题目。只在候选模板与 checkpoint 冻结后构造其推理输入，不把标签/结果提供给模型，并按固定合同做一次 matching Base/Adapter + Gold 对照。
- **SEC-002**: 所有大文件留在仓库外新目录。保留 AST Policy、readonly role、Plan SQL guard、ResultValidator、1000 行执行上限、200 行预览和审计；不通过放宽这些门解决 Gold/input 冲突。
- **CON-001**: 首轮复用 Qwen2.5-Coder-1.5B Base 与 bf16 LoRA，r=16、alpha=32、dropout=0.05、adamw_torch、lr=1e-4、weight_decay=0.01、effective batch=4、2 epoch，重新从原 Base 训练独立 Adapter，不接着旧 Adapter 续训以免混淆变化来源。3072 是长度审计起点，超过则先报告，禁止截断。
- **GUD-001**: matching 的公平性指同一批问题、候选输入、解码与治理/Gold 比较规则一致；不要求 SFT 小模型承担完整对话职责。一次固定场景/结构审计通过不等于语义正确或产品可上线。每一步通过一次相关验证后继续，不重复多层准入。

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: 明确现有输入错配，冻结候选边界。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | 实现 `runtime_alignment_audit.py:replay_single_turn/compare_source_semantics` 和 `scripts/post_training/data/audit_runtime_input_alignment.py`；回放既有 train/validation，记录源文件 hash、状态、指标/维度/时间/结果列差异，不加载 final test。 | 是 | 2026-10-11 |

本项证据：`/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-alignment-20261011-r1/summary.json` 和 `records.jsonl`；定向回归 6 passed，Ruff 通过。该工具不带 grounder、多轮或数据库执行，不是新的共用请求构造器。

| workspace / split | 总数 | 当前结构及列序一致 | 仅列序不一致 | 路由未准入 |
| --- | ---: | ---: | ---: | ---: |
| Olist train | 3000 | 1480 | 1493 | 27 |
| Olist validation | 750 | 338 | 404 | 8 |
| TheLook train | 3000 | 1494 | 500 | 23 |
| TheLook validation | 750 | 132 | 97 | 5 |

TheLook 另外有 train 775 / validation 455 条维度差异，208 / 61 条时间粒度差异，983 / 516 条结果列集合差异；这些类别重叠，不能相加。Olist 本项未发现已准入行的指标、维度、时间数字或结果列集合差异；没有校验端点语义，不能据此说日期过滤完全正确。TheLook 是对现有规则的假设复用，当前仍未接入默认在线页面。上述数字不是模型准确率或全业务语义准入。

### Implementation Phase 2

- **GOAL-002**: 构建运行时和研究脚本共用的服务端候选输入。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-002 | 在 `candidate_sql_generator.py` 新增版本化 CandidateSqlRequest 与纯函数 `build_runtime_candidate_request/render_runtime_candidate_prompt`；接收当前服务端选择、QueryPlan、ResultContract 与问题，排除离线 provenance/答案/摘要，返回稳定 Prompt 和 hash。在 `chat_runtime.py:BudgetedChatHandler.handle_stream` 准入后调用并记录引用，保留默认 API 路径；`replay_single_turn(workspace=...)` 调用同一构造器。后续 opt-in 实际推理仍属独立接入，当前不替换默认模型。 | 是 | 2026-10-11 |
| TASK-003 | 依据 TASK-001 的 train/validation 差异，调整 `QueryPlan` 与 Catalog/Workspace 的维度解析和安全输出基数：使用当前事实域可用维度与别名，不能把 TheLook product_category 变成 Olist product_category_name；补齐中文季度等粒度表达。修复结果列规范、受控绝对时间及端点表达。保留旧冻结离线模板，用明确版本迁移在线字段。 | 否 | — |
| TASK-004 | 回放 TASK-001 同一来源一次；对路由拒绝、歧义、指标/维度/时间不一致保留 exclusion。为实际出现的维度混淆、列序、时间端点补 focused tests，验证新构造器的 runtime/offline 输入字节与 hash 一致；无模型调用测试拒绝/澄清分支不可生成候选。 | 否 | — |

### Implementation Phase 3

- **GOAL-003**: 复用旧语义资产，生成新的可训练版本。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-005 | 新增 `scripts/post_training/data/materialize_runtime_candidate_release.py`，先取 train/validation 的 32 条分层小批，经真实规则重建 QueryPlan 与候选输入；独立比较原 QuerySpec，Gold 保留公式并规范顶层列序。核对时间端点、Gold依赖的表列与实际Catalog；运行必要的小批 Policy/reader/ResultContract/Gold 回归。错误不进入训练标签。 | 否 | — |
| TASK-006 | 小批通过后写新外部 `runtime-candidate-sql-v2-r1/train.jsonl`、`validation.jsonl`、manifest、长度统计与 exclusions，报告 workspace/family/program/风险覆盖及实际减少量。保持旧 split，重算所有输入/目标/父文件 hash。重新使用统一构造器生成 Prompt，不给模型 QuerySpec；不改写旧 6,000/1,500 文件。 | 否 | — |

### Implementation Phase 4

- **GOAL-004**: 验证输入对齐是否改善 SQL 候选质量，完成一次有边界的实验。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-007 | 在 `run_post_training_sft_smoke.py` 新增 v2 release 审计分支，验证输入版本、标签 mask、hash/split 与未截断长度；先最小 GPU smoke 后在空闲 3090/4090 上 screen 启动 1.5B 完整 LoRA，记录 logical/physical/UUID 与日志。复用验证损失选择 adapter_best，不能把历史不同 split/Prompt 的 loss 直接比较为改善。 | 否 | — |
| TASK-008 | 对冻结验证期 adapter_best，用 TASK-002 同一构造器物化独立 test 输入视图和 matching Base/Adapter 生成，源问题来自原 protected holdout，仅用于此冻结实验的最终评测。非准入、缺失上下文或超过长度记录失败并留在总分母，禁止根据 test 再修解析器。生成输出冻结后再执行 Gold；按预定列序协议保留原 Gold 证据与规范化对照，输出协议/合同/denotation 指标。 | 否 | — |
| TASK-009 | 汇总 v2 的实际训练量、validation-best、两 workspace 结果、解析失败与候选 SQL 失败。相对旧实验的比较标明 Prompt/目标列序变化，不用差值证明单独因果。另在独立产品开发用例验证 opt-in 请求→SQL→可信执行链，实际接入失败不能用离线成绩替代；更新 PROJECT/飞书事实。 | 否 | — |

## 3. Alternatives

- **ALT-001**: 把 QuerySpec 字段改名为 QueryPlan。拒绝该做法：语义由离线规格注入，绕过真实问题解析，并不能测线上输入适配。
- **ALT-002**: 把完整历史、图表与工具调用一起作为新 SFT 目标。本轮不选：改变组件职责和标签任务，现有 SQL-only 样本不足以监督完整 Agent 生命周期。
- **ALT-003**: 只测试旧 Adapter 在新候选 Prompt 上的表现。可作为 validation 诊断，但不能代替用户要求的重建数据与新 SFT，也不能在 final test 上反复挑 Prompt。

## 4. Dependencies

- **DEP-001**: 冻结来源 `multi-schema-ecommerce-sql-only-v1-r1`；train SHA-256 `e5790e8f6d67321a37e1d031e7088718b6b9a292b71a73d84b5a44917a8616df`，validation `8793b18689eca6ca83530988be4bd227f4193ccd06d9f3aa4a45084f78646329`，本轮回放与来源 manifest 一致。
- **DEP-002**: Olist v3 / TheLook v2 Catalog、只读 PostgreSQL 与原 Gold renderer。TheLook 使用独立 workspace，不启用默认在线访问。
- **DEP-003**: 本地固定 revision 的 Qwen2.5-Coder-1.5B Base/tokenizer、已有 bf16 LoRA Trainer 与 runtime conda 环境。GPU 只在 TASK-007 前检查。

## 5. Files

- **FILE-001**: `src/data_analysis_agent/runtime_alignment_audit.py`、`scripts/post_training/data/audit_runtime_input_alignment.py`、`tests/test_runtime_alignment_audit.py`：当前只读诊断与回归。
- **FILE-002**: `candidate_sql_generator.py`、`chat_runtime.py`、`semantic_catalog.py`、`query_plan.py`、`working_memory.py`、`workspace.py`：按 TASK-002/003 的必要合同变化修改，不做目录重构。
- **FILE-003**: 新物化器与 `run_post_training_sft_smoke.py`：新 release 与训练验收；历史训练/评测脚本保留可回查版本，不能先删除破坏旧实验。
- **FILE-004**: `docs/learning/agent-project-walkthrough-v1.md`、`PROJECT.md`、`docs/README.md`、飞书项目文档：学习及事实分别记录。

## 6. Testing

- **TEST-001**: 当前已通过 6 条 focused tests：问题派生输入不受 source Spec 影响、定义/越权/相对时间不能生成 Prompt、缺失时间和列序错配被识别。
- **TEST-002**: 候选构造器使用同一参数在 runtime 与离线生成相同文本/hash；禁止离线 provenance、Gold、结果或摘要注入；测试不能靠手工注入正确 Spec 制造一致性。已验证此边界，以及版本/指标/列合同漂移拒绝、路由拒绝、客户端 hash 被服务端覆盖；两组定向测试共 119 passed、7 skipped（数据库集成未启用），Ruff 通过。此证据不证明日期端点或 Gold 标签已对齐。
- **TEST-003**: 单轮绝对日期、含/不含结束日、单年、季度、Olist/TheLook 重名维度和多指标列序的必要回归；测试验证业务要求而非当前生成代码的固定字符串。
- **TEST-004**: 小批 read-only 执行与标签 mask/长度/hash审计，随后最小 GPU smoke。通过之后只做一次正式训练和匹配 final test，不用终测调整配置。

## 7. Risks & Assumptions

- **RISK-001**: 当前结构一致统计没有检查日期端点、Catalog 全部依赖或完整 Gold 语义；不能将 3444 条总结构一致直接标为正式训练准入。
- **RISK-002**: TheLook 的 train/validation 分布差异导致修复后数量和覆盖不一定保持原值；少量不支持请求可 exclusion，大面积排除必须先修共用解析能力并报告。
- **RISK-003**: 新模型效果可能不提高；对齐解决输入契约问题，并不保证 1.5B 的推理能力提升。
- **ASSUMPTION-001**: 用户这次授权针对 1.5B SQL-only 小组件继续改造和实验；不扩大到同时训练 2B/4B、DPO 或全量 Agent 模型。

## 8. Related Specifications / Further Reading

- [`四周产品与学习路线`](process-agent-internship-improvement-v1.md)：当前 Day 1 在用户提出本对齐任务后插入本计划，完整链路复述尚未完成。
- [`当前主模型事实`](../docs/post-training/experiments/project-v1-model-baseline.md)
- [`原多 Schema 数据合同`](../docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md)
- [`运行时请求链`](../docs/architecture/product-request-architecture-v1.md)
