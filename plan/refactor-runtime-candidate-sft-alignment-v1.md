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

用户在 Day 1 代码学习中提出复用现有数据，改造训练/验证/测试输入后重新验证。本计划落实该请求。当前已完成共用候选请求入口、运行时合同修复、原 6,000 train / 1,500 validation 全量回放、32 条小批准入，以及新的 runtime-candidate-sql-v2-r1 正式数据 release。全量准入 5,980 train / 1,500 validation，20 条 TheLook 品牌分组因保守执行上界排除；全部 7,480 条通过只读执行与父 Gold 结果对照。最长 3,592 token，正式长度冻结为 4,096，未截断。Trainer v2 兼容、全量 CPU 标签/长度审计与最小 GPU smoke 已通过，实际 batch=2、累计=2、有效 batch=4；完整两 epoch 于 2026-10-11 22:17:59（Asia/Shanghai）正常结束。2,990 步训练验收通过，step 2,500 的 validation-best 已冻结，best/final checkpoint 和独立重新加载均核对通过。v2 matching 入口已实现，两条 validation GPU 生成、只读执行和 Gold smoke 通过，正式评测合同已冻结；尚未物化或运行 protected final 输入。

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
- **CON-001**: 首轮复用 Qwen2.5-Coder-1.5B Base 与 bf16 LoRA，r=16、alpha=32、dropout=0.05、adamw_torch、lr=1e-4、weight_decay=0.01、effective batch=4、2 epoch，重新从原 Base 训练独立 Adapter，不接着旧 Adapter 续训以免混淆变化来源。3,072 是初始长度审计起点；全量数据最长 3,592，TASK-006 已冻结正式上限 4,096，禁止截断。实际 batch/梯度累计由该长度的 GPU smoke 决定，不能提前宣称显存适配。
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
| TASK-003 | 依据 TASK-001 的 train/validation 差异，调整 `QueryPlan` 与 Catalog/Workspace 的维度解析和安全输出基数：使用当前事实域可用维度与别名，不能把 TheLook product_category 变成 Olist product_category_name；补齐中文季度等粒度表达。修复结果列规范、受控绝对时间及端点表达。保留旧冻结离线模板，用明确版本迁移在线字段。 | 是 | 2026-10-11 |
| TASK-004 | 回放 TASK-001 同一来源；在 safe records 留下路由拒绝、歧义、指标/维度/时间不一致，作为物化时修正/排除依据。为实际出现的维度混淆、列序、时间端点补 focused tests，验证新构造器的 runtime/offline 输入字节与 hash 一致；无模型调用测试拒绝/澄清分支不可生成候选。 | 是 | 2026-10-11 |

TASK-003/004 的最终证据：仓库外 `evals/runtime-candidate-alignment-20261011-fixed-r2/{summary.json,records.jsonl}`，源 train/validation SHA-256 与 DEP-001 一致。`fixed-r1` 是中途诊断，保留但不作为最终统计。新审计使用线上 1,000 行执行上限；先前回放误用 Router 的旧 200 行默认值。日期比较新增端点语义，因此下面的“完整结构一致”不能直接与最初宽松统计比较为提升。

| workspace / split | 总数 | 完整结构及列序一致 | 忽略列序后的结构一致 | 路由未准入 | 时间端点差异 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Olist train | 3000 | 69 | 71 | 27 | 2902 |
| Olist validation | 750 | 13 | 14 | 8 | 728 |
| TheLook train | 3000 | 1620 | 2857 | 143 | 0 |
| TheLook validation | 750 | 460 | 714 | 36 | 0 |

TheLook 已准入行的维度、粒度、列集合差异均为 0；列序差异 train 1,237 / validation 254。非准入为缺粒度 123/36，加 train 20 条保守结果上界超过 1,000 的商品品牌分组。Olist 结果列序差异 2,026/545；非准入为没有明确查询动作 24/4、被判为定义问题 3/4。这些差异计数重叠，不能相加为失败总数；完整 Gold 语义/表列可见性仍在 TASK-005 验证。

