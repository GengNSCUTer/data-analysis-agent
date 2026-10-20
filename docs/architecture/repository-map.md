# 项目代码目录地图

**状态：** 2026-08-30 初稿，2026-10-20 更新产品安装和启动入口。本文描述职责和审阅顺序，不代表启动了新训练。

## 1. 根目录先分三类

这个仓库不是一个从零开始的单一 Python 应用，而是“上游 Vanna 源码 + 本项目定制代码 + 可复现实验资产”共同组成。先区分所有权，才能知道哪些地方可以改、哪些地方只应调用。

| 目录 / 文件 | 所有权与职责 | 首次审阅优先级 |
| --- | --- | --- |
| `src/vanna/` | 上游 Vanna 2.0.2 框架：Agent、工具、SSE、模型集成等。项目通过其扩展点接入，不把它误当作自研业务逻辑。 | 中；先理解调用边界，再按需深入。 |
| `src/data_analysis_agent/` | 本项目自有 Python 核心：可信 Text-to-SQL、语义 Catalog、SQL Policy、PostgreSQL、审计、图表、离线后训练评测。 | 最高。 |
| `frontends/webcomponent/` | Vanna 原生 `<vanna-chat>` Web Component 的 TypeScript 源码、Storybook 和前端测试。 | 中；后端链路读清后再看。 |
| `examples/` | 示例页面和上游示例；产品 Python 入口已迁到 `src/data_analysis_agent/server.py`。 | 中；先从已安装的产品入口理解运行时装配。 |
| `data/` | 可提交的数据 manifest、Catalog、DDL/转换逻辑与小型 fixture；不含 Olist 原始数据或数据库 dump。 | 高；语义和数据版本的事实来源。 |
| `evals/` | 版本化用例、manifest、golden SQL、评测脚本输入和安全聚合报告。 | 高；每一项质量主张都应能回到这里。 |
| `scripts/` | 项目 CLI、评测入口、数据转换与运维脚本。`scripts/post_training/` 是后训练的规范实现位置。 | 高。 |
| `tests/` | Python 测试；当前大多平铺并按被测模块命名。 | 与所读代码同步阅读。 |
| `infra/` | PostgreSQL DDL、迁移、数据加载和服务配置。 | 高；理解真实数据与权限边界时阅读。 |
| `docs/`、`PROJECT.md`、`AGENTS.md` | 架构决策、学习材料和协作/安全约束。 | 最高；先读再改。 |
| `.github/` | CI 任务及仓库自动化。 | 中；提交前或排查 CI 时阅读。 |
| `plan/` | 历史或阶段性计划，不应替代 `PROJECT.md` 的当前事实。 | 低。 |
| `papers/`、`notebooks/` | 背景研究与探索性材料，不是运行时依赖。 | 低。 |
| 仓库外调研归档 | 原 `github-research-output/`（793 MB）已移到仓库外可恢复归档，不参与运行或测试。 | 学习当前产品无需阅读。 |

根目录 `pyproject.toml` 保留 fork 的发行名 `vanna` 和版本 `2.0.2`，安装包同时包含 `vanna` 与 `data_analysis_agent`。产品入口为 `data-analysis-agent`，管理员初始化入口为 `data-analysis-agent-init`；安装与资源打包见 [部署说明](../deployment.md)。运行时不再依赖从仓库根目录设置 `PYTHONPATH`。

## 2. 本项目 Python 核心：`src/data_analysis_agent/`

目录目前采用平铺模块。平铺并不等于混乱：模块数量仍可控，而且测试、脚本和运行时以模块名直接关联。先按下面的逻辑边界理解，不要急于挪动文件。

