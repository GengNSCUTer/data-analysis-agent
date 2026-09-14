# SQL-only SFT 优化研究与下一步方案 v1

**调研日期：** 2026-09-14
**范围：** 只研究“模型一次生成 SQL 候选”的 SFT 路线；结合本项目 1.5B、2B、4B Adapter 的冻结测评与已完成的论文/开源代码审阅。
**本轮状态：** 只形成研究判断和后续实验设计，不启动训练、不修改生产运行时、不使用 TheLook protected 数据反向调参。

## 1. 结论先行

当前项目的主线应该重新明确为：

```text
运行时 Prompt（Catalog + QuerySpec + ResultContract + 问题）
    -> 小模型生成一个 SQL 候选
    -> unwrap_sql_completion
    -> sqlglot AST Policy
    -> PostgreSQL 只读角色
    -> ResultContract / ResultValidator / Gold 或人工口径审计
```

SQL-only SFT 的职责是提高“候选 SQL 的结构、schema 绑定、指标公式和输出边界”；它不负责权限批准、业务口径最终裁决或结果证据生成。服务器的确定性治理链仍然不可替代。

现有证据支持四个判断：

1. **SQL-only 比当前独立 Task B 辅助 JSON 更适合这条线上目标。** Schema-aware Program Adapter 在 TheLook v2 规范化后为 `457/296/284/205`（Policy/PostgreSQL/ResultContract/Gold ordered-or-bag），历史 SQL-only Adapter 为 `467/335/313/225`；Task B 没有证明自己能改善最终 SQL。
2. **模型容量有明显影响，但不是唯一答案。** 同一 Olist 领域合同下，TheLook v2 的 2B Adapter 为 `388/206/196/118`，4B Adapter 为 `518/389/389/340`；4B 明显更强。1.5B SQL-only Adapter 仍达到 `467/335/313/225`，说明任务/输入对齐可能抵消一部分参数差距。
3. **通过前置闸门不等于业务正确。** 4B Adapter 仍有 49 条“ResultContract 通过但 Gold 不匹配”；1.5B SQL-only 仍有 88 条 TheLook 合同有效但 Gold 不匹配。可执行、列名正确、返回行数合理，都不能代替 denotation/指标语义核验。
4. **下一步不应继续堆叠长计划文本，而应优化四层：数据、输入 schema、SQL-only 训练、候选生成/评测。** 这四层都可以泛化到 Olist、TheLook 或后续工作区，不硬编码某个数据集的词。

## 2. 三个 Adapter 结果如何解读

### 2.1 可比性边界

TheLook v2 是 protected 跨 schema holdout。生成阶段不读 Gold 或数据库结果，Base/Adapter 使用相同 case、Prompt bundle、greedy decode 和后置治理链。历史 1.5B SQL-only 与 1.5B Schema-aware 不是同一个 Adapter；2B/4B 使用 Qwen3.5 Instruct 模型，不能与 Qwen2.5-Coder-1.5B 做严格单变量比较。因此下面的数字用于判断趋势，不写成统一 benchmark 排名。

### 2.2 聚合结果

| 模型/路线 | Policy | PostgreSQL 执行 | ResultContract | Gold ordered-or-bag |
| --- | ---: | ---: | ---: | ---: |
| Qwen2.5-Coder 1.5B Base | 259/600 | 173/600 | 127/600 | 67/600 |
| 1.5B Olist SQL-only Adapter | 467/600 | 335/600 | 313/600 | 225/600 |
| 1.5B Olist Schema-aware Program Adapter（规范化后） | 457/600 | 296/600 | 284/600 | 205/600 |
| Qwen3.5-2B Olist Adapter | 388/600 | 206/600 | 196/600 | 118/600 |
| Qwen3.5-4B Olist Adapter | 518/600 | 389/600 | 389/600 | 340/600 |

这些结果共同说明：

