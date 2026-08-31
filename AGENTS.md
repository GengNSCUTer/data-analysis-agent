# Data Analysis Agent — 协作与迭代规则

本文件约束在本仓库内执行的所有后续工作。若用户当前指令与本文件冲突，以用户最新指令为准；否则必须遵守以下流程。

## 1. 项目事实来源

- `PROJECT.md` 是需求、架构、数据策略、技术决策、阶段路线和变更记录的本地基线；
- `docs/README.md` 是本地文档总导航，说明规范文档、历史证据和兼容入口的职责；不要把同一事实复制到多个“最新”文档中；
- 飞书项目文档（<https://my.feishu.cn/docx/QIr2dfKp7oIJvqxcPerckYd6nfC>）是用户可直接查看的同步项目记录；
- 飞书《数据分析 Agent｜微调与后训练学习笔记》（<https://my.feishu.cn/docx/Lx34diMehoPns6xcXP9cb8aLnmh>）是后训练学习内容的同步入口，必须将项目进度与学习材料分开维护；
- GitHub 仓库 `GengNSCUTer/data-analysis-agent` 是项目代码、可复现脚本、文档和提交历史的唯一远端；
- Vanna 已按当前项目决策合并进本仓库，仓库同时保留 `upstream` 远端用于跟踪上游；不要再创建第二个自有 Vanna 仓库。项目不再建设独立 Next.js/TailAdmin 前端，`<vanna-chat>` 是嵌入既有业务页的唯一交互基座。

## 2. 每次迭代的强制闭环

每次完成一个有意义的迭代（需求变化、设计决策、功能、测试、数据管道、Bug 修复、发布准备）时，按下列顺序执行：

1. **开始前记录**：读取 `PROJECT.md` 与飞书项目文档的相关部分，明确本轮目标、范围、验收条件和不做项。若需求或架构发生变化，先更新设计记录再编码。
2. **实施与验证**：只实现本轮范围内的内容；运行与变更相称的格式化、静态检查、单元/集成/E2E 测试，并记录结果。
3. **本地项目文档更新**：更新 `PROJECT.md` 的需求、设计、路线图、决策或变更记录；必要时新增 ADR、评测报告或数据字典。不要只改代码不留决策依据。
4. **飞书同步**：在飞书项目文档追加本轮迭代记录，至少包含日期、目标、实现内容、设计决策、验证结果、风险/限制和下一步。重大架构变更须同步正文对应章节，而不只写日志。
5. **Git 更新**：检查 `git diff` 与 `git status`；只提交本轮相关文件。提交信息使用清晰的 Conventional Commit 风格，例如 `feat(sql-policy): add AST allowlist validation`。
6. **远端推送**：在确认提交内容不含密钥、原始受限数据、缓存和构建产物后，推送到 `origin`。若推送失败，保留本地提交并在飞书迭代记录中注明状态，不能假称已同步。
7. **交付汇报**：说明完成内容、验证证据、飞书文档链接/更新位置、Git commit SHA、是否已推送，以及仍存在的限制。

小型纯讨论不必产生 Git commit；一旦形成实际项目决策，必须更新 `PROJECT.md` 和飞书记录。

### AI 协作与工程主导权

- AI 是调研、实现、测试、代码审查和文档整理的加速器，不是项目需求、架构取舍、数据边界、安全边界或验收结论的默认决策者。用户必须能说明项目解决的问题、明确不做项、关键风险与“什么证据才算完成”。
- 每项有实质影响的开发、重构、实验或评测，在编码前先形成一个最小任务卡：`目标`、`非目标`、`输入`、`输出/接口影响`、`不变量`、`验收证据`。需求不完整时先补齐任务卡，不让 AI 通过默认假设扩大范围。
- 代码阅读不要求逐行覆盖整个仓库，按关键数据流和风险分层推进：先建立模块地图（谁调用、输入、输出、依赖），再审输入/输出契约与失败保护；只有涉及权限、数据隔离、SQL 执行、模型训练/评测、密钥、破坏性操作、异常指标，或准备修改的关键函数，才进入逐行审阅。
- 用户必须亲自掌握每条关键链路的“目标 -> 输入 -> 关键决策 -> 输出 -> 风险 -> 验证证据”。对低风险样板、格式化、重复性脚本和受充分测试保护的局部实现，可由 AI 高自主完成；不得以此降低关键边界的审阅标准。
- AI 提交实质代码变更时，除 diff 外还必须说明：改动文件与原因、接口/调用方影响、保持或改变的不变量、测试覆盖与空白、预期失败模式、回滚或停止条件。用户验收的是这份可审计变更包及其证据，而不只是“代码能运行”。
- 测试通过只证明被覆盖的行为。单元、集成、端到端、离线评测和人工业务核验分别提供不同层级的证据，不能互相替代；不得由 loss 下降、单次成功、单测通过或 AI 总结直接推出语义正确、泛化有效或可上线。
- 每次审阅或实施结束时，明确记录“已验证行为”“测试空白”“待确认设计”和“下一步是否获准”。发现重构机会先记录，不为视觉整齐贸然移动高耦合模块；只有接口、依赖和回归范围已审查并经用户确认后才实施迁移。
- 后训练学习问答也属于项目迭代记录：每次用户复述后，将“用户答案、纠正点、代码入口、测试证据、未解决问题和下一学习单元”写入本地 `docs/post-training/learning/review-*.md`，并追加到飞书项目文档；问答记录不替代代码审阅、测试或业务评测证据。
- 文档治理规则：`PROJECT.md` 只维护项目事实与决策；`docs/post-training/README.md` 维护后训练阶段地图；`docs/post-training/learning/` 维护学习与问答；`docs/post-training/experiments/` 维护实验聚合证据；历史报告保留原文，不为了减少文件数强行合并。根目录后训练旧文件是只读兼容入口，不再追加新内容。
- 学习协作规则：每次只推进一个小代码/概念单元。用户先阅读真实代码和测试，再用自己的话回答；我只在此基础上纠正、补充和给出下一单元。未完成当前单元的理解和审阅前，不启动新的训练、长评测或领域数据构造。

