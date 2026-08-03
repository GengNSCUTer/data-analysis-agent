# 第一轮验收矩阵

验收日期：2026-08-03。本文只记录已实际运行的证据，不把后续生产化能力表述为已完成。

| 需求 | 结论 | 证据 |
| --- | --- | --- |
| FR-01 中文问答 | 通过 | 真实 SiliconFlow SSE 对州订单问题返回流式状态、5 行表格和中文结论。 |
| FR-02 受控 Text-to-SQL | 通过 | `SecurePostgresRunner` 是 trusted demo 的唯一 SQL Runner；真实 SSE SQL 被审计。 |
| FR-03 指标语义层 | 通过 | `docs/metric-catalog.md`、`metric_context.py` 和版本化 evidence API 记录 GMV、订单、履约、好评率。 |
| FR-04 SQL 策略网关 | 通过 | `SqlPolicy` AST 白名单、单语句、危险函数、敏感投影、角色 LIMIT；60 用例中 26/26 安全策略预期通过。 |
| FR-05 数据库纵深防御 | 通过 | `daa_analytics_reader` 只读 analytics，`daa_app_writer` 只写 app 审计；真实集成测试 3/3 通过。 |
| FR-06 结构化结果 | 通过 | SSE 返回 DataFrame、最终中文结论和 ChartComponent；宿主页显示最终 SQL、数据/指标版本、行数和耗时。 |
| FR-07 审计与历史 | 通过 | `app.query_audits` 持久化用户、请求、SQL、策略、版本、耗时和行数；角色过滤 API 与宿主页已验证。 |
| FR-08 基础权限 | 通过（v1 范围） | analyst/admin 的表、字段、历史范围和最大返回行数不同：analyst 200、admin 1000；tenant 级 RLS 不在 v1。 |
| FR-09 可复现数据集 | 通过 | manifest、转换器、8 表 DDL、加载脚本、字段字典、合成 fixture、golden SQL 均在仓库；原始数据在仓库外。 |
| FR-10 离线评测 | 通过（确定性范围） | `evals/cases/v1.yaml` 有 60 条；评测器与报告记录 26/26 安全策略、golden SQL。未声称批量在线 LLM 语义准确率。 |

## 本轮命令结果

```text
pytest deterministic + demo scenario contracts: 88 passed
RUN_PROJECT_DB=1 pytest tests/test_postgres_runner.py tests/test_demo_scenarios.py: 6 passed
run_project_evaluation.py --database: 60 cases, 26/26 safety, golden passed
run_demo_scenario_evaluation.py --database: 3 scenarios, golden passed
```

真实在线 SSE 已验证两条代表性链路：州有效订单表格/中文结论，以及同一聚合结果的受控
Plotly 柱状图。浏览器验证宿主页可以显示角色、版本和最近审计，且无控制台错误。

## 已知限制

- 身份来自签名的短期演示 cookie，可验证 analyst/admin 差异，但不是生产登录；
- 行范围差异在 v1 体现为角色化返回行上限，尚未实现多租户/组织级 RLS；
- 在线模型只运行了代表性真实用例，未对 60 条自然语言问题声称语义准确率；
- 运行环境为本地 loopback PostgreSQL 和 screen 服务，尚未形成生产部署。
