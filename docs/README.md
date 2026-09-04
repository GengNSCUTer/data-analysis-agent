# 文档导航

这里是 `data-analysis-agent` 的文档总入口。文档数量较多，是因为项目同时包含产品设计、运行时架构、数据与运维、评测证据以及离线后训练研究；这些内容的版本、证据和读者不同，不应强行合并成一篇长文。

## 先看什么

| 目的 | 入口 | 文档性质 |
| --- | --- | --- |
| 了解项目目标、当前阶段和技术决策 | [`../PROJECT.md`](../PROJECT.md) | 当前项目基线 |
| 了解目录、模块所有权和代码审阅顺序 | [`architecture/repository-map.md`](architecture/repository-map.md) | 当前架构地图 |
| 了解后训练项目状态和学习入口 | [`post-training/README.md`](post-training/README.md) | 后训练唯一入口 |
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
- [`architecture/data-model.md`](architecture/data-model.md)：运行时数据模型和持久化边界。
- [`data-dictionary.md`](data-dictionary.md)：当前工作区字段字典。
- [`metric-catalog.md`](metric-catalog.md)：当前十项指标的简表与机器可读 Catalog 入口。
- [`metric-contracts/olist-metrics-v2.md`](metric-contracts/olist-metrics-v2.md)：十项指标的公式、分母、归属边界和数据库回归证据。
- [`post-training/data/olist-queryspec-renderer-design-v1.md`](post-training/data/olist-queryspec-renderer-design-v1.md)：Olist 领域 SFT 中离线 QuerySpec 与 deterministic PostgreSQL Gold SQL renderer 的职责边界及实现状态；40 条 Gold 已准入，最终 SFT 输入请看下一项。
- [`post-training/data/olist-pilot-v1-sft-data.md`](post-training/data/olist-pilot-v1-sft-data.md)：第一版正式 Olist SFT Pilot，记录 40 条准入、真实运行时 Prompt、`24/8/8` family-isolated split 和 `2304` 无截断长度合同；尚未训练。
- [`post-training/data/olist-queryspec-coverage-seed-manifest-v1.md`](post-training/data/olist-queryspec-coverage-seed-manifest-v1.md)：小型静态 coverage seed 的输入边界、覆盖分布、split 限制与 protected-summary 准入条件；其中 6 条已作为外部 Gold 准入批，仍不是训练数据。
- [`post-training/data/olist-protected-family-summary-export-v1.md`](post-training/data/olist-protected-family-summary-export-v1.md)：仓库外 protected family fingerprint summary 的受限导出与证据边界；当前 v1 外部 summary/evidence 已经由受限流程导出。
- [`sql-policy.md`](sql-policy.md)：`sqlglot` AST 策略和 SQL 执行边界。
- [`data-loading.md`](data-loading.md)：数据清洗、加载和版本化说明。
- [`local-postgres.md`](local-postgres.md)：本机 PostgreSQL 服务与连接约定。
- [`demo-script.md`](demo-script.md)：可信 Demo 的演示步骤。

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
- `post-training/data/`：数据协议、holdout 隔离、领域训练接口合同和覆盖矩阵；当前 Olist 领域数据以 [正式 Pilot v1](post-training/data/olist-pilot-v1-sft-data.md)、[十指标合同](metric-contracts/olist-metrics-v2.md) 和 [QuerySpec/renderer 设计](post-training/data/olist-queryspec-renderer-design-v1.md) 为入口。四指标 v1 矩阵保留为历史快照，不能用于物化。
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

本轮只做导航和入口治理，不批量移动长文档。原因是已有文档包含相对链接、飞书同步记录和历史实验路径；物理迁移应单独建立任务，迁移后执行本地 Markdown 链接检查和脚本入口回归。`github-research-output/` 是 Git 忽略的调研中间产物，不属于源码或规范文档，暂不纳入本文档树。

今后的规则是：实时项目事实更新 `PROJECT.md` 和飞书项目文档；后训练本地阶段地图更新 `post-training/README.md`，实验聚合事实更新 `post-training/experiments/log.md`；学习问答更新 `post-training/learning/review-*.md` 和飞书学习笔记；历史实验只追加新的版本报告，不覆盖旧结论。