### 2026-08-30：SFT 入口代码审阅回归

- 目标：修复用户中文注释改动覆盖的两个训练入口行为契约，并恢复 GPU 审计字段的正确读取；本轮不启动训练、不构造数据、不修改生产运行时。
- 实现：恢复 `CausalSqlCollator.__call__()` 对 `batch.items()` 的遍历；恢复 `validate_split_audit()` 及其入口调用，核对 split 状态、holdout 隔离、`spider_db_id` 分组、train/validation 行数与 SHA-256；修正 `CUDA_VISIBLE_DEVICES` 环境变量拼写。用户已有中文注释和其他工作区修改保持不回退。
- 验证：数据格式/候选构建/切分 `7 passed`；SFT Dataset、labels、动态 padding、collator、split audit `3 passed`；ruff、compileall、`git diff --check` 通过。
- 代码审阅结论：padding 位置使用 `pad_token`、`attention_mask=0`、`labels=-100`；真实 EOS 保持可关注并参与 SQL 目标 loss。测试只证明确定性输入/输出契约，不证明模型生成的 SQL 业务正确或泛化有效。
- 下一步：继续按“每次一个学习单元”的规则审阅模型加载、`prepare_model_for_kbit_training()` 和 `get_peft_model()`；用户确认理解和测试证据前，不进入 Trainer、GPU 实验或新数据构造。

## 3. 安全、数据与开源边界

- 永远不要提交 API Key、数据库密码、访问令牌、`.env`、模型密钥或含敏感信息的日志；提供 `.env.example` 代替。
- 原始第三方数据集、大型 CSV/Parquet、Kaggle 下载内容、数据库 dump 不得提交 Git。提交数据集 manifest、下载/加载脚本、DDL、转换脚本、许可证与署名说明、小型合成 fixture。
- 使用第三方数据和代码时，记录来源、许可证、版本/commit 和必要的署名；不得把 Olist 或其他境外数据描述为中国真实平台数据。
- 分析数据库必须使用独立只读账号；应用元数据账号和分析查询账号不得混用。
- 不得用字符串黑名单替代 SQL AST 安全策略。所有用户/模型产生的 SQL 都必须经过 AST、对象白名单、单语句、LIMIT 和超时策略。

## 4. v1 架构约束

- 当前基线：Python 3.12、仓库内 Vanna 2.0.2、FastAPI、Vanna 原生 `<vanna-chat>`，先通过 `examples/siliconflow_sqlite_web_demo.py` 跑通最小闭环；
- 后续目标后端：Python 3.12、FastAPI、Vanna、PostgreSQL、SQLAlchemy、Alembic、`sqlglot`、pytest；
- 后续目标前端：原生 Vanna `<vanna-chat>` Web Component；通过宿主页 HTML/CSS、元素属性和浏览器事件完成浮动/侧栏形态、中文文案与业务结果呈现，优先不改 Vanna 组件核心；
- 交互：当前使用 Vanna 原生 SSE 返回进度、表格和结论；后续扩展 SQL、图表和证据对象；
- v1 持久化：PostgreSQL；没有实际缓存、限流、异步导出或多实例协调需求时，不引入 Redis；
- v1 不做多 Agent、MCP、任意 Python 代码执行、写库操作或多数据库方言支持。

## 5. 质量门槛

- 每个成功分析结果必须可追溯到指标口径、最终 SQL、来源表/字段、结果摘要和策略记录；
- 安全测试必须覆盖写操作、多语句、越权对象、无界查询和注释绕过；策略拒绝必须有可读原因；
- 新增或修改指标时，必须同步数据字典、指标版本和相应评测用例；
- 新增功能至少有与风险匹配的测试；不要以“手工页面能打开”代替后端安全/语义测试；
- 任何声称的准确率、延迟、拦截率或性能数据，必须有对应评测用例、运行配置和结果记录。

## 6. 文档与命名约定

- 文档、API、数据模型和代码使用清晰的中英文术语；对外展示使用中文业务语言，并保留原始数据字段映射；
- 长期设计变更可以在 `docs/adr/` 中新增 `NNNN-title.md`；
- 数据集相关内容位于 `data/` 下，但仅提交 manifest、schema、transforms 与 fixtures，不提交原始数据；
- 自有目录约定为 Vanna 源码根目录、`examples/`、`src/data_analysis_agent/`、`tests/`、`docs/`、`data/`、`evals/` 和 `infra/`；本仓库是唯一开发、提交和推送位置。若仍保留 `/disk2/gengnan/_upstream/tailadmin-nextjs-dashboard/`，它只作历史参考，严禁在其中实现本项目业务代码；
- 文档更新应陈述已验证事实与未决假设，不能把计划描述成已实现能力。

## 7. GPU 资源与设备映射

本机 CUDA 逻辑设备编号与 `nvidia-smi` 物理编号并非同一顺序。后续所有本地推理、评测、微调和后训练任务必须以此映射为准，并在实验 manifest、日志或启动命令中同时记录逻辑设备与物理 GPU 编号。

