# 项目级验证记录（2026-08-03）

本记录审查可信数据分析智能体的五条交付主线。结论只基于当前仓库、项目专属 PostgreSQL、
运行中的本机服务、浏览器回归和 GitHub Actions。

| 主线 | 结论 | 证据 |
| --- | --- | --- |
| 嵌入式图表与结果展示 | 通过 | Playwright 覆盖拖拽、缩放、最大化、390px 窄屏、图表容器宽度同步和无横向溢出。 |
| 演示级身份与权限 | 通过（Demo 范围） | HMAC 签名 cookie、篡改/过期回退、旧请求头不提权、角色切换 E2E；SQL 策略和审计 API 使用同一身份解析。 |
| 固定演示场景与评测 | 通过 | 60 条确定性用例、3 条版本化场景契约、项目 PostgreSQL golden SQL。 |
| README、演示与简历材料 | 通过 | README、Demo 脚本、作品集定位、简历条目草稿和验收矩阵均指向同一事实边界。 |
| 发布与回归 | 通过 | GitHub Actions 的 Project Quality Checks 通过；本地运行浏览器、确定性和数据库测试。 |

## 本次运行结果

```text
RUN_VANNA_E2E=1 pytest -m integration tests/e2e/test_trusted_embedded_window.py: 5 passed
pytest test_demo_session/test_demo_scenarios/test_sql_policy/test_visualization_policy/test_trusted_workflow/test_project_evaluation: 30 passed, 1 skipped
RUN_PROJECT_DB=1 pytest tests/test_postgres_runner.py tests/test_demo_scenarios.py: 6 passed
run_project_evaluation.py --database: 60 cases, 26/26 safety expectations, database golden passed
run_demo_scenario_evaluation.py --database: 3 scenarios, database golden passed
```

## 未覆盖的事项

- 真实身份提供方、组织级行权限、生产部署与多实例运行不在本项目 v1 范围。
- 在线模型只验证代表性真实链路；尚未建立批量自然语言语义准确率基准，因此不报告准确率或性能数字。
- Olist 指标目录仍为 `0.1-draft`，golden 结果用于检测转换和 SQL 语义漂移，不等同于业务方正式口径。
