# CSpider bf16 LoRA 两 Epoch v1

## 目标与边界

本实验验证长度物化的 CSpider 官方 train/validation 能否完成两 epoch 的未量化 bf16
LoRA SFT，并产出可独立重载的 adapter。它不运行 CSpider Base/Adapter 自由生成、SQLite
执行诊断、denotation、final test 或 Olist 业务迁移评测，不能据此声称 SQL 质量提升。

## 冻结配置

| 项目 | 值 |
| --- | --- |
| 数据 | `official-splits-length1536-v1`，train / validation 为 8,574 / 1,034 |
| final test | 2,147，未传给训练 runner |
| 基座 | `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 权重方式 | `bf16_lora`，无 4-bit quantization |
| SFT | 2 epoch、4,288 optimizer steps、batch 4、accumulation 1 |
| 优化 | `adamw_torch`、learning rate `1e-4`、weight decay `0.01` |
| LoRA | `r=16`、`alpha=32`、dropout `0.05` |
| 长度 | 1,536，禁止截断 |
| GPU | logical CUDA `1` -> physical GPU `3`，RTX 4090，UUID guard 通过 |

## 工程结果

训练正常退出，`global_step=4,288`、`epoch=2.0`，总用时 `2,330.47` 秒。完整 validation
在训练过程中每 536 step 运行，共 8 个独立 checkpoint 事件；训练结束后又执行一次同配置的
完整 validation。末尾汇总 train loss 为 `0.117384`、末尾 logged train loss 为 `0.0608`、
末尾 full-validation loss 为 `0.318318`。训练峰值 allocated/reserved 为
`16,481,064,448 / 24,865,931,264` bytes，约 `15.35 / 23.16 GiB`，未发生 OOM。

最终 LoRA-only adapter 已保存，`adapter_model.safetensors` 为 `73,911,112` bytes，SHA-256 为
`35eb45c0ebccaaeaf2cefb742473788031eb01bb3948412b2f35c5840c974983`。新的独立进程已重新加载
同一 bf16 基座和 adapter；PEFT 加载成功、GPU UUID guard 通过，并在 validation `cspider_dev:00000`
上得到有限 forward loss `0.018230`。这验证 artifact 不依赖原训练进程。

## 验证曲线与解读

| step / epoch | full validation loss |
| --- | ---: |
| 536 / 0.25 | 0.289461 |
| 1,072 / 0.50 | **0.278697** |
| 1,608 / 0.75 | 0.287931 |
| 2,144 / 1.00 | 0.282592 |
| 2,680 / 1.25 | 0.317112 |
| 3,216 / 1.50 | 0.332047 |
| 3,752 / 1.75 | 0.300258 |
| 4,288 / 2.00 | 0.318318 |

最小 validation loss 出现在 step 1,072；末尾值较它高 `14.22%`，较首个 validation 高
`9.97%`。这表明在当前训练数据、常数学习率和 schema-disjoint validation 下，末尾 adapter
并非 validation-loss 最优。它是后续实验需要控制的现象，不足以证明过拟合的业务影响，也不能
据此直接挑选或宣称 `checkpoint-1072` 更好的 SQL 生成质量。

## 产物位置与下一步

所有权重、checkpoint、日志和 JSON evidence 均在仓库外：

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
qwen25coder15b-cspider-bf16-lora-length1536-full2epoch-v1-20260902/
```

其中 `sft_smoke.json` 是训练汇总，`adapter_final/` 是最终 adapter，
`adapter_checkpoints/checkpoint-3752/` 和 `checkpoint-4288/` 是保留 checkpoint，
`reload-validation/adapter_validation.json` 是独立重载证据，`screen-run.log` 是完整运行日志。

下一项必须先实现、审阅并测试 CSpider 专用的 matching Base/Adapter 生成与评测入口。它必须保持
两侧相同的 validation schema/question、prompt、greedy decode 和基座 revision，且直到两侧生成
冻结后才可读取 validation gold SQL 做只读 denotation 审计。final test 继续不使用。
