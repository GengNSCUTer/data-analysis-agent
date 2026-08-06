# Text-to-SQL 专项调研与优化建议

> 调研日期：2026-08-03（含二次公开仓库/源码核验）；实现状态更新：2026-08-06
> 范围：当前项目、Text-to-SQL/GenBI 开源方案、公开论文、Agent skill 和后续落地路线。
> 说明：2026 年论文多数是 arXiv 预印本，本文标注论文提出的结果，不把预印本结果当作
> 已验证的工程事实。

## 1. 结论先行

Text-to-SQL 的核心不是让模型“写出一条能执行的 SQL”，而是让它在正确的业务语义、允许的
数据范围和可接受的成本内，生成可以被验证、解释和复现的查询。当前项目在安全边界和业务
语义起点上是扎实的。当前生成链路已经加入角色化 Catalog、澄清、一次受限执行修复和结果级
验证；仍缺少第二个真实工作区和在线模型语义评测。

最适合本项目的改进顺序是：

```text
结构化语义 Catalog
  -> 按问题选择上下文
  -> 判断可回答/需要澄清
  -> 生成一条 SQL
  -> Policy + 只读执行
  -> 一次受限修复或可信拒答
  -> 结果/证据校验和可回放记录
```

暂不建议把研究方向直接变成复杂工程：多 Agent、Best-of-N 大采样、训练专用模型、向量
数据库、任意 Python 执行和多方言支持都不是当前 Olist 8 表项目的瓶颈。

## 2. 当前项目到底怎么做

当前 trusted Vanna 链路是：

1. `TrustedWorkflowHandler` 提供中文 starter UI 和项目专属说明；
2. `QuestionRouter` 先按服务器身份、工作区 Catalog 和 `WorkingMemory` 判断可回答性；缺少
   时间、指标或比较基线时先澄清，不调用 SQL；
3. `CatalogContextEnhancer` 只把按角色筛选的 Catalog slice、结果合同和版本字段放入 Vanna
   `ToolContext`，而不是无条件注入所有 Schema；
4. Vanna `Agent` 调用项目包装的 `TrustedRunSqlTool`，仍受 `AgentConfig.max_tool_iterations=4`
   和请求级预算约束；
5. `SecurePostgresRunner` 用 `sqlglot` AST、工作区对象白名单和 PostgreSQL reader role 校验/执行；
6. 执行失败时最多生成一次脱敏修复候选，候选重新经过 Policy 和 reader role；成功结果还要经过
   `ResultValidator`，否则可信拒答；
7. 结果以 DataFrame/CSV 进入表格和受控 Plotly 图表，Agent 生成中文结论；宿主页展示数据/指标
   版本、最终 SQL、耗时、行数、审计和必要的修复证据。

### 已有优势

- 业务指标不是让模型自由猜，而是有版本化指标上下文；
- SQL 生成后还有 AST Policy 和数据库角色两层防线；
- `analytics` 和 `app` 分离，查询不能直接写库或读审计表；
- 结果带 SQL、版本和审计证据，便于复现；
- 有 60 条确定性策略/数据用例和 3 条固定场景 golden。

### 主要缺口

- 通用核心已经通过 `WorkspaceProfile` 与 Olist adapter 解耦；目前还没有第二个真实工作区验证可迁移性；
- 还没有用户级费用/配额台账、结构化旧轮次摘要和结构化结果历史回放；
- SQL 执行修复已限制为一次并接入 Vanna 工具生命周期，但尚未用真实 SiliconFlow 批量评估修复成功率；
- 结果级校验已覆盖空结果、Join 放大、指标列缺失、异常数值、时间越界和截断，仍需补线上人工标签；
- 没有置信度、拒答或人工确认机制；
- 会话与消息已经持久化，宿主页已支持历史恢复和新建会话；但旧轮次仍以受预算约束的完整消息为主，
  尚未形成结构化的指标/时间/筛选摘要，因而还不能稳定表达多轮 Text-to-SQL 的业务状态；
- 确定性 SQL/golden 评测不等于 SiliconFlow 在线模型语义准确率。

### 当前实现的代码证据

- `src/data_analysis_agent/workspace.py`：通用 `WorkspaceProfile`；`metric_context.py` 中的
  `OLIST_WORKSPACE` 只负责当前 Olist 数据集适配。
- `src/data_analysis_agent/semantic_catalog.py`：版本化、角色化 Catalog slice、Join 和 trace。
- `src/data_analysis_agent/sql_policy.py` 与 `postgres_runner.py`：候选 SQL 的 AST、对象白名单、
  敏感投影、LIMIT、超时和 PostgreSQL reader role 约束。
