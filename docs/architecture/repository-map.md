# 项目代码目录地图

**状态：** 2026-08-30 基于当前 `main` 代码树整理。本文只描述职责和审阅顺序，不代表启动了新训练、新评测或重构。

## 1. 根目录先分三类

这个仓库不是一个从零开始的单一 Python 应用，而是“上游 Vanna 源码 + 本项目定制代码 + 可复现实验资产”共同组成。先区分所有权，才能知道哪些地方可以改、哪些地方只应调用。

| 目录 / 文件 | 所有权与职责 | 首次审阅优先级 |
| --- | --- | --- |
| `src/vanna/` | 上游 Vanna 2.0.2 框架：Agent、工具、SSE、模型集成等。项目通过其扩展点接入，不把它误当作自研业务逻辑。 | 中；先理解调用边界，再按需深入。 |
| `src/data_analysis_agent/` | 本项目自有 Python 核心：可信 Text-to-SQL、语义 Catalog、SQL Policy、PostgreSQL、审计、图表、离线后训练评测。 | 最高。 |
| `frontends/webcomponent/` | Vanna 原生 `<vanna-chat>` Web Component 的 TypeScript 源码、Storybook 和前端测试。 | 中；后端链路读清后再看。 |
| `examples/` | 可运行 Demo 入口，例如可信 Olist Web Demo。 | 高；用于从启动脚本反向定位运行时装配。 |
| `data/` | 可提交的数据 manifest、Catalog、DDL/转换逻辑与小型 fixture；不含 Olist 原始数据或数据库 dump。 | 高；语义和数据版本的事实来源。 |
| `evals/` | 版本化用例、manifest、golden SQL、评测脚本输入和安全聚合报告。 | 高；每一项质量主张都应能回到这里。 |
| `scripts/` | 项目 CLI、评测入口、数据转换与运维脚本。`scripts/post_training/` 是后训练的规范实现位置。 | 高。 |
| `tests/` | Python 测试；当前大多平铺并按被测模块命名。 | 与所读代码同步阅读。 |
| `infra/` | PostgreSQL DDL、迁移、数据加载和服务配置。 | 高；理解真实数据与权限边界时阅读。 |
| `docs/`、`PROJECT.md`、`AGENTS.md` | 架构决策、学习材料和协作/安全约束。 | 最高；先读再改。 |
| `.github/` | CI 任务及仓库自动化。 | 中；提交前或排查 CI 时阅读。 |
| `plan/` | 历史或阶段性计划，不应替代 `PROJECT.md` 的当前事实。 | 低。 |
| `papers/`、`notebooks/` | 背景研究与探索性材料，不是运行时依赖。 | 低。 |
| `github-research-output/` | Git 忽略的调研中间产物；当前约数百 MB，不是项目源代码、测试或运行时资产。 | 不读代码时可忽略。 |

根目录的 `pyproject.toml` 仍是上游 Vanna 的包配置，发布模块为 `vanna`。项目自有的 `data_analysis_agent` 是同一仓库中的内部包，而不是独立发布包；这解释了为什么两者都位于 `src/`。

## 2. 本项目 Python 核心：`src/data_analysis_agent/`

目录目前采用平铺模块。平铺并不等于混乱：模块数量仍可控，而且测试、脚本和运行时以模块名直接关联。先按下面的逻辑边界理解，不要急于挪动文件。