- 领域 SQL-only Adapter 能显著提高“能进入治理链”的候选数；
- 4B 在未见 TheLook schema 上最强，复杂 join、时间序列和列绑定更能受益于容量；
- Schema-aware Task B 的训练 loss 很低并不代表 SQL 变好；当前 Task B 的 JSON 与 SQL 是并列样本，线上 SQL 不消费模型生成的 JSON；
- 1.5B/2B 的主要瓶颈不是“完全不会 SQL”，而是 schema 绑定、join 路径、指标粒度和输出协议稳定性。

### 2.3 TheLook 主要错误族

规范化后仍值得优先处理的错误如下：

| 错误族 | 典型现象 | 为什么 SQL-only SFT 要补 |
| --- | --- | --- |
| schema/column hallucination | 生成不存在的中间表、`event_id`、错误主键 | 跨 schema 时名称相似但关系不同，模型会按词面猜表列 |
| 额外或错误 join | 把 `events` 接入订单归因，或走不必要的长 join | SQL 可执行但重复计数/改变粒度，ResultContract 未必发现 |
| 指标粒度 | AOV 直接平均明细价格，而非先按订单聚合；订单数漏 `DISTINCT` | 这是业务公式问题，不是语法问题 |
| 时间归属 | 用履约时间代替订单创建时间进行时间序列 | 同一 SQL 形状可执行，但统计窗口含义改变 |
| 维度归因 | 用户注册来源和事件来源混淆 | 需要在输入和正例中明确“事实表—维度表—归因路径” |
| 输出边界 | `Query`/`Solution`/`Selection` 标题、代码围栏或多余解释 | 会在 AST 前直接失败；必须由统一清洗和 target 合同控制 |
| 多指标组合 | CTE 之间粒度或别名不一致 | 一个错误会使整条多指标结果失效 |

## 3. 已调研方法：真正可借鉴的部分

### 3.1 中间表示只有在“被消费”时才有价值

IRNet/SemQL、NatSQL 的共同点是：中间语言受语法约束，后续 decoder 或确定性 compiler 真正读取它，再生成 SQL。它们不是把一段 JSON 与 SQL 并列放进训练集。

当前 Task B 的结构是：

```text
同一个 QuerySpec
  ├─ Task A：Prompt -> SQL
  └─ Task B：结构上下文 -> SchemaLinkPlan JSON
```

Task B 的结果没有作为 Task A 输入，也没有共享一个“先 plan、再 SQL”的 decoder。因此它属于辅助多任务 LM SFT，而不是 plan-to-SQL。对 1.5B 而言，Task B 还占约 59% 的监督 token，增加了 JSON/SQL/EOS 协议切换和潜在梯度负迁移。

如果未来重新研究结构信息，应优先采用以下两种可验证形式之一：

- **输入侧结构：** 服务器或独立轻量 selector 先筛选候选表列，筛选结果进入 SQL Prompt；selector 单独报告必要表列召回率。
- **可编译短表示：** 只预测短的 metric/grain/time-owner/join-id 标签，并由确定性 renderer/SQL decoder 消费；不能再生成不被线上使用的长 JSON。

### 3.2 Schema linking 应该减少噪声，不应变成权限系统

RAT-SQL 将 question-table/column、foreign-key 等关系放进 encoder；RESDSQL/CodeS 使用独立 table/column classifier，再把筛选结果注入 SQL 输入。AutoLink 通过工具逐步补齐缺失 schema，并把最终结果交给 SQL 生成。

适合本项目的最小版本是：

1. Catalog/QuerySpec 先产生 role-visible 的候选表列集合；
2. 对必要 join path 保持高召回，宁可带少量无关列，也不能漏掉必需列；
3. 将“schema selection recall”和 SQL Gold denotation 分开评测；
4. Catalog 白名单、AST Policy、PostgreSQL reader role 仍是权威边界，模型选择结果不能放宽它们。