- `src/data_analysis_agent/budget.py`、`context_builder.py`、`chat_runtime.py`：请求级工具/SQL/图表/输入/
  上下文/输出预算、完整轮次裁剪和终止状态；上下文摘要和按问题 Schema 选择仍待实现。
- `src/data_analysis_agent/conversation_store.py`、`run_recorder.py`：用户隔离的会话/消息和 Agent Run
  记录；`examples/embedded_analyst_host.html` 与 `vanna-chat.ts` 已完成列表、恢复、刷新恢复和新建会话，
  但历史详情只回放安全文字，不伪造历史 SQL、图表或 DataFrame。
- `src/data_analysis_agent/trusted_sql_tool.py`：包装 Vanna `RunSqlTool`，统一执行失败脱敏、一次
  修复候选、Policy 二次校验、reader role 重执行、结果验证和终止证据。
- `tests/e2e/test_trusted_embedded_window.py`：包含固定 SSE 的多轮澄清回归；它证明会话契约和浏览器
  交互，不代表在线模型 Text-to-SQL 语义准确率。

## 3. 开源平台对比

### Vanna 2.0.2

Vanna 提供 Agent、Tool Registry、用户上下文、ConversationStore、Context Enhancer、过滤器、
生命周期钩子、观测接口、SSE 和 `<vanna-chat>`。这使它适合作为本项目的交互和 Agent 底座。
本地源码也明确提供 `max_tool_iterations` 和 `max_tokens` 配置，但持久化、预算检查、上下文
压缩和业务语义仍由应用实现。当前项目已经正确地把价值放在自有指标、SQL Policy、数据库
角色和审计层，而不是重写 Vanna Agent 核心。

### Dataherald

Dataherald 将 NL-to-SQL engine、Enterprise API、认证/组织逻辑、Admin Console 和 Slackbot
拆成多个服务，适合研究“引擎”和“治理平台”的边界。其公开仓库最近活跃度较低，完整部署依赖
Docker、多服务和较重的管理台；对当前个人项目，借鉴分层思想即可，不应整体移植。

### WrenAI

WrenAI 把业务定义、模型关系、指标、示例查询、公司说明和记忆作为可审阅、可版本化的
Context Layer，并配合 Schema Retrieval、MDL、dry-plan、结构化错误、RLAC/CLAC 和评测。
这与本项目下一步的“指标/Schema Catalog + Context Builder”高度契合。它还覆盖 20+ 数据源、
GenBI dashboard 和 Agent SDK，范围远大于当前项目；应借鉴语义层和证据化上下文，不引入整套
引擎或 dashboard。

### PandasAI 类方案

PandasAI 更偏 DataFrame/CSV 的探索式分析和 Python/图表生成，适合另一类“让 Agent 分析表格”
产品。当前项目的核心约束是 PostgreSQL 只读 Text-to-SQL 和企业式治理；引入任意 Python 执行
会破坏当前安全边界。因此它可以作为未来离线数据分析适配器的参考，不能替换当前 SQL 主链路。

### 3.5 二次公开仓库与源码核验（2026-08-03）

本轮按 `github-research` skill 的六阶段流程补充公开 GitHub API 搜索和浅克隆源码阅读。GitHub
CLI 当前未登录，因此仓库 stars、许可证和 archived 状态来自 GitHub Public API，代码事实来自
对应仓库在本地的浅克隆；这些缓存已加入 `.gitignore`，不作为项目运行时依赖。

