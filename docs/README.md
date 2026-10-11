# 文档导航

这里是 `data-analysis-agent` 的文档总入口。文档数量较多，是因为项目同时包含产品设计、运行时架构、数据与运维、评测证据以及离线后训练研究；这些内容的版本、证据和读者不同，不应强行合并成一篇长文。文档的生命周期、归档和删除规则见 [`architecture/documentation-map-v1.md`](architecture/documentation-map-v1.md)。

## 先看什么

| 目的 | 入口 | 文档性质 |
| --- | --- | --- |
| 了解项目目标、当前阶段和技术决策 | [`../PROJECT.md`](../PROJECT.md) | 当前项目基线 |
| 查看新的四周产品完善、代码学习与实习面试路线 | [`../plan/process-agent-internship-improvement-v1.md`](../plan/process-agent-internship-improvement-v1.md) | 2026-10-10 计划；不重复计算机基础，不自动启动训练 |
| 按小段亲自阅读 Agent 代码并复述 | [`learning/agent-project-walkthrough-v1.md`](learning/agent-project-walkthrough-v1.md) | Day 1 请求入口；问答随用户学习更新 |
| 核对 SQL-only 训练与真实候选输入的对齐改造 | [`../plan/refactor-runtime-candidate-sft-alignment-v1.md`](../plan/refactor-runtime-candidate-sft-alignment-v1.md) | 2026-10-11 已完成共用入口、合同修复、7,500 行回放与 32 条小批物化/只读 Gold 对照；下一步全量重建 train/validation 并冻结长度，新正式 release 与训练尚未启动 |
| 了解本周已核实的用户请求/可信执行链 | [`architecture/project-v1-baseline-review.md`](architecture/project-v1-baseline-review.md) | Week 1 当前审查基线 |
| 查看有证据支持的产品优化候选与暂不处理项 | [`architecture/project-v1-gap-map.md`](architecture/project-v1-gap-map.md) | Week 1 缺口地图，不是实时任务台账 |
| 查看嵌入式 Copilot 与长对话状态的目标架构 | [`architecture/embedded-copilot-and-memory-design-v1.md`](architecture/embedded-copilot-and-memory-design-v1.md) | M1 宿主合同入口；按 M1–M7 状态阅读 |
| 查看 Codex CLI 与开源 Agent 的上下文实现调研 | [`architecture/open-source-agent-context-research-v1.md`](architecture/open-source-agent-context-research-v1.md) | 代码级外部参考与“不采用”边界 |
| 了解目录、模块所有权和代码审阅顺序 | [`architecture/repository-map.md`](architecture/repository-map.md) | 当前架构地图 |
| 了解后训练项目状态和学习入口 | [`post-training/README.md`](post-training/README.md) | 后训练唯一入口 |
| 查看当前主模型、最终评测和 DPO 停止证据 | [`post-training/experiments/project-v1-model-baseline.md`](post-training/experiments/project-v1-model-baseline.md) / [`project-v1-dpo-decision.md`](post-training/experiments/project-v1-dpo-decision.md) | Week 1 已冻结的模型事实 |
| 回顾 Week 2--4 的依赖顺序和小任务验收门 | [`../plan/process-project-v1-week2-4.md`](../plan/process-project-v1-week2-4.md) | 已推进至 W4-04，实际实现以当前代码与 PROJECT.md 为准 |
| 查看 A--E 产品能力基线测试合同与阶段门 | [`../plan/feature-product-capability-baseline-test-v1.md`](../plan/feature-product-capability-baseline-test-v1.md) | 先量能力，再做产品/模型优化 |
| 了解运行、数据、指标和权限边界 | 下方“架构与运行时” | 当前设计/运行说明 |
| 核对质量结论和历史实验 | 下方“评测与后训练实验” | 不可改写的证据记录 |

## 文档分层

### 项目方案与研究