运行时新增 `DimensionBinding`，将 TheLook 13 个逻辑维度绑定真实安全列；Catalog 检索及 Prompt 显示同一映射。结果基数及五个事实时间字段的每日未过滤行数由冻结数据库只读聚合导出，保存于 `data/catalog/thelook_runtime_shape_bounds_v1.json`，用于保守预检，不进入模型输入。脚本为 `freeze_thelook_runtime_shape_bounds.py`，不读取问题、标签或 holdout。当前 TheLook 仍仅用于离线 workspace，未成为默认在线数据源。

新计划标记为 `query-plan-v2`，采用“维度、时间、按 metric_id 排序的指标”列序。`query_time.py` 统一问题解析和 WorkingMemory，显式排除结束日时保留上界，否则日期范围按包含结束日转成下一日上界；单年变为下一年 1 月 1 日上界。新增 ResultContract 标记 `requested_end_exclusive`，Runner 传入 Validator；旧未标记端点状态保持原值，继承查询时要求重新确认，不静默改含义。构造器拒绝计划/合同端点不一致或未确认的范围；旧离线评测保留原默认比较行为。

相关回归 250 passed、7 skipped（PostgreSQL 集成未启用）。覆盖 13 维度及注册州别名、重名维度事实域、真实列可见性、unsupported 维度澄清、短窗口基数、周一分桶边界、年/闰日/非法日期、旧状态兼容、季度措辞、真实比较基线、列序和候选端点一致性；无 LLM/GPU/训练，未读取 final test。

### Implementation Phase 3

- **GOAL-003**: 复用旧语义资产，生成新的可训练版本。

TASK-005 小批任务卡：按 Olist/TheLook × train/validation 各取 8 条，共 32 条，在每组覆盖现有结果形态，并优先补充不同指标、维度、粒度、风险与问法修正类型。确定性选择只读取既有 train/validation。输出仓库外小批 JSONL、修正与父 hash、准入/排除证据及 manifest；逐条执行原 Gold 和仅顶层重排列后的 Gold，比较规范列序下的 denotation。不更换指标公式、过滤条件、关联或 split，不启动完整物化、训练或终测。若出现实际 Catalog 缺列/跨层合同冲突，先修通用合同并重跑这个小批，不把来源 Spec 填入模型输入。

TASK-006 全量任务卡：扩展同一入口的 release 模式，输入固定的 6,000 train / 1,500 validation 与已通过的小批证据；逐条执行相同问题修正、运行时重建、Catalog/计划/Policy/只读执行/结果对照。使用最多 4 个独立只读任务并发，screen 持久记录日志；来源或版本漂移立即停批，普通不支持样本留脱敏排除原因。输出新仓库外 train/validation、逐条证据、source/output hash、split/重复审计、workspace/family/program/风险/八问法覆盖和完整 tokenizer 分布。3,072、4,096 等长度仅作候选统计，按实际全量结果选择无截断上限后冻结 release；不读 final test、不训练、不改默认 provider。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-005 | 新增 `scripts/post_training/data/materialize_runtime_candidate_release.py`，先取 train/validation 的 32 条分层小批。补清有来源证据的结束日/粒度或查询动作歧义，记录原问题 hash、修正原因、新 hash，再经真实规则重建 QueryPlan 与候选输入；独立比较原 QuerySpec，Gold 保留公式并规范顶层列序。核对时间端点、Gold 依赖的表列与实际 Catalog；运行小批 Policy/reader/ResultContract/Gold 回归。执行预算或语义冲突的行排除；输入超长记录待定长度状态，不截断、不发布为正式训练集。 | 是 | 2026-10-11 |
| TASK-006 | 小批通过后写新外部 `runtime-candidate-sql-v2-r1/train.jsonl`、`validation.jsonl`、manifest、长度统计与 exclusions，报告 workspace/family/program/风险覆盖及实际减少量。保持旧 split，重算所有输入/目标/父文件 hash。重新使用统一构造器生成 Prompt，不给模型 QuerySpec；不改写旧 6,000/1,500 文件。 | 是 | 2026-10-11 |

TASK-005 最终证据在仓库外 `evals/runtime-candidate-pilot-20261011-r3/`：Olist/TheLook × train/validation 各 8 条，32/32 准入，0 排除；小批 train/validation 各 16 条，仅用于工程审阅。选择清单 SHA-256 为 `e00163e69d5896a3ce9f52d374c3ed4756ea2b34bdc700a6059b217c075c30c1`，执行前冻结，失败时不换样本。覆盖 35 个不同指标、5 种结果形态、8 个逻辑维度，以及日/周/月/季度/年全部粒度；小批不代表完整来源覆盖。

