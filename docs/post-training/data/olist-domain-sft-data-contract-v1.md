# Olist 领域 Candidate SQL SFT 数据合同 v1

## 1. 决策与范围

**合同 ID：** `olist-domain-sft-data-contract-v1`<br>
**状态：** 已冻结设计；尚未物化任何样本、未加载 tokenizer、未启动 GPU 或训练。<br>
**目标工作区：** `olist-catalog-v2` / `metric_version=0.2-frozen` / `olist-kaggle-v2-2026-08-03` / PostgreSQL / `sql-policy-v1`。<br>
**训练对象：** 离线 `Qwen2.5-Coder-1.5B` LoRA candidate generator；它只产生候选 SQL。<br>
**产品默认路径：** 不变，仍为 Vanna/SiliconFlow。领域 Adapter 不接入运行时，也不能绕过既有可信查询链路。

本合同回答一个可反驳的问题：在**和当前 Olist 运行时完全相同的候选 SQL 输入接口**下，使用严格隔离、可执行且口径受审查的领域样本进行 SFT，能否使小模型相对 matching Base 和现有 CSpider Adapter 更常产生符合 Olist 业务语义、PostgreSQL 方言和结果合同的 SQL 候选。

本轮只冻结数据资产的接口、边界、split 和质量门。它不是数据生成、训练、评测或运行时接入的授权。

### 非目标

- 不训练或替代 `QuestionRouter`、澄清、帮助回答、`SqlPolicy`、PostgreSQL readonly role、`ResultValidator`、`ResultContract` 或 `ChartContract`。
- 不把当前 CSpider/Spider 样本、其 SQLite SQL 或 benchmark 分数混入 Olist 领域样本。
- 不使用当前 60 条 protected Olist holdout、其中此前用于迁移诊断的 12 条案例，或它们的同义改写、SQL 改写、few-shot 示例和反向合成样本。
- 不把可执行 SQL、低训练 loss 或 validation 改善表述为业务准确率或生产可用性。

## 2. 要对齐的真实运行时接口

Olist 不会只向模型暴露指标 ID，也不会只暴露物理 schema。每条可训练样本必须以当前
`render_candidate_sql_prompt()` 输出的完整 Prompt 作为输入，固定版本为
`olist-candidate-sql-v1`。其来源为服务器已经完成的受控链路：

```text
question
-> answerable database route
-> role-scoped Semantic Catalog selection
-> QueryPlan
-> ResultContract
-> CandidateSqlContext
-> render_candidate_sql_prompt()
-> target PostgreSQL SQL + EOS
```

训练 Prompt 必须包含并且仅包含服务器在该轮实际提供的内容：

| Prompt 区域 | 训练中保留的内容 | 作用 |
| --- | --- | --- |
| Candidate contract（含 ResultContract 投影） | SQL-only、单条 read-only SELECT/WITH SELECT、由 `required_result_columns` 派生的最终列白名单 | 约束输出接口，不把解释文本训练为 completion。 |
| Semantic Catalog | 选中的业务指标定义、粒度、时间字段、默认过滤、真实表/列、Join、维度和归因限制 | 让模型学习从业务指标编译到真实物理列，而不是把 `gmv` 等指标 ID 当作表字段。 |
| QueryPlan | 查询形状、指标、维度、时间范围、执行策略和 warnings | 固定本轮查询意图与多指标/粒度边界。 |
| Question | 去标识化的单轮中文业务问题 | 提供自然语言表达。 |

禁止为训练另造 CSpider 风格的 `### SQLite schema` 模板、另加未经运行时使用的 chat template，或向输入注入 Gold SQL、数据库结果行、未来答案、人工解释和未检索 Catalog。否则训练/推理接口不一致，无法诊断领域对齐是否有效。

监督格式固定为 Causal LM：

```text
input_ids = exact_rendered_prompt + canonical_sql + EOS
labels    = -100 for every prompt token + canonical_sql tokens + EOS
```

右侧动态 padding 只在 batch collator 发生，使用 `attention_mask=0`、`labels=-100`；不得进入单样本长度或监督目标。目标 SQL 不能包含 Markdown fence、解释、`SQL:` 前缀、多个语句或 repair 对话。

## 3. Olist 领域样本的语义单元

### 3.1 从 QuerySpec 到训练行

领域样本的事实来源不是模型输出，而是一个先审查再物化的结构化 `QuerySpec`。每个
`QuerySpec` 表示一个业务查询程序，例如它声明指标、结果形状、允许维度、已确认的绝对时间范围和受控 Join 程序；它不是最终 SQL 字符串。v1 不包含自由过滤、排序、LIMIT、Top-N 或未冻结归因策略。

