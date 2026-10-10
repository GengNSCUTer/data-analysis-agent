# Multi-Schema SQL DPO v1 合同

## 1. 本轮要解决什么

当前 `Qwen2.5-Coder-1.5B` 的 multi-schema SQL-only SFT Adapter 已证明能生成大量可通过运行时合同的 SQL，但仍会在两个层面出错：

- Olist：不少候选能通过 `ResultContract`，但与 Gold 的列、行数或数值 denotation 不同；
- TheLook：部分候选仍在 schema linking、join、时间字段、状态过滤或指标口径上失败。

SFT 的学习信号是“把 Gold SQL 续写出来”。DPO 的学习信号是：**给定同一完整 Prompt，Gold SQL 应比一个有明确失败证据的模型候选更受偏好。**

本合同只定义 DPO 数据和评测边界。它不改变生产候选生成器，不绕过 `SqlPolicy`、reader role、`ResultContract` 或 `ResultValidator`，也不授权直接训练。

## 2. 与 SFT 的关系

```text
SFT: prompt -> canonical Gold SQL
DPO: prompt + (chosen Gold SQL, rejected model SQL)
     -> 提高 chosen 相对 rejected 的条件概率
```

对每个样本，DPO 的核心目标可写为：

```text
log pi_theta(chosen | prompt) - log pi_theta(rejected | prompt)
```

应相对冻结参考策略 `pi_ref` 计算，而非只奖励更长、更短或更常见的 SQL。`beta` 控制该偏好更新相对参考策略的强度。DPO loss 降低只说明模型更偏向这批成对标签，仍不能证明 SQL 的业务语义、安全性或跨 schema 泛化。

## 3. 冻结输入与永久隔离

唯一允许的语义来源是仓库外已冻结 SFT release：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  multi-schema-ecommerce-sql-only-v1-r1/
    train.jsonl       # 6,000, sha256 e5790e8f...8616df
    validation.jsonl  # 1,500, sha256 8793b186...46329