| 逻辑组 | 主要文件 | 职责 |
| --- | --- | --- |
| 请求与会话 | `chat_runtime.py`、`question_router.py`、`working_memory.py`、`conversation_store.py`、`budget.py`、`llm_observability.py` | 决定是否查库、维护受控上下文、限制调用预算并记录模型观测。 |
| 语义与计划 | `semantic_catalog.py`、`metric_context.py`、`query_plan.py`、`workspace.py` | 从工作区配置和 Catalog 得到指标、可见表列/Join、查询粒度和版本化结果合同。 |
| 安全执行与审计 | `sql_policy.py`、`postgres_runner.py`、`result_validator.py`、`sql_repair.py`、`trusted_sql_tool.py`、`run_recorder.py` | AST 约束、PostgreSQL 双角色执行、结果合同、一次受控修复和审计证据。 |
| 呈现与 Demo | `chart_contract.py`、`visualization.py`、`text_to_sql_output.py`、`trusted_workflow.py`、`demo_session.py` | 约束图表和结果呈现，定制 Demo 工作流与演示身份。 |
| 离线 Text-to-SQL 研究 | `spider_sft_format.py`、`frozen_sqlite_baseline.py`、`sqlite_benchmark.py`、`spider_test_suite.py`、`post_training_comparison.py`、`candidate_sql_generator.py`、`olist_candidate_sql_evaluation.py`、`external_artifacts.py` | 建模输入格式、候选生成、SQLite/Spider 诊断、业务迁移评测和仓库外原始产物边界。 |

产品运行时最值得先掌握的一条线是：

```text
examples/trusted_olist_web_demo.py
-> chat_runtime.py
-> question_router.py / semantic_catalog.py / query_plan.py
-> trusted_sql_tool.py / sql_policy.py / postgres_runner.py / result_validator.py
-> chart_contract.py / visualization.py / run_recorder.py
```

后训练不是这条生产执行链的替代品。后训练具体审阅顺序见 [`../post-training/learning/code-review-guide-v1.md`](../post-training/learning/code-review-guide-v1.md)。

## 3. 建议保留和后续可优化点

### 当前结构中应保留的边界

- `src/vanna/` 与 `src/data_analysis_agent/` 必须继续区分：前者是上游框架，后者才是项目的可信数据分析能力。将二者混合会让升级、问题归因和简历表述都变得模糊。
- `scripts/post_training/` 与生产运行时必须继续分开：离线实验能够读取 Spider/模型资产，但不能拥有线上数据库执行权限或绕过结果合同。
- 根目录 `scripts/` 下的同名文件目前是兼容入口。它们不应继续承载新逻辑，但在已有文档、历史命令和 `screen` 启动器仍可能引用它们时，不应直接删除。

### 先记录、暂不执行的整理建议

| 优先级 | 建议 | 理由与前提 |
| --- | --- | --- |
| 高 | 将 `github-research-output/` 迁到仓库外的研究归档目录，或在 IDE 中排除索引。 | 它已被 Git 忽略但体积很大，会干扰搜索、备份和目录理解。迁移前需要确认不再有活动调研任务依赖其相对路径。 |
| 中 | 为根目录 `scripts/` 增加总览 README，并在后训练审阅完成后决定是否将非后训练脚本分为 `scripts/evaluation/`、`scripts/data/`、`scripts/runtime/`。 | 当前后训练已经分层，其他项目脚本仍较平铺；先完成调用方审阅，避免移动后难以比对历史命令。 |
| 中 | 逐步把根目录 `docs/` 的项目自有长文按 `architecture/`、`evaluation/`、`operations/` 分类，旧路径保留兼容页。 | 当前已有较多历史链接；只能分批移动并执行全量本地链接检查。 |
| 低 | 将 `tests/` 后续按 `runtime/`、`post_training/`、`e2e/` 分组。 | 当前按模块名平铺仍可搜索；移动不会直接提高质量，等审阅明确稳定边界后再做。 |
| 低 | 评估是否把 `src/data_analysis_agent/` 切分为 `runtime/`、`semantic/`、`offline/` 子包。 | 有约 70 个测试和多处相互导入；现在移动只会扩大回归面。应先完成模块依赖审阅并建立公开接口。 |

`frontends/webcomponent/node_modules/`、`frontends/webcomponent/dist/`、Python `__pycache__/` 和各种模型/数据/实验目录均不应进入 Git；当前它们是本地构建或运行产物，不应作为源码目录重构对象。