| 逻辑组 | 主要文件 | 职责 |
| --- | --- | --- |
| 启动与安装 | `server.py`、`deployment.py`、`runtime_paths.py`、`_build.py` | 应用装配、独立管理员初始化、持久状态路径与安装包资源。 |
| 请求与会话 | `chat_runtime.py`、`semantic_intent.py`、`analysis_state.py`、`conversation_store.py`、`analysis_context.py`、`budget.py`、`llm_observability.py` | 解释当前意图、维护分析状态和历史摘要、限制预算并记录模型观测；旧语言分类不作为在线入口。 |
| 语义与计划 | `semantic_catalog.py`、`metric_context.py`、`query_plan.py`、`workspace.py` | 从工作区配置和 Catalog 得到指标、可见表列/Join、查询粒度和版本化结果合同。 |
| 安全执行与审计 | `sql_policy.py`、`postgres_runner.py`、`result_validator.py`、`sql_repair.py`、`trusted_sql_tool.py`、`run_recorder.py` | AST 约束、PostgreSQL 双角色执行、结果合同、一次受控修复和审计证据。 |
| 呈现与 Demo | `chart_contract.py`、`visualization.py`、`trusted_workflow.py`、`demo_session.py` | 约束图表和结果呈现，定制 Demo 工作流与演示身份。 |
| 离线 Text-to-SQL 研究 | `spider_sft_format.py`、`frozen_sqlite_baseline.py`、`sqlite_benchmark.py`、`spider_test_suite.py`、`post_training_comparison.py`、`candidate_sql_generator.py`、`olist_candidate_sql_evaluation.py`、`external_artifacts.py` | 建模输入格式、候选生成、SQLite/Spider 诊断、业务迁移评测和仓库外原始产物边界；其中 `candidate_sql_generator.unwrap_sql_completion()` 是所有离线 completion 的唯一、有界展示包装清洗入口。 |

产品运行时最值得先掌握的一条线是：

```text
data-analysis-agent -> src/data_analysis_agent/server.py:create_app()
-> chat_runtime.py
-> semantic_intent.py / analysis_state.py / semantic_catalog.py / query_plan.py
-> trusted_sql_tool.py / sql_policy.py / postgres_runner.py / result_validator.py
-> chart_contract.py / visualization.py / run_recorder.py
```

后训练不是这条生产执行链的替代品。后训练具体审阅顺序见 [`../post-training/learning/code-review-guide-v1.md`](../post-training/learning/code-review-guide-v1.md)。

## 3. 建议保留和后续可优化点

### 当前结构中应保留的边界

- `src/vanna/` 与 `src/data_analysis_agent/` 必须继续区分：前者是上游框架，后者才是项目的可信数据分析能力。将二者混合会让升级、问题归因和简历表述都变得模糊。
- `scripts/post_training/` 与生产运行时必须继续分开：离线实验能够读取 Spider/模型资产，但不能拥有线上数据库执行权限或绕过结果合同。
- 后训练只使用 `scripts/post_training/` 的规范入口。根目录17个同名Python转发器、辅助兼容模块和3个shell包装器已删除；测试与保留启动器直接调用规范位置。历史报告保留原命令，重跑时使用规范模块，不恢复兼容层。

### 先记录、暂不执行的整理建议

| 优先级 | 建议 | 理由与前提 |
| --- | --- | --- |
| 中 | 为根目录 `scripts/` 增加总览 README，并在后训练审阅完成后决定是否将非后训练脚本分为 `scripts/evaluation/`、`scripts/data/`、`scripts/runtime/`。 | 当前后训练已经分层，其他项目脚本仍较平铺；先完成调用方审阅，避免移动后难以比对历史命令。 |
| 中 | 有实际查找困难时，逐步把根目录 `docs/` 的项目自有长文按职责分类，同时直接更新引用。 | 当前已有较多历史链接；只能分批移动并检查受影响链接，不为目录整齐新增兼容页。 |
| 低 | 将 `tests/` 后续按 `runtime/`、`post_training/`、`e2e/` 分组。 | 当前按模块名平铺仍可搜索；移动不会直接提高质量，等审阅明确稳定边界后再做。 |
| 低 | 评估是否把 `src/data_analysis_agent/` 切分为 `runtime/`、`semantic/`、`offline/` 子包。 | 有约 70 个测试和多处相互导入；现在移动只会扩大回归面。应先完成模块依赖审阅并建立公开接口。 |

`frontends/webcomponent/node_modules/`、`frontends/webcomponent/dist/`、Python `__pycache__/` 和各种模型/数据/实验目录均不应进入 Git；当前它们是本地构建或运行产物，不应作为源码目录重构对象。

2026-10-20归档位置为仓库外 `data-analysis-agent-data/cleanup-20261020-BlC0evEZ/`：调研克隆、
12 MB旧build产物和原型SQLite库分别存为 `research-clones/`、`build-output/`、`vanna_demo.sqlite`。
它们可从归档恢复。当前服务使用的JS/resources、正式数据、模型和实验目录未清理。
