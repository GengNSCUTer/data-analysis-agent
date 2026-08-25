# 数据分析 Agent｜微调与后训练学习笔记 v1

> 本文是本项目的学习材料和实验记录模板，不把计划写成已实现能力。当前状态：QLoRA
> 环境已验证，Spider train-only 候选已生成；Qwen 1.5B 已完成 forward-only smoke 和一次
> 8-step QLoRA SFT 工程 smoke。Base/Adapter 的完整受控对照已完成，当前得到的是
> 一个需要进一步诊断的负向 ablation；尚未启动 DPO 或 GRPO。

## 1. 为什么要做后训练

当前 `qwen2.5-coder:3b` Ollama 基线已经证明“问题 + SQLite schema → SQL 候选 → 受控执行”
这条离线链路可以跑通，但 1,034 条 Spider dev 候选中有 150 条执行错误，其中主要是
`no_such_column`。这说明当前最值得学习和改进的是 **schema linking、SQL 结构规划和输出格式稳定性**，
而不是把数据库安全边界交给模型。

项目的第一性原理是：模型负责提出候选，服务器负责决定候选是否能执行、是否访问了允许的对象、
是否满足指标和结果合同。后训练可以提高候选质量，但不能替代：

- `sqlglot` AST Policy：单语句、只读、对象和列白名单、限制条件；
- PostgreSQL reader role：数据库实际权限的最后边界；
- `ResultContract` / `ResultValidator`：结果列、粒度、指标口径和返回证据；
- `ChartContract`：图表类型、横轴、指标列和当前结果工件的服务器规则。

## 2. 从预训练到后训练

### 2.1 Pretraining

预训练使用大量文本/代码做下一个 token 预测。给定 token 序列
`x_1, ..., x_t`，模型估计 `p(x_t | x_<t)`，训练目标通常是交叉熵：

`L = -Σ_t log p(x_t | x_<t)`

模型学到的是通用语言和代码分布，不等于它已经知道“如何遵循本项目的 SQL 输出合同”。

### 2.2 Instruction tuning / SFT

SFT（监督微调）使用“输入 → 目标回答”样本，让模型学习特定任务格式。Text-to-SQL 的一条样本通常包含：

```text
SQLite schema
Question
Target SQL
```

训练时仍然是 causal LM 的交叉熵和 teacher forcing：每个目标 token 的正确前缀由数据集提供，
模型不需要在训练时自己采样完整 SQL。SFT 适合先建立可靠基线，能直接观察 loss、格式和可执行率。

### 2.3 Preference optimization / DPO

DPO 使用同一个 prompt 下的 chosen/rejected 回答对，不显式训练奖励模型，通过偏好目标让模型更偏向
chosen。对本项目而言，`chosen` 可以是通过 AST、只读执行和结果合同的 SQL，`rejected` 可以是
越权、多语句、错误列或不满足指标粒度的候选。但在收集足够可信的偏好标签前，不应直接做 DPO。

### 2.4 RL / GRPO

GRPO 等方法从模型采样多个候选，根据奖励优化生成策略。Text-to-SQL 里常见奖励包括 SQL 可执行、
测试套件执行正确、AST 合规和结果 denotation 正确。它的难点是：数据库执行成本高，奖励稀疏，错误
候选可能只是“能运行”而不是“语义正确”，多候选会显著增加延迟和显存占用。因此本项目先做单卡
QLoRA + SFT，只有在有稳定执行评测和可解释奖励后再考虑 GRPO。

## 3. LoRA 与 QLoRA

### 3.1 LoRA 在训练什么

全量微调会更新权重 `W`。LoRA 冻结 `W`，只学习低秩增量：

`W' = W + ΔW = W + (α / r) B A`

其中 `A ∈ R^(r×d)`、`B ∈ R^(d×r)`，`r` 是 rank，`α` 是缩放系数。可训练参数量从约 `d×d`
降为约 `2×d×r`。训练结束可以保留 adapter，也可以在部署前 merge 到基座；本项目第一轮保留
adapter，便于比较和回滚。

面试回答要点：LoRA 不是把整个模型变小，而是在冻结基座的前提下，用低秩参数表示任务相关更新。
rank 越大表达能力和显存/过拟合风险通常越高；`alpha` 改变增量尺度，不等于 rank。

### 3.2 QLoRA 的显存来源

QLoRA 通常用 4-bit NF4 量化冻结的基座权重，再在上面训练 LoRA adapter，并配合 double quantization
和 paged optimizer 降低显存峰值。它不是“4-bit 训练所有参数”：基座权重冻结，反向传播主要更新
adapter；激活、临时计算和部分状态仍可能以更高精度存在，所以长 schema、长序列和较大的 micro-batch
仍可能 OOM。

