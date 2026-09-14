# Olist v3.1｜SQL-only LoRA 训练与 Matching 评测冻结合同

## 1. 决策、范围与停止条件

本合同冻结 Olist v3.1 的第一轮 SQL-only LoRA 实验协议。它只回答一个受限问题：在**相同的运行时 Candidate SQL Prompt**、相同 Qwen2.5-Coder-1.5B Base、相同 greedy decode 下，使用 v3.1 的 train 和 validation 更新 LoRA 后，`adapter_best` 是否比未加载 adapter 的 matching Base 更常生成可通过受控后验链路的 PostgreSQL SQL 候选。

本合同不授权启动训练、GPU smoke、完整评测或生产接入；它只把正式启动前不能漂移的输入、模板、训练和评测条件写清楚。下一项独立工作必须先对本合同所指入口做 CPU preflight；只有用户明确确认后，才允许一次 screen 中的单 step GPU smoke。任何 SFT 文件 hash、release audit、Catalog/Prompt、Base revision、长度合同或 GPU UUID 漂移，均停止后续步骤，不得“就地修复后继续训练”。

## 2. 冻结数据输入与隔离边界

唯一训练数据 release 为仓库外目录：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1.1/
    sft-release-v1.1/
      train.jsonl
      validation.jsonl
      final_evaluation_only/in_domain_test.jsonl
      split_audit.json
```

| 资产 | 用途 | 行数 | SHA-256 |
| --- | --- | ---: | --- |
| `train.jsonl` | 参数更新 | 3,000 | `be3a9276abb3690c223aa68e064e4fb8e1ab3055f282c56c727dadd46aa7c7c8` |
| `validation.jsonl` | 选择 best checkpoint | 750 | `b2544aa8cef3f98134944df3a957dc046938bb351fdc64735b3139f6deefde38` |
| `in_domain_test.jsonl` | 训练后最终评测 | 750 | `67c1db3f9a3d7a74160306cd1680ea6f3c6be00035d2c95d860bb3d6e1266750` |
| `split_audit.json` | 上述切分的元数据与来源绑定 | — | `21bf95d057292e86ee58fa4ad0b515cd2533280dc154e3903a9551f44b98aa24` |

final test 物理位于 `final_evaluation_only/`，不得被 Trainer、validation、few-shot、错误回流或模型选择读取。它与 train/validation 的 `family_id`、`query_spec_id` 和 canonical Gold SQL hash 均为零交集。

训练前数据有效性的最新独立复核位于：

```text
.../olist-v3-balanced-release-v1.1/
  release-contract-audit-20260914-v3_1-recomputed/release_contract_audit.json
```

该报告 SHA-256 为 `58cc0aea5eb26e07bf7ef1bb751eb43ab15360b9f8f028bfb7c0f8313b88384a`。除了结构、准入、八问法、纯中文和长度门，它还重新计算最终三个 SFT 文件的 hash，逐行绑定 `seed_id`、split、family、QuerySpec、runtime Prompt 和 canonical Gold SQL，并重算跨 split 身份隔离。其结论为 `pass`，且记录 `model_called=false`、`gpu_used=false`、`sql_executed=false`。

## 3. 唯一允许的 SFT 输入/标签模板

每一条训练或验证样本必须直接复用真实运行时的 `olist-candidate-sql-v1`：

```text
rendered_prompt
  = Catalog + QueryPlan + ResultContract + 用户中文问题 + "### SQL"

model input
  = rendered_prompt.rstrip() + "\n" + canonical PostgreSQL SQL + EOS

labels
  = [-100] × prompt_tokens + SQL_tokens + EOS
```

这不是 Chat Template，也不是 SQLite Schema-only 模板。Prompt token 可参与注意力计算，但标签为 `-100`，不参与 loss；SQL 与 EOS 才被监督。batch 内使用动态右侧 padding：`attention_mask=0`、`labels=-100`。不得截断 Prompt 或 SQL。

冻结长度上限为 3,072。v3.1 的实际范围如下：

| split | 完整序列 token 范围 | SQL + EOS token 范围 |
| --- | ---: | ---: |
| train | 1,066–2,836 | 74–749 |
| validation | 986–2,906 | 51–721 |
| final test | 1,004–2,875 | 60–745 |

因此推理的 `max_input_tokens=3072` 与 `max_new_tokens=768` 可以覆盖训练时出现的最大输入和目标；任何改动都需要重新物化并重新审计，不可只在运行命令里提高上限。

## 4. 模型与 LoRA/optimizer 配置

| 项目 | 冻结值 |
| --- | --- |
| Base | `Qwen/Qwen2.5-Coder-1.5B` Base，revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 本地模型目录 | `/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 权重模式 | `bf16_lora`；冻结 Base 以 bf16 加载，不使用 4-bit QLoRA |
| LoRA modules | `q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj` |
| LoRA | `r=16`，`alpha=32`，`dropout=0.05`，`bias=none`，`CAUSAL_LM` |
| optimizer | `adamw_torch`（非 8-bit optimizer state），learning rate `1e-4`，weight decay `0.01`，constant scheduler |
| train micro-batch / accumulation | `1 / 4`，有效 batch 为 4 |
| validation micro-batch | 1 |
| epoch / seed | 2 epoch；`20260914` |
| GPU precision | bf16、TF32，gradient checkpointing `use_reentrant=false` |

