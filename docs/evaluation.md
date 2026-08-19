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

## 2026-08-19 定向复测与超时观测

针对上一轮暴露的多跳 Catalog、重复 SQL、时间序列和多指标概览风险，从同一 v2 source suite
选择 6 条 case（`data_010`、`multi_003`、`data_014`、`data_016`、`data_005`、`multi_001`）。
运行器新增 `--allow-small-sample`，默认的 20--30 条批量评测门槛不变；定向 manifest 只保存
source ID 和 review focus，问题原文仍只存在于版本化 source suite。

本轮使用真实 FastAPI/SSE 和 SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`，6/6 Agent Run、0
客户端错误；支付归因样本正确零 SQL 澄清，5 条查库样本均只执行 1 条 SQL，SQL 可执行 5/5、
结果合同 5/5、权限合规 6/6、路由 6/6。provider usage 在 5 条查库样本中均为 `reported`，
总客户端耗时约 499 秒；人工语义/最终表述仍有 3 条 pending，不据此发布在线准确率或 P95。

可靠性方面，OpenAI-compatible Vanna 适配器的同步 HTTP 调用已移入工作线程，避免阻塞事件循环，
`ObservedLlmService` 统一识别 asyncio/OpenAI/httpx 超时并记录有界 `llm_observations`；超时无可信
结果时只返回安全提示，有可信结果时保留已验证结果。观测写入既有 `agent_runs.catalog_trace`
JSONB，不修改数据库表结构。

## 2026-08-19 可信结果确定性收口

在线复测表明，SQL 和结果合同通过后仍由模型生成最后一轮自然语言总结，会引入无证据的趋势、币种或
因果措辞，同时增加一次模型调用。系统现在对未显式要求图表的请求启用可信结果确定性收口：第一轮
模型仍只负责生成 SQL；经 AST、reader role 和 `ResultValidator` 后，表格照常展示，服务端依据
结果合同输出指标列、返回行数和校验状态，不再调用模型生成最终总结。用户明确要求图表时关闭该收口，
保留现有 `visualize_data` 路径。

真实 `data_005` 月度 GMV 回归中，收口标志已写入 run evidence；最终有 1 条实际 PostgreSQL
执行和 `deterministic_result_finalized=true`。模型共两轮，其中第一轮因敏感 `order_id` 投影被
AST Policy 拒绝，第二轮生成有效 SQL；不存在额外的结果总结模型轮次。客户端耗时 68,516 ms，
对比先前同 case 的 89,775 ms 降低约 24%。这是单次定向观测，不是延迟分位数；仍需继续减少
初始 SQL 的敏感投影错误。

## 修复后确定性回归（2026-08-18）

本轮没有重复消耗在线 SiliconFlow 批次，而是先把四类失败抽象为通用机制并用离线合同覆盖：

- `CatalogRetriever` 在可见 Join 图上闭包检索，能为维度目标补入最短桥接路径，并在预算不足时拒绝不完整上下文；
- `MetricDefinition.dimension_policies` 冻结指标与维度的归因规则，要求澄清的维度在路由阶段不生成 SQL；
- 可信结果合同通过后，预算台账记录状态并阻止同一请求的重复 SQL，不把抑制误记成 repair 或 permission failure；
- Catalog 根级币种元数据和趋势表达规则注入模型上下文，避免工作区无关的全局货币替换。

新增/更新专项断言后，相关测试为 **71 passed**，v2 路由/QueryPlan golden 为 **60/60 passed**。
该结果证明确定性合同回归，不代表在线模型语义准确率；在线失败样本仍需在后续模型批次中复核。

## 2026-08-19 敏感关联键投影回归

前一条 `data_005` 在线回归显示，模型在内部 CTE 中按 `order_id` 聚合再 Join 时，旧 Policy
把 CTE 导出的关联键误判为最终敏感投影，导致一次额外模型轮次。现将 AST 边界通用化：内部 CTE
可以保留敏感键用于 Join、过滤和聚合；只有最外层结果阶段的敏感列、结果别名、`GROUP BY` 和
`ORDER BY` 被拒绝。QueryPlan 与 Catalog Prompt 同时声明服务器生成的顶层结果列白名单。

专项测试 50 passed、真实 PostgreSQL 11 passed、v2 golden 60/60。真实 SiliconFlow 对照：

| 指标 | 修复前 | 修复后 |
| --- | ---: | ---: |
| SQL 调用 | 2 | 1 |
| LLM 轮次 | 2 | 1 |
| rejected audit | 1 | 0 |
| allowed audit | 1 | 1 |
| 结果合同/确定性收口 | 通过 / true | 通过 / true |

修复后单次客户端耗时约 93 秒，受在线模型波动影响，不能作为延迟分位数结论；本轮收益是减少
一次 Policy 拒绝、一次模型调用和相应 token 消耗，同时保持最终展示和权限边界不变。

## 2026-08-19 真实业务问题、解释与图表质量评测

新增 `evals/cases/text_to_sql_online_quality_v1.yaml`。它从 60 条 v2 source suite 中选择 20 条
真实业务问题，覆盖 Catalog 定义、通用业务问答、单指标、时间序列、维度分析、多指标、澄清和
当前工作区查询；其中 5 条追加有界图表意图。清单不保存问题原文，在线报告和人工标签仍位于
被 Git 忽略的 `evals/reports/`，不保存回答、SQL、原始行、图表 payload、cookie 或密钥。

真实 FastAPI/SSE 单次批次结果为 20/20 Agent Run、0 客户端错误、20/20 权限合规。人工标签规则是：
指标语义必须同时保留 Catalog 的粒度、过滤、时间与归因；回答有据必须交付被验证证据支持的结论，
超时安全提示不被记为完成；图表质量只在实际调用了可视化工具后人工判定。汇总如下：

| 维度 | 结果 | 边界 |
| --- | --- | --- |
| 路由 | 16 pass / 4 fail | 四条失败均为在线模型在 SQL 前或后超时。 |
| SQL 可执行 | 11 pass / 4 fail / 5 N/A | 失败不等于越权，所有请求仍经过受控路径。 |
| 指标语义 | 11 pass / 2 fail / 7 N/A | 两个失败是支付归因未冻结、评价行度量经商品行关联后失去原始粒度。 |
| 结果合同 | 11 pass / 4 fail / 5 N/A | 合同列能阻止错列，但不能独自证明指标粒度正确。 |
| 权限合规 | 20 pass | 没有未审计 SQL 或跨权限边界执行。 |
| 回答有据 | 11 pass / 9 fail | 包括未完成超时、无证据因果/地理推断和建立在错误归因上的结论。 |

原批次的 5 条图表请求中，3 条已发出 `dataframe` 和 `chart` 富组件，2 条在 SQL 前超时。运行器现在只
额外保存 `chart_requested`、`chart_component_emitted` 和 `dataframe_component_emitted` 等结构性字段，
不保存图表数据或文字。真实 Playwright 页面复核确认一个月度趋势请求的 Plotly SVG、标签和数据标记
可见，嵌入窗口没有横向溢出；但用户要求折线图时实际生成了柱状图，故该请求的图表质量为 fail。对州
维度柱状图的独立重放在 180 秒内未出图，属于在线超时，不能用于证明或否定原批次已经发出的图表类型。

本批 20 条总客户端耗时为 1,487,394 ms。已成功的 PostgreSQL 阶段约 56--177 ms，而模型单回合可达
120,000 ms；本轮再次表明当前吞吐和可用性瓶颈在 provider/模型回合，不在 SQL 执行。每条仅运行一次，
不将总耗时、完成数或组件发出数写成 P50/P95、总体准确率或图表成功率。

下一次实现优先级由本轮失败驱动：第一，给任意涉及一对多支付/归因桥的指标-维度组合声明显式归因
策略，未声明时在 SQL 前澄清；第二，把度量的事实粒度和维度桥接写入服务器结果验证，避免仅靠列合同
放行被 Join 放大的比率；第三，将显式图表请求的类型、轴角色和允许字段写入服务端图表合同，而不是
仅交给模型提示词；最后才针对 120 秒模型超时讨论供应商重试、缓存或异步任务策略。

## 2026-08-19 可信结果呈现与归因边界回归

真实业务质量评测表明，已有 `ResultContract` 的结果仍可能以“返回行数、列名、指标 ID”的机器式
文本结束。这样虽然保守，却让用户无法从 75 行分组表中快速确认返回了什么；同时，先前被允许的
`GMV × payment_type` 和品类履约/好评率组合存在未冻结的一对多归因，不应因为列合同通过而被展示成
可信结论。

本轮将确定性收口改为服务器拥有的受限叙述器。它只读取 `ResultValidator` 已通过的 DataFrame 有界摘要：
单行或分组结果会说明结果数量和“完整明细见上表”，并最多逐字展示三条摘要中的实际记录。字段展示名
来自当前 Catalog 的指标/列元数据；SQL 使用无下划线或不同大小写别名时仍能匹配。叙述器不计算最高/最低、
不宣称趋势、不补充币种，也不作因果解释，且不再次调用模型；它只声明本轮合同实际适用的字段、数值、范围或截断检查。

另一方面，Catalog 将以下组合标为 `requires_clarification`：GMV 按支付方式、平均履约天数按品类、
好评率按品类。`QuestionRouter` 在 SQL 前返回包含具体归因原因的澄清，而不是让模型自行采用首笔支付
或通过商品行复制订单/评价。规则由每个指标的 `dimension_policies` 描述，可由其他 Workspace 的 Catalog
替换，并没有按 Olist 问题文本或评测编号分支。

本轮离线专项为 **77 passed**，v2 路由/QueryPlan golden 为 **60/60 passed**；Playwright 通过真实
`<vanna-chat>` 加载受限结果预览，验证中文表格渲染、样例值和桌面无横向溢出。该回归验证确定性展示
与路由边界，不重新运行在线 SiliconFlow 批次，也不据此声称在线准确率或延迟改进。下一项仍是将图表
类型、轴角色和允许字段收敛为服务器 Chart Contract；在此之前不增加 SQL 修复次数。
