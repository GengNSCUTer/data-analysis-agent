# 评测说明

评测分为三层，不能混淆：确定性路由/QueryPlan 评测验证版本化语义合同；SQL/数据库和浏览器
回归验证执行边界与嵌入交互；在线模型评测才验证真实 SSE 的工具调用、SQL 语义和中文结论。
前两层可以在本地或 CI 中重复运行，在线模型评测需要本地模型密钥，不能在 GitHub Actions
中运行，也不能用 SQL 可执行代替语义正确。

## Text-to-SQL v2 确定性评测

`evals/cases/text_to_sql_v2.yaml` 是当前版本化 golden 集，共 60 条用例，覆盖：

- help、指标定义、通用业务/知识问题与混合请求；
- 单指标、时间粒度、地区/品类/支付方式维度和多指标概览/分组计划；
- 缺少时间、指标、比较基线、无可信结果摘要的追问；
- WorkingMemory 跨轮恢复、越权/敏感请求、不支持能力和边界输入。

运行离线路由与 QueryPlan 评测：

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python \
  scripts/run_text_to_sql_evaluation.py \
  --output evals/reports/text_to_sql_v2_deterministic.json
```

2026-08-06 结果为 **60/60 passed**。报告只保存 case ID、类别、期望/实际结构化路由结果、
QueryPlan、结果列合同和耗时，不保存原始问题、模型回答、API Key、数据库行或查询结果；
`evals/reports/` 已被 Git 忽略。该结果证明确定性路由/合同没有回归，不是 SiliconFlow 在线
模型准确率。

## 嵌入窗口浏览器回归

在 Trusted Demo 已运行于 `127.0.0.1:32010` 时执行：

```bash
RUN_VANNA_E2E=1 RUN_PROJECT_DB=1 \
VANNA_E2E_BASE_URL=http://127.0.0.1:32010 \
  /disk2/gengnan/conda_envs/data-analysis-agent/bin/python -m pytest -q \
  tests/e2e/test_trusted_embedded_window.py
```

2026-08-06 结果为 **9 passed**，覆盖拖拽、八方向缩放、最小化/恢复、窄窗口长 Markdown 历史、
表格语义化渲染、移动端无横向溢出、图表尺寸和会话历史隔离。Web Component 构建使用
`npm run build`，本轮 38 个模块转换成功。

## 第一轮 SQL/数据库 golden

运行既有确定性 SQL 与数据库评测：

```bash
python scripts/run_project_evaluation.py --database \
  --output /tmp/first-round-deterministic.yaml
python scripts/run_demo_scenario_evaluation.py --database \
  --output /tmp/demo-scenarios.yaml
```

`evals/cases/v1.yaml` 包含 60 条用例：指标 12、多表关联 8、趋势 6、口径解释 4、歧义 4、
安全/边界 26。既有报告记录：26/26 安全策略预期通过，且本地 PostgreSQL golden SQL
通过。该结果不是 LLM 准确率。

`evals/cases/demo_scenarios.yaml` 额外固定了 3 条面试 Demo 场景：州前五、品类前十和指标概览。
它们验证固定问题、允许角色、指标口径、结果列、图表要求与 PostgreSQL golden 结果是否一致。
该资产用于验证场景稳定性，不用于声称在线模型批量语义准确率。

## SiliconFlow 在线人工标签评测（2026-08-18）

`evals/cases/text_to_sql_online_v1.yaml` 从 v2 的 60 条版本化合同中选择了 24 条有代表性的
请求：10 条不查库边界（Catalog、通用问答、澄清、安全/不支持）和 14 条数据查询（单指标、
时间/维度、多指标、结果合同及边界措辞）。通过本地 Trusted Demo 的真实 FastAPI/SSE 路径，
以 `analyst` 演示会话调用 SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`；它测到的是产品实际
使用的路由、Catalog、预算、SQL Policy、PostgreSQL、ResultContract 和 SSE 链路，而不是直接
调用供应商私有 API。

运行器 `scripts/run_online_text_to_sql_evaluation.py` 只把 request ID、结构化运行证据和回答
哈希写入被忽略的本地报告；不写入问题原文、回答原文、SQL、数据行、cookie 或密钥。每条均记录
路由、澄清、SQL 可执行、指标语义、结果合同、权限合规和回答有据七项标签，以及工具/SQL/图表
调用次数、修复次数、客户端时延、阶段时延、token 值或 `unknown`。其中路由/审计/合同来自运行时
证据，指标语义和最终表述由人工复核；二者不能互相替代。

本次为 **24/24** Agent Run，所有人工标签已完成：路由 **23 pass / 1 fail**；三条需要澄清的
请求均正确澄清；13 条真实查询至少有一条允许 SQL 并可执行；可判定的指标语义为 **12 pass / 1 fail**；
结果合同 **11 pass / 2 fail**（两条首次 SQL 合同正确、但模型随后发出无关且缺合同列的 SQL，被
`ResultValidator` 安全阻断）；权限合规 **24 pass**；回答有据 **17 pass / 7 fail**。唯一未完成
请求是 `multi_003`：Catalog slice 漏掉实际可用的品类/评价 Join 证据，模型错误称没有该路径，
并在 180 秒客户端超时前未生成 SQL。

本轮没有 `repair_attempted=true`，因此不能用它支持“应把一次修复扩展到两次”的结论。两个
被 AST Policy 拒绝的初始敏感 `order_id` 投影属于模型后续工具调用，不是修复生命周期。7 条
“回答无据”包括把 Olist 巴西雷亚尔错误渲染为人民币符号、把非单调月序列写成“持续上升”，
以及未冻结支付分期归属口径；这些问题即使 SQL 能执行也必须保留为失败。24 条均记录 token 字段，
但当前 provider/Vanna 在 22 条中未回传 usage，故标为 `unknown` 而非 0；只有两条通用问答
分别报告 1,343 与 1,213 total tokens。总客户端时延为 1,374,945 ms；每条只运行一次且样本很小，
本轮不宣称 P50/P95 或在线总体准确率。

建议的下一轮优化顺序是：修复 BRL/R$ 格式化；一旦已有通过的 `ResultContract` 就阻止无关后续
SQL；增强多指标请求的 Catalog slice；在 Catalog 中显式冻结支付方式的拆分订单归因；再补 provider
usage 采集并对这些失败样本做固定回归。