| `CUDA_VISIBLE_DEVICES` 中的逻辑编号 | 实际 GPU | `nvidia-smi` 物理编号 |
| --- | --- | --- |
| `0` | NVIDIA GeForce RTX 4090 | `2` |
| `1` | NVIDIA GeForce RTX 4090 | `3` |
| `2` | NVIDIA GeForce RTX 3090 | `0` |
| `3` | NVIDIA GeForce RTX 3090 | `1` |

- 等价映射为：逻辑 CUDA `0,1,2,3` 分别对应 `nvidia-smi` 的 `2,3,0,1`；不得按 `nvidia-smi` 默认顺序猜测模型实际落点；
- 启动任何占用显存的任务前，必须重新执行 `nvidia-smi` 检查显存和进程占用；不得停止、重启或抢占其他项目的进程；
- 训练/评测脚本应明确设置 `CUDA_VISIBLE_DEVICES`。进程内部的 `cuda:0` 仅表示该变量可见设备集合中的第一个设备，不能单独当作物理卡号；
- 不假定四张卡能够同时使用。多卡训练、分布式启动或显存预算必须在实际空闲状态、互连和任务授权均确认后另行决定。

## 8. 后训练学习协作门

后训练工作以“共同学习和审查”为目标，不以替用户连续执行实验为目标。适用于数据集构建、模型下载、prompt 设计、tokenizer/label、LoRA/QLoRA、SFT、评测、领域迁移和任何 DPO/GRPO 计划。

### 每个阶段的强制顺序

1. **先讲清楚**：在运行命令或修改训练代码前，先说明本阶段假设、要解决的问题、输入文件、输出文件、关键函数、核心参数、资源预算、质量门和不做项。
2. **先读后跑**：先带用户按文件和函数阅读当前实现；必要时用一个最小、只读、低成本示例验证理解，不直接启动长时间 GPU 任务。
3. **每次只推进一个学习单元**：完成一个单元后停下来，让用户用自己的话复述。需要纠正时先指出概念、代码和结论之间的差异，再继续下一单元。
4. **用户确认后才执行**：没有用户明确确认，不开始下一阶段的训练、扩充数据、完整评测、运行时接入或其他长时间/高资源任务。
5. **实验必须可反驳**：用户必须能回答假设、matching Base、唯一变量、数据隔离、评测指标和停止条件；loss 下降、训练完成或输出更短不能直接写成模型质量提升。
6. **过程同步**：每个学习单元在本地文档记录“已学内容、待回答问题、代码入口和验证结果”；形成项目决策后再按既有流程同步飞书和 GitHub。

### 后训练教学材料约定

- `docs/post-training/README.md` 是后训练文档唯一规范入口，按 learning/data/experiments/archive 分类；根目录旧路径只作兼容跳转，不继续承载新内容；
- `docs/post-training/learning/walkthrough-v1.md` 是从环境、数据、prompt、tokenizer、LoRA/QLoRA、SFT、重载到评测的逐步审查主手册；
- `docs/post-training/learning/review-2026-08-28.md` 记录每轮已讨论的问答、代码入口、已纠正概念与下一学习单元；
- `docs/post-training/learning/fundamentals.md` 只讲原理和面试表达，`docs/post-training/archive/learning-notes-v1.md` 只作历史参考；
- `docs/post-training/experiments/log.md` 只记录已完成实验和证据，不把计划写成结果；
- 后训练脚本的规范实现位置是 `scripts/post_training/{data,training,inference,evaluation,launchers}/`；`scripts/` 下同名文件仅保留为兼容入口，新增代码和文档使用规范路径。
- 每次新实验都必须新增或更新：假设、matching Base、唯一变量、数据/模型 hash、质量门、失败停止条件和面试表达。

### 后训练代码审阅门

- 讲解概念、阅读 Markdown 或看到测试通过，均不等于用户已经理解或审阅了实现；在进入新的数据构造、训练、完整评测、运行时接入或后训练算法前，必须先共同阅读该阶段的规范代码、配套测试和输入/输出契约。
- 规范审阅路线记录在 `docs/post-training/learning/code-review-guide-v1.md`。每次只读其中一个单元：用户先在 IDE 打开主文件与测试，说明输入、输出、不变量、失败保护和仍不能保证的结论；随后才讨论代码审查发现或低成本验证。
- 代码审查结论必须以文件/函数/行号和相应测试为证据，区分“已验证行为”“测试空白”“待确认设计”；禁止只根据文档叙述或 loss/单次运行给出质量结论。
- 审阅期间优先记录重构候选，不为目录美观移动生产模块、上游 Vanna、兼容入口或测试。只有接口、依赖和回归范围已共同审查且用户确认时，才拆分/迁移代码。
- 每个审阅单元在本地学习记录中补充已读文件、用户复述、发现、测试证据和下一单元；形成项目决策后仍按飞书、GitHub 同步闭环执行。

### 当前暂停点

2026-08-27 起暂停扩充通用 Spider 数据、启动新训练和 Olist 运行时接入。已共同审查产品/离线边界、数据隔离、tokenizer/labels、forward smoke、SFT/梯度累积、LoRA/QLoRA、validation/test 边界、Olist/PostgreSQL 领域数据合同，以及基础领域样本覆盖矩阵；记录见 `docs/post-training/learning/review-2026-08-28.md` 和 `docs/post-training/data/olist-pilot-coverage-v0.1.md`。在用户请求下，下一步先执行 `docs/post-training/learning/code-review-guide-v1.md` 的真实代码审阅，从 Spider prompt/候选构建/切分开始；未完成相应代码与测试审阅、用户复述和明确确认前，仍不得创建数据模板、coverage seed 或启动训练。当前 Spider 3,600 条 v2 训练和 12 条 Olist 迁移评测的结果只作为已有证据，不自动触发下一轮实验。

