# CSpider 训练入口配置审阅 v1

本记录对应正式物化的 CSpider 长度合同输入，不启动训练。审阅对象是
`scripts/post_training/training/run_post_training_sft_smoke.py` 及其兼容入口。

## 输入与不变量

- train：外部 `official-splits-length1536-v1/train.jsonl`，8,574 行，只允许参数更新。
- validation：同目录 `validation.jsonl`，1,034 行，只允许评估和模型选择。
- final test：`final_evaluation_only/test.jsonl`，2,147 行，只允许配置冻结后的最终评测；训练入口不读取其正文。
- `split_audit.json` 必须证明官方 CSpider 三切分、`cspider_db_id` schema 隔离、输出 hash、SQLite 来源证据、长度合同和排除清单。
- `CausalSqlDataset` 仍按 prompt + candidate SQL + 一个 EOS 计数，超长 fail closed；collator 只做 batch 内动态右 padding。

## 默认配置

| 配置 | 冻结值 | 作用 |
| --- | --- | --- |
| `max_seq_length` | 1536 | 与物化合同一致，不静默截断 |
| `base_weight_mode` | `bf16_lora` | 基座以 bf16 加载，保持未量化 |
| `per_device_train_batch_size` | 4 | 一次 forward/backward 真实并行 4 条样本 |
| `gradient_accumulation_steps` | 1 | 每个真实 batch 完成后更新一次 |
| `per_device_eval_batch_size` | 4 | 验证阶段同样使用 batch，不改变训练参数 |
| `optimizer` | `adamw_torch` | 普通未量化 AdamW；只更新 LoRA 参数 |
| `weight_decay` | 0.01 | 显式启用解耦权重衰减 |
| `learning_rate` | 2e-4（launcher 设为 1e-4） | LoRA 参数更新步长尺度 |
| `gradient_checkpointing` | true | 用重算换显存；不改变 batch 语义 |

“真正批量”与旧配置的区别是：旧配置 `batch=1, accumulation=4` 要做 4 次串行
forward/backward，再更新一次；当前配置 `batch=4, accumulation=1` 在一次张量 batch
上完成 forward/backward，再更新一次。两者 effective batch 都是 4，但显存峰值和吞吐
不同，是否能在目标 GPU 上承受需要单独的短 smoke 验证。本轮不以配置文件静态通过
推断没有 OOM。

## LoRA/量化边界

`get_peft_model()` 注入 LoRA 后，基座参数保持冻结，只有 adapter 参数进入优化器。
bf16 模式不调用 `prepare_model_for_kbit_training()`，也不提供 4-bit quantization config。
`qlora_4bit` 和 `paged_adamw_8bit` 仍保留为历史实验复现入口，但不属于当前 CSpider
正式配置；两种模式的结果不能直接混合比较。

## 证据与未做事项

训练入口在加载模型前校验 split audit、输出 hash、行数、长度合同和外部 exclusion manifest；
最终 evidence 记录实际 batch、梯度累积、effective batch、optimizer、weight decay、基座模式和
数据 hash。验证通过只说明输入和配置满足合同，不说明 loss、SQL 语义或业务迁移质量。

本轮未启动训练、未加载模型权重、未使用 GPU、未评测 CSpider test。下一步若用户确认，
先做一次短时 batch-4 bf16 forward/backward smoke，记录峰值显存和是否 OOM；通过后再决定
是否进行完整两 epoch 训练。
