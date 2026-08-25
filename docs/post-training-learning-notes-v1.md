# 数据分析 Agent｜微调与后训练学习笔记 v1

> 本文是本项目的学习材料和实验记录模板，不把计划写成已实现能力。当前状态：QLoRA
> 环境已验证，Spider train-only 候选已生成，Qwen 1.5B 已完成 forward-only QLoRA smoke；
> 尚未开始 SFT、DPO 或 GRPO。

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

未完成：

- train/validation 分组与极小 SFT smoke；
- 与冻结基线在同一评测协议上的比较；
- DPO/GRPO。它们都不能使用 v2 永久 holdout。

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
4. 在外部目录做极小 SFT smoke，只观察 loss、显存和输出格式；
5. 扩大训练集前先建立 train/validation 分组和固定回归；
6. 只有 SFT 基线稳定后，再讨论 DPO/GRPO 和执行反馈奖励。

本轮的目标是建立可解释、可回滚的学习闭环，而不是尽快得到一个不可复核的“微调后分数”。
