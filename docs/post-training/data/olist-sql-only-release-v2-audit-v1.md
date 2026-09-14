# Olist SQL-only Release v2 数据审查

## 1. 审查范围

本报告只审查已冻结的 Olist SQL-only Release v2，不修改数据、不启动训练，也不把 TheLook
内容读入本次统计。

外部资产：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-domain-sft-release-v2-20260909/
    sft-release-surface-random-v2/
      train.jsonl                         2,400
      validation.jsonl                      600
      final_evaluation_only/in_domain_test.jsonl 600
    queryspec-materialized-v2/query_specs.jsonl
    question_variants.json                 18,000 个 surface variants
    release_manifest.json
```

本次使用的文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `train.jsonl` | `43a8312c958cdc4501ef78738ad6ba7fa246938b43bcb97470704aece3d4fd5f` |
| `validation.jsonl` | `1d5a6cac70a76008e369360aed81c42b8fc4988622a9c6f69107ef918ce164ef` |
| `in_domain_test.jsonl` | `2013f6c453f33a16c1cf396a7817f24164b4e1e74d64041a1695e9ec823b6b5e` |

## 2. 一条样本到底是什么

每个 JSONL 行是一个已经通过 Gold SQL 准入的 SQL-only SFT 实例。核心字段可以分成几组：

| 组 | 字段 | 含义 |
| --- | --- | --- |
| 模型输入 | `rendered_prompt` | 生产候选 SQL Prompt，包含中文问题、Catalog、QueryPlan、ResultContract，末尾是 `### SQL`。 |
| 训练标签 | `candidate_sql` | deterministic renderer 生成并通过 Policy、PostgreSQL reader 和 ResultContract 的 canonical PostgreSQL SQL。 |
| 拼接边界 | `training_text` | `rendered_prompt + "\n" + candidate_sql`；tokenizer 再追加 EOS。Prompt token 的 `labels` 为 `-100`，只对 SQL 和 EOS 计算 loss。 |
| 追溯元数据 | `sample_id`、`seed_id`、`query_spec_id`、`family_id`、`sql_program_id` | 用于定位结构实例、程序族、切分和 renderer。 |
| 质量证据 | `admission_status`、`execution_outcome`、`token_length` | 记录 Gold 准入、只读执行/结果合同和长度审计。 |

五种中文问法是同一 QuerySpec 的 surface overlay，不是五条独立语义样本。`question_variants.json`
有 18,000 条表面变体，但正式 SFT 只有 3,600 条主表述：每个 query instance 通过稳定 hash 选择一条
变体。它扩大的是语言表面，不会新增指标公式、Join 程序或业务能力。

## 3. 三个 split 的实际覆盖

### 3.1 总览

| split | 行数 | family | QuerySpec | 不重复 SQL | SQL skeleton | 典型用途 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| train | 2,400 | 914 | 2,400 | 2,400 | 742 | 参数更新；同一 family 可有多个合法时间窗口 |
| validation | 600 | 600 | 600 | 600 | 523 | 模型选择；每个 family 一个实例 |
| in-domain test | 600 | 600 | 600 | 600 | 498 | 只做最终域内评测；不进入训练或 checkpoint 选择 |

训练集 family 实例数为：293 个 family 各 1 条、377 个各 3 条、244 个各 4 条。
这解释了为什么 2,400 行不能等同于 2,400 个独立程序。多出来的实例主要是同一结构族的不同
时间窗口和指标组合实例，而不是新的数据库 schema。

### 3.2 结果形态、指标数量和程序族

| split | `scalar` | `state_grouped` | `category_grouped` | `time_series` |
| --- | ---: | ---: | ---: | ---: |
| train | 638 (26.6%) | 639 (26.6%) | 8 (0.3%) | 1,115 (46.5%) |
| validation | 181 (30.2%) | 212 (35.3%) | 2 (0.3%) | 205 (34.2%) |
| test | 196 (32.7%) | 173 (28.8%) | 0 (0%) | 231 (38.5%) |

指标数量非常偏向多指标：

| split | 1 个指标 | 2 个指标 | 3 个指标 | 4 个指标 |
| --- | ---: | ---: | ---: | ---: |
| train | 92 | 316 | 841 | 1,151 |
| validation | 21 | 98 | 159 | 322 |
| test | 20 | 73 | 198 | 309 |

三个主要程序族占比：

| 程序族 | train | validation | test |
| --- | ---: | ---: | ---: |
| `JP11_purchase_time_multi_metric` | 1,098 (45.8%) | 204 (34.0%) | 231 (38.5%) |
| `JP10_state_multi_metric` | 626 (26.1%) | 208 (34.7%) | 168 (28.0%) |
| `JP09_scalar_multi_metric` | 623 (26.0%) | 177 (29.5%) | 191 (31.8%) |
| 三者合计 | **2,347 (97.8%)** | **589 (98.2%)** | **590 (98.3%)** |

其余程序族在 train/test 中很稀疏：`JP01`/`JP02`/`JP03`/`JP04`/`JP05`/`JP06`/`JP07`/`JP12`
大多是个位数到十几条。尤其是 `category_grouped` 只有 8 train、2 validation、0 test，
因此目前不能声称模型覆盖了品类查询。

### 3.3 十项指标的分布

训练集中的指标出现次数如下。多指标一行会贡献多个出现次数，因此这里不是样本数：

