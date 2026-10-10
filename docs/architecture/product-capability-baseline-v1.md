# 产品能力基线测试 v1

> **状态：** Phase 0、A 已完成。Phase B 自动运行已完成，人工语义审核仍待完成。Phase C 已有会话复测。Phase D、E 还没有收成总表。  
> **日期：** 2026-09-20  
> **用途：** 用分层测试回答“当前 Agent 到底能做什么、哪些场景不可靠”；不作为本地模型训练、验证、checkpoint 选择或 protected final test 的替代。

## 1. 为什么需要独立产品基线

当前已有 Router、SQL Policy、reader role、ResultValidator、WorkingMemory、ChartContract 等定向测试，也有 local Adapter 的 Olist/TheLook 离线 final evaluation。这两类证据都不能单独回答用户在网页中提出一个中文问题时，系统是否能正确澄清、生成 SQL、返回可信答案、展示图表并保持多轮状态。

因此 v1 将能力拆成五层，避免用一个无意义的“准确率”掩盖故障位置：

```text
A 路由 / 澄清 / 拒绝
B 单轮业务问答 / SQL / 结果 / 图表
C 多轮短期记忆 / 会话恢复 / ContextBudget
D 浏览器 / SSE / 表格 / 图表体验
E 证据汇总 / 失败分类 / 唯一下一任务
```

## 2. 冻结边界