24GB 卡上的第一轮工程策略：

- 先 `batch_size=1`，用 `gradient_accumulation_steps` 增大有效 batch；
- 限制 `max_seq_length`，先测 schema 序列长度分布，再决定 1024/1536/2048；
- 开启 gradient checkpointing，必要时使用 bf16/fp16；
- 保存 adapter，不保存重复的全量模型；
- 每次记录峰值显存、训练步数、有效 batch、序列长度和 GPU UUID。

有效 batch size 约为：

`micro_batch × gradient_accumulation_steps × data_parallel_size`

单卡时 `data_parallel_size=1`。梯度累积不是把一个样本拆成更短的序列，而是多次小 batch 梯度累积
后再更新一次参数。

## 4. Text-to-SQL 的核心知识

### 4.1 Schema linking

Schema linking 是把问题中的实体、属性、时间和关系映射到数据库表/列/连接路径。小模型常见失败是：

- 把自然语言同义词映射到不存在的列；
- 选错同名列；
- 忽略外键关系或连接方向；
- 只看到局部 schema，无法找到跨表路径。

本项目通过 schema serialization、Catalog 检索、Join 图闭包和 `QueryPlan` 减少输入歧义；SFT 训练可以
让模型学习稳定的 schema 表达和 SQL 结构，但检索和权限仍由服务器控制。

### 4.2 SQL AST 与数据库执行

SQL 可执行只说明语法、对象名和类型大致成立，不说明指标口径正确。比如列名存在但统计粒度错误，
或把一对多评价表直接 Join 导致订单重复计数。评测必须分开记录：

1. 路由/澄清是否正确；
2. SQL 是否通过 AST 和只读权限；
3. SQL 是否可执行；
4. 指标、过滤、粒度和时间范围是否正确；
5. 结果合同和图表合同是否通过；
6. 回答是否有结果证据支持。

### 4.3 常用指标

- Exact Match（EM）：预测 SQL 与 gold SQL 的规范化字符串/结构是否匹配，容易受等价写法影响。
- Execution Accuracy（EX）：预测 SQL 执行结果是否与 gold 结果相同；可能把某些语义错误掩盖掉。
- Test Suite Accuracy：在多个数据库实例/测试数据上比较执行结果，能减少偶然通过，但成本更高。
- 本项目额外记录 schema-linking 执行错误、AST 拒绝、结果合同通过和人工语义标签，不能只看一个分数。

## 5. 当前实验资产与边界

已冻结：

- 基线：`qwen2.5-coder:3b` Q4_K_M，完整 Spider dev 候选 `1,034/1,034`；Test Suite 内部参考 all=`0.585`，
  仅用于当前镜像/资产组合的内部比较，不能写成官方 leaderboard 分数。
- 环境：`/disk2/gengnan/conda_envs/data-analysis-agent-qlora`，Python 3.11，PyTorch 2.5.1+cu121，
  transformers 4.48.3，PEFT 0.14.0，TRL 0.15.2，bitsandbytes 0.45.1。
- 设备：逻辑 `CUDA_VISIBLE_DEVICES=0/1/2/3` 对应物理 `nvidia-smi` `2/3/0/1`；第一轮默认使用逻辑 `1`
  （物理 GPU 3，RTX 4090），启动前必须重新检查占用。
- 候选：Spider train-only 128 条，位于仓库外 `spider-sft-candidates-v1-20260825`，128/128 通过只读
  SQLite `EXPLAIN QUERY PLAN`，v2 holdout 碰撞 0。JSONL hash 记录在 `post_training_candidates_v1.yaml`。
- SFT smoke：128 条候选按 Spider `db_id` 分成 102 条 train / 26 条 validation，66/19 个 schema 完全不重叠，
  SQL shape 交集也为 0。Qwen 1.5B 使用 4-bit NF4 和 `r=16` LoRA，`batch=1`、accumulation=4、8 个
  optimizer step；train/eval loss 分别为 `0.556203/0.466989`，峰值 allocated 显存约 3.31 GiB。adapter 为
  74 MB，已从基座重新加载并在 validation 样本上得到有限 loss。完整证据见 `post_training_sft_smoke_v1.yaml`。

未完成：

- 对已完成的 Qwen 1.5B Base/Adapter 对照做状态迁移、错误类别、输出长度和受限人工 changed-case 分析，确认回退的具体模式；
- 扩大且人工抽检训练候选，加入 schema-linking、路由/澄清和安全负例；
- DPO/GRPO。它们都不能使用 v2 永久 holdout。

### 5.1 如何解读这次 Base 的三层结果