```

每行的 `rendered_prompt` 是 DPO prompt，`candidate_sql` 是 canonical Gold SQL，即 `chosen`。必须保留原始 `sample_id`、workspace、QuerySpec、program/family、风险标签、Gold SQL hash 和原始 prompt hash。

以下数据永久禁止读入 DPO pair 构造、DPO validation、checkpoint 选择、few-shot 或错误回流：

- Olist v3.1 final test 750 条及其 Gold、候选、denotation 报告；
- TheLook v3 final test 750 条及其 Gold、候选、denotation 报告；
- 从上述 holdout 改写问题、日期、别名或错误得到的任何派生记录。

## 4. Rejected SQL 的来源和等级

`rejected` 不能手写、不能由 Gold SQL 人为扰动，也不能与 `chosen` 仅有空格、大小写或注释差异。它必须来自冻结 parent Adapter 在**同一字节级 prompt**上的独立生成，并保存生成设置、模型/adapter hash、SQL 规范化 hash 和完整治理结果。

优先级从高到低如下：

| 等级 | 可用 rejected 条件 | 学到的边界 |
| --- | --- | --- |
| A | 已通过 Policy、reader role 和 ResultContract，但 Gold denotation 不匹配 | 指标公式、join、时间/状态过滤、分组和排序等语义差异 |
| B | Policy/reader 通过，但 ResultContract 拒绝 | 必需列、别名、粒度、结果形态 |
| C | Policy 接受但 PostgreSQL 执行失败 | 表列、别名、语法和 join 可执行性 |
| D | Policy 拒绝 | 只作为单独小配额诊断，不进入 v1 正式训练 |

正式 v1 首选 A，其次 B、C；不得为了凑行数让低质量 D 类负例主导。每个 rejected 必须与 Gold 规范化后不同，并有单一、可审计的失败类别。若一个 prompt 采样到多个合格 rejected，只保留“等级优先、再按固定 hash 决胜”的一个，避免把同一语义样本复制成许多训练行。

## 5. Pair 物化与配额

先在 `train.jsonl` 上做小批，不触碰 validation 或最终 holdout。来源选择不是按
`sample_id` 哈希截取：每个 workspace 先取相同数量的 source，再在 workspace 内为每种
可用 result shape 分配相差不超过一条的配额；每个 shape slot 优先选择尚未出现的
`scenario_family_id`，并以未覆盖、较少见的 risk tag 作确定性次级排序。选择清单只记录
sample ID、workspace、family、shape、risk tag 与 prompt hash，不记录 Prompt、Gold SQL 或结果。
若冻结 SFT release 的历史行缺失 risk tag，不得直接改写 release。唯一允许的 metadata overlay
是已版本化的 `olist-dpo-risk-tag-backfill-v1`：仅当 QuerySpec 证明为 Olist、单指标、`scalar` 且
旧 tag list 为空时，确定性补上 `simple_scalar_aggregation`。overlay 必须在 selection manifest 和
最终 pair source 中记录；任何其他缺失或畸形标签仍 fail-closed。

默认分层小批为 Olist / TheLook 各 6 个 source、每 source 4 次采样。它不是 DPO 训练量，
而是用于验证真实错误是否足以构成受控偏好数据。小批的最低审计维度是：

- workspace：Olist / TheLook；
- rejected 等级：A / B / C / D；
- scenario family、program、result shape、metric 个数和 risk tag；
- chosen/rejected completion token 长度、完全重复率和 canonical SQL hash；
- 每种生成结束原因、Policy、PostgreSQL、ResultContract、Gold denotation 状态。

小批自动停止门为：每个 workspace 至少 3 条 A--C pair，覆盖至少 3 个 scenario family、
至少 2 种 result shape 与至少 2 个 risk tag。未达标时审计必须写
`stop_no_dpo`，DPO Trainer 保持未授权；不得放宽门槛、借用 final test 或人工伪造 rejected。
即使通过，也仅进入独立的 pair-quality review，不自动启动训练。只有该 review 证明两侧
workspace 都能持续得到高质量 A--C pair，才冻结完整 `train_pairs.jsonl`。DPO validation
pair 只能由原 `validation.jsonl` 独立生成；它只用于训练期检查和 checkpoint 选择，不用于
梯度更新。train/validation 的 `sample_id`、QuerySpec、Gold SQL hash、prompt hash、rejected
SQL hash 均不得重合。

若某一 family 只会生成 Gold 或只有无意义的乱码/拒绝候选，应如实记录为“无合格 pair”，不从测试集借错误，也不复制其他 family 的负例。

## 6. Parent、reference 与可训练参数

为使这成为一次可解释的 SFT -> DPO 增量实验，v1 固定：

1. parent policy：当前 multi-schema SQL-only SFT `adapter_best`；
2. reference policy：同一 parent 的冻结快照；
3. trainable policy：在 parent 之上新增的 DPO LoRA delta；parent LoRA 与基座参数均冻结；
4. 推理时只有在显式加载 parent + DPO delta 时才启用新策略；现有 SFT adapter 不被覆盖。

这能回答“DPO delta 是否比同一 SFT parent 更好”，而不是把基座差异、SFT 训练差异和偏好训练混在一起。实现前必须确认所选 TRL/PEFT 多 adapter 或等价 merged-parent 方案满足上述语义，且 reference 绝不接收梯度。

## 7. 训练与评测质量门

- 训练前检查 pair JSONL hash、输入 release hash、parent adapter hash、prompt version、tokenizer、最大 prompt/完成长度和所有隔离检查；任一漂移即停止。
- DPO 的主训练验证指标为 Olist/TheLook macro preference validation loss，不得只看合并 micro loss。
- 每个候选 checkpoint 必须在原 validation 生成集上做独立的 Policy -> reader role -> ResultContract -> Gold denotation 抽样检查；不能用最终测试选 checkpoint。
- 最终只对一个预先选定 checkpoint，在两套 750 条 protected test 执行统一 Base / SFT parent / DPO Adapter 评测与 Gold denotation。
- 最终报告同时给出可执行、ResultContract valid、ordered Gold、ordered-or-bag Gold、workspace 宏平均、回退数和延迟；不将任一单项写成“业务准确率”或“生产可用”。

## 8. v1 停止条件

在以下任一情况下不启动正式 DPO：

- 小批 pair 的 rejected 大多没有明确可复核错误，或 A--C 覆盖严重偏向单一 workspace/family；
- 训练/验证/holdout 出现任何身份、Gold、问题或 prompt 泄漏；
- 无法冻结 parent/reference 的 adapter hash，或 reference 仍参与梯度；
- 显存无法支持成对 log-prob 计算而必须偷偷改为不同模型、不同 precision 或截断样本；
- 小批 DPO 后 validation 的治理/denotation 抽查明显回退。

## 9. 当前实现与小批证据

CPU-only pair schema validator 已实现为 `src/data_analysis_agent/dpo_pair.py`，覆盖 split/holdout、原始 Prompt/SQL hash、格式等价负例、A--D 证据等级和候选 provenance 的 fail-closed 回归。chosen-side seed 投影位于 `src/data_analysis_agent/dpo_pair_seed.py` 与 `scripts/post_training/data/project_multi_schema_dpo_pair_seed.py`；它只接受 manifest hash 通过的 train 行。rejected-side 纯组装器 `src/data_analysis_agent/dpo_pair_assembly.py` 要求候选同时绑定 source ID、完整 Prompt hash、raw completion hash 和共享的受限包装清洗结果，只允许 A--C 证据后再委托 pair validator 二次校验。

小批 collector 位于 `scripts/post_training/data/collect_multi_schema_dpo_candidates.py`。其生成边界只投影 `sample_id`、workspace、完整 Prompt 与 Prompt hash，刻意不读取 `candidate_sql` 或 `training_text`；全部 raw completion 先落盘并写入 `generation-completed` marker，随后重新核对 frozen train hash，才读取同一 source 的 Gold SQL。候选始终经过 `SqlPolicy -> PostgreSQL readonly transaction + workspace reader role -> ResultContract/ResultValidator`；只有合同有效候选才执行 Gold 并进行 denotation 对照。一个 source 即使采样到多个错误也只保留一个 `A > B > C` 的稳定候选。

2026-09-17 已完成最小真实 smoke，输出位于仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/multi-schema-dpo-candidate-pilot-v1-20260917/`。冻结输入为
`multi-schema-ecommerce-sql-only-sft-v1-r1` train（SHA-256 `e5790e8f...8616df`）；parent 为固定的
Qwen2.5-Coder-1.5B multi-schema SFT adapter（SHA-256 `76c77a75...ca8f6b5`）。在 physical GPU 0 的
RTX 3090 上，对 Olist 与 TheLook 各一条 train source 各采样两次（合计 4 个 raw completion）；两个
workspace 的 reader-role / ResultContract smoke 均已先通过。4 次生成均成功，raw completion 均与 Prompt
绑定，且 final test 未读、生产默认链路未改、DPO training 未启动。

这 4 次生成全部与 Gold denotation 匹配，因此 `eligible_by_tier={}`、`selected_pairs=0`；同一 source 的
两次 raw completion hash 也相同。空 pair 不是 collector 故障，正是“答对的输出不能伪装为 rejected、不能为凑
数量造负例”的预期行为。该 smoke 只证明字段流、隔离、权限、结果合同和空结果处理可靠；它**不**证明两侧
workspace 已有持续、覆盖均衡的 A--C pair，也不授权 DPO Trainer。

已实现按 workspace、scenario family、risk tag 与 result shape 分层的 train-only source
selector，且 pair schema 现在将上述 metadata 与原始 Prompt / Gold hash 一并绑定。下一项
是运行这一受控小批、审阅其 `selection-manifest.json` 与 `safe-audit.json`；若仍无足量、可复核
的 A--C pair，则按第 8 节停止条件不启动 DPO。