16 条 Olist 问题补明“含开始日、不含结束日”，2 条 TheLook 问题补明来源规定的时间粒度；修改问题之后再从问题独立推导 QueryPlan，没有从 Spec 给计划补字段。`runtime_candidate_materializer.py` 检查父 Gold hash、workspace 版本、运行时语义和实际检索片段中的表/列/Join；标签仅规范顶层投影，保留父/新 SQL hash 与问题、输入 hash。`review-preview.md` 可直接查看原/新问题、QueryPlan、完整输入及 SQL 标签；Spec、provenance 与执行证据不进入模型输入。

原/新 Gold 在同一 REPEATABLE READ 只读事务中执行，分别使用 `daa_analytics_reader` 与 `daa_thelook_reader`，通过计划 Guard、SqlPolicy、1,000 行执行预算和真实 ResultContract/ResultValidator；32 条结果状态均为 valid，按既有数值容差协议比较均为 ordered_denotation_match。结果 hash 用于溯源，不以字节相等代替数值 denotation 判断。发现并修正两处通用问题：v2 计划只使用声明的 top_n，不再误继承 Olist 旧城市 Top20；Catalog 可用翻译 Join 不证明 SQL 返回英文品类，标签改为中性“商品品类”。无版本的历史 Guard 保留原规则。

本地固定 tokenizer 加 EOS 长度为 1,410–3,463，23 条不超过 3,072，9 条标记 requires_length_decision；manifest 标记 not_a_formal_training_release。全量长度审计后再冻结上限，4,096 仅是候选，不提前作为正式配置。相关回归 218 passed，定向 Ruff、compileall 通过；本轮没有读取 final test、调用 LLM/GPU 或启动训练。r1/r2 保留为诊断中间产物，以 r3 为最终小批证据。

TASK-006 已冻结正式 release：`/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-sql-v2-r1/`。4 路独立只读任务在 `daa-runtime-release-20261011` screen 执行，日志为外部 `evals/runtime-candidate-materialization-20261011-r1.log`。先写 staging，源文件/运行时代码/Catalog hash、重复与 split 审计通过之后一次发布正式目录；原 source 与 pilot 文件不改写。

| workspace / split | 来源 | 正式准入 | 排除 | 场景类别 | 独立切分 family / program | 原形态覆盖 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Olist train | 3000 | 3000 | 0 | 455 | 455 family；11 program 类别 | scalar 792；州分组 680；品类 17；时序 1511 |
| Olist validation | 750 | 750 | 0 | 113 | 113 family；10 program 类别 | scalar 183；州分组 142；品类 8；时序 417 |
| TheLook train | 3000 | 2980 | 20 | 16 | 596 program | scalar 1040；维度分组 955；时序 985 |
| TheLook validation | 750 | 750 | 0 | 5 | 150 program | scalar 5；维度分组 455；时序 290 |

20 条全部为 `runtime_route:result_row_budget_exceeded`，对应原 4 个 TheLook 品牌分组 program × 5 个窗口，保守基数门拒绝；不修改日期、不换样本填数、不放宽预算。相对原来源没有丢失指标、场景类别或时间粒度。Olist 两组保留 19 项指标；TheLook train 保留原 18 项、validation 保留原 3 项（event_count、registered_user_count、unique_session_count），验证覆盖偏向用户/事件事实域，不能描述为全面均衡。此次迁移保持原 split，不重新分配验证集。

3,824 条问题有修改：3,630 次补清排除结束日、159 次补清粒度、35 次明确数据查询动作；原因可重叠。保持一条样本唯一主问法和原 v1–v8 身份，train 各类 744–750 条、validation 各类 186–188 条；未新增问法变体或把变体算作新样本。Olist 保留原 49 条 train / 16 条 validation 的空 risk_tags，并在 coverage-audit 显式报告；此前 DPO collector 的 simple-scalar 补标签仅作用于其选择 metadata，未覆盖冻结 source。这些标签不进入 SQL-only 模型输入，当前不能主张全部风险元数据完备。