这可以在一次模型调用内完成，因为筛选发生在模型输入构造阶段，而不是再调用一个 planner LLM。

### 3.3 约束解码比长辅助 target 更直接

PICARD 在 beam search 的每一步增量解析 SQL，非法 token 直接从候选中移除；它解决的是语法/结构合法性，不是指标语义。项目当前的 `sqlglot Policy -> PostgreSQL reader -> ResultValidator` 仍必须保留。

对消费级 GPU 和当前延迟目标，不建议立刻引入完整 PICARD 服务。可以先实现低风险子集：

- 严格的 SQL-only prompt/assistant 前缀和 EOS；
- `unwrap_sql_completion()` 的统一、有界清洗；
- 生成结束后的 AST 失败分类；
- 只有在基线稳定后，才评估 token-level constrained decoding。

### 3.4 Prompt、样例选择和候选选择通常比额外长思维链更贴近目标

DAIL-SQL/DIN-SQL 主要优化 schema 表达、相似样例选择和 self-consistency 候选；它们的“分解”发生在 prompt-time orchestration，并不要求小模型额外学习一段 JSON。MAC-SQL/CHESS 则用多个 agent/阶段把中间结果真正传给后续阶段，但调用次数和延迟明显增加，且公开实现主要针对 SQLite/BIRD/Spider。

当前可借鉴的是：

- 根据 QuerySpec 的 metric、grain、join family 选择少量相似示例；
- 生成 1–4 个候选，使用 Policy、执行和 ResultContract 做确定性排序；
- 将候选失败原因结构化保存，供下一轮数据构建使用。

这仍然符合“模型只负责提出 SQL 候选、服务器负责裁决”的职责边界。

### 3.5 执行反馈应直接作用于 SQL 候选

FINER-SQL 将格式、执行、atomic operation 等拆成可解释 reward；SQL-R1 采用 SFT 冷启动后再做执行感知 GRPO；SLM-SQL/SHARE 也把候选纠错和错误族作为核心。它们的共同点是奖励/选择信号围绕最终 SQL，而不是无消费方的计划 JSON。

本项目后续可以先做“离线 reward report”而不是直接 RL：

```text
format -> AST/policy -> PostgreSQL execution -> result contract
       -> Gold denotation / 人工口径 -> error family
```

先验证哪些信号能预测人工语义正确，再考虑 1–4 候选 reranking，最后才是 2–4 rollout 的小规模 GRPO。SQLite benchmark 的执行结果不能替代 PostgreSQL 业务合同。

## 4. 下一版 SQL-only SFT 的具体优化方向

### P0：先修训练工程和输入/目标一致性

这些改动收益高、风险低，应先于扩大模型或引入 RL：

1. **validation-best checkpoint。** 训练结束后不能默认使用 final checkpoint；当前 Trainer 先按 SQL `eval_loss` 最小值选择 best 并保存选择证据。格式通过率、ResultContract 和 Gold/contract 代理指标应在后续评测阶段报告，待有稳定验证集代理指标后再考虑多指标选择。
2. **target 只保留 canonical SQL + EOS。** Prompt、padding、展示标题和解释部分全部 `labels=-100`；禁止 `Query`、`Solution`、代码围栏等前缀进入 target。
3. **训练/推理模板完全一致。** 同一 Prompt bundle、字段顺序、Catalog 版本、QuerySpec/ResultContract 序列化方式、SQL 前缀和停止 token；每次运行回读 hash。
4. **按 token 和 family 采样。** 记录 scalar/grouped/time-series/multi-metric/join/aggregation 的监督 token 占比，防止大量短 scalar 淹没复杂场景。
5. **去重和泄漏审计。** question surface、QuerySpec、Gold SQL、规范化 AST 和 family 分开去重；同一 family 或等价程序不跨 train/validation/test。

### P1：扩大“程序覆盖”，而不是复制同一 SQL

建议下一版先按结构分层，再决定总条数：