- [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)：早期开发计划和阶段拆解，作为历史方案参考；当前状态以 `PROJECT.md` 为准。
- [`AGENT_PLATFORM_NEXT_PLAN.md`](AGENT_PLATFORM_NEXT_PLAN.md)：Agent 平台能力研究与后续规划。
- [`TEXT_TO_SQL_RESEARCH.md`](TEXT_TO_SQL_RESEARCH.md)：Text-to-SQL 方法、论文和开源项目调研。
- [`ADVERSARIAL_REVIEW_TEXT_TO_SQL.md`](ADVERSARIAL_REVIEW_TEXT_TO_SQL.md)：Text-to-SQL 安全与鲁棒性审查。
- [`VANNA_CAPABILITY_AUDIT.md`](VANNA_CAPABILITY_AUDIT.md)：Vanna 原生能力与本项目启用状态审计。
- [`PORTFOLIO_POSITIONING.md`](PORTFOLIO_POSITIONING.md)：项目定位、简历表述和不可夸大的能力边界。

这些文档回答“为什么这样设计、有哪些候选方向”，不作为每次迭代的实时状态台账。

### 架构与运行时

- [`architecture/repository-map.md`](architecture/repository-map.md)：上游 Vanna、本项目核心、前端组件、脚本、测试和实验资产的所有权地图。
- [`architecture/project-v1-baseline-review.md`](architecture/project-v1-baseline-review.md)：Week 1 以真实代码和定向测试核实的 Web 请求、Router、QueryPlan、SQL Policy、reader role、ResultValidator、repair 与审计链；明确区分已验证行为与待验证项。
- [`architecture/project-v1-gap-map.md`](architecture/project-v1-gap-map.md)：从本周审查证据提炼的运行时优化候选、优先级、验收条件与明确不做项；不替代 `PROJECT.md` 的项目进度。
- [`architecture/embedded-copilot-and-memory-design-v1.md`](architecture/embedded-copilot-and-memory-design-v1.md)：将当前可信 Demo 收束为可嵌入 Copilot 的 Host Integration Contract、可信结果 artifact 驱动的会话状态模型、上下文装配优先级与分步实施门；设计提案，不代表实现完成。
- [`architecture/embeddable-copilot-shell-v1.md`](architecture/embeddable-copilot-shell-v1.md)：当前唯一产品页 `/embedded-copilot`。演示角色和审计在「管理」后面，`/embedded-demo` 重定向到此页。
- [`architecture/open-source-agent-context-research-v1.md`](architecture/open-source-agent-context-research-v1.md)：对 Codex CLI、Aider、LangGraph 和 Mem0 的实际 session/context/memory 代码做的限定调研；给出可吸收的实现模式、许可证与本项目不直接复用/不采用的边界。
- [`architecture/data-model.md`](architecture/data-model.md)：运行时数据模型和持久化边界。
- [`data-dictionary.md`](data-dictionary.md)：当前工作区字段字典。
- [`metric-catalog.md`](metric-catalog.md)：冻结测试用的十项 v2 指标简表。在线演示读取 v3 目录，见 [v3 指标合同](metric-contracts/olist-metrics-v3-proposal.md)。
- [`metric-contracts/olist-metrics-v2.md`](metric-contracts/olist-metrics-v2.md)：十项指标的公式、分母、归属边界和数据库回归证据。
- [`metric-contracts/thelook-metrics-v1.md`](metric-contracts/thelook-metrics-v1.md)：TheLook 首批六项指标合同及状态、时间、币种和跨 schema 比较边界。
- [`metric-contracts/thelook-metrics-v2.md`](metric-contracts/thelook-metrics-v2.md)：TheLook 20 项跨事实域指标的公式、时间归属、可展示维度和明确排除的业务口径。
- [`post-training/data/olist-queryspec-renderer-design-v1.md`](post-training/data/olist-queryspec-renderer-design-v1.md)：Olist 领域 SFT 中离线 QuerySpec 与 deterministic PostgreSQL Gold SQL renderer 的职责边界及实现状态；40 条 Gold 已准入，作为工程验收证据。
- [`post-training/data/olist-pilot-v1-sft-data.md`](post-training/data/olist-pilot-v1-sft-data.md)：40 条工程验收集，记录真实运行时 Prompt、`24/8/8` family-isolated 验收和 `2304` 无截断长度合同；不作为正式训练规模。
- [`post-training/data/olist-medium-v1-sft-data.md`](post-training/data/olist-medium-v1-sft-data.md)：正式中等规模领域 release 的 `720/240/240` 目标、准入和物化契约。
- [`post-training/data/olist-v3-balanced-release-v1-1.md`](post-training/data/olist-v3-balanced-release-v1-1.md)：Olist v3.1 平衡、纯中文 SQL-only SFT release 的 `3000/750/750` 资产、八问法配额和 SHA-256 指纹；后续已训练及评测。v3.0 的五问法版本保留为历史证据。
- [`post-training/data/olist-v3-1-sql-only-training-evaluation-contract.md`](post-training/data/olist-v3-1-sql-only-training-evaluation-contract.md)：v3.1 训练前冻结的 Prompt/SQL/EOS、bf16 LoRA、validation-best checkpoint 与后置 Gold matching 协议；当前主实验结果看模型实验卡。
- [`post-training/data/qwen35-olist-sft-template-contract-v1.md`](post-training/data/qwen35-olist-sft-template-contract-v1.md)：Qwen3.5-4B Instruct 的官方消息模板、SQL-only label 边界、EOT 停止条件和训练前 token 审计合同；复用 Olist Release v2 的既有训练/验证 split，不读取 TheLook。
- [`post-training/experiments/qwen35-4b-olist-instruct-sft-smoke-v1.md`](post-training/experiments/qwen35-4b-olist-instruct-sft-smoke-v1.md)：Qwen3.5-4B bf16 LoRA 的 target 审阅、单步训练/保存/fresh reload 证据与正式训练显存资源门。
- [`post-training/data/olist-domain-sft-expansion-plan-v2.md`](post-training/data/olist-domain-sft-expansion-plan-v2.md)：基于 TheLook 错误证据的数据扩展、中文 surface-form 多样性、程序族切分和下一轮 `1600/400/400` release 方案。
- [`post-training/data/schema-aware-program-sft-contract-v1.md`](post-training/data/schema-aware-program-sft-contract-v1.md)：新的 Olist Schema-aware Program SFT 合同；训练期从 QuerySpec 确定性派生表列、Join、grain、时间和 alias 结构监督，线上仍只生成一次 SQL。
- [`post-training/data/thelook-cross-schema-workspace-v1.md`](post-training/data/thelook-cross-schema-workspace-v1.md)：第二电商 schema 的独立 PostgreSQL workspace、来源、最小权限与审计边界；早期只作 Olist-only 模型的零样本评测，后续主实验已加入独立 TheLook train/validation。
- [`post-training/data/thelook-queryspec-contract-v1.md`](post-training/data/thelook-queryspec-contract-v1.md)：TheLook 结构化 QuerySpec 字段、允许范围和 fail-closed 校验；不包含自然语言问题或 Gold SQL。
- [`post-training/data/thelook-renderer-contract-v1.md`](post-training/data/thelook-renderer-contract-v1.md)：TheLook deterministic PostgreSQL Gold SQL renderer 的固定公式、粒度边界和真实 reader 执行证据。
- [`post-training/data/thelook-cross-schema-final-test-v1.md`](post-training/data/thelook-cross-schema-final-test-v1.md)：TheLook 206 条冻结跨 schema Base/Adapter final test 的覆盖、准入和评测边界。
- [`post-training/data/thelook-queryspec-contract-v2.md`](post-training/data/thelook-queryspec-contract-v2.md) 与 [`post-training/data/thelook-renderer-contract-v2.md`](post-training/data/thelook-renderer-contract-v2.md)：TheLook v2 的独立结构化施工图、fail-closed 边界与 deterministic Gold SQL 编译合同。
- [`post-training/data/thelook-cross-schema-final-test-v2.md`](post-training/data/thelook-cross-schema-final-test-v2.md)：冻结的 600 条 protected cross-schema final test、覆盖、真实数据库准入与下一步 matching 边界。
- [`post-training/data/thelook-v2-matching-evaluation-contract.md`](post-training/data/thelook-v2-matching-evaluation-contract.md)：TheLook v2 的 Base/Adapter matching 三阶段隔离、完全一致的 Prompt/解码/模型条件、GPU guard 和 Gold 后置评测入口；已完成生成与后置评测，结果见实验记录。
- [`post-training/experiments/thelook-v2-matching-v1.md`](post-training/experiments/thelook-v2-matching-v1.md)：600 条 protected TheLook v2 cross-schema Base/Adapter 对照的脱敏聚合结果、评测器语义修复、结论与适用边界。
- [`post-training/data/thelook-v2-metric-scenario-candidates.md`](post-training/data/thelook-v2-metric-scenario-candidates.md)：v2 冻结前的只读画像和候选设计历史记录；当前规范以 v2 指标/QuerySpec/renderer/final-test 合同为准。
- [`post-training/data/olist-queryspec-coverage-seed-manifest-v1.md`](post-training/data/olist-queryspec-coverage-seed-manifest-v1.md)：小型静态 coverage seed 的输入边界、覆盖分布、split 限制与 protected-summary 准入条件；其中 6 条已作为外部 Gold 准入批，仍不是训练数据。
- [`post-training/data/olist-protected-family-summary-export-v1.md`](post-training/data/olist-protected-family-summary-export-v1.md)：仓库外 protected family fingerprint summary 的受限导出与证据边界；当前 v1 外部 summary/evidence 已经由受限流程导出。
- [`sql-policy.md`](sql-policy.md)：`sqlglot` AST 策略和 SQL 执行边界。
- [`data-loading.md`](data-loading.md)：数据清洗、加载和版本化说明。
- [`local-postgres.md`](local-postgres.md)：本机 PostgreSQL 服务与连接约定。
- [`demo-script.md`](demo-script.md)：W4-04 演示脚本，同时是当前产品的全量功能测试说明。
- [`architecture/product-request-architecture-v1.md`](architecture/product-request-architecture-v1.md)：当前请求、SQL 治理、会话结果和本地模型的位置。
- [`architecture/execution-row-cap-v1.md`](architecture/execution-row-cap-v1.md)：1,000 行执行上限下，完整与截断如何告知用户。
- [`interview-qa-v1.md`](interview-qa-v1.md)：SFT、LoRA、AdamW、DPO、泄漏和 Gold denotation 的简答。
- [`post-training/experiments/project-v1-model-card.md`](post-training/experiments/project-v1-model-card.md)：一页模型实验卡，数字回链到主模型基线。

