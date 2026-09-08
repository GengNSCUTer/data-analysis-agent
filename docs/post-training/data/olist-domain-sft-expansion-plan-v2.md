# Olist 领域 SFT 数据扩展方案 v2

## 1. 这份方案要解决什么

TheLook 是 Olist LoRA 在未见电商 schema 上的跨 schema 评测。统一清洗候选前缀后，Adapter 的 206 条结果为：

| 阶段 | 数量 |
| --- | ---: |
| 生成 | 206/206 |
| 通过 Policy 并执行 | 174/206 |
| ResultContract 通过 | 171/206 |
| Gold ordered denotation match | 100/206 |

这说明当前主要瓶颈已经不是“模型有没有输出 SQL”，而是：

1. 能否从运行时提供的 schema 和 Catalog 选择最小、正确的表连接路径；
2. 能否把业务指标编译成正确的聚合粒度和分母；
3. 能否在多指标、维度和时间序列组合中保持每个 CTE 的口径一致；
4. 能否理解自然语言的不同表达，而不是记住固定模板。

本方案只规划下一版数据建设，不授权修改生产路由、Policy、reader role 或 ResultValidator，也不把 TheLook 测试内容加入 Olist 训练。

## 1.1 调研得到的通用原则

结合仓库已有的 Text-to-SQL 论文与开源实现调研（Spider/CSpider、BIRD、DIN-SQL、DAIL-SQL、BIRD-INTERACT，以及 schema retrieval/self-correction 方向），可以抽出几条对本项目真正有用的原则：

- **覆盖程序，不只覆盖字符串。** Spider/BIRD 类基准把数据库、查询结构、Join 和难度作为独立维度；同一 SQL 的大量改写不能替代新的查询程序。
- **schema linking 要单独看。** 近期 schema retrieval/context compression 工作都强调先缩小相关表列，再生成 SQL；数据中应有最小 Join、相似表名和错误关联的对照，而非只放最终正确 SQL。
- **分解和验证不是 Gold 的替代品。** DIN-SQL 等方法把意图、schema linking、SQL 生成拆开，执行反馈/self-correction 用于发现错误；本项目可以借鉴错误标签和验证集设计，但 Gold 仍由 QuerySpec/renderer 冻结，不能由模型自举。
- **交互数据要单独建合同。** BIRD-INTERACT 的 ask/clarify/execute/submit 思路说明澄清和工具预算应单独评测；“无法回答/需要澄清”不能强行配一条 SQL 混入 SFT。
- **语言改写必须保持语义身份。** 中文变体可以扩大语言覆盖，但必须通过结构化 QuerySpec identity check、路由重建和 Gold hash 检查；自由 LLM paraphrase 不能直接入库。

这些工作支持“结构化程序族 + 受控语言变体 + 分层评测”的路线，但不意味着我们要直接复现论文的模型、数据或 RL 流程。

## 2. 评测暴露出的数据缺口

### 2.1 Schema linking 与 Join

TheLook Adapter 仍会生成不存在的表/列，或为了“流量来源”无条件连接 `events`。因此 Olist 下一版必须明确提供：

- 每个指标的最小合法 Join path；
- 常见错误路径的对照，例如订单归因使用 `orders -> users -> users.traffic_source`，不能用 `events.traffic_source` 代替；
- 多表 Join 后订单必须 `COUNT(DISTINCT order_id)` 的事实粒度；
- 不存在表、列、连接键以及无必要 Join 的拒答/错误分析样本。

错误 SQL 不能作为普通 SFT target。它可以作为拒答分类、偏好数据、验证器回归或错误标签的输入，但不能教模型生成错误答案。

### 2.2 指标公式与粒度

当前最明确的业务错误是：

- AOV 被写成 `AVG(item.sale_price)`，而合同要求先按订单 `SUM`，再对订单总额求 `AVG`；
- Join 商品行后把 `COUNT(order_id)` 写成非去重计数；
- 退货率分子/分母状态集合不完整；
- 履约指标的时间序列误用 `delivered_at`，而 Olist 合同规定按订单创建时间分桶；
- 多指标查询中一个 CTE 的粒度或过滤错误会污染整条结果。

因此每个高风险指标至少要覆盖 scalar、维度分组、月/季度时间序列和多指标组合，而不能只增加同一 SQL 的日期改写。

### 2.3 当前中文问法的模板集中