## 9. 最近一次同步记录

### 2026-08-30：后训练数据边界与 SFT 入口加固

- 目标：修复已审阅的两个会影响后续训练可信度的边界问题，不扩充数据、不调整模型或超参数。
- 实现：`build_spider_sft_candidates.py` 的 overfetch 预期数量改为只统计通过字段/schema 校验、实际可进入分组的候选；`run_post_training_sft_smoke.py` 新增 `validate_split_audit()`，在模型加载前核对 train/validation 当前文件的行数和 SHA-256、`spider_db_id` 分组策略、`status=pass` 与 holdout 标记，任何审计缺失或文件替换都 fail closed。
- 验证：`data-analysis-agent` 环境的数据格式/候选构建/切分专项 **7 passed**；`data-analysis-agent-qlora` 环境的 Dataset/label/collator/审计专项 **3 passed**；ruff、compileall、`git diff --check` 通过。验证覆盖了“坏行 + overfetch 不误报”和“split 文件篡改被拒绝”两个回归路径。
- 边界：`assert_train_only()` 仍是路径防呆而非完整 provenance 证明；`read_only_explain()` 仍只证明 SQLite 解析/名称解析，不证明业务语义；`question_redacted` 命名和 Prompt 版本/调用方一致性仍是后续审阅项。本轮未启动 GPU、未构造数据、未训练、未评测、未改变生产运行时。
- 提交：`329ccb8 fix(post-training): verify split artifacts before sft`，已推送 `origin/main`。用户此前在三个数据构建文件中的中文注释改动保持工作区未提交。

### 2026-08-30：AI 协作与工程主导权规则

- 决策：AI 是实现、测试、调研和审查的加速器；用户保留问题定义、非目标、架构取舍、数据/安全边界和验收结论的主导权。每个实质任务先明确“目标、非目标、输入、输出/接口影响、不变量、验收证据”，避免 AI 以默认假设扩大范围。
- 审阅方法：不要求逐行覆盖全仓库；先建立模块地图，再阅读契约和失败保护，只在权限、数据隔离、SQL 执行、训练/评测、密钥、破坏性操作、异常指标或待修改关键函数处升级为逐行审阅。用户应能讲清关键链路的目标、输入、关键决策、输出、风险和验证证据。
- 交付要求：AI 的实质变更必须附带影响范围、不变量、测试覆盖/空白、失败模式和回滚/停止条件。单元、集成、端到端、离线评测和人工业务核验分别提供不同证据；不得将 loss、单次成功、测试通过或 AI 总结直接写成业务语义正确、泛化有效或可上线。
- 范围：本轮只更新协作约束与项目台账，保留用户已存在的三个数据构建源码改动，不启动 GPU、不生成数据、不运行训练或评测、不改变生产运行时。

### 2026-08-30：项目目录地图与后训练代码审阅门

- 目标：让后训练学习从“理解原理和实验结论”转为“用户亲自审阅真实代码与测试”，并明确上游 Vanna、项目运行时、离线研究脚本和本地研究产物的目录边界。
- 实现：新增 `docs/architecture/repository-map.md` 和 `docs/post-training/learning/code-review-guide-v1.md`，按 prompt/数据、token/训练、生成、SQLite/Test Suite/denotation、Olist 业务迁移和启动器划分审阅单元，列出规范文件、关键函数、配套测试、审阅问题和结束标准。
- 决策：在任一后训练新阶段前，用户必须先阅读相关实现和测试、复述输入输出与失败保护；讲解或测试通过不能代替代码审阅。审阅期间只记录重构候选，不移动生产模块、上游 Vanna、兼容入口或测试目录。`github-research-output/` 是 Git 忽略的大型调研产物，后续经确认后可迁出仓库根目录；本轮不移动、不删除。
- 验证：本轮只做目录/依赖审计和文档更新；未读取密钥、未启动 GPU、未构造领域数据、未改变 Vanna/SiliconFlow/PostgreSQL 生产运行时。

### 2026-08-28：后训练学习问答沉淀与脚本/文档分层

- 目标：停止“只运行实验、不理解过程”的协作方式，将本轮已经共同审查的 Text-to-SQL SFT、LoRA 和评测知识沉淀为可持续学习材料，同时清理后训练代码与文档的平铺目录。
- 实现：新增 `docs/post-training/` 分类入口和 `learning/review-2026-08-28.md`，覆盖候选 SQL 边界、gold SQL 泄漏、schema-disjoint、SQLite EXPLAIN、labels/EOS、forward smoke、梯度累积、AdamW、validation/test、LoRA A/B、rank、bf16 LoRA/QLoRA 与 PEFT 生命周期。后训练真实实现移动至 `scripts/post_training/` 的 data/training/inference/evaluation/launchers 分类目录，根目录同名脚本保留兼容入口。
- 学习：已补充 Olist/PostgreSQL 领域数据合同，明确 60 条 v2 holdout 及其派生表达永久隔离；领域 SFT 输入需要 QuestionRouter、Catalog slice、QueryPlan 和结果列合同，不能只用问题到 SQL；候选 SQL Adapter 只训练可回答数据库路由。规模先做数据合同，之后最多从约 300--500 条语义独立 pilot 开始，不将“凑到几千条”当作前置条件。
- 决策：生产 `src/data_analysis_agent/` 运行时模块不做为了目录美观的全量迁移，以免引入大范围导入风险；原始数据、模型、Adapter、checkpoint 和日志仍不进入 Git。新训练、数据构造、完整评测和运行时接入仍暂停，下一步先审查领域样本覆盖矩阵。
- 验证：后训练运行时回归 `54 passed, 1 skipped`、QLoRA dataset/label 回归 `2 passed`、ruff、兼容 CLI、shell syntax、Markdown 链接与 diff check 已通过；本单元只阅读现有可信链路，没有启动 GPU 或修改生产运行时。