| 仓库 | 公开 API 观察 | 代码级观察 | 对本项目的取舍 |
| --- | --- | --- | --- |
| [OpenChatBI](https://github.com/zhongyu09/openchatbi) | 613 stars，MIT，活跃 | `text2sql/generate_sql.py`、`sql_graph.py`、`confidence.py` 将表/列/指标 Catalog、schema linking、执行错误重试、结果限制、置信度和可视化拆成节点；`AskHuman` 支持人工澄清 | 最接近“数据分析 Agent 平台”的参考。借鉴状态图、一次受限重试、结果校验和 confidence gate；保留本项目 `sqlglot` AST Policy，不复制其正则安全检查 |
| [PremSQL](https://github.com/premAI-io/premsql) | 461 stars，未从 API 得到 SPDX 许可证 | `text2sql.py` 提供 SQLite/PostgreSQL/MySQL、本地模型接口和 execution-guided decoding；失败后把错误交给 correction prompt，结果限制 200 行 | 借鉴本地 Ollama/HuggingFace 接口和一次修复；先确认真正剪枝 Schema，避免只选择表但仍把全量 Schema 放进 prompt |
| [BIRD-INTERACT](https://github.com/bird-bench/BIRD-Interact) | 1,010 stars，MIT，ICLR 2026 Oral 标识 | 数据库环境与用户模拟器分离，动作包含 `ask`、`execute`、`get_schema`、`get_column_meaning`、`submit`，并为工具调用、澄清和用户耐心设置预算 | 将 Text-to-SQL 评测改为中文多轮会话；生产系统只吸收澄清状态、action trace 和预算思想，不引入用户模拟器 |
| [Lumen](https://github.com/holoviz/lumen) | 303 stars，BSD-3-Clause | SQL source、schema profiling、charts、dashboard、report/export 和 query limit 组合成完整 GenBI | 作为产品形态参考；不引入 Panel/Lumen，Vanna `<vanna-chat>` 已满足当前嵌入式入口 |
| [PandasAI](https://github.com/sinaptik-ai/pandas-ai) | 23.7k stars，API 许可证字段为 `NOASSERTION` | `SemanticLayerSchema` 支持 alias、relations、expression 和 transformations；`SQLDatasetLoader` 支持 SQL connector，但安全检查仍混合 AST 与关键词黑名单 | 证明它不只支持 CSV/Excel；可作为未来 DataFrame 适配器参考，不能替换 PostgreSQL + AST + reader role 主链路 |
| [Dash](https://github.com/agno-agi/dash) | 2.2k stars，Apache-2.0 | 以六层上下文、自学习和评测为卖点，目录包含 knowledge、memory、evals 和数据 Agent 运行层 | 借鉴上下文分层与反馈闭环；暂不引入其完整框架 |
| [SQL-R1](https://github.com/DataArcTech/SQL-R1) / [MAC-SQL](https://github.com/wbbeyourself/MAC-SQL) | 分别 145/344 stars | 前者是强化学习训练 Text-to-SQL 推理模型，后者是多 Agent 协作框架 | 研究路线，不解决当前项目的 Catalog、澄清和证据链瓶颈，暂缓 |
| [test-suite-sql-eval](https://github.com/taoyds/test-suite-sql-eval) | 321 stars，Apache-2.0 | 通过多个数据库实例比较 denotation，处理行/列顺序、重复行、空结果和 literal 替换 | 借鉴“结果语义而非 SQL 字符串”的评测契约；先在固定 Olist 上实现轻量 golden/人工语义标签 |

源码阅读得到的共同事实是：成熟实现都把 SQL 生成前的上下文选择、生成后的执行反馈、结果
验证和失败终止分开；单纯把 `max_tool_iterations` 调大不能替代这些语义状态。当前项目的
独有边界（AST policy、PostgreSQL 双角色、版本化证据）应保留为不可下放给开源 Agent 的信任层。

## 4. 论文与研究方向

### 4.1 基准正在从单轮 SQL 走向真实交互

- [Spider 2.0-AIFunc (arXiv:2607.06229)](https://arxiv.org/abs/2607.06229)：覆盖云数据平台中的
  AI-native SQL 函数，说明传统 NL2SQL 不等于完整分析工作流；当前项目暂不做 Snowflake AI 函数，
  但应保持工具和方言边界清晰。
- [ABISS (arXiv:2607.23340)](https://arxiv.org/abs/2607.23340)：将歧义、不可回答问题和多轮
  用户交互纳入评测；论文报告即使给出正确澄清信息，最终 SQL 仍可能失败。它直接支持本项目
  建立“先澄清再查”的状态机和多轮评测。
- [BIRD-INTERACT (arXiv:2510.05318)](https://arxiv.org/abs/2510.05318)：把 Text-to-SQL 从单轮
  题目改成动态交互；对本项目而言，后续评测应包含“沿用上轮时间/指标/筛选”而不是只测独立问题。
- [Falcon Chinese Benchmark (arXiv:2510.24762)](https://arxiv.org/abs/2510.24762)：面向中文企业级
  Text-to-SQL，适合作为中文问题分类、口径歧义和业务表达的外部参考，但不能直接替代 Olist
  的本地 golden 数据。

### 4.2 Schema 检索和上下文压缩成为瓶颈

- [Finding the Right Tables and Columns (arXiv:2607.13311)](https://arxiv.org/abs/2607.13311)：把表/列
  选择单独定义为检索任务，并报告通用 embedding 在企业 Schema 上迁移不理想；先做可解释的
  词法/别名/指标映射基线，再决定是否训练或引入 embedding。
- [Database Context Compression (arXiv:2606.28601)](https://arxiv.org/abs/2606.28601)：提出离线
  压缩重复列、同构表和冗余文档，报告在大 Schema 上显著减少上下文；当前 8 张表不需要复杂
  压缩算法，但其“数据库侧先整理，再按问题净化证据”的思想适合我们的 Catalog。
- [Schema-First Retrieval (arXiv:2606.28387)](https://arxiv.org/abs/2606.28387)：强调先从 Catalog
  找相关对象，再生成 SQL；与当前固定 `SYSTEM_PROMPT` 相比，是最自然的扩展方向。

### 4.3 执行反馈、验证和停止策略

- [How Far Do On-Prem Open LLMs Get (arXiv:2606.29733)](https://arxiv.org/abs/2606.29733)：报告在 BIRD
  上 self-correction 是稳定收益，而 self-consistency 在高 token 成本下收益很小；支持本项目
  先实现一次执行错误修复，不做多次候选投票。
- [Test-Time Verification via Outcome Reward Models (arXiv:2606.30851)](https://arxiv.org/abs/2606.30851)：
  用结果奖励模型选择候选 SQL，报告优于仅按执行成功/多数投票；这是后续研究方向，不是当前
  小规模项目应立即训练的组件。
- [What Predicts Correctness (arXiv:2607.06799)](https://arxiv.org/abs/2607.06799)：单纯可执行性、
  字符/结构一致性和 log-probability 的正确性预测能力有限，验证型 judge 更有价值；因此项目
  不能把“SQL 跑通”写成“语义正确”，需要人工核验和选择性拒答。
- [Knowing When to Stop (arXiv:2607.03991)](https://arxiv.org/abs/2607.03991)：研究何时停止
  重复执行验证；对我们当前只有一次修复的预算设计有启发，但不值得现在引入学习型停止器。

### 4.4 权限和安全应进入 Text-to-SQL 评测

- [Benchmarking Text-to-SQL under RBAC (arXiv:2607.22115)](https://arxiv.org/abs/2607.22115)：指出
  无权限 benchmark 会高估真实系统，需同时评估 SQL utility 和 access compliance。当前项目的
  AST Policy、PostgreSQL 双角色和 26/26 安全预期正是这一方向的工程化版本。
- [Policy-Conditioned Constrained Decoding (arXiv:2607.12341)](https://arxiv.org/abs/2607.12341)：
  将列在输出/过滤/聚合中的使用策略加入解码约束；这是比生成后拦截更深的研究方向，但当前
  项目应继续以生成后 AST + 数据库角色为主，避免把模型解码器和权限逻辑耦合。

### 4.5 多轮记忆和企业业务知识

- [Memory Architectures for Multi-Turn Text-to-SQL (arXiv:2605.26394)](https://arxiv.org/abs/2605.26394)：
  用多轮 benchmark 分离 working memory、episodic retrieval 和 semantic augmentation，报告
  无状态多轮会在后续轮次快速崩溃，也指出记忆复杂度不与准确率单调增加。它支持先做可靠的
  working memory，再谨慎增加检索记忆。
- [Learning to Retrieve: Dual-Level Long-Term Memory (arXiv:2606.00547)](https://arxiv.org/abs/2606.00547)：
  将 episode 级策略记忆和 turn 级局部记忆分开；当前项目可以借鉴分层概念，但不应直接复制
  需要强化学习训练的检索器。
- [EntSQL (arXiv:2606.03363)](https://arxiv.org/abs/2606.03363)：面向企业长上下文知识的中英
  Text-to-SQL benchmark，显示仅有 Schema 和问题仍不足以处理企业定义；这强化了指标目录、
  业务说明和可追溯版本的必要性。
- [Beyond Text-to-SQL: Governed Enterprise Analytics APIs (arXiv:2605.21027)](https://arxiv.org/abs/2605.21027)：
  将受治理的 Analytics API 作为模型调用边界，提醒我们未来可以把稳定指标封装成受控工具，
  但不应因此放弃 SQL 证据链。

### 4.6 SQL 与 Python 的组合

- [ProSPy (arXiv:2606.05836)](https://arxiv.org/abs/2606.05836)：先做 profiling 和 Schema 剪枝，再
  用 SQL 获取中间视图，最后用 Python 做灵活分析。它适合大型企业复杂问题，但对当前项目
  的任意 Python 执行风险较高；后续如需扩展，只能设计受限、沙箱化、结果文件白名单工具。
- [SQuaD-SQL (arXiv:2607.08161)](https://arxiv.org/abs/2607.08161)：通过合成数据、蒸馏和参数高效
  微调降低小模型成本；这属于模型训练路线，暂时不如请求预算和一次修复直接。

## 5. 针对本项目的技术路线

### T0：先建立在线基线

在不改 Agent 的前提下，选取 20--30 个代表性问题，记录模型、Prompt、Schema/指标版本、
SQL、执行结果、是否需要修复、耗时和人工判定。指标至少包括：

- SQL 可执行率；
- 业务语义正确率；
- 指标口径正确率；
- 安全合规率；
- 澄清正确率；
- P50/P95 时延和每问工具/token 成本。

这一步是后续判断任何优化是否有效的基线，不能用公开 benchmark 分数替代。

### T1：语义 Catalog 和上下文选择

把当前长字符串 `SYSTEM_PROMPT` 拆成结构化 Catalog：

- 表、列、类型、业务别名、敏感级别；
- 指标公式、粒度、默认过滤、时间字段、可用维度；
- 合法 Join 路径和禁止的粒度组合；
- 已核验的少量问题-SQL 示例；
- 数据集、指标和策略版本。

第一版使用确定性的别名/关键词/指标匹配和 Join 图，不先上向量库。对当前 8 张表，优先
证明“减少无关上下文后是否降低错误”，再评估 embedding retrieval。

### T2：问题分类和澄清

在 SQL Agent 前增加轻量分类：

```text
可直接回答 | 缺时间范围 | 缺指标定义 | 缺对比基线 | 无权限 | 不支持
```

只有可回答请求进入 SQL 生成；歧义请求返回结构化澄清问题，并把用户选择保存为本轮上下文。
分类器可以先用规则 + 结构化 Catalog，避免再引入一个不可审计的 LLM 闸门。

### T3：执行引导的单次修复

流程固定为：

```text
候选 SQL -> Policy -> 只读执行 ->
成功：结果校验/回答
失败：仅把安全的错误类别和必要上下文交给一次修复 -> 重新 Policy -> 执行
再次失败：可信拒答
```

不得把原始数据库异常、敏感值或其他用户信息直接交给模型；每次修复都重新走 AST、白名单、
只读角色、LIMIT 和超时。保存候选 SQL、最终 SQL、修复原因和终止原因。

### T4：结果级验证与选择性拒答

先实现确定性检查：列是否符合问题/指标、结果是否超过行数、聚合是否有明显 Join 放大、
时间范围是否存在、空结果是否需要澄清。检查失败时展示原因或进入澄清，不生成貌似确定的
数字。后续才考虑 judge/ORM/多候选验证。

### T5：多轮记忆评测

把问题组织成会话而非散题：

```text
“看 2026 年各州订单”
  -> “只看前五”
  -> “按品类拆开”
  -> “和上一个结果比较好评率”
```

刷新恢复、上下文裁剪和用户隔离已经有浏览器/API 覆盖；下一阶段还要为每轮记录它依赖的时间、指标、
筛选和上轮结果摘要，才能评测“沿用上轮口径”是否正确。

## 6. 不同优化的优先级

| 能力 | 当前价值 | 实施建议 |
| --- | --- | --- |
| 指标/Schema Catalog | 很高 | P0/P1，先结构化和确定性检索 |
| 一次执行修复 | 很高 | P1，限制 1 次并记录原因 |
| 澄清和不可回答检测 | 很高 | P1，先规则化 |
| 多轮 working memory | 很高 | P0 持久会话已完成；P1 增加结构化指标/时间/筛选摘要 |
| 结果级校验/拒答 | 很高 | P1，先确定性检查 |
| Schema embedding 检索 | 中 | 大 Schema 或基线证明不足时再做 |
| Best-of-N/多模型投票 | 低到中 | 成本高，当前暂缓 |
| Outcome Reward Model/Judge | 中 | 有足够在线样本后再评估 |
| RL/蒸馏/专用微调 | 低 | 需要数据、GPU 和稳定评测，不是当前瓶颈 |
| 任意 Python 分析 | 低 | 与当前安全目标冲突，明确暂缓 |

## 7. 建议追加的评测集

在现有 60 条确定性用例之外，新增版本化多轮/线上模型小集：

- 10 条指标口径明确的单轮问题；
- 10 条多表 Join 和粒度陷阱问题；
- 10 条缺时间、缺比较基线或“最好/异常”歧义问题；
- 10 条连续追问会话；
- 10 条执行错误/空结果/需要修复问题；
- 10 条 RBAC、敏感列、越权和不可回答问题。

每条记录问题、用户角色、数据/指标版本、期望分类、期望 SQL 语义、允许的澄清问题、结果
golden、是否应拒答和人工判定。报告必须分开统计执行正确、语义正确、安全合规、澄清正确和
成本，不能只给一个总准确率。

## 8. 参考项目、论文和工具

### 开源项目

- [Vanna](https://github.com/vanna-ai/vanna)：本项目当前 Agent/Web Component 底座；GitHub API
  当前标记该仓库已归档，后续以仓库内锁定版本和差异审查为准。
- [Dataherald](https://github.com/Dataherald/dataherald)：引擎、企业 API、管理台拆分参考。
- [WrenAI](https://github.com/Canner/WrenAI)：语义 Context Layer、MDL、受治理 GenBI 参考。
- [Spider 2-AIFunc](https://github.com/Leolty/Spider2-AIFunc)：AI-native SQL benchmark 参考。

### 已找到的 Agent Skill

- `oimiragieo/agent-studio@text-to-sql`：140 次安装，偏 Text-to-SQL 工作流实践；
  <https://skills.sh/oimiragieo/agent-studio/text-to-sql>
- `lingzhi227/agent-research-skills@literature-review`：约 3.3K 次安装，适合论文检索/综述；
  <https://skills.sh/lingzhi227/agent-research-skills/literature-review>
- `collaborative-deep-research/agent-papers-cli@literature-review`：696 次安装，另一个论文检索/综述选项；
  <https://skills.sh/collaborative-deep-research/agent-papers-cli/literature-review>

本轮没有安装它们：当前任务需要的是对项目做一次可审阅的专项研究，外部 skill 本身不应成为
运行时依赖。`github-research` 和 `find-skills` 只用于仓库/技能发现；如果经常做论文追踪，可
单独安装 literature-review；如果需要重复生成 Text-to-SQL 实践模板，再考虑安装 text-to-sql skill。

## 9. 研究结论转译为工程合同

为避免把“研究方向”变成无界开发，后续每次实现都必须遵守以下合同：

1. **上下文合同**：模型只能看到服务器按角色筛选后的 Catalog slice、必要 working memory
   和版本化规则；检索结果要记录命中的表/列/指标及原因。
2. **状态合同**：请求先落入 `answerable`、`missing_time`、`missing_metric`、
   `missing_comparison`、`unauthorized`、`unsupported` 之一；非 `answerable` 不得直接生成数字。
3. **修复合同**：候选 SQL 最多执行一次安全修复；原始候选、错误类别、修复候选和最终终止原因
   都写入 run evidence，修复 SQL 重新经过完整 AST/role/timeout/limit 检查。
4. **结果合同**：成功回答必须通过列/指标、空结果、时间覆盖、Join 放大和 LIMIT 截断检查；失败
   时输出澄清或拒答，不用“SQL 可执行”冒充业务正确。
5. **成本合同**：独立记录 Vanna 工具迭代上限、项目总工具预算、SQL/图表预算、输入/上下文/输出
   token 预算、实际 usage（没有 usage 时记为 `unknown`），并允许在预算耗尽时安全终止。
6. **评测合同**：至少分开统计 executable、semantic、metric、security、clarification、latency、
   tool_calls 和 token_cost；公开 benchmark 只能作为方向参考，不能替代本项目 Olist golden。

这一合同把 OpenChatBI 的状态图、WrenAI 的语义层、BIRD-INTERACT 的交互预算和
`test-suite-sql-eval` 的 denotation 思路收束为当前项目可解释的小步实现。

## 10. 研究限制

- arXiv API 查询结果包含 2026 年预印本，论文结论需要后续核对版本、代码和同行评审状态；
- GitHub CLI 当前未登录，仓库元数据使用公开 API/README；没有把未读源码的项目写成代码级结论；
- 公开 benchmark 的数据库、模型、提示和评估口径与 Olist/SiliconFlow 不同，只用于方向判断；
- 本文是设计与调研，不代表 T1--T5 已经实现。

## 11. 2026-08-03 第二轮核验与当前实现状态

### 11.1 最新论文核验

本轮直接查询 arXiv API（查询时间 2026-08-03，按提交时间倒序）并重新阅读摘要。下面的
结论只用于确定工程优先级，不把预印本结果当成本项目的准确率：

| 论文 | 核验到的贡献 | 对本项目的工程含义 |
| --- | --- | --- |
| [ABISS](https://arxiv.org/abs/2607.23340) | 将歧义/不可回答问题划分为 8 类，并测试模拟用户交互；即使给出正确的类别或澄清信息，最终 SQL 仍可能失败。 | Router 不能只返回一次追问；必须保存原问题、结构化缺失字段，并测试“澄清后重新生成”这一整条链路。 |
| [Benchmarking Text-to-SQL under RBAC](https://arxiv.org/abs/2607.22115) | 将 SQL utility 与 access-control compliance 分开度量，指出无权限 benchmark 会高估真实系统表现。 | 保留 `sqlglot` allowlist、PostgreSQL reader role 和 analyst/admin Catalog 过滤；不能只报告 execution accuracy。 |
| [Finding the Right Tables and Columns](https://arxiv.org/abs/2607.13311) | 把表/列选择单独定义为检索任务，并报告通用 embedding 在企业 Schema 上迁移不稳定；语料自适应训练可提高召回。 | 先用可解释的别名/关键词检索建立本地基线；只有在 Olist 基线证明不足时才评估 embedding，并记录 recall@k。 |
| [Schema-First Retrieval](https://arxiv.org/abs/2606.28387) | 将表、列、指标、关系和历史查询作为 typed Catalog 对象，配合向量召回、关系扩展、重排和权限门。 | 当前 YAML Catalog 的对象划分是正确的方向；关系扩展和重排属于大 Schema 后续项，不是 v1 必需品。 |
| [Database Context Compression](https://arxiv.org/abs/2606.28601) | 通过离线整理重复列、同构表和冗余文档，再做在线证据净化，显著压缩大数据库上下文。 | 当前 9 张 Olist 表先做 Catalog slice 和字符预算；不要为了追随论文提前引入复杂压缩中间件。 |
| [How Far Do On-Prem Open LLMs Get](https://arxiv.org/abs/2606.29733) | 在 BIRD 上做 schema linking、self-correction、self-consistency 的消融；摘要报告 self-correction 更稳定，而 self-consistency 的 token 成本/收益很差。 | 先做一次执行错误修复和成本记录，暂不做 Best-of-N 或多模型投票；本结论仍需用 SiliconFlow/Olist 自己复测。 |
| [Bootstrapping Semantic Layer from Execution](https://arxiv.org/abs/2606.05634) | GATE 用执行反馈验证多个 grounding 假设，并将验证过的 grounding 写入可复用记忆。 | 未来可把人工确认过的值映射/业务别名沉淀为审核记忆；当前先不把自动学习写入生产 Catalog。 |
| [DataClawEval](https://arxiv.org/abs/2607.28033) | 使用隔离沙箱和确定性规则脚本评估端到端数据工程 Agent，而不是只用 LLM judge。 | 本项目的评测必须保留 PostgreSQL golden、策略断言和人工语义标签；不能用一个总分替代可解释证据。 |

Spider 2.0 仍然是企业级复杂 Schema/多查询工作流的上界参考，但当前项目固定为单一
PostgreSQL 方言、只读分析和 Olist 9 表，不把 Spider 2.0 的工作流规模误包装为现有能力。
BIRD-INTERACT 的动态交互协议适合作为多轮评测设计参考，生产系统不引入它的用户模拟器。

### 11.2 最新开源实现核验

GitHub Public API 在本轮返回的可复核元数据如下（star 会变化；许可证为 API 字段值，不替代
逐仓库许可证审查）：

| 仓库 | Stars | License/API 状态 | 代码/产品事实 | 本项目取舍 |
| --- | ---: | --- | --- | --- |
| [vanna-ai/vanna](https://github.com/vanna-ai/vanna) | 23,822 | MIT；`archived=true` | Agent、Tool Registry、ConversationStore、Context Enhancer、SSE、Web Component。 | 继续使用仓库内锁定源码；适配器放在 `src/data_analysis_agent/`，不把上游归档误认为本项目不可维护。 |
| [Canner/WrenAI](https://github.com/Canner/WrenAI) | 16,793 | `NOASSERTION` | MDL/Context Layer、关系/业务规则、查询记忆、dry-plan 和治理能力。 | 只借鉴语义层和上下文版本化，不整体迁移其多服务/GenBI 平台。 |
| [sinaptik-ai/pandas-ai](https://github.com/sinaptik-ai/pandas-ai) | 23,682 | `NOASSERTION` | DataFrame/文件分析为主，也有 `SQLDatasetLoader`、SemanticLayerSchema 和 SQL connector。 | 证明它不是 CSV/Excel-only；任意 Python 执行仍不进入当前 SQL 信任边界。 |
| [zhongyu09/openchatbi](https://github.com/zhongyu09/openchatbi) | 613 | MIT | SQL graph 将 Schema linking、执行重试、结果限制、confidence 和图表拆成状态节点，并有人工澄清入口。 | 借鉴状态图、一次修复、结果 gate；不复制其正则安全检查。 |
| [premAI-io/premsql](https://github.com/premAI-io/premsql) | 461 | API 未给 SPDX | 提供多数据库/本地模型接口和 execution-guided correction。 | 借鉴 Ollama/vLLM 接口和修复契约；先测实际上下文是否剪枝。 |
| [bird-bench/BIRD-Interact](https://github.com/bird-bench/BIRD-Interact) | 1,010 | MIT | `ask`、`execute`、`get_schema`、`submit` 等交互动作和工具/耐心预算。 | 借鉴 action trace 与多轮预算，生产系统不引入模拟用户。 |
| [taoyds/test-suite-sql-eval](https://github.com/taoyds/test-suite-sql-eval) | 321 | Apache-2.0 | 以 denotation 比较结果，处理行列顺序、重复行、空结果和 literal 替换。 | 借鉴结果语义比较，先在固定 Olist golden 上实现轻量版本。 |

### 11.3 Skill 发现结论

`find-skills` 查询到 `oimiragieo/agent-studio@text-to-sql`（约 140 installs、源仓库约 36
stars、未声明 SPDX 许可证）。它提供数据库连接、Schema 导出、SQL 文件和结果落盘模板，
适合从零搭一个简单脚本，但没有当前项目所需的 Vanna Context Enhancer、角色隔离、AST
Policy、审计或结果级语义校验，因此本轮不安装、不作为运行时依赖。已安装的
`lingzhi227/agent-research-skills@github-research` 适合仓库调研；literature-review 类 skill
适合持续追踪论文，但也不应替代版本化本地评测。

### 11.4 当前代码事实与边界

第二轮开发已经把研究结论落到 `src/data_analysis_agent/`：

- `data/catalog/olist_catalog.yaml`、`semantic_catalog.py`、`question_router.py` 和
  `working_memory.py` 提供可版本化、角色隔离、有限长度的 Catalog、路由和多轮状态；
  `CatalogLoader` 仍可加载 9 张表、4 个指标和 7 条 Join。
- `CatalogRetriever` 对 GMV、州订单数、品类 GMV 做稳定选择；小表/列/Join/字符上限不会
  丢掉指标必需对象或生成无 Join 的孤立表，超限明确 fail closed。
- Trusted Demo 已装配 `CatalogContextEnhancer`，固定系统提示缩短为安全边界；trace 进入
  `BudgetUsage` 和 `app.agent_runs.catalog_trace`，不包含原始问题或结果行。
  后可恢复原问题口径。服务层和固定 SSE 浏览器多轮回归均已测试。
- `trusted_sql_tool.py` 将 `sql_repair.py` 的一次候选修复接入 Vanna 原生 `RunSqlTool` 生命周期；
  `postgres_runner.py` 对数据库错误做安全分类，候选必须通过完整 `SqlPolicy` 并由 reader role 重执行。
  `result_validator.py` 对空结果、缺列、非有限值、时间越界、截断和 Join 放大返回
  `valid`/`needs_clarification`/`refuse`。服务器在 Catalog/WorkingMemory 之后构建 `ResultContract`，
  把指标/时间别名/请求范围/Join 和 `catalog/dataset/metric/policy` 版本写入实际 Vanna `ToolContext`；
  原始/修复候选、Policy 状态、执行状态和终止原因写入 `repair_evidence` 并持久化。

本轮 Conda 指定确定性专项测试 `84 passed`，项目 PostgreSQL Runner/Run recorder 测试 `4 passed`，
固定 SSE 多轮浏览器测试 `1 passed, 6 deselected`。没有运行批量 SiliconFlow 线上评测，因此不报告
Text-to-SQL 在线准确率、修复成功率、token 成本或 P95 延迟；全量 Vanna 上游可选驱动测试缺少外部
依赖，也不作为本项目质量门。

对应的第二轮执行计划已经单独保存为
[`plan/feature-text-to-sql-reliability-v2.md`](../plan/feature-text-to-sql-reliability-v2.md)，验证
记录见 [`docs/verification-text-to-sql-v2.md`](../docs/verification-text-to-sql-v2.md)。下一步顺序固定为：
先建立 60 条版本化 v2 评测及人工标签，再用同一批问题做 SiliconFlow 小规模核验和前后对比。任何
embedding、judge、RL、多 Agent 或 Python 分析扩展都必须等这条基线有数据后重新评审。
