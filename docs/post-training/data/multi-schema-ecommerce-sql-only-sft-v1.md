# 多 Schema 电商 SQL-only SFT v1 冻结合同

## 1. 目的与结论

本合同冻结下一轮后训练的数据设计：在保留 Olist 域内能力的同时，引入**新建且与 TheLook v3 r3 protected test 隔离**的 TheLook 训练/验证数据，训练一个能够在两套电商 Schema 间切换的 SQL-only LoRA Adapter。

本合同冻结数据边界、配额、模板和验收规则。TheLook SFT v1 的静态 seed、deterministic Gold 和 reader-role 准入已在 2026-09-16 完成；本合同仍不授权立即训练。当前已经完成的 Olist v3.1 test 与 TheLook v3 r3 test 继续保持只读 holdout。

核心目标不是让模型记住某个数据集，而是让它学习同一协议：

```text
当前 workspace 的中文业务问题
+ 当前 workspace 的 Semantic Catalog
+ 当前 workspace 的 QuerySpec
+ 当前 workspace 的 ResultContract
-> 只输出当前 workspace 合法的 PostgreSQL SQL
```

## 2. 数据集边界

### 2.1 可用于本轮 SFT 的数据

| workspace | train | validation | 来源 | 角色 |
| --- | ---: | ---: | --- | --- |
| Olist v3.1 | 3,000 | 750 | 已冻结 `olist-v3-balanced-release-v1.1` | 保留已验证的中文 Olist 指标与 SQL 生成能力 |
| TheLook 新 SFT release | 3,000 | 750 | `thelook-sql-only-sft-v1-r4`，全量 Gold/reader-role 已准入 | 补充第二套电商 Schema、join 和事实域 |
| **合计** | **约 6,000** | **约 1,500** | — | 多 Schema SQL-only SFT |

Olist 的现有 `train.jsonl` 与 `validation.jsonl` 字节、行数和 hash 以 [`olist-v3-1-sql-only-training-evaluation-contract.md`](olist-v3-1-sql-only-training-evaluation-contract.md) 为准，不在本合同中重写或就地修改。

### 2.2 永久不可用于训练或验证的数据

以下资产均为最终评测 holdout，不得用于参数更新、validation、few-shot、Prompt 调优、错误回流或 checkpoint 选择：

- Olist v3.1 `final_evaluation_only/in_domain_test.jsonl`（750 条）；
- TheLook v3 r3 `cases.jsonl`（750 条）；
- 两套 holdout 的 Gold SQL、结果行、失败分析和 denotation 报告；
- 任何从 holdout 改写措辞、替换日期、改别名得到的派生样本。

TheLook 新训练/验证 release 必须使用新的 seed、QuerySpec ID、自然语言问题、时间窗口和 Gold SQL。允许复用指标类别、事实域和合法 join program，因为这些是领域能力本身；不允许复用 protected case 的实例身份或答案文本。

## 3. TheLook 新 train/validation 配额

TheLook 新 release 目标为 `3,000 train / 750 validation`。配额按能力而不是简单随机行数分配：

| 能力族 | train 目标 | validation 目标 | 说明 |
| --- | ---: | ---: | --- |
| sales item / order item（商品行、成交额、件数、客单） | 600 | 150 | 维度分组、标量、时间序列均需覆盖 |
| orders / status / fulfillment（订单状态、履约时长、退货） | 750 | 180 | 优先补 duration eligibility、status filter、二层聚合 |
| inventory received / sold / snapshot | 600 | 150 | 重点补 sold/received 字段和库存事实域切换 |
| events / session / traffic | 450 | 110 | 事件计数、会话、来源和低基数维度 |
| users / registration / geography | 450 | 110 | 用户注册、国家/州/流量来源分组 |
| 跨上述事实域的多指标组合 | 150 | 50 | 仅纳入已验证的合法 join program |
| **合计** | **3,000** | **750** | 每个能力族按此目标物化，失败时报告实际减少量 |

最终物化目标为 3,000/750。TheLook SFT v1 已达到该目标：3750/3750 deterministic Gold 在 `daa_thelook_reader` 下通过 SqlPolicy、ResultContract 和 ResultValidator；无 exclusion。若后续版本某个能力族准入失败导致无法达到目标，必须保留实际减少量并说明原因，不得通过复制、只换日期或只换措辞填补缺口。

### 3.1 已实现的 TheLook SFT v1 口径

