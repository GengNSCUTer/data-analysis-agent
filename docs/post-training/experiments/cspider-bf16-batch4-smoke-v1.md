# CSpider bf16 LoRA Batch-4 Smoke v1

## 目标

验证正式 CSpider 长度物化输入与未量化 bf16 LoRA 的最小训练工程链路：真实
batch-4 forward/backward、一次 AdamW 参数更新、完整 validation loss、adapter 保存与
新进程 PEFT reload。它不是完整 SFT，也不测最终 test 或 SQL 生成质量。

## 冻结输入与配置

| 项目 | 值 |
| --- | --- |
| 数据 | `official-splits-length1536-v1` |
| train / validation | 8,574 / 1,034 |
| final test | 2,147，未作为 runner 输入 |
| 模型 | `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 基座模式 | `bf16_lora`，无 4-bit quantization |
| optimizer | `adamw_torch`，`weight_decay=0.01` |
| train/eval batch | 4 / 4 |
| gradient accumulation | 1 |
| effective batch | 4 |
| LoRA | `r=16`，`alpha=32`，`dropout=0.05` |
| max sequence length | 1,536，不截断 |
| GPU | logical CUDA `1` -> physical `nvidia-smi` GPU `3`，RTX 4090 |

## 结果

`max_steps=1` 成功完成，一次 optimizer update 的 train loss 为 `0.752299`；之后对全部
1,034 条 validation 运行 loss，得到 `0.996588`。训练阶段峰值为 11,550,524,416 bytes
allocated、12,480,151,552 bytes reserved，未发生 OOM。`adapter_final` 成功写出 LoRA-only
adapter，`adapter_model.safetensors` 大小为 73,911,112 bytes。

新的独立进程重新加载 bf16 基座和 adapter 后，在 validation `cspider_dev:00000` 得到有限
loss `0.997075`，reload 阶段峰值 allocated 为 3,552,441,856 bytes。GPU UUID、train/validation
hash、split audit hash 与配置清单均已记录在仓库外 evidence。

外部证据目录：

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
qwen25coder15b-cspider-bf16-lora-batch4-smoke-v1-20260902/
```

其中包含 `sft_smoke.json`、`adapter_validation.json`、adapter、checkpoint 和 screen log，均不进入 Git。

## 正确解读与下一步

本实验只证明 1.5B bf16 LoRA、真实 batch=4、普通 AdamW、动态 padding、长度合同、adapter
保存与 reload 在目标 RTX 4090 上可运行。单步 train loss、validation loss 和有限 reload loss
不能证明中文 Text-to-SQL 的 SQL 可执行性、业务语义、跨 schema 泛化或生产迁移效果。

下一项完整训练前仍需冻结 epoch、evaluation/save 间隔和 matching Base/Adapter 生成评测合同；
完整训练及其最终 test 评测必须作为后续独立批准的任务。
