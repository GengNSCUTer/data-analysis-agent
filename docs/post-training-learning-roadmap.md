# 微调与后训练学习路线

目标不是代跑一个训练脚本，而是能解释每个设计、复现实验、定位失败，并说明为什么运行时安全合同不能交给模型决定。正式训练前先完成小批量 forward smoke；没有通过数据审计和 holdout 检查，不启动训练。

## 阶段 1：语言模型基础

- Transformer、attention、tokenization、causal language modeling；
- pretraining、instruction tuning、SFT、偏好优化和 RL 的目标差异；
- cross-entropy、perplexity、teacher forcing，以及 loss 下降不等于 SQL 业务正确。

面试重点：decoder-only 模型如何预测下一个 token？为什么训练时能并行而生成时通常要逐 token？

## 阶段 2：LoRA/QLoRA

- LoRA 将权重更新约束为低秩矩阵 `ΔW = B A`，冻结基座只训练 A/B；
- QLoRA 用 4-bit NF4 保存冻结权重，再配合 LoRA 和 paged optimizer 降低显存；
- rank、alpha、dropout、target modules、gradient accumulation、checkpoint/resume；
- fp16、bf16、4-bit 的数值范围、稳定性和显存取舍。

面试重点：LoRA 可训练参数量如何计算？为什么 QLoRA 不等于“4-bit 训练全部参数”？24GB 卡为什么仍可能 OOM？

## 阶段 3：Text-to-SQL

- schema linking、schema serialization、指标/列消歧、SQL AST 与执行反馈；
- EM、execution accuracy、Test Suite Accuracy、结果等价性和人工语义准确率的差异；
- 将路由、QueryPlan、SQL 生成、修复、结果合同拆成可观测阶段。

面试重点：SQL 执行成功为什么不代表语义正确？如何定位表选错、列选错、聚合粒度错和过滤范围错？

## 阶段 4：单卡工程

- 先做 tokenizer 与 1 batch forward，再做极小 SFT smoke，再扩展 QLoRA；
- 记录 `CUDA_VISIBLE_DEVICES`、逻辑/物理 GPU 映射、显存峰值、吞吐、loss、checkpoint；
- 用 gradient checkpointing、micro-batch、accumulation 和量化处理 24GB 显存限制；
- 失败恢复、随机种子、数据哈希、配置快照和实验 manifest。

面试重点：global batch size 如何由 micro-batch、gradient accumulation 和 GPU 数计算？如何区分数据 OOM、激活 OOM 和 optimizer OOM？

## 阶段 5：评测与发布

- 按 SQL shape/语义模板/Catalog/workspace 分组，防止同义改写泄漏；
- 同时报告路由 Macro-F1、澄清召回、AST/权限/执行/ResultContract、安全 false-allow、延迟和 token；
- 永久 holdout 只用于最终比较；训练 loss 或单一 pass 指标不能单独支撑“准确率提升”；
- 模型只生成候选，不能替代 AST Policy、数据库 reader role、ResultValidator 或 ChartContract。

面试重点：如何设计无泄漏 Text-to-SQL 评测？为什么安全策略必须是服务器确定性模块？

## 每次实验的学习记录

每次实验都记录：问题、原理假设、配置、GPU 映射、数据版本、训练/验证结果、失败排查、与冻结基线的对比、面试表达。第一轮只做 1 batch forward，不下载大模型、不训练。

