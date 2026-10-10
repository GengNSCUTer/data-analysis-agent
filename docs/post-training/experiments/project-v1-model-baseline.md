# Project v1 主模型基线与可主张结论

> **状态：** Week 1 复盘冻结的主实验说明。  
> **审查日期：** 2026-09-18；2026-10-10 校正训练域与 ordered/bag 统计口径。  
> **范围：** 只汇总已经完成、可回读的多 Schema SQL-only SFT 和两套 protected final test；不重跑生成、训练或最终评测。  
> **原始大文件位置：** `/disk2/gengnan/data-analysis-agent-data/experiments/`；本文件不复制问题、Gold SQL、模型 completion 或数据库结果行。

## 1. 一句话结论

项目当前可作为主实验表述的后训练成果是：

```text
Qwen2.5-Coder-1.5B Base
  + bf16 LoRA
  + 两个隔离电商 workspace 的 SQL-only SFT
  + validation-best adapter_best
```

在固定中文业务 Prompt、greedy decode，以及同一条受控执行链
`SqlPolicy -> PostgreSQL readonly role -> ResultContract / ResultValidator -> Gold denotation`
下，该 Adapter 相对同一 Base 在 Olist 与 TheLook 的留出测试上均有明显提升。训练集已包含两个 workspace，因此两者均属于已见 Schema 的留出评测，不能称为未见 Schema 泛化。

它是**离线候选 SQL 生成**的实验证据，不是“已经替代在线模型”、不是开放域 Text-to-SQL SOTA，也不是生产可用性证明。

## 2. 被冻结的训练对象