对 Olist Medium v1 的 1,200 条问题做初步归一化（仅替换部分日期、时间范围和常见指标词）曾得到约 108 类主要句式；本轮新增审计器使用完整的指标/维度别名表和 QueryPlan 兼容读取，严格归一化后得到 38 类模板。两种数字代表不同的归一化粒度，不能直接比较；它们共同说明高频句式集中在：

```text
请统计 + 时间范围 + 各客户州/整体 + 指标列表
请按季度/按月统计 + 时间范围 + 指标列表
```

文本表面上 1,200 条均不重复，但这不等于 1,200 种语言能力。当前数据缺少：

- 口语、省略和管理者表达；
- 指标别名，如“成交金额/销售额/营收”与 `gmv` 的受控映射；
- 不同的日期表达和“截至某日”的边界表达；
- 先说业务目的、后说维度和指标的自然顺序；
- 真实多轮追问和需要澄清的非 SQL 问题。

问题是成立的，但解决方式不是让 LLM 无约束改写。无约束改写会偷偷改变指标、时间边界、粒度或引入“同比/Top-N”等尚未冻结的能力。

## 3. 扩展数据的两个独立轴

### 轴 A：语义程序覆盖

先固定 `QuerySpec -> deterministic Gold SQL`，再扩展下列程序族：

| 程序族 | 建议占比 | 本轮必须覆盖 |
| --- | ---: | --- |
| 单指标 scalar | 15% | 十项指标、全量与绝对日期窗口 |
| 单指标维度分组 | 20% | 客户州、商品类目、卖家/评价等合同允许的维度 |
| 单指标时间序列 | 15% | 月、季度；统一订单创建时间字段 |
| 多指标 scalar | 15% | 2--4 个指标、独立 CTE 后再合并 |
| 多指标分组/时间序列 | 15% | 相同粒度、相同时间合同、AOV 与订单指标组合 |
| 粒度难例 | 15% | AOV 二层聚合、订单 DISTINCT、比例分母、状态过滤 |
| Schema/安全拒答 | 5% | 不存在对象、错误 Join、敏感投影或需要澄清 |

占比是覆盖目标，不是为了凑行数。每个程序族需要记录 `family_id`、`query_spec_id`、`sql_program_id`、`join_program_id` 和风险标签。

### 轴 B：自然语言表达覆盖

同一个已冻结 QuerySpec 可以有多种 surface form，但所有变体必须保持完全相同的：

- `metric_ids`、`result_shape`、维度和时间范围；
- 时间粒度、Join 程序、过滤与必需结果列；
- Gold SQL 和 Prompt 版本。

建议每个 family 首轮保留 5 种语言形态：

1. 正式：`请统计 2018 年各州的有效订单数。`
2. 业务口语：`看一下 2018 年各州完成了多少单。`
3. 管理者表达：`我想比较 2018 年不同州的成交订单规模。`
4. 简洁省略：`2018 各州订单量。`
5. 结果导向：`按州列出 2018 年订单数，方便做表格比较。`

可选的第 6--8 类包括受控指标别名、不同日期书写和轻微业务背景。首轮不加入同比/环比、Top-N、因果解释、模糊“最近/本月”或未冻结自由筛选。

## 4. 推荐规模与切分

当前 720/240/240 的 Olist Medium v1 已完成一轮训练，下一版建议分两步：

### 4.1 先做可审阅的小批

先选 20 个新的语义 family，每个 family 生成 5 种中文问法，共约 100 条。这个小批只验证：

- 变体是否保留 QuerySpec 身份；
- 中文表达是否自然而不改变口径；
- Router/Catalog/QueryPlan 是否能还原同一计划；
- 完整 Gold SQL 准入链路是否通过；
- 句式分布是否比旧版更丰富。

小批通过后再自动化扩展；任何一条语义漂移都先修生成规则，不扩大规模。

### 4.2 再物化中等规模 release

建议目标为约 `2,400` 条：

```text
train：1,600
validation：400
in-domain test：400
```

如果程序族数量不足，不复制同一 Gold SQL 来填数；可以降到 1,000--1,500 条，并如实报告 family 数。行数是训练吞吐指标，`family/program` 数和跨 split 泛化才是能力证据。

切分按 `family_id`/`sql_program_id` 隔离：同一 family 的所有中文变体只能进入一个 split。可以共享底层表和原子指标，但不共享完整指标组合、结果形状、维度、时间模式和 Join/聚合程序。TheLook 保持独立 final test，只用于跨 schema 回归。

## 5. 生成与质量控制流水线