### 2026-08-26：Olist PostgreSQL 业务迁移候选 SQL 质量门

- 目标：在不改变 Vanna/SiliconFlow 默认模型、不开启 SQL repair 的前提下，验证已通过 Spider SQLite 离线质量门的 bf16 LoRA Adapter 是否能迁移到当前中文 Olist PostgreSQL 工作区。
- 实现：新增 `olist-candidate-sql-v1` SQL-only prompt、12 条永久 holdout manifest、Base/Adapter runner、脱敏 paired analyzer 和顺序 `screen` launcher。每条候选仍复用 QuestionRouter、Catalog、QueryPlan、ResultContract、SqlPolicy、reader role 和 ResultValidator；候选 SQL、问题、数据库行、模型和 Adapter 一律留在仓库外。
- 证据：最终 v2 对照的 Base/Adapter 均为 12/12 generation、SqlPolicy `6 -> 6`、PostgreSQL executed `4 -> 2`、ResultContract valid `2 -> 0`；没有 `non_valid -> valid`，有 2 条 `valid -> non-valid`。Base 有 5/12 触达 256 token 生成上限；Adapter 更短但未得到有效业务合同结果。
- 决策：Spider 离线候选质量门仍为通过，但 Olist 业务迁移子门为未通过；不得把 Adapter 接入生产默认模型，也不因本结果盲目扩大通用 Spider。下一实验先构建与 v2 60 条 holdout 及其改写严格隔离的领域对齐 Olist/PostgreSQL train/validation 数据，再按同一业务合同复测。
- 健壮性：截断 SQL 的 `sqlglot.TokenError` 现与 `ParseError` 同样归一为可审计 `PolicyViolation`；`WorkspaceProfile` 与 `MetricDefinition` 的不可变映射默认值改为 `default_factory`，使 QLoRA Python 3.11 环境可加载项目可信链路。专项 68 passed、项目 PostgreSQL runner 5 passed、ruff/compileall/shell/CLI smoke/diff check 通过。

### 2026-08-26：Spider SFT v2 全量质量诊断闭环

- 目标：完成 3k 级 bf16 LoRA Adapter 与 matching bf16 Base 的完整 1,034-case Spider dev 对照，并避免把离线生成质量误写为生产能力或官方榜单。
- 证据：Base/Adapter 均完成生成、只读 SQLite diagnostics、固定 commit Test Suite bridge 和生成冻结后全量 bounded denotation audit。SQLite executed 为 `950 -> 961`；fixed Test Suite internal all 为 `0.507 -> 0.667`，四个难度桶均提升；denotation exact-or-bag match 为 `570 -> 708`，净增 138（213 non-match -> match、75 match -> non-match）。原始问题、gold/candidate SQL、数据库行、模型、预测与日志均留在仓库外。
- 诊断：Adapter 的 direct SQL-shaped completion `370 -> 1,034`、generation-cap hit `264 -> 1`，但仍有多余 join、表列混淆、`DISTINCT`/分组/集合重复度、投影顺序和函数形状回退。SQLite snapshot denotation 不足以证明所有可能数据实例的等价性，固定 Test Suite 仅是当前固定资产组合的内部输出。
- 修复与验证：denotation audit 对 Spider SQLite 中非 UTF-8 TEXT 以原始 bytes 比较，避免只读结果比较中断且不改变 SQL、数据库或报告脱敏边界；新增对应回归测试。完整结论见 `docs/post-training-spider-sft-v2-full-analysis.md` 与冻结 manifest。
- 决策：`offline_candidate_generator_quality_gate_passed_runtime_integration_deferred`。下一步只设计可开关、可回退的 runtime candidate generator，并在独立业务 workspace 评测；不得移除 `QuestionRouter`、Catalog/QueryPlan、AST Policy、PostgreSQL reader role、Result/Chart Contract，不进入 DPO/GRPO。

### 2026-08-25：后训练材料重组与 26-step LoRA/QLoRA 覆盖度实验

- 目标：将概念学习、实时项目状态和实验结论拆开，验证首轮 8-step 负向 ablation 是否至少受训练覆盖度或 4-bit 基座加载方式影响。
- 文档：新增 `docs/post-training-index.md` 作为唯一入口，新增独立学习指南和实验台账；历史路线/笔记保留为参考并明确不承担实时状态。项目运行时的 Vanna/PostgreSQL 可信链路与 Spider SQLite 离线研究边界被单独说明。
- 实现：SFT runner 和 adapter reload validator 都支持 `qlora_4bit` / `bf16_lora`，记录 launcher 声明的物理 GPU、进程内 UUID、量化方式和证据；两个 26-step 任务使用同一 Qwen 1.5B revision、102/26 schema-disjoint split、seed、学习率、有效 batch 与 LoRA 配置，只改冻结基座的 4-bit NF4/bf16 表示。
- 验证：QLoRA 在 logical `0` / physical `2` 的 RTX 4090 完成 26 step，train/eval loss `0.427482/0.290193`、allocated peak `4,675,977,728` bytes；bf16 LoRA 在 logical `1` / physical `3` 完成，`0.426504/0.309192`、`5,574,457,856` bytes。两个 74 MB adapter 均 fresh PEFT reload 并在 validation sample 得到 finite loss。Python dataset tests **2 passed**、shell syntax、CLI help 和 diff check 通过。
- 边界：loss/reload 只证明训练工程与资源差异，不构成 SQL 执行、Test Suite 或业务语义质量结论。下一步必须对每种加载精度单独完成同合同的 1,034-case base/adapter 对照、错误迁移与人工语义核验；不进入 DPO/GRPO。