- 静态 seed 为 `750` 个 split-disjoint program，每个 program 固定物化 `5` 个不同时间窗口实例，得到 `3,000 train / 750 validation`；**program 是切分单位**，五个实例不会跨 train/validation。
- 20 个非 snapshot scenario family 覆盖 order items、orders、inventory received、inventory sold、events 和 users。`current_unsold_inventory_unit_count` 当前只有四种合法 all-time QuerySpec，因此本版明确不将其用于 SFT 扩量，继续只保留在 protected test 覆盖中。
- 每条多指标 QuerySpec 仍只能落在同一 fact domain；“跨事实域覆盖”指数据集层面覆盖多种 domain，并不表示单条 SQL 已支持跨订单、库存和事件的任意联表指标组合。
- 高基数分组 `customer_city`、`product_brand`、`user_state` 使用固定 7 天窗口；该窗口不是整个数据集仅覆盖 7 天，而是单条高基数实例的时间范围。完整 release 使用运行时一致的 **1000 行执行上限**和独立的 **200 行 UI 预览预算**：14 条训练结果为 201--829 行、`display_truncated=true`，但没有一条达到 1000 行执行上限，因此均是完整的已准入训练样本；validation 最大 125 行。
- 受保护 TheLook v3 r3 碰撞审计为 QuerySpec ID `0`、窗口候选 `0`；自然语言问题尚未生成，后续问题物化仍需审计其 normalized hash 和 SQL 泄露。

仓库外证据目录：

```text
/disk2/gengnan/data-analysis-agent-data/evals/thelook-sql-only-sft-v1-r4/
  seeds.jsonl                         # sha256 3aa67d6b...983a64e
  gold/train_gold.jsonl               # 3,000，sha256 dcb5aa37...dee506f
  gold/validation_gold.jsonl          # 750，sha256 3e19eca1...0915b8
  admission/admission_records.jsonl   # 3,750，sha256 97e3c76f...6bd551
  admission/exclusions.jsonl          # 0 行
```

每个能力族还必须满足以下结构下限：

- result shape：scalar、dimension_grouped、time_series 都有样本；
- metric multiplicity：1、2、3、4 指标组合均有样本，3/4 指标不低于 validation 的 10%；
- time grain：day、week、month、quarter、year 在 TheLook validation 中均有覆盖；
- risk tags：status filter、time filter、time bucket、distinct、duration eligibility、two-stage aggregate、multi-metric CTE 均有覆盖；
- dimension：至少覆盖用户、订单、商品、事件和库存相关的安全维度；
- family：每个纳入训练的 scenario family 至少 6 条 train，validation 中至少 2 条；低于该门的 family 只能作为 pilot，不得宣称已覆盖。

## 4. split 与泄漏隔离

切分主键为：

```text
workspace_id + scenario_family_id + query_spec_id + canonical_gold_sql_hash
```

要求：

1. 同一 `query_spec_id`、canonical Gold SQL hash、自然语言问题 normalized hash 不得跨 train/validation/test；
2. TheLook v3 r3 的 750 个 `case_id`、QuerySpec ID、Gold SQL hash、primary question hash 和窗口候选不得出现在新 train/validation；
3. 同一 scenario family 可以在 train 和 validation 出现，但具体 QuerySpec、程序签名和窗口实例必须不同；TheLook SFT v1 进一步要求同一 `program_id` 的五个窗口实例不可跨 split；
4. 只改变中文措辞不产生新 semantic sample；每条 semantic sample 只选一个主问法进入 SFT，其余问法只作为 overlay 元数据；
5. 日期窗口变化可以产生新实例，但不能被统计为新的 join program 或新的指标能力；
6. release audit 必须报告 Olist/TheLook 各自以及合并后的行数、family 数、program signature 数、QuerySpec/hash overlap、问题 overlap 和 protected collision。

## 5. 统一 SQL-only 输入与标签模板

本轮引入统一外层模板 `multi-schema-sql-only-v1`。两套 workspace 使用相同的章节顺序，只有 Catalog、QuerySpec、ResultContract、问题和 Gold SQL 内容不同：

```text
### Task
Generate exactly one read-only SQL query for the supplied Chinese business question.
### SQL dialect
PostgreSQL
### Candidate contract
Return SQL only; one SELECT or WITH ... SELECT statement; use only the server-provided workspace Catalog.
### Server-provided Semantic Catalog
<当前 workspace 的 Catalog>
### Server-provided QuerySpec
<当前 workspace 的 QuerySpec JSON>
### Server-provided Result Contract
<当前 workspace 的 ResultContract JSON>
### Question
<中文业务问题>
### SQL
```

标签布局保持现有 causal SFT 规则：

```text
input_ids = prompt_tokens + canonical_postgresql_gold_sql + EOS
labels    = [-100] * len(prompt_tokens) + sql_tokens + [EOS]
```

Prompt 参与 attention 但不计算 loss；padding 的 `attention_mask=0` 且 `labels=-100`。不得把 Task B 计划文本、模型解释、执行结果或 Gold denotation 说明塞入标签。不得把 Olist 的物理表名写入 TheLook 样本，反之亦然。

现有 Olist `olist-candidate-sql-v1` 和 TheLook v2 的内容字段可以作为来源，但在合并物化器中必须按本合同重新渲染统一外层模板，并记录 `source_prompt_version` 与 `prompt_format_version=multi-schema-sql-only-v1`。若统一渲染改变了 Olist 的文本字节，必须新建 multi-schema release，不能覆盖原 Olist v3.1 release。