```text
冻结 Catalog/Metric Contract
 -> 生成 QuerySpec family
 -> deterministic Gold SQL renderer
 -> Policy -> reader role -> ResultContract/ResultValidator
 -> 生成 5--8 个中文 surface forms
 -> 结构身份检查
 -> Router/Catalog/QueryPlan 重建检查
 -> Prompt/SQL 长度检查
 -> family split、holdout、近重复审计
 -> 分层人工/LLM 辅助口径抽检
 -> 物化 train/validation/test
```

LLM 只用于语言润色建议和分层抽检，不能决定 Gold SQL、指标公式或准入结果。每条记录至少保留：

```text
sample_id, query_spec_id, family_id, sql_program_id,
language_variant_id, split, prompt_version, catalog_version,
gold_sql_sha256, length_stats, admission_status
```

质量报告至少包括：

- exact duplicate rate、模板重复率和 n-gram 聚类；
- 中文问题长度 P50/P95；
- 指标别名、维度表达、时间表达覆盖；
- Router agreement、QuerySpec preservation、Gold SQL hash 一致率；
- 每个程序族的准入率、Policy/执行/结果合同失败数；
- train/validation/test 的 family、program、holdout 交集，必须为零。

## 6. 对“负例”的边界

负例需要分开存储，不能混入 canonical SQL SFT target：

| 负例类型 | 适合用途 | 是否作为 SFT SQL 标签 |
| --- | --- | --- |
| 不存在表/列、错误 Join | schema linking 诊断、拒答/偏好训练 | 否 |
| AOV 行均值、非 DISTINCT 订单数 | 错误分类、validator 回归、偏好对照 | 否 |
| 指标无定义、维度归属不清 | Router/澄清训练与评测 | 否 |
| 合法 QuerySpec 的 canonical SQL | Candidate SQL SFT | 是 |

这样既能让模型学习正确候选，也不会把错误 SQL 当作可模仿答案。

## 7. 下一步只做一件事

下一项不直接启动训练。先实现并运行一个**中文 surface-form 多样性审计**，对 Olist Medium v1 和 20-family 小批分别输出：

1. 归一化模板数与最高频模板占比；
2. 开头动词、时间表达、指标别名、维度表达的分布；
3. exact duplicate、模板重复和 family 内变体数量；
4. 发现的语义漂移或近重复样本。

审计报告通过后，再冻结 20-family/100-row 小批，供人工审阅；审阅通过后才开始构建约 2,400 条正式 release。这样能快速验证“语言多样性”这条假设，同时不把大量低质量改写带入训练。

### 7.1 Medium v1 基线审计结果

2026-09-08 已用
`scripts/post_training/data/audit_olist_question_diversity.py` 审计 Medium v1 的
`runtime_candidates.jsonl`。完整报告保存在仓库外：

```text
/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-medium-v1/
  question-diversity-audit-v1/report.json
```

| 项目 | 结果 | 解读 |
| --- | ---: | --- |
| 输入行 / family | 1,200 / 1,200 | 当前每个语义 family 只有一个中文问题。 |
| 完全相同问题 | 0 | 没有逐字重复，不能据此推断语言丰富。 |
| 归一化模板数 | 38 | 日期、指标和维度别名归一化后，句式高度集中。 |
| 归一化模板重复率 | 96.83% | 1,162 行属于重复的表层结构。 |
| `请统计` 开头 | 805 | 开头动词/语气高度单一。 |
| 绝对日期 / all-time / 时间序列 | 400 / 405 / 395 | 时间形态有覆盖，但表达形式仍基本固定。 |
| 单 / 双 / 三 / 四指标 | 50 / 161 / 399 / 590 | 多指标覆盖为主；不能替代口语和别名覆盖。 |

审计同时确认：family 内没有结构签名不一致，三 split 是 `720/240/240`，因此现有
Medium v1 的问题-计划绑定没有发现这个层面的数据漂移。这里的“模板”是按指标、维度、日期
占位后的启发式统计，不等同于 SQL/语义重复判定；它的用途是决定下一批需要增加语言多样性。

## 8. 当前结论

TheLook 结果支持扩大 Olist 领域数据，但方向应是“语义程序覆盖 + 受控语言多样性”双轴扩展，而不是简单增加中文行数。第一优先级是 AOV、订单去重、`traffic_source` 归因、最小 Join 和时间字段；第二优先级才是更多同义问法。新的 Adapter 必须同时通过 Olist in-domain Gold 对照和 TheLook cross-schema 回归，不能只看训练 loss 或单一测试集分数。

本方案是设计文件，不表示新数据集已经生成、训练已经启动或模型已经获得生产接入资格。