### 评测与验收

- [`evaluation.md`](evaluation.md)：当前评测分层、运行命令和证据边界。
- [`first-round-acceptance.md`](first-round-acceptance.md)：第一轮功能验收矩阵。
- [`verification-text-to-sql-v2.md`](verification-text-to-sql-v2.md)：Text-to-SQL v2 确定性链路验证。
- [`verification-2026-08-03.md`](verification-2026-08-03.md)、[`verification-2026-08-03-v2.md`](verification-2026-08-03-v2.md)、[`verification-2026-08-06.md`](verification-2026-08-06.md)：按日期保存的历史验证证据，不合并为一个“最新结果”。
- [`official-spider-test-suite.md`](official-spider-test-suite.md)：官方 Spider Test Suite 的代码、数据和可比性边界。
- [`frozen-sqlite-baseline.md`](frozen-sqlite-baseline.md)：SQLite 离线基线及其固定协议。
- [`sqlite-benchmark-adapter.md`](sqlite-benchmark-adapter.md)：SQLite/Spider 离线评测适配器边界。

评测文档中的数字只对各自写明的数据版本、模型、Prompt、解码和评测器负责；不能跨报告直接比较。

### 后训练研究

后训练文档已经完成目录化，唯一规范入口是 [`post-training/README.md`](post-training/README.md)：