| 项目 | 冻结值 |
| --- | --- |
| Base | `Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 训练目标 | 给定中文业务问题、当前 workspace Catalog、QuerySpec、ResultContract，只续写 canonical PostgreSQL SQL + EOS |
| Prompt | `multi-schema-sql-only-v1`；训练和评测使用同一 SQL-only 输入边界 |
| 训练 workspace | Olist 与 TheLook；每个 workspace 都提供自己的 Catalog、QuerySpec 和 ResultContract |
| 训练 / validation | Olist `3,000 / 750` + TheLook `3,000 / 750`，合计 `6,000 / 1,500` |
| 禁止输入 | 两套 protected final test 的问题、Gold、数据库结果、失败分析和任何派生改写 |
| 权重方式 | bf16 LoRA，`r=16`、`alpha=32`、dropout `0.05`；Base 冻结 |
| Optimizer | `adamw_torch`，learning rate `1e-4`，weight decay `0.01` |
| 批处理 | micro batch `1`，gradient accumulation `4`，effective batch `4` |
| 训练长度 / 轮数 | `3072` tokens，2 epoch，3,000 optimizer steps |
| 可训练参数 | `18,464,768 / 1,562,179,072`，约 `1.182%` |

标签布局仍是 causal SFT 的标准形式：Prompt token 可被模型注意到，但对应 `labels=-100`，只有 Gold SQL 和 EOS 参与 loss。它训练的是“在受控输入下产生候选 SQL”，而不是在运行时自行决定权限、指标口径或是否执行查询。

## 3. 为什么主模型是 `adapter_best`

训练的最终 step 是 3,000，但项目主结论只使用训练期 validation 已选出的
`adapter_best`，而不是事后从 final test 挑模型：

| 选择字段 | 值 |
| --- | --- |
| best checkpoint | step `2700` |
| best validation loss | `0.00026942291879095137` |
| 训练最后 step | `3000` |
| final train loss | `0.0011010917428381315` |
| Adapter config SHA-256 | `d2fb8ebc074c919173f2c9e5244bf439209362d05f5a739ebdb6e9406cf52b80` |
| Adapter weights SHA-256 | `76c77a75461ce5b3b8d30047c226a44087538259aa97b8509c3f7350bca8f6b5` |

这里的 validation loss 用于在训练期确定 checkpoint；它本身不等于业务语义准确率。最终语义比较在模型与生成输出冻结后才访问各自 Gold denotation。

## 4. 最终评测合同

两套 final test 各 750 条，并且均在生成阶段禁止访问 Gold SQL 或结果。生成完成、Base/Adapter case 顺序与 Prompt hash 通过核对后，候选才进入：

```text
completion normalization
-> SqlPolicy
-> 对应 workspace 的 PostgreSQL reader role
-> ResultContract / ResultValidator
-> 对合同有效候选执行 Gold denotation 对照
```

`ResultContract valid` 只表示候选返回的列、粒度和结果形态满足当前运行时合同；它**不是** Gold 语义正确。Gold ordered denotation 才要求结果列顺序、行和数值都与 Gold 对照一致。

终测汇总文件的 SHA-256 为：

```text
bf6ae7e5c3395b183fdd02afeedaa3f35bc55933080ff499a44c35ed1b92b847
```

## 5. Base 与 `adapter_best` 对照

### 5.1 Olist v3.1 域内 protected test（750 条）

| 关口 | Base | Adapter-best | Adapter-best 占 750 条 |
| --- | ---: | ---: | ---: |
| 成功生成 | 750 | 750 | 100.0% |
| Policy accepted | 338 | 747 | 99.6% |
| PostgreSQL executed | 135 | 732 | 97.6% |
| ResultContract valid | 54 | 716 | 95.5% |
| ordered Gold denotation match | 2 | 403 | 53.7% |

Base 另有 1 条仅 bag 匹配；原汇总中的 3 条是 ordered 与仅 bag 匹配的合计，不是 3 条 ordered 匹配。

Adapter 的 716 条合同有效候选中，403 条 ordered Gold match；其余失败由
`column_mismatch=296`、`denotation_mismatch=17` 和未通过结果合同 `34` 构成。
这说明“能按受控合同生成并执行”与“最终业务答案一致”仍是两层不同证据。

### 5.2 TheLook v3 已见 Schema protected test（750 条）

| 关口 | Base | Adapter-best | Adapter-best 占 750 条 |
| --- | ---: | ---: | ---: |
| candidate executed | 182 | 611 | 81.5% |
| ResultContract valid | 133 | 550 | 73.3% |
| ordered Gold denotation match | 41 | 396 | 52.8% |
| bag Gold denotation match | 62 | 437 | 58.3% |

在 ordered-match 转移中，360 条从 Base 非匹配变为 Adapter ordered match，5 条从 Base ordered match 退化为 Adapter 非匹配，36 条两者均匹配，349 条两者均不匹配。

TheLook 是第二个隔离电商 workspace，但本轮训练已经包含 TheLook 的 3,000 条样本。这组数据支持双 workspace 留出质量的结论，不支持未见 Schema 迁移或开放域泛化的结论；真正未见 TheLook 的证据只存在于早期 Olist-only 模型实验。

## 6. 支持与不支持的项目表述

| 可以诚实表述 | 不能表述 |
| --- | --- |
| 构建了 `6,000/1,500`、双 workspace、SQL-only 的 SFT 数据与 hash/holdout 隔离合同。 | “在任意数据库上准确率 53.7%/52.8%。” |
| 用 bf16 LoRA 微调 1.5B Coder 模型，并以 validation-best checkpoint 选择 `adapter_best`。 | “loss 很低，所以模型理解了所有业务语义。” |
| 在 2 × 750 条冻结电商评测上，Adapter 相对 Base 提高了受控执行、ResultContract 和 Gold denotation 指标。 | “716 条 ResultContract valid 就是 716 条业务正确。” |
| 把候选 SQL 放在 Policy、reader role、ResultValidator 与审计链之后，模型不能自行绕过这些边界。 | “本地 Adapter 已替代 SiliconFlow / 已在 Web 默认路径运行。” |
| 本轮 TheLook 结果是同领域、已见 Schema 的独立留出评测。 | “本轮证明未见 Schema 泛化 / 已达 Text-to-SQL SOTA / 生产可用。” |

## 7. 与产品运行时的关系

当前 Web Demo 仍使用 SiliconFlow 的在线模型处理对话、工具调用与受限 repair。本地 1.5B
`adapter_best` 已被核实为离线候选 SQL 资产，但尚未有 Web provider、GPU queue、超时、故障策略或运行时模型选择合同。

若以后接入，最小正确路径仍然只能是：

```text
server-derived Catalog / QueryPlan / ResultContract
-> local Adapter candidate SQL
-> SqlPolicy -> reader role -> ResultValidator
-> ResultArtifact / UI
```

因此，模型能力和可信执行能力是互补关系：前者改善候选，后者决定是否允许执行和展示。

## 8. 相关证据与下一步

- 数据边界与训练模板：[`../data/multi-schema-ecommerce-sql-only-sft-v1.md`](../data/multi-schema-ecommerce-sql-only-sft-v1.md)
- DPO 是否有资格启动：[`project-v1-dpo-decision.md`](project-v1-dpo-decision.md)
- 产品/模型缺口与后续优先级：[`../../architecture/project-v1-gap-map.md`](../../architecture/project-v1-gap-map.md)
- 原始训练状态：仓库外 `qwen25coder15b-multischema-sql-only-sft-v1-20260916/adapter_checkpoints/trainer_state.json`
- 原始 final summary：仓库外 `qwen25coder15b-multischema-v3_1-olist-thelook-base-adapter-v1-20260916/terminal-evaluation-summary-v1.json`

下一步不是在 protected test 上继续调参或重跑，而是按项目 Week 2--4 路线先收束产品运行时、结果交接与独立开发集，再决定是否需要新一轮 SFT 或有证据授权的 DPO。