所有 7,480 条准入样本的结果状态均 valid，与父 Gold 按既有容差全部 ordered_denotation_match。训练长度 1,265–3,538（p95=3,300），验证 1,165–3,592（p95=3,339）；660 train + 202 validation 超过旧 3,072，但均小于 4,096，因此正式上限为 4,096、零截断、零长度排除。后续 Trainer 必须读 manifest 的上限，不能再沿用旧默认值。

来源与输出均无样本/QuerySpec/Gold/规范化问题/Prompt 重复或跨 split 交集；Olist 切分按原 family，TheLook 按 program，独立 split_family_id 零交集。TheLook 有一个共享 scenario 类别，这是覆盖标签，不是切分单位。正式 train SHA-256 `372254d553248ce7f5068b966f3ee99e805d9b78b0e0ed9d4cc1bab482b10b31`，validation `161e02be0e2b2c12cae4cf1e99d990a6b4370370e82b174b00e2e2622e7cc4a6`，manifest `5a850f34cbc2e3dd36d770ea9203e0cf7e1cda0ef4b7d73351c4b47138ff904d`。独立逐条回读重算 7,480 条 token 长度、输入/标签与父记录/hash，验收为 pass，报告在外部 `evals/runtime-candidate-release-verification-20261011-r1.json`。相关回归 230 passed；未读取 final test、调用 LLM/GPU 或启动训练。

### Implementation Phase 4

- **GOAL-004**: 验证输入对齐是否改善 SQL 候选质量，完成一次有边界的实验。

TASK-007 当前小单元任务卡：输入是已冻结的 `runtime-candidate-sql-v2-r1` 全量 train/validation 与本地固定 Base；输出是 Trainer 的 v2 manifest 校验、推理一致的标签边界、可复现的有界 smoke 与 GPU 证据。先校验全部来源 hash/长度，再从两个 workspace 各取最长样本，使用 8 train / 4 validation、2 次 optimizer step、每步 eval/save；不在 smoke 中评估全量 1,500 条。新 Prompt 精确停在 `### SQL`，其后的分隔换行归属 completion，与 SQL/EOS 一起监督；Prompt 与 padding 均不参与 loss。保留旧协议标签边界。本小单元不启动长训练、不读取 final test；以有限 loss、LoRA 实际更新、冻结 Base、未量化 AdamW、best/final 保存与重新加载成功为验收。完整训练仍按 CON-001，实际并行 batch 在 smoke 后确定。

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-007 | 在 `run_post_training_sft_smoke.py` 新增 v2 release 审计分支，验证输入版本、标签 mask、hash/split 与未截断长度；先最小 GPU smoke 后在空闲 3090/4090 上 screen 启动 1.5B 完整 LoRA，记录 logical/physical/UUID 与日志。复用验证损失选择 adapter_best，不能把历史不同 split/Prompt 的 loss 直接比较为改善。 | 是 | 2026-10-11 |
| TASK-008 | 对冻结验证期 adapter_best，用 TASK-002 同一构造器物化独立 test 输入视图和 matching Base/Adapter 生成，源问题来自原 protected holdout，仅用于此冻结实验的最终评测。非准入、缺失上下文或超过长度记录失败并留在总分母，禁止根据 test 再修解析器。生成输出冻结后再执行 Gold；按预定列序协议保留原 Gold 证据与规范化对照，输出协议/合同/denotation 指标。入口及 validation smoke 已完成，正式终测待运行。 | 否 | 2026-10-11（入口） |
| TASK-009 | 汇总 v2 的实际训练量、validation-best、两 workspace 结果、解析失败与候选 SQL 失败。相对旧实验的比较标明 Prompt/目标列序变化，不用差值证明单独因果。另在独立产品开发用例验证 opt-in 请求→SQL→可信执行链，实际接入失败不能用离线成绩替代；更新 PROJECT/飞书事实。 | 否 | — |

