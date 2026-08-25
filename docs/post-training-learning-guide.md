# Text-to-SQL 后训练学习指南

本指南只讲原理和面试表达，不记录某一次实验的实时数字。当前项目的实验状态请看 [后训练研究总览](post-training-index.md)，具体结果看 [实验台账](post-training-experiment-log.md)。

## 1. 先建立正确的问题模型

Text-to-SQL 不是“让模型直接操作数据库”。模型的职责是基于问题与受控 schema 生成一个 SQL 候选；服务器的职责是验证并执行候选。一个可靠系统至少要分别处理：是否该查库、指标和维度含义、SQL 语法与对象权限、执行结果是否满足本轮合同，以及最终文字或图表是否有数据证据。

因此，后训练的目标很窄也很明确：改善候选质量，尤其是 schema linking、Join/聚合结构和受控输出格式。它不学习权限绕过，不代替业务指标定义，也不让模型决定结果是否可信。

## 2. 一条 SFT 样本如何训练模型

训练文本可简化为：

```text
SQLite schema
Question
SQL
```

Decoder-only 模型训练的是下一个 token 概率。对于目标 token `y_t`，损失是交叉熵：

```text
L = -sum(log p(y_t | prompt, y_<t))
```

训练阶段使用 teacher forcing：目标 SQL 的前缀来自数据，而不是模型自己采样的错误前缀。项目的 labels mask 让 schema 和问题 token 为 `-100`，只对 SQL 与 EOS token 计算损失。生成时则不同，模型要逐 token 根据自己前一步输出继续生成，这就是“validation loss 下降不保证实际 SQL 更好”的根本原因。

面试可这样回答：SFT 让模型拟合指定输入输出分布；Text-to-SQL 真正关心的是生成时能否做对 schema linking、结构和业务语义，因此必须补充执行、Test Suite、结果合同和人工语义评测。

## 3. LoRA 与 QLoRA 到底差在哪

LoRA 冻结原始权重 `W`，不直接更新它；只学习低秩增量：

```text
W' = W + (alpha / r) * B * A
```

`r` 是 rank，`alpha` 是缩放系数。项目只向 attention 和 MLP 的线性层注入 LoRA，因此训练和保存的是 adapter，不是 1.5B 的整套模型。

QLoRA 是 LoRA 的显存优化方式：冻结基座使用 4-bit NF4 表示，计算仍使用 bf16，并常结合 double quantization、paged optimizer 和 gradient checkpointing。它节省的是冻结权重存储空间；激活、LoRA 梯度和部分临时状态不会都变为 4-bit。

| 对比项 | bf16 LoRA | QLoRA |
| --- | --- | --- |
| 冻结基座 | bf16 | 4-bit NF4 |
| 可训练参数 | 相同的 LoRA A/B | 相同的 LoRA A/B |
| 显存 | 更高 | 更低 |
| 数值近似 | 较少 | 多一层量化近似 |
| 本项目价值 | 判断 1.5B 在 24GB 卡上是否能直接做 LoRA | 判断能否节省显存而不引入可观测退化 |

对于 1.5B 模型，24GB 卡通常足以做 bf16 LoRA，特别是 micro-batch=1、序列 1536、gradient checkpointing 时。没有必要“为了 QLoRA 而 QLoRA”；应通过控制变量实验比较显存、吞吐与最终行为。

## 4. 一次训练到底做了什么

项目的 micro-batch 为 1，gradient accumulation 为 4。每读入 4 个样本才更新一次 adapter：

```text
effective global batch = micro_batch * accumulation * data_parallel_size
                       = 1 * 4 * 1 = 4
```

8 optimizer steps 最多暴露 32 个样本；对 102 条 train 数据只约为 0.31 个 epoch。26 steps 则约覆盖 104 个样本，是“先保证训练大致看过一遍数据”的最小变更。它仍然是小样本实验，不足以代表正式 benchmark 成绩。

## 5. 数据为什么这样准备

产品案例使用 Olist PostgreSQL；后训练研究使用公开 Spider SQLite。Spider 的同一 `db_id` 共享表、列和外键，如果随机按问题切分，训练集和验证集可能看到同一 schema，loss 会虚高。当前采用按 `db_id` 的 schema-disjoint 切分：102 条 train 来自 66 个 schema，26 条 validation 来自 19 个不同 schema。

项目已有 60 条运行时 golden 被永久列为 holdout，不能进入 SFT、LoRA、DPO、GRPO、示例检索或同义改写。原始 Spider 问题、SQL、预测、结果行和模型权重均保存在仓库外；仓库只保存脚本、数据来源、哈希、协议和聚合证据。

## 6. Text-to-SQL 应该怎么评测

没有单一分数可以代表“SQL 正确”。项目分别观察：

| 层级 | 要回答的问题 | 不能说明什么 |
| --- | --- | --- |
| train/validation loss | 模型对目标 SQL token 的 teacher-forced 拟合 | 生成时的 schema linking 或业务正确性 |
| AST/权限 | 候选是否违反只读、对象/列白名单等规则 | SQL 的业务口径是否正确 |
| 单库可执行性 | 在固定 SQLite 快照中能否运行 | Join 粒度、指标、过滤是否正确 |
| Test Suite | 在多个测试数据库实例上执行结果是否一致 | 当前生产业务指标正确性，也不自动等价于官方榜单 |
| ResultContract/人工审核 | 返回列、指标、时间、粒度与解释是否对 | 模型训练 loss 或简单 EM |

面试的关键句：SQL 能执行只证明语法、对象和类型大致成立。错误 Join 造成重复计数、错误时间条件或错误字段都可能成功执行，所以必须把可执行性与语义正确性分层。

## 7. 从 SFT 到 DPO/GRPO 的门槛

SFT：有可信的 input -> target SQL 时先用它建立基线。当前处于此阶段。

DPO：同一 prompt 必须有可比较的 chosen/rejected 对，例如同一 schema、权限和结果合同版本下的正确候选与错误候选。若 chosen 本身仅是“能执行”但业务指标错误，DPO 会把错误偏好写进模型。

GRPO/RL：需要模型一次采样多条候选，以执行、Test Suite、合同等信号构造奖励。它会放大数据库执行成本、采样成本和奖励投机风险。因此，只有当 SFT 对照稳定、训练数据和奖励可审计、持久 holdout 不泄漏时才进入这一阶段。

## 8. 高频面试问题

**为什么不用全量微调？** 1.5B 的全量训练要为每个参数保存梯度和优化器状态，在 24GB 消费卡上不必要且对多实验回滚不友好。LoRA 只训练低秩 adapter，更适合单卡可复现的受控实验。

**为什么用 schema-disjoint 切分？** 同库问题共享 schema。随机切分会把表名、列名和外键泄漏到验证集；按 `db_id` 分组更接近未知 schema 的泛化。

**QLoRA 为什么仍可能 OOM？** 4-bit 只压缩冻结权重。长序列激活、LoRA 梯度、batch、临时 buffer 和 optimizer state 仍占显存。排查顺序是 GPU 占用、micro-batch、sequence length、checkpointing、rank 和加载配置。

**为什么不让微调模型决定权限？** 模型输出始终是不可信输入。权限需要 AST Policy、服务器白名单和数据库 reader role 的确定性纵深防御。

**如何证明模型变好了？** 固定数据、split、base revision、prompt、解码、数据库和 evaluator，只改变一个变量；同时报告执行、Test Suite、错误类别、人工语义、token、延迟与显存。训练 loss 不是充分证据。