| 程序族 | 初始目标占比 | 应覆盖的变化 |
| --- | ---: | --- |
| scalar 单指标 | 15% | 全表/绝对日期/相对时间，6–10 个指标 |
| 维度分组 | 20% | 低基数状态、类目、来源，最小 join path |
| 时间序列 | 20% | day/week/month/quarter，明确时间 owner |
| 多指标同粒度 | 15% | 2–4 指标，统一维度/时间窗口，别名一致 |
| 二层聚合 | 10% | AOV、订单级去重、先聚合后平均 |
| 过滤/状态/集合 | 10% | status 集合、NULL、distinct、top-k/排序 |
| 受控困难/负例 | 10% | 错列、额外 join、错误粒度；只作训练纠错或评测诊断，不把错误 SQL 当正例 |

每个 family 可以生成 3–5 个中文表面问法，但一个 family 只计一个语义样本；train/validation/test 按 family 分组切分。自然语言多样性应改变表达，不改变指标、粒度、时间 owner 或 Gold SQL。

### P1：把业务语义变成输入证据，而不是模型自由猜测

运行时 Prompt 应紧凑但明确包含：

- 可用表/列及中文含义；
- 指标 ID、公式、分母/去重规则；
- 维度归属和允许 join path；
- 时间字段 owner 与默认时区；
- ResultContract 要求的列别名、粒度和排序；
- role-visible 白名单。

不建议把全部数据库 schema 无差别塞给模型；应先由 Catalog/QuerySpec 做确定性裁剪。裁剪器的错误必须 fail closed，并记录“漏掉必要列”而不是静默生成。

### P2：生成阶段的轻量质量增强

在单次生成稳定后再按顺序尝试：

1. greedy 单候选作为锚点；
2. 2 候选并行生成，先过 AST/Policy，再过只读执行和 ResultContract；
3. 4 候选只用于离线评测，比较延迟、token、执行成本和 Gold 提升；
4. 只有候选均失败时才允许一次有界 SQL repair，repair 也必须重新走完整治理链。

候选选择不能只用字符串相似度或模型自评分；优先级应是安全通过、执行成功、合同通过、Gold/人工语义证据，且每一步记录 token 与耗时。

### P2：后续 execution-aware 学习

先构建 PostgreSQL reader 隔离的候选对：同一 Prompt 下，一个候选通过合同且 Gold 正确，另一个可执行但指标/粒度错误。记录格式、Policy、执行、合同、denotation、人工口径等分项标签。

然后才选择训练方式：

- 首选：在 SQL-only SFT 后做小规模 pairwise preference（DPO/ORPO）或 deterministic reranker；
- 研究项：2–4 rollout 的 GRPO，奖励拆成 format、AST、execution、contract、denotation；
- 暂不做：30+ 候选、三模型串行、多 agent SQLite pipeline。

## 5. 推荐的最小后续实验设计（只冻结方案，不运行）

等下一版 SQL-only 数据和 validation 合同准备好后，先做一个回答力强的小矩阵：

| 组 | 目标 | 只允许改变的因素 |
| --- | --- | --- |
| A | 当前 SQL-only 锚点 | 无，记录 validation-best |
| B | schema 裁剪输入 | 仅加入确定性候选 schema 子集 |
| C | 错误族重采样 | 仅提高 AOV/join/time-owner/distinct 样本比例 |
| D | B+C | 验证输入与数据覆盖是否互补 |

固定基座、split、seed、模板、EOS/masking、LoRA 配置和 decode。每组必须报告：

- SQL 直接生成率、前缀/EOS 错误；
- SqlPolicy、PostgreSQL 执行、ResultContract；
- Gold ordered/bag denotation；
- metric/grain/time-owner/join/alias 分层准确率；
- Base-valid → Adapter-non-valid 回退数；
- token、延迟、显存和 repair 次数。

