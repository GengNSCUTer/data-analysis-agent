# 第二轮基础设施验证记录

> 日期：2026-08-03
> 范围：可信 Olist Vanna Demo 的 P0 会话、上下文和请求预算基础

## 本轮结论

本轮没有改写 Vanna Agent 核心，也没有引入新的前端框架、Redis、队列、多 Agent、MCP 或任意
Python 执行。项目在现有 Vanna 原生 `<vanna-chat>` 和 PostgreSQL 双角色边界上增加了持久会话、
运行台账、上下文裁剪和请求级预算的第一版适配器。

## 已验证内容

| 检查 | 命令/方式 | 结果 |
| --- | --- | --- |
| 预算与上下文单元测试 | `python -m pytest -q tests/test_budget.py tests/test_context_builder.py` | 11 passed |
| PostgreSQL 会话、SQL runner、路由与 run recorder 集成 | `RUN_PROJECT_DB=1 python -m pytest -q tests/test_postgres_conversation_store.py tests/test_postgres_runner.py tests/test_trusted_routes.py tests/test_postgres_run_recorder.py` | 8 passed |
| Python 编译 | `python -m compileall -q src/data_analysis_agent examples/trusted_olist_web_demo.py` | 通过 |
| Git 空白检查 | `git diff --check` | 通过 |
| Ruff lint/format | `python -m ruff check ...`；`python -m ruff format --check ...`（本轮涉及文件） | 全部通过 |
| 项目 CI 确定性测试子集 | `.github/workflows/tests.yml` 中的 Python 测试命令，加入本轮预算/上下文测试 | 101 passed, 1 skipped |
| GitHub Actions | `Project Quality Checks` run `30806496513`（commit `0a04c7d`） | success |
| 评测清单 | `scripts/run_project_evaluation.py`、`scripts/run_demo_scenario_evaluation.py` | 60 条用例唯一，26/26 安全预期通过；3 条 Demo 场景契约完整 |
| 真实 run/audit 关联 | 创建临时 conversation/run，执行只读 SQL 后查询 `app.agent_runs` 与 `app.query_audits` | `termination_reason=completed`，`tool_calls_used=1`，audit 的 `run_id` 正确回链 |
| 应用装配 | `from examples.trusted_olist_web_demo import create_app; create_app()` | 18 routes 注册成功，conversation 列表/详情/删除路由存在 |

真实 run/audit 验证使用唯一临时用户和会话，验证后已删除临时会话及其级联消息/运行记录；没有
把 API Key、数据库密码、原始 Olist 数据或查询结果写入仓库。

## 当前实现边界

- PostgreSQL `ConversationStore` 已执行用户归属校验、分页上限、malformed ID 拒绝和 owner-only 删除；
  路由 DTO 会隐藏 tool content/arguments。宿主页尚未接入历史列表、会话切换和新建会话控件。
- `ContextBudgetFilter` 已按完整轮次、消息数和字符数裁剪并记录 `context_truncated`，尚未生成
  结构化旧轮次摘要或按问题检索 Schema/指标。
- 总工具、`run_sql`、`visualize_data`、输入长度、上下文长度和输出 token 上限已接入 trusted Demo；
  provider 不返回 usage 时 token 成本保持未知。用户配额、费用台账和时延预算尚未实现。
- P1 的可回答性分类、澄清、一次受限 SQL 修复、结果级校验、选择性拒答和线上模型评测尚未实现。
- Ruff 已安装到项目专用 Conda 环境并通过本轮涉及文件的 lint/format；远端 GitHub Actions 已通过。

完整 `pytest -q` 没有作为本项目发布门：它会主动收集 Vanna 上游的可选集成测试，本环境缺少
Ollama、ChromaDB、Snowflake 等可选依赖/服务，并包含一个未提供 fixture 的上游测试，结果为
215 passed、57 skipped、38 failed、1 error。该失败集合与项目 CI 的确定性子集不同，也不是本轮
新增代码的回归证据；相关可选集成仍按各自 marker 单独运行。

## 下一步

先完成宿主页的 current/new/history 状态与 `conversation_id` 传递，再进入 Text-to-SQL P1：结构化
Schema/指标 Catalog、确定性澄清、一次执行错误修复和结果级安全校验。每一项都必须单独记录
执行正确性、业务语义正确性、安全合规、澄清质量、时延和 token 成本，不能用一次在线回答替代评测。