```text
Catalog snapshot + permitted QuerySpec
-> deterministic PostgreSQL Gold SQL renderer
-> SqlPolicy
-> PostgreSQL reader-role execution
-> ResultContract / ResultValidator
-> semantic review
-> exact runtime Prompt + canonical SQL target
```

`QuerySpec -> SQL` renderer 是下一项独立工作，当前不存在。它必须由 Catalog/固定语义规则驱动；不能把外部模型生成、一次人工复制的 SQL 或“看起来合理”的 SQL 直接升格为 Gold。

### 3.2 指标与字段的职责

训练目标要求模型同时遵守以下区别：

| 类型 | 例子 | 在 SQL 中的正确角色 |
| --- | --- | --- |
| 业务指标 ID | `gmv`、`paid_order_count`、`average_delivery_days`、`positive_review_rate` | 指标定义的语义入口和最终结果 alias；不是物理列。 |
| 物理表/列 | `fact_orders.order_purchase_timestamp`、`fact_orders.order_delivered_customer_date`、`dim_customers.customer_state` | 目标 SQL 的表达式、过滤、Join 和分组依据。 |
| 结果合同列 | `customer_state`、`average_delivery_days`、`time` | 最终顶层 SELECT 可输出的严格白名单。 |

例如 `average_delivery_days` 的合格候选必须使用 Catalog 所给的订单时间字段、非空送达过滤和允许的 Join 路径计算聚合值，并以 `AS average_delivery_days` 返回；直接引用同名字段是错误的。每条领域样本都要显式记录这一类“指标定义到物理表达式”的来源证据。

### 3.3 首版可训练范围

首版 SQL SFT 只接纳满足全部条件的单轮 `answerable` 数据库问题：

1. `QuestionRouter` 允许生成 SQL；
2. Catalog 和 QueryPlan 没有未解决的归因需求、`dimension_not_supported_by_all_metrics` 或不可执行 warning；
3. SQL 可由确定性 renderer 在 PostgreSQL 方言下生成唯一的 canonical target；
4. 目标可经完整 SQL Policy、reader role 和结果合同 gate；
5. 指标口径、事实粒度、默认过滤、Join 和最终别名能够人工审核。

SQL SFT 不接纳下列样本：常识/帮助/指标释义、非数据库问题、需要澄清的问题、无定义指标、越权对象、未解决的支付归因、按品类计算订单/评价粒度指标、结果追问和多轮记忆问题。它们并非“不重要”，而是属于 Router/澄清或对话数据，需要另立输出标签和评测合同，不能强配一条 SQL。

当前 `olist-pilot-coverage-v0.1.md` 仅作为历史的安全单指标探索范围参考。本合同不把其 18 个覆盖单元或 120--160 条设想行自动视为可训练数据；任何 v1 覆盖范围均须在下一项覆盖盘点中重新冻结。

## 4. 数据规模与切分合同

### 4.1 规模单位

数据集必须同时报告三层计数，禁止只报告“几千条”而掩盖模板重复：

| 单位 | 定义 | 不允许的虚增方式 |
| --- | --- | --- |
| `QuerySpec` | 一种结构化业务查询程序 | 同一个程序只替换日期或排序措辞。 |
| `family_id` | 同一业务语义、SQL 程序、结果形状和可替换表达的隔离组 | 将同一 Gold SQL 的中文改写计作泛化能力。 |
| materialized row | 一条 `Prompt -> SQL + EOS` SFT 行 | 用大量表面同义句冒充独立领域覆盖。 |

首版物化的规模目标为约 `3,000` train rows、`400--500` validation rows 和 `400--500` in-domain test rows；它是资源与覆盖规划目标，不是完成判据。每个 split 的最小 `family_id` 数、QuerySpec 覆盖矩阵、语言表达比例和 SQL 形状比例由下一项覆盖盘点提出并经审阅冻结。若无法在不制造近重复的前提下达到这些规模，应缩小规模而不能复制模板。

### 4.2 Olist 的 split 方法

Olist 只有一个业务 Schema，不能伪造 Spider 的 database-disjoint 结论。因此 v1 使用**语义程序族隔离**，而不是随机逐行切分。每个训练记录都需要至少具有：

```text
sample_id
query_spec_id
family_id
sql_program_id
language_variant_id
split
```

`family_id` 至少由以下不含字面日期/措辞的语义特征稳定派生：

