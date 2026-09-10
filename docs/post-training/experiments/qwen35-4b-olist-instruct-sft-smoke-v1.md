# Qwen3.5-4B Instruct Olist bf16 LoRA：Trainer Smoke v1

## 结论

Qwen3.5-4B Instruct 的专用 bf16 LoRA Trainer 已完成一次最小 GPU 生命周期验证。它证明：冻结 Olist Release v2 的输入 hash / split / 官方 chat template 合同可被 Trainer 回读；LoRA 会被限定注入真实文本语言模型模块；一次参数更新、验证、checkpoint、最终 Adapter 保存和“全新 bf16 Base + Adapter”重新加载均可完成，且 loss 有限。

这不是两 epoch 训练，也不是 SQL 质量、最长序列显存或 TheLook 泛化结论。当前共享 GPU 的可用显存仍不足以安全启动正式训练，因此正式启动器已准备好但会在资源门前 fail closed。

## 固定对象与边界

- 模型：`Qwen/Qwen3.5-4B@851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，后训练/Instruct 权重；基座以 bf16 冻结加载，未使用 4-bit/QLoRA。
- 数据：仅 Olist Release v2 的原始 `train.jsonl` / `validation.jsonl`，hash 分别为 `43a831…d4fd5f` 与 `1d5a6c…ce164ef`。TheLook v2 未读取，Olist in-domain final test 未读取。
- 模板：官方 Qwen3.5 chat template，`enable_thinking=False`；唯一 user message 是完整运行时 Prompt，唯一 assistant 目标是 canonical SQL 与 assistant EOT。模板 role marker、user 内容、assistant generation prefix 和空 think block 均不计 loss。
- 运行时：本实验不改变 Vanna/FastAPI/PostgreSQL 默认链路。即使后续 Adapter 参与候选生成，仍必须经过 `unwrap_sql_completion -> SqlPolicy -> readonly role -> ResultContract/ResultValidator`。

## 静态审阅结果

Qwen3.5-4B 的结构不是“每层同一套 attention + MLP”：实际枚举到 128 个允许 LoRA target：`q/k/v/o_proj` 各 8 个，`gate/up/down_proj` 各 32 个。Trainer 先逐个验证 target 位于 `model.language_model.layers.*`，若出现视觉或其他组件的同名 projection 则拒绝；随后把这些完整路径交给 PEFT，而非按全模型 suffix 盲匹配。

最终 LoRA 可训练参数为 `21,233,664 / 4,560,499,200`（`0.4656%`）；优化器为未量化的 `adamw_torch`，`weight_decay=0.01`，并启用 gradient checkpointing。

## Smoke 证据

成功 run：

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
qwen35-4b-olist-instruct-sft-smoke-v2-20260910/
```

为在共享卡上验证完整生命周期，smoke 从 train 和 validation 各选择一条**实际 token 序列最短**的冻结行（`1116`、`1086` token），做 1 step，绝不将这种 subset policy 用于正式训练或质量判断。其聚合证据 `sft_run.json` 不保存问题、SQL 或结果行。

| 项目 | 结果 |
| --- | ---: |
| global step | 1 |
| train / eval loss | `0.503757` / `0.506993` |
| fresh reload masked loss | `0.504594`（有限） |
| peak allocated / reserved | `12,204.7` / `13,384.0` MiB |
| 设备 | `CUDA_VISIBLE_DEVICES=0` -> nvidia-smi GPU 2，RTX 4090，UUID `GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52` |

首次 smoke 使用 validation 文件中的第一条 2,498-token 样本：训练前向完成并记录有限 loss，但验证阶段申请额外 `2.31 GiB` 时 OOM。该失败留下仓库外日志，不被删改；它说明不能把短样本的 14.0 GiB peak 外推到正式最长输入。第二次仅将 smoke subset 选择变为最短冻结行，才验证了保存与 fresh reload 生命周期。

## 正式训练的资源门

正式命令和数据/超参已经冻结：2,400 train、600 validation、2 epoch、最大长度 3,072、micro batch 1、gradient accumulation 4（effective batch 4）、lr `1e-4`、weight decay `0.01`、LoRA `r=16/alpha=32/dropout=0.05`、bf16，评估每 150 optimizer steps、保存每 300 steps。

正式 launcher 在加载模型前检查 GPU UUID，且要求物理 GPU 2 至少有 `17,408 MiB` 空闲。这个门来自上述实测短样本 peak 加上 2,498-token 验证额外申请的保守余量；它不是性能保证。待一张映射 GPU 的其他任务自然释放、重新满足门后，才可在独立 screen 中启动：

```bash
cd /disk2/gengnan/data-analysis-agent
screen -dmS daa-qwen35-4b-olist-sft-v1 \
  bash scripts/post_training/launchers/start_qwen35_olist_instruct_sft_screen.sh
```

完成后必须用同一 Qwen3.5-4B Base 与加载该 Adapter 的模型，在 protected TheLook v2 600-case 上按相同 Prompt、greedy decode 与运行时治理链做 matching Base/Adapter 对照；在此之前不改生产默认路径。