### 2026-08-25：26-step matching Base/Adapter 全量评测已启动

- QLoRA-26 与 bf16 LoRA-26 的完整质量评测已分别在 `daa-qwen15b-qlora26-eval-v1` 与 `daa-qwen15b-bf16lora26-eval-v1` 中启动。前者复用已完成的 matching 4-bit Base，只生成新 adapter；后者在同一 GPU 上按 bf16 Base、bf16 adapter 的顺序运行，避免同一精度对照发生显存竞争。
- 设备守卫固定为 logic `0` -> physical `2` -> UUID `GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52`，以及 logic `1` -> physical `3` -> UUID `GPU-10863af0-8588-7625-5609-640ba794f64b`。启动前 bf16 adapter 的 2-case fresh-load smoke 已通过。
- 每条完成后才依次读取 1,034-case generation evidence、SQLite diagnostics、pinned Test Suite bridge 和安全聚合的 paired report；在此之前不得宣称 non-regression、精度差异或语义提升。

### 2026-08-18：真实 SiliconFlow 人工标签评测

- 目标：将已完成的前端加固、v2 确定性资产收敛为可复核的真实模型小样本，不把 SQL 执行成功伪装成语义正确。
- 实现：新增 24 条 online v1 清单和通过 Trusted Demo SSE 的运行器；报告按 request ID 回读 Agent Run/SQL 审计，逐条记录路由、澄清、SQL、指标口径、结果合同、权限、回答有据、工具/SQL/修复次数、时延和 token 状态。原始问题、回答、SQL、结果行、cookie、密钥和运行报告均不提交 Git。
- 结论：24/24 Agent Run；路由 23 pass/1 fail，权限 24 pass，回答有据 17 pass/7 fail。`data_014`/`data_016` 的首个正确 SQL 后续被无关 SQL 破坏，ResultValidator 已安全阻断；`multi_003` 因 Catalog slice 遗漏可用 Join 而在 180 秒后未完成。当前没有 repair lifecycle recovery；provider 未返回 usage 时明确记为 unknown。
- 后续：先修复币种、已通过合同后的工具停止、多指标 Catalog slice 和支付归因；修复后对已失败 case 做固定回归，再评估多次修复或延迟优化。

### 2026-08-18：通用语义检索与结果合同加固

- 目标：将在线评测暴露的币种误标、多跳 Catalog 缺表、支付维度自行猜口径和合同通过后重复 SQL，收敛为可复用的 Workspace/Catalog/预算机制。
- 实现：Catalog 支持工作区币种元数据与 `DimensionPolicy`；检索在可见 Join 图上做 BFS 路径闭包，受表、Join、列和 Prompt 预算约束；QuestionRouter 对声明为歧义的维度返回零 SQL 澄清；ResultValidator 成功后标记 `result_contract_satisfied`，预算层拦截后续冗余 `run_sql` 并保留可用图表调用；Prompt 增加币种和保守趋势表述合同。没有加入 Olist 问题文本特判。
- 验证：相关专项测试 71 passed；`run_text_to_sql_evaluation.py` 60/60 passed；全量 pytest 的失败来自 Vanna 上游可选依赖/缺失 fixture，不计入本项目质量门。
- 后续：补充真实 runner 对合同状态的集成证据，复跑固定失败样本，随后再评估在线模型延迟、usage 采集和第二数据集适配。

### 2026-08-19：真实 PostgreSQL 合同链路集成测试

- 目标：验证结果合同状态不是仅存在于单元测试或内存对象，而是能沿真实 SQL 工具链和 Agent Run 持久化链路闭环。
- 实现：新增 `test_result_contract_state_flows_through_real_runner_and_budget_registry`，使用项目专属 PostgreSQL 执行真实有效订单聚合，经 `ResultValidator` 通过后检查 `ToolContext`/`BudgetUsage` 状态；再次提交 `run_sql` 时由 `BudgetedToolRegistry` 抑制，不增加 SQL/tool budget，且查询审计不产生第二条记录。运行记录将合同通过和冗余 SQL 抑制计入既有 `catalog_trace` JSONB，不改数据库表结构。
- 验证：`RUN_PROJECT_DB=1 DATA_ANALYSIS_POSTGRES_HOST=/tmp pytest tests/test_postgres_runner.py tests/test_postgres_run_recorder.py` 为 **5 passed**；ruff、compileall 和 diff check 通过。
- 风险/下一步：测试验证的是确定性真实数据库链路，不包含在线模型多轮行为；后续可针对真实 SSE 请求补充一条模型发出重复工具调用的固定 mock 回归。

### 2026-08-19：SSE 模型响应合同回归

- 目标：验证冗余 SQL 在模型响应边界被抑制，而不只依赖工具注册表的最后防线。
- 实现：用固定 `LlmService` 驱动真实 `Agent`、`BudgetedChatHandler`、PostgreSQL ConversationStore/RunRecorder 和生产 SQL 工具链；同一请求中第一轮模型调用受控 SQL，第二轮模型再次返回 `run_sql`。`BudgetSafetyMiddleware` 移除第二轮工具调用，Agent 正常完成。
- 验证：真实数据库专项由 5 项扩展为 **6 passed**；合并预算、Catalog、路由、SQL 工具、PostgreSQL 与运行记录专项为 **75 passed**，v2 golden **60/60 passed**。断言 Agent Run 为 1 次 SQL/工具调用、`completed`、合同/抑制证据完整，且仅有 1 条 allowed 审计。
- 后续：接着处理在线模型的 token usage 可观测性和长响应超时边界；仍不据固定模型回归声称在线准确率或延迟分位数。

