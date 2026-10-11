# Runtime candidate SQL v2：1.5B SFT 训练结束验收

2026-10-11 的独立两 epoch 训练已正常结束，退出码为 0，完成 2,990 个 optimizer step。按预先冻结的完整 validation `eval_loss` 选择 step **2,500**，`adapter_best` 与该 checkpoint 逐张量及文件 hash 一致；独立重新加载检查通过。本轮 SQL 生成、matching Base/Adapter 与最终 Gold 对照尚未运行，不能根据低 loss 更新项目的 SQL 准确率或默认在线模型。

本轮任务边界是训练结束验收与模型选择冻结。没有读取 protected final test，没有重新调参，没有重复全量 validation，也没有启动旧模板评测。阶段计划见 [`runtime 候选输入对齐`](../../../plan/refactor-runtime-candidate-sft-alignment-v1.md)。

## 训练配置与数据

| 项目 | 实际配置 / 结果 |
| --- | --- |
| 原始 Base | `Qwen/Qwen2.5-Coder-1.5B`，revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 数据协议 | `runtime-candidate-sql-v2-r1`，5,980 train / 1,500 validation；从原 Base 新训 |
| workspace | Olist 3,000/750；TheLook 2,980/750，20 条品牌分组保守预算拒绝保持排除 |
| 输入 | 当前问题 + Catalog + 实际问题派生 QueryPlan + ResultContract；无离线 QuerySpec/Gold/结果注入 |
| 标签 | 分隔换行 + PostgreSQL SQL + EOS；Prompt/padding labels=-100 |
| 长度 | 上限 4,096，实际最长 train 3,538 / validation 3,592，零截断 |
| 精度 | 冻结 Base bf16，LoRA/AdamW 历史状态未量化 |
| LoRA | r16、alpha32、dropout0.05；七类 attention/MLP 投影 |
| 可训练参数 | 18,464,768 / 1,562,179,072，约 1.182%，392 个 LoRA 张量 |
| 优化器 | `adamw_torch`，lr1e-4、constant scheduler、weight_decay0.01 |
| batch | train/eval micro-batch 2；累积 2；有效 train batch 4 |
| 调度 | 两 epoch、2,990 step；每 250 step 完整 validation eval/save，末步额外 eval/save；保留两个 checkpoint |
| best 选择 | 最小 `eval_loss`，训练结束恢复 best；final 单独保留用于诊断 |
| 随机种子 | 20261011 |
| GPU | logical CUDA 1 → physical GPU 3，RTX 4090，UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` |
| 时间 | 20:05 启动，22:17:59 退出（Asia/Shanghai）；Trainer 记录含验证/复验耗时 7,864.82 秒，约 2 小时 11 分钟 |
| 显存峰值 | allocated 17.86 GiB；reserved 22.91 GiB |
| 训练平均 loss | 0.0012747832；末次日志 loss=0.0 是日志舍入，不能解释为数学意义的零损失 |

## 完整验证曲线与 best 选择

以下 12 个点是训练期参与选择的完整 1,500 条 validation 结果。每个点均有对应保存动作，最小值出现在 step 2,500。

| step | epoch | eval_loss |
| ---: | ---: | ---: |
| 250 | 0.1672 | 0.0004893963 |
| 500 | 0.3344 | 0.0001527343 |
| 750 | 0.5017 | 0.0006466503 |
| 1,000 | 0.6689 | 0.0001124198 |
| 1,250 | 0.8361 | 0.0002002017 |
| 1,500 | 1.0033 | 0.0000717873 |
| 1,750 | 1.1706 | 0.0000491182 |
| 2,000 | 1.3378 | 0.0000300132 |
| 2,250 | 1.5050 | 0.0000229077 |
| **2,500** | **1.6722** | **0.0000192972** |
| 2,750 | 1.8395 | 0.0000326360 |
| 2,990 | 2.0000 | 0.0000271639 |

Trainer 恢复 best 后，又对完整 validation 复验了一次，得到 `0.000019297187463962473`，与 best 点一致。该复验日志也标注 step 2,990，因为训练计数没有回退；它不是末步权重的评估，不能拿来覆盖上表的 final loss。审计与曲线已把两者分开。

final loss 相对 best 高约 40.8%，但绝对差值约 `7.87e-6`。这足以支持使用预选 best，尚不足以证明业务能力退化或确定过拟合。验证曲线中也存在局部波动，并非每次更新都降低验证损失。

可交互曲线：[validation-curve.html](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-validation-curve.html)。原始选择点：[validation-curve.csv](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-validation-curve.csv)。

## 产物审计与重新加载

训练代码、release 文件、manifest、运行时/Catalog 资产和 Base manifest/各文件 SHA-256 均与冻结值一致。全部训练/验证日志中的 loss、eval_loss 和 grad_norm 有限；最后计数为 epoch 2.0 / step 2,990。

`adapter_best` 与 checkpoint-2500、`adapter_final` 与 checkpoint-2990 分别逐张量及文件 hash 一致；best/final 的 392 个 LoRA 张量均不同，两份参数均有限。两个保留 checkpoint 的 optimizer、scheduler、RNG、trainer_state 和 training_args 文件齐全；回读 optimizer 确认每个 LoRA 参数的 m/v 为 fp32，参数组 lr1e-4、weight_decay0.01/0.0，保存的 TrainingArguments 与冻结合同一致。

独立进程用训练入口同一个 `CausalSqlDataset` 与 collator 重新加载原 Base + best。每个 workspace 确定性选最长 validation 样本，分别为 3,592 / 1,918 token；确认 Prompt mask、分隔换行/SQL 监督及 EOS，重载后的 LoRA 与磁盘 best 完全一致。两条 teacher-forced loss 分别为 `2.75655e-6` / `1.66987e-6`，均有限。该检查只验证产物可用和输入合同，不是两条自由 SQL 生成评测，也没有再次选择模型。

| 产物 | SHA-256 |
| --- | --- |
| best `adapter_model.safetensors` | `926d16e4218fb9f83dc99cb1ef79094dac90adc397e3c5e0ad9526c7eb834c9e` |
| final `adapter_model.safetensors` | `51ae4a9c14954fade134008d54ea798d88591635eb596463b1f82d9a4621c75f` |
| best/final `adapter_config.json` | `f40e9a2c28f0559e44c278295d4094ab529774eabf94dba95ce534d1757ad40f` |
| best 冻结清单 | `762a54f373fe9c108721fe13c5a6f469f04af777d68fed8698b96672a6f3b3bf` |

## 证据位置与下一步

模型目录：[adapter_best](/disk2/gengnan/data-analysis-agent-data/experiments/runtime-candidate-qwen25coder15b-bf16-lora-2epoch-20261011-r1/adapter_best)。同级 `adapter_final/` 和 `adapter_checkpoints/` 保留诊断/续训产物，不作为默认终测目标。正式训练 summary 沿用历史文件名 [sft_smoke.json](/disk2/gengnan/data-analysis-agent-data/experiments/runtime-candidate-qwen25coder15b-bf16-lora-2epoch-20261011-r1/sft_smoke.json)，其 `experiment_type=runtime_candidate_sft`，不是两步 smoke。

完整 [completion-audit.json](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-completion-audit.json) 保存选择点、artifact hash、optimizer 回读和 fresh reload；同目录 `runtime-candidate-qwen25coder15b-sft-20261011-r1-audit.py` 保存本次审计脚本。用于后续评测的 [best-checkpoint-freeze.json](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-best-checkpoint-freeze.json) 绑定 Base、release、候选构造器、Adapter/hash 和选择点。启动参数/代码基线 `ad5179a`、完整日志、首 checkpoint 与退出状态均在该前缀的 `-launch.json`、`.log`、`-first-checkpoint-audit.json`、`-exit.json` 中。

目前可以确认“这轮训练流程完成、最佳产物选择正确、产物可重新加载”。loss 衡量的是给定正确前文时预测 SQL token 的能力；受控指标/SQL 结构有重复、两个 Schema 都已见过，加上 TheLook validation 只覆盖用户/事件域的三个指标，因此极低 loss 不能直接代表自由生成业务 SQL 的准确率或未见 Schema 泛化。既有项目 headline 成绩 Olist 403/750、TheLook 396/750 保留原协议，不与本轮 loss 做改善比较。

下一小单元为 TASK-008：实现并测试 v2 matching 输入/生成/治理与 Gold 对照入口，先冻结共同解码规则与源文件 hash。输入必须复用 `candidate_sql_generator.py`，不能继续用旧评测里的 QuerySpec Prompt。原 protected 问题不修正；非准入、缺少上下文或超长行保留在原 750+750 总分母，不从 Gold 补答案。完成 checkpoint 与生成输出冻结后，才能执行结果对照；当前未运行终测。