| 指标 | train | validation | test | 判断 |
| --- | ---: | ---: | ---: | --- |
| `cancellation_rate` | 912 | 200 | 230 | 充分，但仍主要在多指标程序中 |
| `paid_order_count` | 910 | 211 | 214 | 充分 |
| `freight_amount` | 870 | 219 | 202 | 充分 |
| `average_order_value` | 859 | 228 | 217 | 充分；需要继续关注二层聚合 |
| `gmv` | 854 | 219 | 218 | 充分 |
| `item_count` | 852 | 216 | 220 | 充分；需要继续关注商品行粒度 |
| `on_time_delivery_rate` | 848 | 210 | 225 | 充分；需要继续关注 eligible 分母 |
| `average_delivery_days` | 833 | 212 | 222 | 充分；需要继续关注时间字段 owner |
| `positive_review_rate` | 464 | 145 | 117 | 稀疏，评价时间程序很少 |
| `average_review_score` | 449 | 122 | 131 | 稀疏，评价时间程序很少 |

评价指标不是没有出现，而是缺少独立的标量、评价时间序列和多指标评价结构。继续复制订单
指标组合不会补上这个缺口。

## 4. 语言表面审查

将正式行的 `language_variant_id` 回填到外部 `question_variants.json` 后，使用仓库内
`audit_olist_question_diversity.py` 做启发式归一化（日期、已登记指标别名、维度别名和数字替换）：

| split | 不重复问题 | 归一化模板 | 归一化重复行 | 结论 |
| --- | ---: | ---: | ---: | --- |
| train | 2,400 | 173 | 2,227 (92.8%) | 表面问法高度重复 |
| validation | 600 | 141 | 459 (76.5%) | 有变体，但仍集中 |
| test | 600 | 143 | 457 (76.2%) | 不能代表开放式中文表达 |

这是启发式统计，不是语义准确率。它能证明“逐字不重复”不等于“语言多样”，但不能单独
判定某个问题自然或不自然。更细的人工审阅仍应按正式/口语/管理者/省略/结果导向五类抽样。

完整的 18,000 条 overlay 也已用同一审计器运行，得到 190 个归一化模板；该数字不能与上表
的 split 主表述模板数相加。可复现命令为：

```bash
python scripts/post_training/data/audit_olist_question_diversity.py \
  --input-jsonl /disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/runtime-prompts-v2/runtime_candidates.jsonl \
  --output-json /disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/question-diversity-audit-v1/report.json
```

该命令只输出聚合统计和 ID，不把原始问题或 SQL 写入仓库；报告位于仓库外。

## 5. 长度与切分边界

`max_seq_length=3072`，没有静默截断；当前序列长度如下：

| split | P50 | P95 | max |
| --- | ---: | ---: | ---: |
| train | 2,348 | 2,793 | 2,917 |
| validation | 2,358 | 2,795 | 2,921 |
| test | 2,341 | 2,772 | 2,884 |

当前没有长度排除，但 P95 已接近上限。以后加入第二 workspace 的 Catalog 或更长问题时，
不能把长度上限悄悄放大，也不能截断 SQL；超长行应保留 exclusion manifest。

切分审计通过：

- train/validation/test 的 `family_id` 交集均为 0；
- `query_spec_id` 交集均为 0；
- 完整结构签名交集均为 0；
- `sql_program_id` 和 SQL skeleton 可跨 split 共享，这是同一 renderer/Join 原子的共享，
  不等于完整答案泄漏；三者的 SQL skeleton 交集分别为 122、120、100，说明程序模板共享明显。

## 6. 第一性结论

### 已经做得对的地方

1. Gold SQL 不是模型自举结果，而是 QuerySpec、指标合同和 deterministic renderer 生成，并通过
   Policy、只读角色和结果合同。
2. family/QuerySpec 物理隔离有效；final test 也确实不参与训练和模型选择。
3. Prompt、SQL、EOS 边界和长度审计是可回放的，训练行可追溯到运行时合同。

### 当前数据的主要问题

1. **程序覆盖集中。** 97% 以上样本落在三个多指标程序族；category test 缺失，评价时间程序稀疏。
2. **单指标训练不足。** 模型主要看到 3--4 指标 CTE 合并，容易学会固定模板，却不能证明单指标、
   小组合和复杂粒度都稳健。
3. **行数高估独立能力。** train 的 914 family 扩成 2,400 行主要依靠同一 family 的时间窗口和组合，
   继续按同一方式扩到一万行收益会很低。
4. **跨 schema 证据不足。** Olist 共享 Catalog/renderer 的 in-domain 600/600 只能证明协议拟合；
   TheLook 才是未见 schema 的后置泛化门，不能被 Olist 训练替代。
5. **中文表达仍集中。** 需要受控增加别名、语序、口语和省略，但不能用无约束改写改变 QuerySpec。

## 7. 扩展优先级

下一版不应先把行数从 3,600 机械扩大到数万。建议按以下顺序：

1. 先补结构稀缺项：`category_grouped` 的独立 test、评价时间 `JP12`、单指标和 2 指标组合、AOV
   二层聚合、订单 DISTINCT、状态分母和跨事实表多指标的独立 CTE。
2. 将每个程序族设最低 family 配额，并让 validation/test 也包含同一程序族；没有 test 的能力不写进
   “已覆盖”。
3. 每个新 family 再产生 3--5 个受控中文表达；表面变体不能改变指标、时间、维度、结果列或 Gold hash。
4. 引入第二个关系型电商 workspace 后，按 workspace、family 和程序族分层采样；不要把 Olist 的
   物理表名、指标 ID 或 renderer 输出直接复制成跨域训练模板。

因此当前最合理的下一步是“选择并审查第二个数据集 + 做 Olist 稀缺程序小批”，而不是立刻启动
新一轮 GPU 训练。
