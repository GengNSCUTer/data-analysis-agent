# Olist Medium v1 训练与成对评测合同

本合同对应仓库外已冻结的 `olist-domain-sft-medium-v1` 数据集。它只定义离线
候选 SQL 生成实验，不改变 Vanna/PostgreSQL 生产路径。

## 数据边界

- `train.jsonl`：720 条，只用于参数更新。
- `validation.jsonl`：240 条，只用于训练期间的 loss 观察和配置/模型选择。
- `final_evaluation_only/in_domain_test.jsonl`：240 条，只能在训练配置和最终 adapter 冻结后做一次成对生成评测。
- 三个 split 的 `family_id` 与 `QuerySpec` 不重叠；测试文件不得被训练入口读取。
- 训练入口必须校验 `split_audit.json`、文件 hash、runtime Prompt 版本和 `max_seq_length=3072`；禁止静默截断。

## 冻结训练配置

| 参数 | 冻结值 | 说明 |
| --- | --- | --- |
| 基座 | Qwen2.5-Coder-1.5B | 与已物化数据的 tokenizer 合同一致 |
| 基座权重 | `bf16_lora` | 未量化、冻结；本轮不使用 QLoRA |
| LoRA | `r=16, alpha=32, dropout=0.05` | 只更新 adapter 参数 |
| 目标模块 | q/k/v/o projection 与 gate/up/down projection | 覆盖注意力和 MLP |
| optimizer | `adamw_torch` | optimizer state 不做 8bit 量化 |
| learning rate | `1e-4` | LoRA 参数更新步长 |
| weight decay | `0.01` | 解耦权重衰减已启用 |
| train batch | 2 | 一次真实并行 forward/backward 处理 2 条 |
| gradient accumulation | 2 | 每 2 个 batch 做一次 optimizer step，有效 batch=4 |
| eval batch | 2 | 仅影响验证吞吐，不改变训练梯度 |
| epochs | 2 | 不使用 test 做 early stopping |
| max sequence length | 3072 | 覆盖实际最大 2915 token，禁止截断 |
| seed | 20260904 | 固定 Python/NumPy/PyTorch/Trainer 数据顺序 |
| precision | bf16 + gradient checkpointing | 用重算换显存，不改变标签合同 |

训练期间按 global step 记录 train loss 和 validation loss，并保存有限 checkpoint；
validation 只参与稳定性观察和事先约定的模型选择，不能据此宣称 SQL 业务正确。完整训练
前必须用相同参数完成 1-step GPU smoke，确认 batch、反传、optimizer step、显存峰值和
adapter 写出。

## 成对生成评测

训练结束后 fresh reload 最终 adapter，使用同一基座 revision、tokenizer、runtime Prompt
顺序、greedy decode、seed、输入/输出 token 预算，分别生成 Base 与 Adapter。两次生成唯一
变量是是否加载 adapter；两侧均不得读取 gold SQL、数据库结果或最终测试内容以外的训练信息。

冻结 `in_domain_test` 后，才允许读取 Gold SQL 做只读比较。报告至少包含：SQL 可解析/可执行、
SqlPolicy 通过率、ResultContract/ResultValidator 通过率、canonical SQL/结果等价性、changed
case 数量、失败类别和固定抽样的人工口径复核。不得把生成成功或 validation loss 直接等同于
业务准确率，也不得直接接入生产默认模型。

## 停止条件

发现 split/hash/Prompt/长度合同漂移、测试数据被读取、显存 OOM、adapter reload 失败或
成对生成变量不一致时立即停止；保留外部日志和 evidence，不修改数据集来适应训练脚本。

