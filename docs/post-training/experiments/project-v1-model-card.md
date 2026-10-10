# 模型实验卡

数字来自 [主模型基线](project-v1-model-baseline.md) 和 [DPO 停止记录](project-v1-dpo-decision.md)。这里不新跑评测。

| 项目 | 事实 |
| --- | --- |
| Base | Qwen2.5-Coder-1.5B，提交 `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 训练 | bf16 LoRA，`r=16`，`alpha=32`，dropout `0.05`。Base 冻结。可训练参数约 1.182% |
| 优化器 | AdamW，学习率 `1e-4`，weight decay `0.01` |
| 数据 | Olist 与 TheLook 各 `3,000 / 750` 条 SQL-only。Prompt 不进 loss，只有 Gold SQL 和 EOS 进 loss |
| 选择 | validation loss 最低的 step `2700`，loss `0.000269`。不用训练末步，也不在看过 final test 之后改选 |
| 在线产品 | 仍用 SiliconFlow。这个 Adapter 没有接进网页 |

Olist 域内 750 条：Adapter 的 Policy 通过 747，执行 732，ResultContract 有效 716，ordered Gold 一致 403（53.7%）。Base 的 ordered Gold 一致是 2，另有 1 条仅 bag 一致，共 3 条 Gold 匹配。

TheLook 留出测试 750 条：Adapter 执行 611，ResultContract 有效 550，ordered Gold 一致 396（52.8%），bag Gold 一致 437。Base 的 ordered Gold 一致是 41。该主模型的训练集已包含 TheLook，因此这是已见 schema 的留出评测；早期 Olist-only 模型的 TheLook 迁移实验另行保留，不能混用口径。

ResultContract 有效只说明列和形状过了合同。ordered Gold 才要求行和数值一致。716 不是 716 条业务正确。

DPO 没有启动。train 与 validation 各 48 次分层采样都是 Gold 一致，真实 rejected SQL 是 0 对。不手写坏 SQL，也不把 protected test 拿来凑 pair。状态是 `stop_no_dpo`。