- `post-training/learning/`：概念、代码审阅和用户问答；当前重点是逐小单元审查真实训练代码。项目进度和实验状态不放入学习笔记，统一看 `PROJECT.md` 及飞书项目文档。
- `post-training/data/`：数据协议、holdout 隔离、领域训练接口合同和覆盖矩阵；当前 Olist 领域数据以 [v3.1 平衡纯中文 release](post-training/data/olist-v3-balanced-release-v1-1.md)、[v3.1 修复合同](post-training/data/olist-v3-1-balance-and-surface-repair-contract.md)、[训练/评测冻结合同](post-training/data/olist-v3-1-sql-only-training-evaluation-contract.md)、[v3 指标合同](metric-contracts/olist-metrics-v3-proposal.md) 和 [QuerySpec/renderer 设计](post-training/data/olist-queryspec-renderer-design-v1.md) 为入口。四指标 v1 矩阵与 v3.0 release 保留为历史快照，不能作为当前物化输入。
- `post-training/experiments/`：实验台账，只记录配置、聚合结果和结论。
- `post-training/archive/`：不再作为实时状态依据的旧路线和旧笔记。

根目录以下文件保留为兼容入口，不再追加新内容：

- [`post-training-index.md`](post-training-index.md)
- [`post-training-learning-guide.md`](post-training-learning-guide.md)
- [`post-training-learning-notes-v1.md`](post-training-learning-notes-v1.md)
- [`post-training-learning-roadmap.md`](post-training-learning-roadmap.md)
- [`post-training-learning-walkthrough-v1.md`](post-training-learning-walkthrough-v1.md)
- [`post-training-experiment-log.md`](post-training-experiment-log.md)
- [`post-training-data-protocol.md`](post-training-data-protocol.md)