微 batch 固定为 1 不是退回“串行训练”：每个 optimizer step 仍累计 4 次 forward/backward 的梯度。这里不先提高 micro-batch，是因为 validation 的已物化最长序列达到 2,906 token；在没有 v3.1 GPU smoke 的实际显存证据前，不能把 batch=2 当成安全结论。

## 5. validation-best checkpoint 合同

3,000 个 train 行在有效 batch 4 下每 epoch 是 `ceil(3000 / 4) = 750` 个 optimizer steps；两 epoch 共 1,500 steps。冻结：

```text
evaluation_steps = 150
save_steps       = 150
save_total_limit = 2
metric_for_best_model = "eval_loss"
greater_is_better = false
load_best_model_at_end = true
```

因此正好有 10 个持久化 validation 点，每次 evaluation 都有对应 checkpoint。训练器必须在最后一个 optimizer step、best reload 之前保存 `adapter_final`；随后由 `load_best_model_at_end` 恢复 validation-best，再保存 `adapter_best`。实验 evidence 必须记录 `best_metric`、`best_global_step`、`best_model_checkpoint`、`final_global_step` 和两个 adapter 路径。

后续 headline Base/Adapter 评测**只加载 `adapter_best`**。`adapter_final` 只是诊断资产，不能在看过 final test 后临时替换成 headline 模型，否则相当于使用 final test 做模型选择。

## 6. Matching Base/Adapter 评测合同

v3.1 不复用旧的 `run_olist_medium_matching_evaluation.py` 作为正式评测入口：旧脚本名称、五问法历史说明和“生成时直接进入执行器”的结构都不满足本 release 的三阶段隔离。正式实现必须沿用 TheLook v2 的边界，而不是在旧入口上补参数。

### Stage A：generation-only

从 `in_domain_test` 投影一个仅含 `sample_id`、`seed_id`、选中的 `primary_variant_id`、运行时 `rendered_prompt`、QuerySpec/ResultContract 身份和必要 hash 的 generation-safe case 文件。投影函数必须显式逐字段读取；不得通过 `dict(row)` 把 `candidate_sql`、Gold hash 或执行结果带进 prompt。

Base 和 Adapter 各生成 750 条 completion，且两边完全一致：

- 同一 Base revision、同一 tokenizer、同一 v3.1 runtime prompt 顺序和 prompt-bundle hash；
- `do_sample=false`、`num_beams=1`、`max_input_tokens=3072`、`max_new_tokens=768`、seed `20260914`；
- 同样 bf16 加载与同一 GPU 精度；唯一变量是 adapter 是否加载；
- raw completion 仅保存在仓库外；safe report 不包含问题、SQL、Gold 或结果行；
- generation 阶段不访问 Gold SQL、数据库、Policy 或 ResultValidator。

### Stage B：pair marker

不加载模型、不连接数据库、不读取 Gold。验证 Base/Adapter 的 safe reports、raw completion hashes、case ID 覆盖、Base revision/tokenizer、prompt-bundle hash、decode 参数和 generation isolation 都完全一致后，才写一个 hash-only matching marker。任一项不同则不能进入下一阶段。

### Stage C：受控执行与 Gold 后置评测

只有 Stage B marker 通过后，才允许读取 Gold 并将两侧 completion 统一经过 `unwrap_sql_completion() -> SqlPolicy -> daa_analytics_reader -> ResultContract / ResultValidator`。随后只对合同有效且未被 Policy 截断的候选执行 Gold denotation 对照。

报告至少包含：generation completion、Policy accepted、PostgreSQL executed、ResultContract valid、eligible ordered/bag denotation match、`Base valid -> Adapter invalid` 回退、变更样本失败类别、固定分层语义抽检、生成耗时和 token 数。它不因 loss 更低、SQL 字符串更像 Gold 或个别样本通过而声明业务准确率或生产可用。

## 7. 本轮完成定义

本轮“训练前准备完成”仅表示：数据 release、v3.1 审计、输入标签模板、初始训练配置与评测隔离合同都已经冻结。尚未发生的事项包括 CPU preflight、GPU smoke、两 epoch 训练、validation-best fresh reload、Olist final test matching 和 TheLook cross-schema 评测。产品默认 SQL 候选生成与所有运行时安全边界保持不变。