### 2026-08-19：LLM 可观测性与 6 条定向 SiliconFlow 复测

- 实现：`ObservedLlmService` 统一 asyncio/OpenAI/httpx timeout，记录 bounded `llm_observations`；Vanna OpenAI-compatible 同步调用移到工作线程，避免阻塞事件循环。新增 targeted manifest 和 `--allow-small-sample`，默认批量评测门槛仍为 20--30 条。
- 验证：LLM/线程/评测器单测 **8 passed**；项目 PostgreSQL/run recorder **10 passed**；真实 SSE 定向复测 **6/6 Agent Run、0 客户端错误**，路由 6/6、SQL 可执行 5/5、结果合同 5/5、权限 6/6，5 条查库请求各 1 条 SQL，usage 均 reported，总客户端耗时 499,174 ms。人工语义/最终 grounded 仍有 3 条 pending，不发布在线准确率或 P50/P95。
- 边界：在线模型仍可能有较高延迟；人工标签尚未覆盖全部定向结果；报告只保存在被忽略的 `evals/reports/`，不含问题、回答、SQL、数据行或密钥。下一步是将定向失败转成固定回归并优化模型轮次/缓存，而不是扩大 SQL 修复次数。

### 2026-08-19：可信结果确定性收口

- 实现：未显式要求图表时，已通过 `ResultValidator` 的 SQL 结果由服务端依据结果合同收口，不再调用模型生成最终摘要；收口不产生趋势、币种或因果判断。图表请求显式禁用收口，保留 `visualize_data`。运行台账和在线脱敏评测均记录 `deterministic_result_finalized`。
- 验证：真实 `data_005` SSE 回归为 `deterministic_result_finalized=true`、1 条实际 PostgreSQL 执行、2 次模型调用、68,516 ms；首次模型 SQL 被 AST Policy 以敏感 `order_id` 拒绝，第二次有效，因此两轮不是冗余总结。单测 25 passed（1 skipped）、PostgreSQL 10 passed、v2 60/60。
- 边界：这是一个定向单次时延观测，不是 P50/P95；服务端收口优先保证“表格可核对、结论不越界”，更丰富的业务解释应通过后续明确追问和可信结果摘要完成。

### 2026-08-06：证据路由、QueryPlan 与可信结果记忆

- 目标：按 Text-to-SQL 改造顺序拆开“是否命中指标”和“是否允许查库”，补齐多指标查询的服务器计划，并让结果追问只依赖可信结果证据。
- 实现：`QuestionRouter` 新增 `intent`、`requires_database`、`evidence_mode`、`confidence` 和 `reason_code`，覆盖帮助、指标定义、通用业务/知识、数据查询、数据分析、混合请求、结果追问和澄清；通用回答使用 `_send_llm_request` 的 `tools=None` 边界，帮助/指标定义走确定性回答。新增 `QueryPlan`，把多指标标量概览约束为每指标独立聚合后 `CROSS JOIN`，把分组维度和时间列加入 `ResultContract` 与 ToolContext。`ResultValidator` 成功后生成有界摘要，`BudgetUsage`、Agent Run trace 和 `WorkingMemory.previous_result_summary` 均可记录；没有可信摘要的结果追问会先澄清。
- 验证：QuestionRouter、QueryPlan、WorkingMemory、ResultValidator、预算、TrustedRunSqlTool、SQL Policy 和修复专项 **88 passed**；`ruff check`、`compileall`、`git diff --check` 通过。全量 Vanna 上游可选驱动测试仍受环境依赖影响，未作为本项目质量门；未运行在线 SiliconFlow 批量语义评测。
- 设计决策：指标命中不等于查库；通用回答不得看到 SQL/图表工具；QueryPlan 当前是服务器拥有的 grounding/result contract 和 Prompt 约束，尚不是完整 SQL AST 形状证明；结果摘要只来自通过 ResultValidator 的 DataFrame，不接受助手文本或客户端 metadata。
- 风险/限制：仍需真实模型路由/多指标批量评测，QueryPlan 的 CTE/Join 形状还要在后续 AST 检查中增强；结果追问目前只解释有限样例摘要，不是任意历史结果回放；Olist 仍是当前 adapter，演示 cookie 不是生产认证。
- 下一步：建立版本化 v2 路由/语义 golden，先做固定问题集上的人工核验，再决定是否加入低置信度结构化分类器和更强的 QueryPlan 执行校验。

### 2026-08-06：通用工作区与一次 SQL 修复生命周期