TASK-007 前半段已通过：Trainer 校验固定 manifest SHA-256、train/validation 文件与行数、逐条准入、运行时资产 hash、4,096 长度与 tokenizer；全部 7,480 行回算 token 和 Prompt/completion 边界。v2 的分隔换行进入 completion 监督，保留历史格式的 mask。新增 `--smoke-samples-per-workspace`，全量验证之后再按 workspace 选最长 8 train / 4 validation；该模式只允许 1–4 step，不允许 epoch 模式。末步额外 eval/save 保证完整训练的最后一步能参与 validation-best 选择。

最小 smoke 于 2026-10-11 在逻辑 CUDA 1 / 物理 GPU 3（RTX 4090，UUID `GPU-10863af0-8588-7625-5609-640ba794f64b`）通过；入口是 `scripts/post_training/launchers/start_runtime_candidate_sft_smoke_screen.sh`，screen `daa-runtime-smoke-b2-20261011` 已正常退出。外部 `experiments/runtime-candidate-sft-smoke-20261011-b2-r1/sft_smoke.json` SHA-256 `79854c528ed8ffad6d34431f17228e2b428c10217bec0a35bfe58d418bb5fee2`，日志在外部 `evals/runtime-candidate-sft-smoke-20261011-b2-r1.log`。实际优化/评估 12.6 秒，含全量 CPU 检查、加载/回读总约 2 分钟；两次更新平均 train loss 0.215921，4 条 smoke validation loss 0.150052。392 个 LoRA 张量改变，Base 冻结，AdamW m/v 是 fp32；best 在 step 2，其权重逐张量匹配 checkpoint-2，重新加载后单条 loss 0.137117 有限。峰值 allocated/reserved 为 19,212,910,592 / 24,668,798,976 字节（17.89 / 22.98 GiB）。这是流程与最长实际样本显存验证，不是全量验证准确率或 SQL 质量提升；4,096 是接纳上限，此次实际最长 3,592，未测试人工构造的恰好 4,096 序列。

正式训练配置冻结：从原 `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` 新训练；bf16 Base，不量化权重或 optimizer state；LoRA r16/alpha32/dropout0.05，覆盖七类 attention/MLP 投影；AdamW lr=1e-4、constant scheduler、weight_decay=0.01；seed=20261011；train/eval micro-batch=2、累积=2、有效 batch=4、2 epoch、max_seq_length=4096、动态右 padding、non-reentrant gradient checkpointing。每 250 optimizer step 对完整 1,500 validation eval/save，末步也 eval/save；保留 2 个 checkpoint，按最小 eval_loss 恢复并单独保存 adapter_best，adapter_final 只作诊断；不在训练前评估完整 validation、不使用 smoke adapter 续训。源 train 5,980 行整除有效 batch，单卡每 epoch 1,495 optimizer step、两 epoch 2,990。实际选择只比较预先指定验证点，不读取 final test；原 TheLook validation 三指标偏斜保留为解释限制。

smoke 单元定向 Trainer/历史输入回归 42 passed，共用候选/物化回归 28 passed，Ruff、compileall、bash -n、diff check 通过；当时完整训练与 matching 尚未运行。后续训练启动与结束验收见以下记录。

TASK-007 正式启动任务卡：复用上一小单元的已冻结参数和通过的 smoke，从原 Base 在独立 screen 启动完整两 epoch。新增明确的正式 launcher，启动前校验 release/smoke/model manifest hash 和 GPU 空闲状态；输出仓库外 launch manifest、日志、checkpoint 与退出状态。保持 batch2×累计2、1,500 条完整 validation、250 步 eval/save、末步额外 eval/save、最小 eval_loss 选择 best；不更换数据/参数、不读 final test、不续训 smoke、不自动启动生成终测。启动验收至少观察实际 optimizer 更新、有限 loss/grad_norm 与首个 checkpoint 的验证/保存，关闭 SSH 不影响 screen。训练结束后的质量分析另属下一小单元。