TheLook protected holdout 只在方案冻结后做一次后置对照，绝不参与数据构造、Prompt 调参或 checkpoint 选择。

## 6. 不建议现在做的事情

- 不继续把独立长 `SchemaLinkPlan` JSON 与 SQL 目标 1:1 交错训练；除非未来定义真正的消费路径和独立 decoder/head。
- 不因为 4B 更强就直接把模型接入生产；它仍有合同通过但 Gold 不匹配的样本。
- 不把 SQLite/Spider/CSpider 的 execution accuracy 写成 PostgreSQL 业务准确率。
- 不把 `ResultContract valid`、SQL 可执行或 loss 下降写成语义正确。
- 不用 TheLook 错误文本构造 Olist 训练样本；它是跨 schema protected holdout。
- 不先上 GRPO、复杂多 Agent 或 30+ 候选；先把 SQL-only SFT 的数据、validation-best 和错误分层做好。

## 7. 简历和项目叙事建议

可以准确表述为：

> 设计并评测 SQL-only 小模型候选生成器：以版本化 Semantic Catalog、QuerySpec 和 ResultContract 构造训练输入，以 canonical PostgreSQL Gold SQL + EOS 做 LoRA SFT；候选生成后依次通过 sqlglot AST Policy、PostgreSQL 只读角色和结果合同。通过 Olist 领域训练与 TheLook 未见 schema holdout，定位并量化 schema linking、join、指标粒度和输出协议错误，并比较 1.5B/2B/4B Adapter 的泛化差异。

不要写“实现了 plan-to-SQL 推理”或“达到了开放式 Text-to-SQL 100% 准确率”。当前证据更适合强调：

- 线上一致的 SQL-only 目标；
- 模型生成与服务器治理解耦；
- 执行、结果合同和 denotation 的多层评测；
- 通过负向 Task B 结果做出路线收敛。

## 8. 参考资料与代码证据

本报告基于仓库外 `github-research-output/slm-text-to-sql-posttraining-2026/` 的代码级审阅，以及临时 clone 的真实代码，不仅依据 README：

1. IRNet / SemQL — <https://arxiv.org/abs/1905.08205> / <https://github.com/microsoft/IRNet>
2. RAT-SQL — <https://arxiv.org/abs/1911.04942> / <https://github.com/microsoft/rat-sql>
3. PICARD — <https://arxiv.org/abs/2109.05093> / <https://github.com/ServiceNow/picard>
4. RESDSQL — <https://arxiv.org/abs/2302.05965> / <https://github.com/RUCKBReasoning/RESDSQL>
5. DAIL-SQL — <https://github.com/BeachWang/DAIL-SQL>
6. MAC-SQL — <https://arxiv.org/abs/2312.11242> / <https://github.com/wbbeyourself/MAC-SQL>
7. CHESS — <https://arxiv.org/abs/2405.16755> / <https://github.com/ShayanTalaei/CHESS>
8. AutoLink — <https://github.com/wzy416/AutoLink>
9. FINER-SQL — <https://arxiv.org/abs/2605.03465> / <https://github.com/thanhdath/finer-sql>
10. SQL-R1 — <https://arxiv.org/abs/2504.08600> / <https://github.com/DataArcTech/SQL-R1>
11. SLM-SQL — <https://arxiv.org/abs/2507.22478> / <https://github.com/CycloneBoy/slm_sql>
12. SHARE — <https://github.com/quge2023/SHARE>

仓库内配套材料：

- [`text-to-sql-training-organizations-v1.md`](text-to-sql-training-organizations-v1.md)：Task B 诊断和方法组织；
- [`thelook-v2-matching-v1.md`](../experiments/thelook-v2-matching-v1.md)：跨 schema matching 聚合结果；
- [`thelook-matching-error-analysis-v1.md`](../experiments/thelook-matching-error-analysis-v1.md)：格式、schema、join、指标错误族；
- [`log.md`](../experiments/log.md)：实验台账和边界。