- 目标：完成本轮四项优化，明确 Olist 适配层与通用分析 Agent 核心的边界，并把一次 SQL 修复从独立契约接入 Vanna 工具生命周期。
- 实现：新增 `WorkspaceProfile`；`SqlPolicy`、`CatalogLoader`、`SecurePostgresRunner` 和预算处理器读取工作区配置。新增 `TrustedRunSqlTool`，失败 SQL 只经过一次脱敏修复；修复候选重新通过 AST Policy，由 PostgreSQL reader role 重执行，成功后再过 `ResultValidator`，二次失败直接可信拒答。`query_audits`、`agent_runs` 和预算记录保存 `repair_evidence`。
- 测试：专项集合 `84 passed`；项目 PostgreSQL `test_postgres_run_recorder.py` 与 `test_postgres_runner.py` 为 `4 passed`；固定 SSE 的浏览器多轮澄清回归 `1 passed, 6 deselected`；ruff、compileall、`git diff --check` 通过。未将这些确定性结果写成在线 LLM 语义准确率。
- 边界：Olist 仍是当前 adapter/展示案例，尚无第二真实数据集；演示 cookie 不是生产认证，未实现组织级 RLS；尚未做 SiliconFlow 批量修复成功率和 token/P95 评测。
- 下一步：建立版本化 v2 评测集，先用固定 Olist golden 和人工标签核验真实模型，再决定是否引入更复杂的 schema retrieval、judge 或多候选策略。

### 2026-08-06：嵌入式可靠性与延迟定位

- 目标：处理 `/embedded-demo` 的窗口恢复布局、非数据库问题误走 SQL、历史消息 Markdown 原样显示和响应过慢四项反馈。
- 实现：SQL Policy 增加多 CTE 输出列/外层别名识别，并修复 scalar subquery 关联键的敏感列误判；`QuestionRouter` 对纯指标定义/统计口径问题直接从 Catalog 返回 `catalog_answered` Markdown；历史 assistant 消息与流式文本共用转义后渲染的 Markdown renderer；宿主页通过双 `requestAnimationFrame` 与 `ResizeObserver` 同步组件高度。预算台账增加 `route_catalog`、`llm_request`、`sql_policy`、`postgres_sql` 阶段耗时证据。
- 验证：Web Component `npm run build` 通过；相关后端专项 **63 passed, 1 skipped**；Playwright `tests/e2e/test_trusted_embedded_window.py` **7 passed**；`ruff check`、`compileall` 和 `git diff --check` 通过。真实多指标请求已完成，代表性观测约 77.7 秒，其中两轮 SiliconFlow 模型调用约 77.2 秒，PostgreSQL 约 0.39 秒。
- 边界：该延迟是单次观测，不是 P95；仍未完成模型批量语义准确率、模型超时/流式优化、真实认证、组织级 RLS 和第二真实数据集。
- 下一步：先建立带人工标签和 golden SQL 的 v2 评测集，再基于阶段耗时做模型调用超时、流式状态和 Prompt/上下文压缩优化。

### 2026-08-03：Text-to-SQL 第一阶段运行时合同

- 目标：修正 Catalog/WorkingMemory 派生的结果语义合同没有进入实际 Vanna `ToolContext` 的硬缺口。
- 实现：新增服务器拥有的 `ResultContract`，接入指标/时间别名/请求范围/Join 与版本证据；补充
  `ResultValidator` 和 PostgreSQL Runner 的别名校验；统一 Prompt、Catalog trace、Agent Run 与 SQL
  审计中的版本字段；预算 trace 改为合并写入。
- 验证：Text-to-SQL 专项 `68 passed`；项目 PostgreSQL 集成 `9 passed, 1 warning`；编译检查和
  `git diff --check` 通过。未将在线模型准确率、token 成本或完整自动修复生命周期写成已完成能力。
- GitHub：`cc8b688 feat(text-to-sql): wire semantic result contract` 已推送到 `main`；后续同步文档提交
  会继续记录在飞书项目文档。

### 2026-08-19：敏感关联键投影误拒绝修复

- 问题：真实 `data_005` 首轮 SQL 在内部 CTE 中按 `order_id` 聚合并用于 Join，最终只返回
  `time/gmv`；旧 AST Policy 将 CTE 内部关联键误当作对外投影，造成一次不必要的模型重试。
- 实现：SQL Policy 区分内部 CTE/子查询阶段与最外层结果阶段。CTE 内可保留敏感键以保持事实
  粒度；最外层仍拒绝敏感结果列、结果别名、`GROUP BY` 和 `ORDER BY`，并继续执行 Catalog、
  reader role 和 ResultValidator 边界。QueryPlan/Catalog Prompt 同步明确顶层结果列白名单和
  内部键使用规则，不绑定 Olist 字段名。
- 验证：专项 **50 passed**，v2 golden **60/60**，真实 PostgreSQL 链路 **11 passed**；真实
  SiliconFlow 回归由 `2 SQL/2 LLM rounds/1 rejected audit` 收敛为 `1 SQL/1 LLM round/0
  rejected audit`，结果合同通过、1 条 allowed 审计、`deterministic_result_finalized=true`。

### 2026-08-19：真实业务解释与图表质量评测

- 目标：用真实 SiliconFlow 业务问题验证指标语义、结果解释和显式图表请求，不把 SQL 执行成功当作业务正确。
- 实现：新增 20 条脱敏 quality manifest（其中 5 条携带有界图表意图）、人工标签和运行器结构性图表证据字段；报告不保留问题、回答、SQL、行或图表 payload。
- 验证：20/20 Agent Run，权限 20/20；指标语义 11 pass / 2 fail / 7 N/A，回答有据 11 pass / 9 fail；原批次 3/5 发出图表组件、2/5 在 SQL 前超时。Playwright 真实页面确认 SVG、标签、数据标记和无横向溢出，但折线图请求实际渲染为柱状图；独立州图表重放 180 秒超时。评测合同测试 9 passed，嵌入窗口 E2E 9 passed。
- 风险/下一步：支付方式归因、评价行粒度与图表类型仍由模型提示主导，下一轮必须把三者收敛为服务器 Result/Chart Contract，再评估模型超时、缓存或异步化；不发布总体准确率、图表成功率或 P50/P95。