```text
ordered_metric_ids
+ plan_type / result_shape
+ dimensions
+ time_grain + time_filter_semantic_mode
+ filter_semantic_classes
+ join_program_id
+ aggregation_and_deduplication_strategy
+ ranking_or_limit_mode
+ attribution_rule_id_or_none
+ catalog_version + metric_version + policy_version
```

必须满足：

- 同一 `family_id`、`query_spec_id`、`sql_program_id` 及其中文改写全部只属于一个 split；
- 仅替换 literal 时间范围、数字阈值、排序文案、SQL 格式或表别名的行仍视为同族，不能跨 split；
- validation 只能用于模型选择、epoch/learning-rate/prompt 等开发决策；in-domain test 只在配置冻结后用于一次最终对照；
- train、validation、test 可以共享基础指标和物理 Schema，但不得共享完整查询程序或近重复程序；测试应有预先声明的组合/语言泛化单元；
- `evals/manifests/post_training_holdout_v1.yaml` 的 60 条案例是永久外部 gate。其问题、答案、SQL、派生改写、相同 SQL 程序或 few-shot 线索都不能参与此数据集的任何阶段；当前 12 条业务迁移评测继续按该保护边界处理；
- 生成、切分和审计流程不可读取 holdout 的问题、Gold SQL、结果或人工标签。若要检测候选数据与 holdout 的同族/同程序冲突，必须由隔离步骤预先生成只含不可逆 `family_id` / `sql_program_id` 摘要的受保护 exclusion manifest；数据构造器只读取该摘要及 case ID / hash，不读取原文或答案。

### 4.3 Split audit 的最低证据

每次物化必须在仓库外生成 `split_audit.json`，并记录：输入 Catalog/数据/代码 hash，行数，`QuerySpec`/family/SQL-program 计数，split 交集，holdout 碰撞数，近重复检测版本和结果，token 长度统计，排除清单 hash。任何非零的 family/program/holdout 交集、缺少版本 hash、或未经解释的近重复都应 fail closed，拒绝进入训练。

## 5. 训练长度与本地资产边界

`max_seq_length` 冻结为 `1536`，长度公式必须与实际 SFT Dataset 一致：

```text
tokenize(rendered_prompt + SQL marker, add_special_tokens=False)
+ tokenize(canonical_sql, add_special_tokens=False)
+ 1 EOS
```

长度审计在模型加载前进行。禁止静默截断 Catalog、QueryPlan、Question 或 SQL；超过上限的样本必须写入不含原文/SQL 的外部 exclusion manifest，并在训练入口加载前再次校验。该合同尚无 Olist 实测长度分布，因此 `1536` 是待验证的运行上限，不是已证明覆盖率的结论。

仓库内只保留合同、构造/审计代码、schema、fixture、manifest 和聚合证据。原始 Olist 数据、完整问题、完整 rendered Prompt、Gold SQL、执行结果、训练 JSONL、模型/adapter、checkpoint 和日志均保存在：

```text
/disk2/gengnan/data-analysis-agent-data/
```

后续应使用独立、版本化的 `text-to-sql/olist-domain-sft/<release>/` 路径；不得在仓库 `data/` 下创建原始数据副本，也不得提交含受保护问题或 SQL 的数据片段。

## 6. Gold SQL 与样本准入质量门

一条行只有按此顺序通过，才能进入 train、validation 或 in-domain test：

```text
permitted QuerySpec
-> exact runtime Prompt rendering
-> deterministic canonical PostgreSQL SQL
-> SqlPolicy pass
-> daa_analytics_reader execution pass
-> ResultValidator / ResultContract pass
-> catalog-semantic review
-> split / holdout / near-duplicate audit pass
-> token-length pass
```

各门的含义不能混淆：

| 质量门 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| SQL Policy | 只读、对象白名单、语法/结构策略边界满足 | 指标口径正确。 |
| reader role execution | PostgreSQL 可以按最低权限执行该 SQL | 没有 Join 放大或业务语义错误。 |
| ResultContract / ResultValidator | 必需 alias、结果结构、范围和部分异常风险满足 | 完整业务事实或因果解释正确。 |
| Catalog-semantic review | 指标表达式、粒度、默认过滤、Join/归因与 Catalog 一致 | 模型已学会该能力。 |
| split audit | 不存在定义范围内的泄漏或近重复 | 对未见业务 Schema 的泛化能力。 |

Gold 的 canonical form 必须是单条 PostgreSQL read-only SQL，并稳定使用 Catalog 的 metric ID / time / dimension alias。若同一 QuerySpec 存在多个结果等价 SQL，renderer 必须选择一个稳定 canonical form；不要把不同格式的同义 SQL 随意混入 target，避免把训练噪声误判成模型能力。