| 项目 | v1 规则 |
| --- | --- |
| 当前产品路径 | Olist trusted demo：`trusted_olist_web_demo.py` -> SSE -> Router/Catalog/QueryPlan -> Agent -> SQL 安全链 |
| 当前在线模型 | SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`；B/D 才能调用，且须用户逐阶段确认 |
| 当前产品 workspace | Olist；TheLook 仍主要用于离线跨 Schema 的 local-model final evaluation |
| 浏览器自动化 | Web 环境已安装 Python Playwright；2026-09-19 已验证默认 Playwright Chromium `151.0.7922.34` 与系统 Chrome `131.0.6778.85` 都可无头启动。后续 D 复用现有 Python Playwright/pytest E2E，不引入 Selenium 或第二套测试框架。 |
| SQL 治理 | 不变：`SqlPolicy -> PostgreSQL readonly role -> ResultContract / ResultValidator` |
| 执行 / 展示行数 | 最多执行 1,000 行；UI 预览最多 200 行 |
| 原始证据 | 位于仓库外 `/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/` |
| 仓库内证据 | case 合同、runner、测试、safe aggregate report、artifact SHA-256；不保存原始 SSE、SQL、结果行、cookie、token 或截图 |
| 数据隔离 | `runtime_regression_only`；禁止 SFT training/validation、DPO、few-shot、local checkpoint selection、protected final test 替代 |

## 3. 阶段与最小覆盖量

| 阶段 | 数量 | 自动证据 | 人工审核 | 阶段停止条件 |
| --- | ---: | --- | --- | --- |
| A | 12 路由 case | Router、QueryPlan、终端状态 | 不需要 | 安全拒绝或相对时间错误进入 database path 即 P0 停止 |
| B | 16 单轮 case | SSE、audit、Policy、PostgreSQL、ResultContract、组件类型、耗时 | 指标口径、时间范围、回答有据、图表语义 | 高频同类语义失败后停止，先修该类问题 |
| C | 10 会话脚本 | state transition、Memory projection、tool-free、ContextBudget source | 每一轮是否错误继承条件 | 缺失能力标为 `not_implemented`，不直接实现 M2--M7 |
| D | 8 browser flow | loading、DOM、错误状态、历史、表格/图表状态 | 少量 live flow 的体验核验 | stub 失败先修 UI/runtime，不用 live 网络波动掩盖 |
| E | A--D 汇总 | suite/hash/阶段一致性、failure taxonomy | 对汇总结论审阅 | 只指定一个证据最强的下一任务 |

## 4. Case 合同与完成模式

机器可读合同是 [`../../evals/cases/product_capability_v1.yaml`](../../evals/cases/product_capability_v1.yaml)。Phase A 已填入并冻结 12 条路由 case，Phase B 已填入并冻结 16 条单轮 case；C/D 数组仍为空，表示这些阶段尚未开始，不代表已完成。

`validate_product_capability_suite(..., require_complete=False)` 允许 Phase 0 骨架通过；任何声称要执行完整 A--D suite 的 runner 必须使用 `require_complete=True`，此时会强制：

- A/B/C/D 分别为 `12/16/10/8`；
- 所有 case ID 唯一，格式为 `pcv1-a-*` 到 `pcv1-d-*`；
- 每个 case 有受控预期字段；
- 不包含 Gold SQL、SQL 字符串、结果行、raw completion 或 protected final case ID；
- `forbidden_uses` 不得删减或扩大为模型开发用途。

## 5. Safe report 合同

Runner 只能将如下字段写入仓库内 safe report：

```text
case_id
status
failure_layer
evidence_codes
artifact_sha256
latency_ms
manual_review_status
```

`question`、assistant text、SQL、结果行、SSE payload、trace、截图和凭据都不能出现在 safe record。原始证据仍可在用户拥有的仓库外目录中按 hash 回读。

这条边界意味着：后续人工审核可以查看经授权的外部原始证据，但 Git 文档只保留聚合结果和脱敏判断。

## 6. Phase 0 / Phase A / Phase B 验收

Phase 0 的完成只表示：

1. YAML skeleton 与禁止用途已经冻结；
2. Python contract 能区分“阶段尚未填 case”与“完整 suite 不合格”；
3. 任何 SQL/Gold/结果泄露、重复 ID、非法 usage、protected 标识、畸形会话和 unsafe safe-report 都会 fail closed；
4. 没有在线模型调用、业务数据库执行、前端改造、SFT/DPO 或 protected final test 重跑。

Phase A 已完成以下确定性验证：

1. 12 条 case 按冻结配额覆盖 3 条数据库可答、2 条帮助/Catalog、3 条澄清、3 条安全拒绝和 1 条无可信结果追问；
2. `QuestionRouter` 实际结果为 **12/12 通过**，其中安全拒绝、相对时间缺失和无可信结果追问均未进入数据库路径；
3. runner 统计 `database_calls=0`、`llm_calls=0`，没有 PostgreSQL、在线模型、SQL 生成或浏览器调用；
4. safe report 仅写入 case ID、状态、结构化 evidence code、hash 和耗时，报告位于仓库外：
   `/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-a/routing-safe-report.json`。

Phase A 不能证明单轮答案、SQL 语义、结果合同、图表、多轮记忆或浏览器体验正确；这些留待 Phase B/C/D。

### Phase B 当前结果：运行完成，人工语义审核待完成

2026-09-19 的首次运行来自旧的 32010 服务实例（`metric_version=0.1-draft`、`catalog_version=olist-catalog-v1`），只能保留为历史基线，不能与当前代码的能力结论混合。

服务已重载到当前运行时合同（`metric_version=0.2-frozen`、`catalog_version=olist-catalog-v2`、10 个已注册指标），并于 2026-09-20 重新运行 16 条单轮 case。每条使用独立 conversation/request ID。原始 SSE、回答、SQL 和数据库证据位于仓库外，safe report 位于：

`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-b/runtime-20260920/single-turn-safe-report.json`

当前结构化结果：

| 观察项 | 数量 | 解释 |
| --- | ---: | --- |
| AgentRun 可回读 | 16/16 | 请求均进入当前服务的审计链路 |
| SQL 已执行且 Policy 放行 | 16/16 | 每条均有 1 次允许的 SQL audit；不等价于业务语义正确 |
| 终态 `completed` | 16/16 | 不能直接等价为业务正确 |
| 二层聚合请求完成 | 2/2 | `average_order_value`、`on_time_delivery_rate` 当前 Catalog/QueryPlan 可进入 SQL/结果合同链路 |
| 图表请求产生图表组件 | 3/3（2 条初跑 + 1 条修复复测） | 图表合同只允许服务器固定字段/类型；仍需人工审核图表业务语义 |
| 浏览器真实图表 + 数据依据卡 | 1/1 | 真实嵌入组件显示结果表、受控 Plotly 折线图及底部数据依据卡；浏览器 console error 为 0 |

首次重跑中有一条“按州有效订单数柱状图”只返回了表格：服务器 `ChartContract` 实际为 `valid`，但模型跳过了 `visualize_data` 并错误声称合同不可用。为消除此类编排不确定性，当前产品已改为：模型仅生成一次 SQL 候选；SQL 通过 Policy、reader role 与 ResultContract 后，服务端直接使用请求内的已验证 DataFrame 和冻结 ChartContract 渲染 Plotly 图表。该路径不再注册 `visualize_data` 给模型，因此图表请求的目标审计形态为 `sql_calls_used=1`、`visualization_calls_used=0`、`llm_rounds_used=1`、`deterministic_chart_rendered=true`。ChartContract v2 只允许服务器固定的 `bar`、`horizontal_bar`、`line`、`area`、`donut`：品类图默认横向条形图；面积图只允许单指标无分组的时间序列；环形图必须由用户显式请求，且只允许单个可相加指标、单维度、至多 8 个非负类别。服务端不做重算、补值、去重或隐式聚合。散点、热力、地图、直方、漏斗、瀑布和堆叠图仍需要新的结果语义合同，本版本明确拒绝，不能静默替换。

本轮同时修复两项 P1 展示语义：通过 `products_translation` 返回的值显示为“商品品类（英文展示名）”，不再错误标为“原始品类”；`positive_review_rate`、`on_time_delivery_rate` 与 `cancellation_rate` 的 UI 表格和可信结果摘要统一显示为百分比。它们仅改变展示层，不改变 SQL、ResultValidator、结果 CSV 或图表输入的原始数值。

为避免后续报告误读，线上 runner 也修复了 SimpleText SSE 的字段解析；此前的 `has_text_response=false` 是 runner 取错组件序列化路径，不是服务没有返回文字。该修复不改变 SQL、图表或回答行为。

人工审核模板已生成到：

`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-b/runtime-20260920/manual-review-template.json`

只有完成 `metric_semantics`、`time_scope`、`answer_grounded`、`chart_semantics` 人工审核后，B 阶段才可以形成语义结论。`completed`、SQL allowed、ResultContract valid 和图表组件存在，都不能替代这些审核。

2026-09-22 复核了仓库外的 `manual-review-template.json`。安全产物仍是 `pending_manual`，并且不包含回答正文，因此这次不能给语义项打通过或失败。Phase B 的人工语义结论继续空着。

当前产品壳的浏览器检查补在 `tests/e2e/test_trusted_embedded_window.py`：会话列表、管理面板默认隐藏、保存分析和展示偏好可见；最近一条用户问题的编辑入口由已构建的聊天组件提供。这不是 Phase B 的语义评分。

### Phase C 当前结果：摘要、工件恢复与隔离已完成真实复测

Phase C 使用 10 个不含 SQL、Gold、结果行或模型答案的会话脚本，通过当前 trusted demo
SSE 运行了 26 个 turn。canonical safe report 为：

`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-c/rerun-v2/conversation-safe-report.json`

第一轮 runner 因只比较路由状态而没有把未完成 SQL 标成失败，保留为诊断证据；`rerun-v2` 才是修正
`execution_not_completed` 判定后的正式结构性报告。报告自身已通过 `validate_safe_evidence()`，只含
case ID、hash、状态、结构化 route/memory/budget 投影，不含问题、SQL、结果行或回答全文。

| 观察项 | 数量 | 解释 |
| --- | ---: | --- |
| 会话脚本 | 10/10 | 覆盖澄清继承、维度切换、可信结果追问、无结果追问、新会话、客户端重载和长历史 |
| turn | 26 | 每个脚本使用同一会话 ID；新会话脚本显式切换会话 ID；重载脚本重建客户端 |
| 严格通过脚本 | 4/10 | 只表示 route、WorkingMemory 投影、可信摘要存在性和 SQL 完成状态均符合合同 |
| 维度状态缺口 | 4 个 turn | `WorkingMemory.apply()` 保留旧 `dimensions`，没有把当前轮明确维度写入结构化状态；SQL 本身仍可能基于当前问题生成 |
| 结果追问路由偏差 | 1 个 turn | “刚才排名第一的州，结果是否合理” 被 `_DRILLDOWN_REQUEST` 判为新的 `data_analysis`，而不是基于可信结果的 `result_followup` |
| SQL 未完成 | 8 个 turn | route 仍为 `answerable`，但真实运行终态不是 `completed`；不能称为多轮业务回答成功 |
| ContextBudget 截断 | 本批未观察到 | 长历史脚本未触发 `context_truncated`；这不证明任意更长历史都安全 |

已验证的正向行为：相对时间澄清后能保留指标并在补充年份后进入查询；无可信结果追问会先澄清；
新会话不会继承旧会话指标/时间；可信结果追问在有摘要时可走 tool-free 路径。

在这轮基线后已实现、但尚未用真实 SSE 小批复测的收口如下：

1. `QueryPlan.dimensions` 是当前 SQL 轮唯一的维度事实源；当前问题显式识别到的维度会写回
   `WorkingMemory`，新维度替换旧维度，`不按/不要按/不分组/总体/总计` 会显式清空。该状态目前只用于
   观察和后续受控澄清，**不会**悄悄作为下一轮 SQL 的隐式分组条件。
2. 结果解释意图（“是否合理”“为什么这么高/低”“怎么理解”等）优先于下钻词。因而“刚才排名第一的州，
   结果是否合理？”应走携带可信摘要的 tool-free `result_followup`；只有“把刚才结果按州拆分”这类明确
   重查动作才重新进入数据库链路。
3. 每一个不匹配的 Phase-C turn 会额外写一份权限为 `0600` 的仓库外诊断 JSON。它记录该 request 的候选/
   最终 SQL、Policy/执行状态、受限安全错误文本及 repair evidence，供失败分类使用；safe report 只记录
   该工件的 SHA-256，不携带 SQL、错误文本、问题、结果行或模型回答。
4. `ResultArtifact v1` 将上一轮**已通过 ResultContract**的有界结果摘要、指标/结果列和数据/指标版本写入
   会话 working memory。客户端恢复历史时可显示一张“已恢复、未重跑 SQL”的可信摘要卡；不缓存完整表、CSV、
   原始 SQL 或 Plotly payload，历史图表明确标为未缓存。首轮 SQL 未成功时不会构造 artifact，重载后仍应澄清。

未实现项保持明确：workspace/state version、跨会话长期记忆、向量检索、完整结果表/图表工件缓存都不在本轮范围内。它们不能从当前 `WorkingMemory`、ResultArtifact v1 或 HistorySummary v1 推出“长期记忆已完成”。

### Phase C 长历史预算、语义摘要与工件回归

`ContextBudgetFilter` 按完整 user-led turn 选择最新连续后缀；有旧 turn 被省略时插入 bounded `HistorySummary v2`。摘要文本只用于叙事连续性，指标、时间、维度、筛选、权限、SQL 和结果事实仍来自 `WorkingMemory`、`QueryPlan`、`ResultContract` 与 `ResultArtifact`。`agent_runs.catalog_trace.context_budget` 只记录字符/token 预算、输入/输出数量、摘要范围 digest、版本和 tokenizer mode；safe report 不包含摘要正文。

2026-09-21 的隔离真实回归使用临时 `1,200` history-token 上限，服务端口 `32012`，不影响常驻 `32010`。6/6 检查通过：

- 受控查询生成完整 CSV，服务端确定性图表生成 Plotly JSON；客户端重载后通过 owner/workspace-scoped manifest 读取并重新校验 SHA-256 与字节数。
- 连续 6 个 user-led turn 触发 `summary_inserted=true`、`history-summary-v2`、`summary_status=semantic_generated`；摘要以 owner-scoped conversation metadata 保存，但 typed `WorkingMemory` 解析会丢弃该私有键，不能进入 SQL 条件链路。
- 摘要后的新 SQL 回合通过 PostgreSQL reader、ResultContract/ResultValidator，最终 `completed` 且存在 allowed audit。
- 切换到另一 demo role 访问同一 artifact manifest 返回 `404`，不泄露会话或 workspace 存在性。
- 摘要 provider 失败回退和 payload 篡改检测由确定性回归覆盖；这两项不是线上故障注入。

安全报告和原始 SSE 位于仓库外：

`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase3-context-artifacts/safe-report-rerun.json`

其中 tokenizer mode 明确为 `local_tokenizer_estimate`，不能解释为 DeepSeek-V4-Flash 的精确 token 数。

在默认线上预算 `12,000 chars / 40 messages` 下，`pcv1-c-long-history-budget` 的 8 个 turn 最大仅为 `1,535 chars / 25 messages`，所以**未触发压缩是正确结果**。其新 run 位于：

`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-c/history-compression-v1/conversation-safe-report.json`

为验证实际压缩路径，额外以隔离的临时 32011 服务将字符预算收紧为 `1,100`，不修改正常 32010 服务。相同脚本在第 6 turn 起触发：第 6 turn 将 `1,103` 输入字符收敛为 `779`，压缩 1 个旧 turn；第 7 turn 将 `1,433` 收敛为 `941`，压缩 2 个旧 turn；两者均有 `HistorySummary v1` digest、没有当前回合 fallback。证据位于：

`/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-c/history-compression-v1/tight-budget/conversation-safe-report.json`

该脚本严格通过数为默认预算 `3/8`、紧预算 `4/8`，失败仍主要来自既有 online SQL 未完成，不能归因为压缩功能、也不能据此宣称业务问答正确。当前实现只完成“预算内结构安全、压缩可观测、真实 SSE 触发”三件事；没有实现长期记忆或语义历史总结。下一步可以进入浏览器 Phase D，或先单独处理 Phase C 中已有的 SQL 未完成分类，不应把二者混为一项改动。

## 7. 相关文档

- [正式 A--E 计划](../../plan/feature-product-capability-baseline-test-v1.md)
- [Project v1 基线审查记录](project-v1-baseline-review.md)
- [Project v1 缺口地图](project-v1-gap-map.md)
- [嵌入式 Copilot 与对话记忆设计](embedded-copilot-and-memory-design-v1.md)