正式启动验收已通过：2026-10-11 20:05（Asia/Shanghai）在逻辑 CUDA 1 / 物理 GPU 3（RTX 4090，UUID 同 smoke）启动 `daa-runtime-sft-20261011-r1`，代码基线 `ad5179a`。正式 launcher 为 `scripts/post_training/launchers/start_runtime_candidate_sft_training_screen.sh`，其真实 argv 已经由 Trainer parser 回读，与冻结配置逐项核对通过；重复启动尝试返回 exit 2，未产生第二个训练进程。首个 step 250 对完整 1,500 validation 的 eval_loss 为 `0.0004893963341601193`，评估耗时 159.6 秒；checkpoint 保存后训练继续。只读回读确认 LoRA r/alpha/dropout、2,990 总步数、AdamW lr1e-4/weight_decay0.01、fp32 m/v 以及 optimizer/scheduler/RNG/trainer_state 齐全；首点 best 仍是临时训练状态，不是最终模型选择或 SQL 语义成绩。

仓库外证据：运行目录 `experiments/runtime-candidate-qwen25coder15b-bf16-lora-2epoch-20261011-r1/`；完整日志 `evals/runtime-candidate-qwen25coder15b-sft-20261011-r1.log`；启动 argv/code hash 为同名前缀的 `-launch.json`；首 checkpoint hash/状态回读为 `-first-checkpoint-audit.json`；结束时 launcher 写 `-exit.json`，Trainer 写运行目录下 `sft_smoke.json` 和 `adapter_best/adapter_final`。以上外部根目录为 `/disk2/gengnan/data-analysis-agent-data/`。这次仅启动并确认运行正常；完整训练尚未结束，未自动终测。按首阶段速度粗估整轮约两小时，实际耗时以日志为准。

TASK-007 训练结束验收任务卡：输入是本轮固定 run、launch/exit、完整 Trainer state、release manifest 和 best/final checkpoint；输出是仓库外完成审计、validation 曲线、best 权重 hash 与独立重新加载证据，以及本地/飞书实验结论。检查两 epoch/2,990 步、预定验证点、最小验证损失选择、best/final 权重对应关系、有限参数/梯度、未量化 optimizer state 和来源 hash。用同一 `CausalSqlDataset` 在两个 workspace 的最长 validation 样本上 fresh reload，不重跑全量 validation、不读取 final test、不改超参、不自动启动终测。验收通过才冻结 step 2,500 的 Adapter 供下一项 v2 matching 使用；旧 QuerySpec 模板的评测入口不能直接套用新 Adapter。

训练结束验收已通过：22:17:59 正常退出，完成两 epoch / 2,990 步；12 个预选 validation 点中 step 2,500 的 eval_loss `0.000019297187463962473` 最低，恢复 best 后的完整复验与之相同。step 2,990 的 final eval_loss 为 `0.000027163852792000398`，与恢复 best 后同 step 标记的复验日志分开。best/final 各自精确匹配 checkpoint-2500/2990，两者 392 个 LoRA 张量均不同且有限；两个 checkpoint 的续训状态齐全，AdamW m/v 均 fp32。固定 source/runtime/Base 文件 hash 未漂移。独立进程用共享 Dataset 在两个 workspace 最长 validation 样本上 fresh reload，通过权重、标签/EOS 和有限 loss 检查。

best 权重 SHA-256 `926d16e4218fb9f83dc99cb1ef79094dac90adc397e3c5e0ad9526c7eb834c9e`。外部同一 eval 前缀下新增 `-completion-audit.json`、`-best-checkpoint-freeze.json`、`-validation-curve.csv/.html` 和可回读审计脚本。完整结果与局限见 [`训练结束验收`](../docs/post-training/experiments/runtime-candidate-sft-v2-r1.md)。TASK-007 完成，TASK-008 尚未开始；本轮没有 SQL 生成/终测，低 loss 不能作为项目模型改善成绩。旧 QuerySpec matching 模板与旧 reload encoder 都不能直接用于 v2，本次独立 reload 已复用实际 Trainer 的 Dataset；下一项只实现 v2 评测入口与冻结共同解码规则。

