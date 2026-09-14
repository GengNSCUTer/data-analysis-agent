# Text-to-SQL 训练组织方式调研与 Task B 诊断 v1

**调研日期：** 2026-09-14  
**范围：** 论文、作者/项目官方代码和当前项目的冻结实验事实  
**本轮状态：** 仅调研和设计判断；没有启动消融、重新训练或修改生产运行时

## 1. 先给结论

你的怀疑方向是对的，但需要把问题说准确：Task B 不是因为“计划这个想法天然错误”而失败，而是当前实现并没有形成真正的
`问题 → 计划 → SQL` 条件生成链路。

当前训练是两个独立的因果任务共享一组 LoRA 参数：

```text
同一个 QuerySpec
  ├─ Task A：真实运行时 Prompt → SQL
  └─ Task B：受控结构上下文 → SchemaLinkPlan JSON

训练：交错输入 A/B，优化 L = L_A + L_B
上线：只输入 Task A Prompt，只取 SQL
```

Task B 的 JSON 不会被送回 Task A，也没有一条训练样本明确要求模型“先生成这个计划，再读取自己生成的计划写 SQL”。因此它更准确的名称是
**辅助式多任务语言模型 SFT（auxiliary multi-task LM SFT）**，不是 plan-to-SQL。

对 1.5B 模型而言，这个设计还有三个可观察的压力：

1. Task B 目标更长。当前真实物化中，Task A 约占监督 token 的 41%，Task B 约占 59%，虽然事件数量是 1:1；
2. 输出协议混合。模型需要在 SQL-only 和 canonical JSON 两种 target、两个 task header、两种停止边界之间频繁切换；
3. 线上不使用 Task B。训练增加的能力没有明确的在线消费方，却会改变共享 decoder 的参数和输出分布。

所以目前最稳妥的判断是：**结构性不匹配和监督量/协议漂移是首要嫌疑；小模型容量和梯度负迁移是合理的贡献因素，但还不能仅凭现有结果证明某一个因素是唯一原因。**

## 2. 当前项目到底做了什么

当前实现位于：

- Trainer：`scripts/post_training/training/run_qwen25coder_schema_aware_sft.py`
- 物化：`scripts/post_training/data/materialize_olist_schema_aware_program_sft.py`
- 计划派生与验证：`src/data_analysis_agent/olist_schema_link_plan.py`
- 合同：`docs/post-training/data/schema-aware-program-sft-contract-v1.md`

每个业务 `query_instance` 保持一条语义样本，并生成一个稳定 `pair_id`：

| 事件 | 输入 | 标签 | 线上是否使用 |
| --- | --- | --- | --- |
| Task A | 与生产一致的 Olist runtime Prompt（Catalog + QueryPlan + ResultContract + question） | deterministic Gold PostgreSQL SQL + EOS | 是，目标是一次 SQL 候选生成 |
| Task B | 紧凑 Catalog/QueryPlan/ResultContract/question + training-only task header | deterministic SchemaLinkPlan JSON + EOS | 否，仅训练期辅助监督 |

`PairEventDataset` 对每个事件做 `prompt_ids + target_ids + EOS`，Prompt 和 padding 的 label 为 `-100`，只在 target/EOS 上计算 causal-LM loss。两类事件稳定交错，因此训练器优化的是共享 LoRA 参数上的联合损失，而不是先完成 B 再把 B 的结果喂给 A。

这条链路的优点是实现简单、可审计、不会增加线上 LLM 调用；缺点是“辅助目标”和最终 SQL 目标的因果关系没有被编码。TheLook v2 的归一化后结果也没有显示出相对 SQL-only Adapter 的最终优势：Schema-aware 的 ResultContract/Gold ordered-or-bag 为 `284/205`，历史 SQL-only Adapter 为 `313/225`。这不是单独证明 Task B 必然造成退化，但足以停止把它当作默认改进路线。

## 3. 业界方法按“中间信息在哪里”分类