## 6. 训练采样与验证策略

- 初始训练采样按 workspace 等权：每个 epoch Olist/TheLook 各约 50%；不能让 TheLook 的 family 数或样本数吞没 Olist。
- batch 内允许混合 workspace，但每条样本必须携带 workspace metadata；若 tokenizer 长度或显存出现异常，可按 workspace 分桶后交替采样，不能静默丢弃一侧。
- validation 同时计算 `olist_validation_loss`、`thelook_validation_loss` 和 `macro_workspace_eval_loss=(olist+thelook)/2`；不能只报告合并 micro average。
- checkpoint 选择先以 macro workspace validation loss 为主，workspace 单侧 loss 退化超过预注册阈值时标记为不可选。最终仍必须在两个冻结 test 上做 Base/Adapter matching 与 Gold denotation，不得用 test 重新选择 checkpoint。
- 推荐首轮保持已验证的 bf16 LoRA、`r=16`、`alpha=32`、dropout `0.05`、`adamw_torch`、learning rate `1e-4`、weight decay `0.01`、effective batch `4`、2 epoch；训练入口与长度合同需在单独任务中审阅，不在本数据合同中擅自改变。

## 7. 数据生成与准入顺序

TheLook 新 SFT 的唯一允许顺序：

```text
静态 coverage seed
-> QuerySpec 校验
-> deterministic Gold renderer
-> SqlPolicy
-> PostgreSQL reader role
-> ResultContract / ResultValidator
-> 统一中文主问法选择
-> tokenizer 长度审计
-> split / overlap / protected collision audit
-> 物化 train / validation
```

Gold SQL、数据库结果和模型生成结果不可用于生成自然语言问题。问题生成只允许读取已通过准入的 QuerySpec、指标描述、维度描述和时间合同；生成后必须检查 SQL/Gold 不泄露到 question。

小批准入至少覆盖每个能力族及当前 TheLook v3 r3 的主要失败 family。小批未通过时只修复源合同或 renderer，不用换日期、换措辞、复制结构来凑目标数。

## 8. 合并 release 的验收门

正式训练前必须生成一个新的仓库外 release，例如：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  multi-schema-ecommerce-sql-only-v1/
    train.jsonl
    validation.jsonl
    manifest.json
    split_audit.json
    length_audit.json
    exclusions.jsonl
```

`manifest.json` 至少绑定：

- 两个 workspace 的数据、Catalog、metric、policy、renderer、prompt 版本；
- train/validation 行数、字节数和 SHA-256；
- workspace、family、shape、metric multiplicity、time grain、risk tag 配额；
- QuerySpec、program signature、canonical Gold SQL hash 的跨 split overlap；
- TheLook v3 r3 与 Olist v3.1 protected collision 检查结果；
- tokenizer、最大序列长度、超长 exclusions 及其原因；
- 生成时间、代码 revision 和运行环境。

确定性质量门：

1. 每条 Gold SQL 通过对应 workspace 的 renderer、SqlPolicy、reader role 和 ResultContract/ResultValidator；
2. 所有 train/validation 行的 question 不包含 SQL 文本、Gold SQL hash 或结果行；
3. 不存在 protected collision、重复 QuerySpec、重复 canonical SQL hash 或跨 split primary question overlap；
4. 统一模板和 label mask 可由 tokenizer-only audit 重建；
5. 超长记录不截断，进入 exclusion manifest；
6. 失败样本数、实际配额和未覆盖 family 必须显式报告。

## 9. 本合同不解决的问题

- 不保证混合训练一定优于当前 Olist-only Adapter；
- 不把 TheLook v3 r3 的测试错误回流为训练答案；
- 不移除 SQL AST Policy、数据库只读角色、ResultContract 或 ResultValidator；
- 不规定 Task B、中间计划或 RL/GRPO；本阶段仍是 SQL-only SFT；
- 不授权把新 Adapter 接入生产默认路径。只有两个 holdout 的 matching、Gold denotation、回退率和延迟均完成审阅后，才讨论是否接入。

## 10. 任务完成定义

本合同完成只表示多 Schema SFT 的规则、边界和配额已冻结。后续任务必须按顺序执行：

1. 构建 TheLook 新静态 seed 和 train/validation 候选；
2. 小批 QuerySpec/Gold/reader/result-contract 准入；**已完成：20 family × 2 条，40/40。**
3. 物化并审计 3,000/750 TheLook 数据；**已完成：全量 3,750/3,750。**
4. 按统一模板合并 Olist 与 TheLook，生成 6,000/1,500 的 multi-schema release；**下一步，尚未开始。**
5. tokenizer/preflight、GPU smoke、正式 bf16 LoRA 训练；
6. 在 Olist v3.1 和 TheLook v3 r3 两套 protected test 上做 matching 与 Gold denotation 对照。