TASK-008 入口实现任务卡：本小单元冻结 v2 matching 合同，实现统一的 freeze/prepare/generate/evaluate 入口，并用合成样本和 train/validation smoke 验证完整链路。输入为 step 2,500 best 冻结清单、固定 Base 和两个原 final 来源；生成侧仅投影 sample ID 与原问题，经 shared replay/builder 重建候选输入，不读取 Spec/Gold 修正问题。输出为仓库外合同、输入视图、每个模型完整候选记录、完成 hash 和统一治理/Gold 报告。解码固定 bf16、greedy、beam1、seed20261011、输入4096/输出1024，无修复重试；拒绝/超长/空输出/长度停止均留在总分母。Gold 在两个生成文件封存且身份/hash 对齐后才读取，使用原标签合同执行；规范列序仅重排最终投影，时间别名只按预存 ResultContract 规范化 DataFrame，不改 SQL 语义。另报 runtime/source Spec 差异，不把旧问题歧义误称模型错误。验收覆盖泄漏、版本漂移、两模型输入一致、缺失/重复记录、失败计数、安全/只读/合同/结果对照；本小单元不启动完整 protected 终测，不更换 best，不修改产品解析器或默认 provider。

TASK-008 入口验收通过：新增 `runtime_candidate_matching.py` 与统一 CLI `run_runtime_candidate_matching.py`。新入口 30 项定向测试通过，另有 70 项共用候选、清洗、Gold 比较、Policy、Plan Guard 和 ResultValidator 回归通过；Ruff、compileall、diff check 通过。最终版本 smoke 使用 validation 中两个 workspace 各一条，Base/Adapter 同一输入 hash。物理 GPU 0/1 的两张 RTX 3090 上分别生成，Adapter 2/2 EOS、valid、ordered Gold 匹配；Base 为一条长度停止、一条 PostgreSQL 字段歧义。该两条只验证工程链路，不替代正式准确率。

最终 smoke 证据为仓库外 `evals/runtime-candidate-v2-matching-validation-smoke-20261011-r3/`；正式合同为 `evals/runtime-candidate-v2-matching-final-20261011-r1/contract.json`，SHA-256 `f68fdeda3aff58e492f50a9935b23b7e52cb12550c8f6b7193e6709ec1ba97a3`。冻结时只计算两个 final 来源的文件 hash，没有读取问题/Spec/Gold 字段或物化正式输入。完整规则及代码入口见 [`v2 matching 合同`](../docs/post-training/data/runtime-candidate-v2-matching-contract-v1.md)。下一小单元物化原两套各 750 条输入、screen 生成 Base/Adapter，再运行后置统一治理与 Gold 对照；不以 smoke 结果修改正式解码。

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
- **TEST-003**: 单轮绝对日期、含/不含结束日、单年、季度、Olist/TheLook 重名维度和多指标列序回归已完成；当前一组相关回归 250 passed、7 skipped。新的端点合同并不让旧 Olist 问题自动符合 Gold，需要下一阶段修正 train/validation 问法。
- **TEST-004**: 32 条小批 read-only 执行、Catalog 依赖、问题/SQL/hash、未截断长度及原/新 Gold 对照已通过；相关回归 218 passed，包含源文件不变、保护 test split、显式含结束日冲突拒绝、源 Spec 不可注入维度、顶层投影外 AST 不变及分层选择可复现。Trainer 标签 mask 与 GPU smoke 在正式 release 完成后验证，尚未运行。随后只做一次正式训练和匹配 final test，不用终测调整配置。

## 7. Risks & Assumptions

- **RISK-001**: 初始结构统计未检查日期端点。修复后的回放比较了显式端点，但仍未检查 Catalog 全部依赖或完整 Gold 语义；任何结构一致数量都不能直接标为正式训练准入。
- **RISK-002**: TheLook 的 train/validation 分布差异导致修复后数量和覆盖不一定保持原值；少量不支持请求可 exclusion，大面积排除必须先修共用解析能力并报告。
- **RISK-003**: 新模型效果可能不提高；对齐解决输入契约问题，并不保证 1.5B 的推理能力提升。
- **ASSUMPTION-001**: 用户这次授权针对 1.5B SQL-only 小组件继续改造和实验；不扩大到同时训练 2B/4B、DPO 或全量 Agent 模型。

## 8. Related Specifications / Further Reading

- [`四周产品与学习路线`](process-agent-internship-improvement-v1.md)：当前 Day 1 在用户提出本对齐任务后插入本计划，完整链路复述尚未完成。
- [`当前主模型事实`](../docs/post-training/experiments/project-v1-model-baseline.md)
- [`原多 Schema 数据合同`](../docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md)
- [`运行时请求链`](../docs/architecture/product-request-architecture-v1.md)