Base 的 SFT 前向 loss、SQLite 诊断和 Test Suite 输出分别在测不同的东西。此前 `0.466989` 的
validation loss 是 teacher forcing 下对 26 条验证 SQL token 的交叉熵，主要验证训练工程和目标 token 拟合；它
不能预测 dev 集生成效果。现在 Base 对 1,034 条 dev completion 在共享展示包装规范化后得到 831 条 SQLite
安全执行、4 条 policy rejection、199 条 execution error。这个 831/1,034 反映“候选在一个固定开发库中能否
被解析、通过只读策略并执行”，其中主要失败是 schema linking，例如生成不存在的列。

同一批候选进入未修改 Test Suite evaluator 后，内部 all execution value 为 `0.427`。它低于单库可执行比例是
正常的：Test Suite 会在更多生成数据库实例上验证预测结果与 gold 的行为是否一致，能过滤“列和语法都对、但
过滤条件、Join 或聚合语义错”的偶然可执行 SQL。反过来，Test Suite 也不是业务生产正确率，仍不能覆盖 Olist
工作区的指标口径、权限、ResultContract 和图表合同。

### 5.2 已完成的 Base vs Adapter 受控对照

唯一自变量是是否加载 74 MB LoRA adapter；模型 revision、4-bit 配置、prompt、贪心 decode、1,034 条
Spider dev 输入、SQLite 诊断和 Test Suite evaluator 均冻结。Base 得到 831 条 SQLite executed、4 条
policy rejected、199 条 execution error 和 Test Suite all=`0.427`；Adapter 分别为 666、29、339 和
`0.215`。Adapter 生成 61,796 token，少于 Base 的 128,957 token，但总生成时间反而略长。

所以这次的正确结论是：在 102 条训练样本、8 个 optimizer step 的 schema-disjoint QLoRA SFT smoke 配置下，
Adapter 在固定 Spider mirror/Test Suite 资产组合上出现明显回退。少生成 token 不等于 SQL 更好，validation loss
下降也不等于 dev 生成能力提升。这是该配置的负向证据，不是“QLoRA 无效”或“不能做 SFT”的普遍结论。

面试时可以这样回答：我没有把 loss 下降或 SQLite 可执行率当作微调成功，而是冻结 prompt、decode、数据、
模型 revision、GPU 和 evaluator，先记录 Base 的生成成本、SQLite 错误类别和 Test Suite 内部结果，再让只改变
LoRA adapter 是否加载的 Adapter 走同一管线。完整 delta 出现回退后，我不会直接堆数据或进入 RL，而会先做
changed-case 和错误类型诊断。能诚实报告失败 ablation，说明评测能否证伪假设，而不是只挑成功数字。由于当前镜像
早于官方修订，`0.427` 和 `0.215` 都只是固定资产组合上的内部输出，不能写成当前 Spider 官方榜单成绩。

### 5.2.1 从负向结果学习什么

这轮有一个容易忽略但很关键的事实：102 条 train-only 候选不等于模型已经训练过完整 102 条。global batch
size 是 4，`max_steps=8` 只产生最多 32 次样本暴露，Trainer 的 epoch 是 `0.313725...`。因此该 adapter
既可能因为样本太少，也可能因为更新尚未覆盖足够多 schema 而出现不稳定变化；但两者都还是待验证假设。

诊断器显示 Base 有 806 条 completion 在首段 SQL 后继续输出 prompt 风格 section，Adapter 则有 1,033 条
直接 query-shaped completion，且 256-token cap hit 从 342 降到 43。这说明 adapter 学到了停止/展示格式行为。
然而共享 normalizer 后，Adapter SQL 的中位长度反而从 87 字符升到 158 字符；不存在列错误中，带限定别名的
引用从 Base 的 16 条升到 Adapter 的 254 条。格式更像 SQL、原始 token 更少，都不足以说明 schema linking 更好。

对 3 条回退和 4 条“错误变可执行”的非随机样本做人工核验后，后四条仍存在错误 Join/粒度、缺失要求列或错误
聚合指标。因此“从 error 变 executed”的 88 条只能叫 execution recovery，不能叫正确 SQL。下一步的最小实验
只把 step 从 8 扩展至 26，让有效 batch 4 覆盖 102 条训练数据约一轮；完整评测不回退才有资格讨论扩大数据或
DPO/GRPO。

## 5.3 本次 SFT smoke 要学会什么

**为什么按 db_id 分组？** 同一个 Spider 数据库共享表、列、外键和命名风格。若随机把同库问题分到
train/validation，模型可能在训练中已经见过同一份 schema，validation loss 会过于乐观。此次 102/26
切分让 validation schema 从未出现在训练中；这比随机切分更接近 schema linking 的泛化问题，但规模仍
然太小，不代表标准 benchmark 成绩。