### 3.1 中间表示由模型生成，再由确定性编译器生成 SQL

#### IRNet / SemQL（ACL 2019）

- 论文：[Towards Complex Text-to-SQL in Cross-Domain Database with Intermediate Representation](https://arxiv.org/abs/1905.08205)
- 代码：[microsoft/IRNet](https://github.com/microsoft/IRNet)
- 代码证据：`preprocess/sql2SemQL.py` 把 SQL 转成 SemQL；`sem2SQL.py` 把逻辑形式编译回 SQL；`src/models/model.py` 同时有 sketch decoder 和 logical-form decoder；`src/utils.py` 将 `loss_lf + sketch_loss_coefficient * loss_sketch` 组合。

IRNet 是真正的结构化分解，但它和当前 Task B 有关键区别：

1. SemQL 是受语法约束的中间语言，不是任意长的 JSON 说明文字；
2. 推理先得到 sketch，再在 sketch 条件下解码 logical form；
3. 最终 SQL 由确定性 `sem2SQL` 编译器生成，而不是把计划丢在旁边；
4. 计划/草图的监督有明确的 decoder 和推理消费路径。

因此 IRNet 支持“计划有价值”，但不支持“把独立计划 JSON 交错塞给同一个 decoder 就会提升 SQL”。

#### NatSQL

- 论文/代码入口：[ygan/NatSQL](https://github.com/ygan/NatSQL)
- NatSQL 用比 SQL 更容易生成的中间查询语言降低嵌套、连接和结构复杂度，再通过确定性转换恢复 SQL。RESDSQL 的官方仓库也明确提供 `text2natsql` 和 `text2sql` 两条训练/推理路径。

这类方法的核心不是增加一段解释，而是**改变最终生成目标为可编译的受限程序**。如果中间语言无法被稳定编译或线上不消费，它就只是额外文本。

### 3.2 Schema linking 是模型内部表示或独立分类器，不是生成一段计划文本

#### RAT-SQL（ACL 2020）

- 论文：[RAT-SQL: Relation-Aware Schema Encoding and Linking for Text-to-SQL Parsers](https://arxiv.org/abs/1911.04942)
- 代码：[microsoft/rat-sql](https://github.com/microsoft/rat-sql)
- 代码证据：`ratsql/models/spider/spider_match_utils.py` 计算 question-column/table 的 schema/cell linking；`ratsql/models/spider/spider_enc.py` 把 `qc/q t/cc/ct/...` 关系编码进 relation-aware Transformer；最终 decoder 仍输出结构化 SQL AST。

RAT-SQL 的 linking 结果是 encoder 的关系特征，不是额外 assistant 文本 target。因此它不会让 decoder 同时学习 SQL 和长 JSON 输出协议。

#### RESDSQL（AAAI 2023）

- 论文：[RESDSQL: Decoupling Schema Linking and Skeleton Parsing for Text-to-SQL](https://arxiv.org/abs/2302.05965)
- 代码：[RUCKBReasoning/RESDSQL](https://github.com/RUCKBReasoning/RESDSQL)
- 官方 README 明确写出两阶段：第一阶段 cross-encoder 预测相关表/列，第二阶段 T5/mT5 根据处理后的 schema 生成 SQL/NatSQL；CSpider 还替换为 XLM-R + mT5。
- 代码证据：`schema_item_classifier.py` 的 `prepare_batch_inputs_and_labels()` 对表和列做二分类，并在 `_train()` 中用独立分类损失与 AUC/早停；`text2sql.py` 读取预处理后的输入，只在 decoder 上监督 SQL/NatSQL。

这确实是“schema linking → SQL”，但连接点是**分类器的选择结果进入第二阶段输入**，不是两个无关 target 交错训练。其代价是第二个模型/阶段和额外延迟。

#### CodeS（SIGMOD 2024）

- 论文：[CodeS](https://arxiv.org/abs/2402.16347)
- 代码：[RUCKBReasoning/codes](https://github.com/RUCKBReasoning/codes)
- CodeS 发布 1B/3B 等 SQL-domain checkpoint，并把 schema item classifier 作为独立阶段；它强调在进入 SQL 生成前过滤表列。
- 这与当前项目最值得借鉴的部分是“schema filtering 应单独评估召回率”，但它不能替代我们的 Catalog 白名单、AST Policy 或 PostgreSQL reader role。

### 3.3 约束解码：不增加计划，直接限制 SQL token/AST

#### PICARD（EMNLP 2021）

- 论文：[PICARD - Parsing Incrementally for Constrained Auto-Regressive Decoding](https://arxiv.org/abs/2109.05093)
- 代码：[ServiceNow/picard](https://github.com/ServiceNow/picard)
- PICARD 在 beam search 每一步把候选 token 增量送入 SQL parser/AST；无法继续解析的 token 被丢弃。它不需要改模型、不需要额外训练计划，README 报告 Spider 开发集执行错误从 12% 降到 2%。

PICARD 只解决“生成的 SQL 是否满足语法/解析约束”，不解决指标口径、权限和结果合同。它和当前 `sqlglot Policy -> reader role -> ResultValidator` 的关系是：可以借鉴“早期约束减少无效候选”，但服务器后置治理仍必须保留。

### 3.4 提示分解、self-consistency 和多 Agent：计划是真正的额外推理阶段

#### DIN-SQL、DAIL-SQL

- DIN-SQL 论文：[DIN-SQL: Decomposed In-Context Learning of Text-to-SQL](https://arxiv.org/abs/2304.11015)
- DAIL-SQL 代码：[BeachWang/DAIL-SQL](https://github.com/BeachWang/DAIL-SQL)
- DAIL 的 `generate_question.py` 负责 schema/示例选择和 prompt 构造；`ask_llm.py` 用 `--n` 生成 self-consistency 候选，再通过结果选择。它的主要贡献是示例选择、提示组织和候选投票，不是训练同一 decoder 的辅助 JSON 任务。

这类方法说明“先分析再写 SQL”可以提升大模型效果，但通常是 prompt-time orchestration，成本是多次调用或多候选执行，并不等于小模型 SFT 能直接学会同样的关系。

#### MAC-SQL（COLING 2025 版本）

- 论文/代码：[wbbeyourself/MAC-SQL](https://github.com/wbbeyourself/MAC-SQL)
- `core/agents.py` 里有 Selector、Decomposer、Refiner：Selector 生成/读取裁剪后的 schema，Decomposer 生成 SQL，Refiner 执行 SQLite 并把错误反馈给模型；`core/chat_manager.py` 顺序驱动三者。
- 这是显式的多 Agent、多轮链路。计划/分解结果真正传给下一阶段，因此不是当前 Task B 的独立辅助文本；但它增加调用次数，且代码假设 SQLite 和 BIRD/Spider，不可直接替换当前生产链路。

#### CHESS（2024）

- 论文：[CHESS: Contextual Harnessing for Efficient SQL Synthesis](https://arxiv.org/abs/2405.16755)
- 代码：[ShayanTalaei/CHESS](https://github.com/ShayanTalaei/CHESS)
- 官方 pipeline 是 Information Retriever → Schema Selector → Candidate Generator → Unit Tester。`templates/template_generate_candidate_*.txt` 要求递归分解/CoT，`src/llm/parsers.py` 将 reasoning 和 SQL 分开解析；`team_builder.py` 用 LangGraph 串联 agent。
- Schema Selector 的结果进入后续 SQL prompt；Candidate Generator 可生成多个候选；Unit Tester/执行结果参与选择或修订。

CHESS 证明复杂 pipeline 可以把中间步骤真正接入后续，但它依赖多个模型调用、候选和工具，线上成本显著高于我们的“一次模型调用 + 确定性治理”。此外，CHESS/相关模板默认 SQLite 方言，不应把其执行结果直接当 PostgreSQL 业务正确性。

#### AutoLink（AAAI 2026）

- 论文/代码：[wzy416/AutoLink](https://github.com/wzy416/AutoLink)
- 官方 README 将 schema linking 做成迭代 agent：初始检索 → 工具检索缺失表列/值 → SQL draft 检查 schema 是否足够 → 停止；随后再生成、修订、执行并按结果选择 SQL。
- 代码证据：`run/complete_schema.py` 负责迭代 schema exploration；`run/generate_schema.py` 生成最终 SQL prompt；`run/sql_generation.py`、`sql_revise.py`、`sql_selection.py` 分离候选生成、修订和基于执行结果的选择。

AutoLink 的重要启示是：schema linking 的第一指标是高召回，且可以在进入 SQL 生成前逐步补齐 schema；但它是工具/agent 编排，不是给同一个 decoder 追加一个无消费方 JSON target。

### 3.5 执行反馈、偏好优化和 RL：奖励通常直接作用于 SQL 候选

| 方法 | 训练/推理组织 | 可借鉴点 | 不应照搬的部分 |
| --- | --- | --- | --- |
| FINER-SQL（ICDE 2026） | SFT 后用 GRPO，多候选 SQL 通过格式、执行、atomic operation 等 reward 评分 | 将稀疏“对/错”拆成可解释 reward，并保留执行轨迹 | 默认约 30–32 候选、SQLite sandbox，不适合线上单次请求 |
| SQL-R1（NeurIPS 2025） | SynSQL/CoT SFT 冷启动，再用 verl/GRPO 做执行反馈 | SFT → RL 顺序和 reward 分解 | 官方约 8×80GB，不能当单 24GB 复现配方 |
| SLM-SQL（AACL 2025 Findings） | SFT + GRPO + corrective self-consistency；论文描述 64 候选/校正 | 候选校正和错误反馈作为离线研究方向 | 当前 GitHub 基本只有 README，64 候选成本过高 |
| SHARE（ACL 2025） | BAM/SAM/LOM 三个专门 SLM 串行 LoRA | 按 schema/logic/SQL error family 组织数据 | 三模型在线串行延迟高，不能把轨迹简单并入当前 SQL target |

这些工作共同点是：奖励或选择信号围绕最终 SQL 候选和执行结果组织，而不是让一个 decoder 在不使用计划的情况下生成长计划 JSON。

## 4. “中间计划 → SQL”到底有哪些不同含义

不能把下面五件事混称为 Chain-of-Thought：

| 形式 | 计划在哪里 | SQL 如何使用计划 | 是否与当前 Task B 相同 |
| --- | --- | --- | --- |
| 文本 CoT | assistant 文本中间 | 同一序列后续 token 可注意到前文 | 否；当前 A/B 是两条独立样本 |
| 可编译 IR（SemQL/NatSQL/AST） | 受语法约束的程序 | compiler 或条件 decoder 消费 | 否，但这是最接近“计划有实际作用”的范式 |
| 独立 schema classifier | 分类 logits/选择列表 | 选择结果进入 SQL prompt/encoder | 否；连接显式但通常多一个 head/阶段 |
| 两阶段 planner → SQL | 第一模型输出计划 | 第二次调用将 plan 作为输入 | 否；当前线上明确不希望增加调用 |
| 辅助多任务 LM | 与 SQL 平行的额外 target | 没有强制消费路径 | 是当前 Task B |

因此，当前 Task B 不能因为 target 叫 `SchemaLinkPlan` 就声称模型学会了 `Plan → SQL`。

## 5. 为什么 Task B 可能干扰 1.5B

### 5.1 已有证据支持的结构问题

1. **无条件连接：** A 的输入没有 B 的生成结果，B 的 loss 无法直接约束“使用计划写 SQL”。
2. **token 不平衡：** B 平均 target+EOS 约 688 token，A 约 478 token；按 token 统计 B 占约 59% 监督量。
3. **协议漂移：** TheLook 原始 Schema-aware completion 中 `480/600` 额外出现 `Proposal` 标题。通用有界清洗恢复了大量可解析 SQL，但总体验仍低于历史 SQL-only Adapter，说明输出边界稳定性是实质问题。

### 5.2 合理但尚未单独证明的因素

- **梯度负迁移：** SQL token 需要学习关键字、标识符、谓词和聚合；JSON token 需要学习字段名、引号、逗号、排序和长结构。共享 LoRA 更新方向可能不一致。
- **小模型容量：** 1.5B 的表示空间和上下文预算有限，同时拟合 SQL、JSON、任务头和 EOS 边界比只拟合 SQL 更难。
- **长目标暴露错误：** B 目标更长，任意一个字段顺序/别名/ID 错误都会带来大量 loss，但不一定改善 SQL。
- **训练/部署目标错位：** 训练期间一半以上监督 token 属于线上永远不输出的任务，参数被推向一个线上不会请求的分布。

### 5.3 不能这么下结论

- 不能说“loss 高所以 Task B 没学会”；也不能说“loss 低所以 SQL 变好”。
- 不能把 TheLook 的一次迁移结果归因成单一梯度因素；输出包装、schema 未见、语言、模型容量和 checkpoint 选择都可能影响结果。
- 不能据此删除 QuerySpec、Catalog、ResultContract 或服务器治理；Task B 是否保留与生产安全边界是两件事。

## 6. 对当前项目的判断

### 6.1 立即判断

当前目标是“单次模型调用提出 SQL 候选，服务器决定是否执行”。在这个目标下，Task B v1 不应继续作为默认训练目标，也不应接入生产。保留其代码和结果作为负向/中性研究记录即可。

更准确的简历表述应是：

> 尝试过训练期 SchemaLinkPlan 辅助监督；在保持生产 SQL Prompt 不变的前提下，发现独立辅助 JSON 任务没有形成 plan-to-SQL 因果路径，并伴随输出协议稳定性退化，因此将 schema linking 改为独立可评估的输入筛选/结构特征方向。

不要写成“实现了计划推理并提升 SQL 泛化”，当前证据不支持。

### 6.2 建议优先级

1. **直接 SQL-only SFT 作为主线：** Task A 保持真实 runtime Prompt 和 canonical SQL/EOS，使用 validation-best checkpoint；这是唯一与线上输出完全一致的目标。
2. **把 schema linking 做成输入侧能力：** 由已验证 QuerySpec/Catalog/Join registry 确定性裁剪候选 schema，或训练一个轻量 table/column selection head；输出是选择结果，不是长 JSON assistant target。选择器单独报告 recall，漏掉必要列时 fail closed。
3. **保留结构信息但缩短表示：** 如果确实需要辅助监督，优先考虑短标签/分类 head（metric、grain、time owner、join IDs），并让它不污染 SQL decoder；目标是 representation regularization，不宣称 plan-to-SQL。
4. **确定性编译优先：** QuerySpec → Gold renderer 已经是可复现的计划到 SQL 编译器。让模型学习候选 SQL，服务器用 QuerySpec/Contract/Policy 做裁决，比让小模型生成完整 JSON 计划更符合职责边界。
5. **执行反馈放在 SQL 候选上：** 后续可从 FINER-SQL/SQL-R1 借鉴格式、执行、结构、合同 reward，但先做离线 1–4 候选和 PostgreSQL reader 评测，不直接上 30+ rollout。

### 6.3 暂不采用

- 不把 Task B JSON 直接拼到 Task A target 后面并继续训练，除非先定义线上如何可靠地只取 SQL、如何处理计划错误以及每段 loss 权重；
- 不为了“真正 plan-to-SQL”在生产增加第二次 LLM 调用；
- 不把 CHESS/MAC-SQL 的多 Agent SQLite pipeline 原样搬入 Vanna/FastAPI/PostgreSQL；
- 不把 PICARD/SqlPolicy 的语法通过当成指标语义正确；
- 不用 TheLook protected holdout 反向调 Task B、数据或 checkpoint。

## 7. 后续最小验证（只记录计划，本轮不运行）

等研究判断冻结后，才考虑一个小而有回答力的 Olist train/validation-only 实验。它的目的不是继续追求更低 loss，而是验证“辅助任务是否真正帮助 SQL”。

预注册的最小对照可以是：

| 组 | 训练目标 | 目的 |
| --- | --- | --- |
| A | SQL-only | 线上一致的锚点 |
| B | 当前 1:1 Task A + Task B | 复现已有设计，确认结果稳定 |
| C | Task A + 低比例/低权重短结构标签 | 判断长度和监督量是否是关键因素 |

所有组必须固定基座、数据 split、seed、Prompt、EOS/masking、LoRA 配置和 validation-best 选择；TheLook v2 只能在所有配置冻结后做一次最终后置评测，不参与选择。

除 aggregate loss 外，必须按任务和错误层级报告：

- SQL 完整性/前缀/EOS 稳定性；
- SqlPolicy acceptance；
- PostgreSQL reader 执行；
- ResultContract/ResultValidator；
- Gold denotation（ordered/bag）；
- metric、grain、time owner、join、alias 的分层错误；
- 训练 token 占比、生成 token、延迟和回退数量。

只有当 SQL-only 的线上一致目标在 Olist validation 上不退化，且结构标签在至少一个预注册层面稳定改善，才值得继续投入结构化辅助监督。

## 8. 参考资料与代码证据索引

### 论文/官方项目

1. IRNet — <https://arxiv.org/abs/1905.08205> / <https://github.com/microsoft/IRNet>
2. RAT-SQL — <https://arxiv.org/abs/1911.04942> / <https://github.com/microsoft/rat-sql>
3. PICARD — <https://arxiv.org/abs/2109.05093> / <https://github.com/ServiceNow/picard>
4. DIN-SQL — <https://arxiv.org/abs/2304.11015>
5. RESDSQL — <https://arxiv.org/abs/2302.05965> / <https://github.com/RUCKBReasoning/RESDSQL>
6. DAIL-SQL — <https://github.com/BeachWang/DAIL-SQL>
7. MAC-SQL — <https://arxiv.org/abs/2312.11242> / <https://github.com/wbbeyourself/MAC-SQL>
8. CHESS — <https://arxiv.org/abs/2405.16755> / <https://github.com/ShayanTalaei/CHESS>
9. CodeS — <https://arxiv.org/abs/2402.16347> / <https://github.com/RUCKBReasoning/codes>
10. SQL-R1 — <https://github.com/DataArcTech/SQL-R1>
11. SHARE — <https://github.com/quge2023/SHARE>
12. FINER-SQL — <https://github.com/thanhdath/finer-sql>
13. AutoLink — <https://github.com/wzy416/AutoLink>
14. SLM-SQL — <https://github.com/CycloneBoy/slm_sql>

### 本项目证据

- [`schema-aware-program-sft-contract-v1.md`](../data/schema-aware-program-sft-contract-v1.md)
- [`run_qwen25coder_schema_aware_sft.py`](../../../scripts/post_training/training/run_qwen25coder_schema_aware_sft.py)
- [`olist_schema_link_plan.py`](../../../src/data_analysis_agent/olist_schema_link_plan.py)
- [`experiments/log.md`](../experiments/log.md)
- 仓库外 TheLook v2 重评报告：`/disk2/gengnan/data-analysis-agent-data/experiments/evaluation-presentation-normalizer-v1-20260914/evaluation-report.json`

## 9. 一句话带走

**中间表示只有在“被 SQL 生成器消费”或“由确定性编译器消费”时才是计划；与 SQL 平行生成但线上不使用的长 JSON，只是辅助多任务目标。对当前 1.5B 单次 SQL 候选生成目标，先让模型专注 SQL，schema linking 放到输入筛选/结构 head，安全和业务正确性继续由服务器合同裁决。**