根目录中带 `post-training-` 前缀、但不在上述兼容清单内的文件是历史实验报告，例如 Spider 全量分析、Olist 业务迁移、Base/Adapter 对照和数据候选审计。它们保留原路径与内容，避免破坏 GitHub、飞书和实验报告中的引用；后续新实验只在 `post-training/experiments/log.md` 增加索引，并单独创建带版本号的报告。

### 其他目录

- `docs/qlora-environment.md`：后训练环境安装与 GPU 约定。
- `docs/text-to-sql-data-acquisition.md`：公开数据集获取和来源说明。
- `docs/spider-1.0-data-provenance.md`：Spider 版本与文件哈希溯源。
- `docs/resume-project-entry.md`：简历项目条目草稿。

## 当前整理决定

本轮完成导航和生命周期治理，不批量移动长文档。已有文档包含相对链接、飞书同步记录和历史实验路径；物理迁移应单独建立任务，迁移后执行本地 Markdown 链接检查和脚本入口回归。未发现可以在不损失唯一证据的前提下安全删除的大批文件，因此历史验证和实验报告继续保留，根目录兼容入口不再追加正文。`github-research-output/` 是 Git 忽略的调研中间产物，不属于源码或规范文档，暂不纳入本文档树。

今后的规则是：实时项目事实更新 `PROJECT.md` 和飞书项目文档；后训练本地阶段地图更新 `post-training/README.md`，实验聚合事实更新 `post-training/experiments/log.md`；学习问答更新 `post-training/learning/review-*.md` 和飞书学习笔记；历史实验只追加新的版本报告，不覆盖旧结论。