**8 个 step 实际做了什么？** 每个 micro-batch 只有 1 个样本，连续积累 4 次梯度后才更新一次 LoRA 参数，
所以有效 batch size 是 `1 × 4 × 1 = 4`。基座 4-bit 权重不更新；训练器只对约 1,846 万 adapter 参数做
`backward + optimizer.step`。这就是 QLoRA 的“参数高效”部分。

**loss 怎么读？** Causal LM loss 是每个未 mask 目标 token 的平均负对数似然。`0.466989` 表示模型对这 26
条 validation SQL token 的拟合程度，不等于“46.7% 错误率”，也不能和另一个模型、另一个 tokenizer、
另一个数据切分的 loss 直接横比。adapter reload 的单样本 loss `0.309798` 更不能代表整体验证集；它只证明
adapter 文件能被 PEFT 正确恢复和前向运行。

**这次为什么要保存 checkpoint 和 adapter？** checkpoint 含 optimizer、scheduler 和随机状态，可用
`--resume-from-checkpoint` 中断续跑；adapter 只保存 LoRA 增量，能在原始冻结基座上重新挂载。二者均在
仓库外，避免把模型或训练状态提交到 Git。

## 6. 面试高频问题与回答框架

### 为什么不用全量微调？

本项目目标是单张 24GB 消费卡可复现。全量微调需要保存梯度、优化器状态和激活，显存与训练时间
明显更高；LoRA/QLoRA 用少量 adapter 参数完成任务适配，更容易做基线对照、回滚和多实验管理。

### 为什么 SQL 能执行还不够？

可执行只覆盖语法和对象存在性，不能保证列的业务含义、Join 粒度、时间范围和结果解释。项目用 Catalog、
QueryPlan、AST Policy、数据库 reader role、ResultContract 和人工语义标签分层验证。

### 训练集如何防止数据泄漏？

不能只随机切分文本。要按 SQL shape、语义模板、Catalog/workspace 快照和业务时间范围分组；同义改写、
同一 SQL 模板和 v2 60 条 golden 永久 holdout 不能进入训练、偏好样本或 prompt 示例。

### 如何判断模型真的变好了？

固定模型、prompt、解码、数据库和 evaluator；对比 EX/Test Suite、schema-linking 执行错误、人工指标语义、
合同通过率、延迟和 token。训练 loss 下降只能说明拟合训练分布，不能单独证明业务准确率提升。

### QLoRA 仍然 OOM 怎么排查？

先记录 GPU UUID 和显存占用，确认没有抢占其他项目；再降低 micro-batch 和序列长度，开启 checkpointing，
减少 LoRA rank，检查是否错误地加载了全量 optimizer/多个模型，最后再考虑 4-bit 配置和量化实现兼容性。

### 为什么不能让模型决定权限？

模型输出是不可信输入。权限必须在 AST、服务器白名单、PostgreSQL role 和结果合同层确定；否则模型一句
“请忽略限制”就可能造成越权或敏感列泄漏。

## 7. 每次实验记录模板

```text
实验 ID：
假设：
基座模型 / revision / license：
数据 manifest / JSONL SHA-256：
训练 split / holdout 检查：
CUDA_VISIBLE_DEVICES / 物理 GPU UUID：
max_seq_length / micro-batch / accumulation：
LoRA target_modules / rank / alpha / dropout：
量化、精度、checkpointing：
训练步数 / wall time / peak memory：
冻结基线与本次结果：
失败样本和错误分类：
是否改变生产 PostgreSQL/Vanna 链路：否 / 是（需说明）
面试可讲的结论：
```

## 8. 当前学习顺序

1. 先读懂 tokenizer、causal LM loss 和单 batch forward；
2. 用 128 条 train-only 候选验证 schema + question + SQL 的 tokenization 和 labels mask；
3. 已下载并冻结 Qwen 1.5B，完成不训练的 forward smoke；
4. 已完成 schema-disjoint 的极小 SFT smoke，验证了 loss、显存、checkpoint 和 adapter reload；
5. 已完成 Base/Adapter 的同合同对照，并确认当前 smoke 配置在 SQLite/Test Suite 上回退；
6. 先诊断状态变化、schema-linking 错误和输出停止行为，再提出下一个最小 SFT ablation；
7. 扩大训练集前继续人工抽检数据、增加不泄漏的 schema-linking/安全样本；
8. 只有 SFT 基线稳定且没有退化后，再讨论 DPO/GRPO 和执行反馈奖励。

本轮的目标是建立可解释、可回滚的学习闭环，而不是尽快得到一个不可复核的“微调后分数”。