每个首版指标、事实表、Join 程序、时间字段、聚合/去重策略和边界类别都要进行预定义比例的人工语义抽检。具体抽检率、审阅表和覆盖阈值由下一项覆盖矩阵冻结；“所有 Gold 都执行成功”不能取代人工口径审查。

## 7. 物化记录与可复现性

每个外部训练行至少记录以下字段；含原文、完整 Prompt、SQL 或结果的字段必须保持 Git 忽略：

| 字段 | 目的 |
| --- | --- |
| `sample_id`, `split`, `query_spec_id`, `family_id`, `sql_program_id`, `language_variant_id` | 稳定追踪、隔离和近重复审计。 |
| `workspace_id`, `catalog_version`, `dataset_version`, `metric_version`, `policy_version`, `prompt_version` | 确认运行时接口版本。 |
| `route_snapshot`, `catalog_selection_ids`, `query_plan`, `required_result_columns` | 证明监督上下文来自服务端边界。 |
| `question_redacted`, `rendered_prompt`, `canonical_sql` | 实际 SFT 输入/目标，仅存外部受控路径。 |
| `policy_evidence`, `reader_execution_evidence`, `result_contract_evidence`, `semantic_review` | 逐层准入证据。 |
| `token_length`, `tokenizer_fingerprint`, `materializer_version` | 长度和重现实验。 |
| `source_provenance`, `created_at`, `record_hash` | 追溯与防止无声改写。 |

每次训练前，训练入口必须验证 manifest 与物化 JSONL 的 SHA-256、row count、split audit、Catalog/Prompt/tokenizer 指纹、最大长度和 holdout 碰撞状态；任一不匹配即拒绝加载模型。训练只读取 train/validation，绝不读取 in-domain test 或永久 holdout。

## 8. 评测和停止条件

领域训练完成后，先用冻结 validation 选 checkpoint，随后以 matching Base、CSpider Adapter 和 Olist-domain Adapter 在相同 Olist Prompt、模型 revision、decode 参数、seed、token 上限、SQL repair 状态、Catalog/Policy/数据库快照下比较。生成阶段不得读取各测试/holdout Gold SQL 或结果行。

至少逐条记录：是否生成 SQL、Router 是否正确、是否本应澄清、Policy 是否接受、PostgreSQL 是否执行、指标口径人工判定、ResultContract 是否通过、是否合规、有无证据、修复次数、耗时和 token。字符串 exact match 仅可做诊断，不作为业务主指标。

出现下列任一情况，停止进入运行时接入并回到错误分析：

- 领域 Adapter 在 validation 或永久业务 holdout 的 `ResultContract valid` / 语义人工核验没有相对 matching Base 的可复核提升；
- SQL Policy、reader-role 执行或安全回归出现明显退化；
- 有 holdout/近重复泄漏、版本 hash 不一致、静默截断或 Gold 审核失败；
- 训练数据不能证明 Prompt 与生产 `olist-candidate-sql-v1` 一致。

即便通过上述门，也只允许讨论下一项“受控 shadow candidate generator”设计；不得自动替换 Vanna/SiliconFlow 默认生产路径。

## 9. 后续单一任务顺序

已完成：盘点当前 v2 Catalog 中可训练的指标、维度、Join、时间与归因边界，并冻结
[`olist-domain-sft-coverage-matrix-v2.md`](olist-domain-sft-coverage-matrix-v2.md)；已实现 QuerySpec/renderer、受控
结构物化器、15 条静态 coverage seed fixture 和受限 protected-family summary 导出器。真实 protected summary
尚未导出，未生成 Prompt/训练行。之后只按以下顺序逐项推进，每项需要单独审查与用户确认：

1. 受限人工环境审阅 protected case family 映射，运行导出器生成真实 fingerprint summary/evidence；
2. 设计并执行 evidence 绑定的小批结构物化，验证 seed/summary/版本/家族/程序碰撞；
3. 让小批 Gold 逐条经过 Policy、reader role、ResultContract/ResultValidator 与人工语义抽检；
4. 仅对已准入记录派生真实运行时 QueryPlan/Prompt，并审阅受控中文 query 语言变体；
5. 扩展独立 family/program 后冻结正式 split audit，再进行 token-length audit，证明 `1536` 的真实覆盖率；
6. 用户审阅通过后才物化完整 train/validation/in-domain-test、训练和 matching 评测。

任何步骤都不能顺带推进下一项。
