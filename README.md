# 可信数据分析智能体

面向经营分析场景的嵌入式 Text-to-SQL Demo。用户通过原生 Vanna `<vanna-chat>` 提出中文问题，
系统在受控 PostgreSQL `analytics` Schema 中生成并执行只读 SQL，返回结论、结果表、受约束的
Plotly 图表，以及数据/指标版本和最终 SQL 证据。

项目基于 Olist Brazilian E-Commerce 公开数据集构建。它是巴西电商公开数据，不是中国真实本地生活平台数据。

## 已实现

- **受控 Text-to-SQL**：所有模型 SQL 经 `sqlglot` AST 校验，只允许单条只读查询、允许的
  Schema/表/列和字面量 LIMIT；写操作、多语句、越权对象、敏感标识投影及危险函数被拒绝。
- **数据库纵深防御**：`daa_analytics_reader` 只读查询 `analytics`；`daa_app_writer` 仅写入
  `app.query_audits`。策略拒绝和执行结果均有审计记录。
- **可追溯结果**：宿主页展示数据/指标版本、角色和审计摘要；聊天结果保留表格、SQL 和流式状态。
- **受控图表**：图表仅可读取当前 `run_sql` 产生的 `query_results_<id>.csv`，限制 200 行、3 列。
- **嵌入式交互**：无框架宿主页中嵌入 `<vanna-chat>`；桌面端支持拖拽、缩放、最小化/最大化和位置记忆，移动端自适应。
- **演示级角色**：签名短期 cookie 支持预置 `analyst` / `admin` 切换，并真正影响 SQL 限制与审计范围；它不是密码登录、OAuth 或生产认证。
- **可复现评测**：60 条确定性策略/数据用例，以及 3 条固定演示场景的 SQL 语义与 PostgreSQL golden 校验。

## 架构

```text
既有业务页 + 嵌入式 <vanna-chat>
            | SSE
FastAPI + Vanna Agent + 签名演示会话
            |
sqlglot AST 策略 -> SecurePostgresRunner -> PostgreSQL analytics (只读角色)
            |                                      |
            +-> app.query_audits (应用写角色)       +-> Olist 分析表
```

完整决策、数据来源和限制见 [PROJECT.md](PROJECT.md)。

## 快速运行

前置条件：Python 3.12、Node.js 22、已加载的项目 PostgreSQL 数据库，以及一个 OpenAI-compatible
模型 API Key。原始 Olist 数据和 `.env` 均不会进入 Git。

```bash
conda activate data-analysis-agent
pip install -e ".[test]"
cd frontends/webcomponent
npm install --package-lock=false
npm run build
cd ../..
python examples/trusted_olist_web_demo.py
```

根目录 `.env` 至少需要：

```dotenv
SILICONFLOW_API_KEY=your_key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V4-Flash
```

服务默认仅监听 `127.0.0.1:32010`，打开 `http://127.0.0.1:32010/embedded-demo`。远程服务器访问可使用：

```bash
ssh -L 32010:127.0.0.1:32010 ligengnan@202.38.247.145
```

## 推荐演示

首屏的三个示例问题均有版本化场景契约：

1. 州前五：按客户州统计有效订单数前五名，并生成柱状图。
2. 品类前十：统计有效订单数最多的前十个商品品类。
3. 指标概览：概览 GMV、有效订单数、平均履约天数和好评率，并说明统计口径。

每条场景的预期数据、来源表、排序、图表要求和证据要求在
[evals/cases/demo_scenarios.yaml](evals/cases/demo_scenarios.yaml)。完整讲解顺序见
[docs/demo-script.md](docs/demo-script.md)。

## 验证

```bash
# 无数据库、无在线模型的确定性用例
pytest -q tests/test_sql_policy.py tests/test_demo_session.py tests/test_demo_scenarios.py
python scripts/run_project_evaluation.py --output /tmp/project-evaluation.yaml
python scripts/run_demo_scenario_evaluation.py --output /tmp/demo-scenarios.yaml

# 项目专属 PostgreSQL 的 golden 校验
RUN_PROJECT_DB=1 pytest -q tests/test_postgres_runner.py tests/test_demo_scenarios.py
python scripts/run_project_evaluation.py --database --output /tmp/project-evaluation-db.yaml
python scripts/run_demo_scenario_evaluation.py --database --output /tmp/demo-scenarios-db.yaml

# 已启动服务的浏览器回归
RUN_VANNA_E2E=1 VANNA_E2E_BASE_URL=http://127.0.0.1:32010 \
  pytest -q -m integration tests/e2e/test_trusted_embedded_window.py
```

GitHub Actions 仅执行不依赖私有模型密钥和本地数据库的确定性测试与 Web Component 构建。

## 诚实边界

- 不是生产系统：没有真实身份提供方、组织级行权限、限流、异步导出、多实例协调或多数据库方言支持。
- `analyst` / `admin` 选择器仅用于验证服务端策略差异；不证明登录身份。
- 数据集语义和指标目录仍标记为 `0.1-draft`，数据 golden 用于检测转换和 SQL 漂移。
- 确定性评测不等于在线 LLM 语义准确率；未记录模型、提示、运行结果和人工判定时，不报告准确率。

## 相关文档

- [数据与字段字典](docs/data-dictionary.md)
- [指标目录](docs/metric-catalog.md)
- [SQL 策略](docs/sql-policy.md)
- [评测说明](docs/evaluation.md)
- [演示脚本](docs/demo-script.md)
- [项目定位与简历表述](docs/PORTFOLIO_POSITIONING.md)

## 数据与许可证

Olist Brazilian E-Commerce 数据集来自 Kaggle，许可证为 CC BY-NC-SA 4.0。原始文件不提交至本仓库；
数据集清单、转换逻辑、字段字典和可复现验证脚本已保留。详见
[data/manifest/datasets.yaml](data/manifest/datasets.yaml)。
