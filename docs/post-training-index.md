# 后训练研究总览

这份文档是后训练分支的唯一入口。它把“数据分析 Agent 产品本身”和“离线 Text-to-SQL 后训练研究”分开：前者继续使用 Vanna、FastAPI、PostgreSQL 和服务器拥有的安全/结果合同；后者只在仓库外的 Spider SQLite 实验资产上训练和评测一个候选生成模型。研究模型不能直接获得生产数据库权限，也不能替代 SQL Policy、PostgreSQL reader role、ResultValidator 或 ChartContract。

## 先看这一页

| 问题 | 结论 |
| --- | --- |
| 项目主体是什么？ | 一个可嵌入业务网页的可信数据分析 Agent：自然语言问题经过路由、Catalog、QueryPlan、SQL AST Policy、只读 PostgreSQL、ResultContract/ResultValidator 后，返回表格、图表和证据。 |
| 后训练在解决什么？ | 提升离线模型从“问题 + 数据库 schema”生成 SQL 候选时的 schema linking、SQL 结构和格式稳定性。它不负责权限和业务口径的最终裁决。 |
| 为什么改用 Spider？ | Olist 是产品演示案例；Spider 是公开、结构化的跨 schema Text-to-SQL 研究数据，适合做可复现的候选生成和执行诊断。两者不混为一个数据集或一个指标。 |
| 现在处于哪一阶段？ | 已完成数据/环境/8-step QLoRA SFT/完整 Base-Adapter 评测和失败诊断，以及两条 26-step 受控训练与 adapter reload；自 2026-08-25 18:06 CST 起，matching base/adapter 的完整质量评测正在两个独立 `screen` 会话中运行。 |
| 当前最重要的结论？ | 首轮 8-step QLoRA 在固定 Spider dev 协议上回退。原因尚未被单点证实，但它只覆盖约 0.31 个 epoch，因此下一步先控制变量验证训练覆盖度与 4-bit 量化的影响，而不是盲目加数据或做 GRPO。 |

## 两条链路，不要混淆

```text
产品运行时：用户问题 -> Vanna/路由/语义层 -> 候选 SQL
          -> AST Policy -> PostgreSQL reader role -> ResultValidator
          -> 表格/图表/审计证据

离线研究：Spider question + SQLite schema -> Qwen 1.5B 候选 SQL
        -> 只读 SQLite diagnostics + Test Suite bridge -> 对照报告
```

运行时链路回答“这个 SQL 是否允许、结果是否符合合同”；离线研究回答“某个训练配置能否让模型提出更好的候选”。即使研究模型表现提升，生产链路的确定性安全边界也不能移除。

## 当前阶段地图

| 阶段 | 目标 | 状态 | 已有证据 / 退出条件 |
| --- | --- | --- | --- |
| R0 数据与隔离 | 可训练数据、许可证、哈希与 holdout 边界 | 已完成 | Spider train-only 候选 128 条；102/26 schema-disjoint 切分；项目 v2 60 条 golden 永久隔离。 |
| R1 环境与工程 | 单卡可加载、可训练、可恢复 | 已完成 | Qwen2.5-Coder-1.5B，Python 3.11/CUDA 12.1，QLoRA forward 和 8-step SFT smoke 通过。 |
| R2 SFT 质量门 | 先建立不回退的 SFT 基线 | 运行中 | 8-step adapter 的全量对照已是负向结果；26-step QLoRA/bf16 LoRA 训练和 reload 已完成，matching base/adapter 的 1,034-case 质量评测正在运行。 |
| R3 数据与错误迭代 | 基于诊断补数据、改模板或超参 | 未开始 | 前提是 R2 完成各自 base/adapter 成对评测和人工 changed-case 审核。 |
| R4 偏好/RL | DPO/GRPO 与执行反馈 | 未开始 | 前提是可复核的 SFT 非回退、可信 chosen/rejected 数据和成本可控的奖励。 |
| R5 受控接入 | 将候选模型作为运行时可选生成器 | 未开始 | 前提是独立质量门通过；安全、权限、结果与图表合同仍在服务器端执行。 |

## 已完成实验，按目的理解

| 实验 | 为了回答什么 | 结果 | 正确解读 |
| --- | --- | --- | --- |
| Frozen 3B Ollama baseline | 离线生成与 SQLite 受控执行能否跑通 | 1,034 条候选完成；是历史参考 | 它与 1.5B Qwen 训练实验模型、推理引擎不同，不能直接当 SFT 效果对照。 |
| Qwen 1.5B forward smoke | 模型、token mask 和显存链路是否正常 | 通过 | 只验证单 batch 前向，不是训练或准确率。 |
| 8-step QLoRA SFT smoke | 训练、checkpoint、adapter reload 能否跑通 | 102 train / 26 validation，adapter 74 MB | 只验证工程；8 steps 只产生最多 32 次样本暴露。 |
| 8-step Base vs Adapter | 这个 adapter 是否比同一基座更好 | SQLite executed 831 -> 666；Test Suite internal all 0.427 -> 0.215 | 一个有效的负向 ablation，说明不能只看 validation loss 或 SQL 外观。 |
| changed-case diagnosis | 回退是否是个别 schema 或包装问题 | 20 个开发库中 17 回退；新 alias/schema-linking 错误增加 | 当前不能归因到单一因素；先做覆盖度/量化控制实验。 |

## 本轮最小对照：质量评测运行中

两个训练任务使用相同的模型 revision、102/26 split、prompt、26 optimizer steps、seed、learning rate、batch/accumulation 和 LoRA `r=16, alpha=32, dropout=0.05`。唯一训练变量是冻结基座的存储方式。

| 任务 | 基座权重 | GPU | 已完成的工程证据 | 要回答的问题 |
| --- | --- | --- | --- | --- |
| `qlora_coverage26_v1` | 4-bit NF4 + double quant，训练 LoRA adapter | `CUDA_VISIBLE_DEVICES=0`，物理 GPU 2，RTX 4090 | 26/26 steps，reload finite loss；peak allocated 4.35 GiB | 在约一轮覆盖后，QLoRA 是否仍回退？显存是否继续有优势？ |
| `bf16_lora_coverage26_v1` | bf16 冻结基座，训练相同 LoRA adapter | `CUDA_VISIBLE_DEVICES=1`，物理 GPU 3，RTX 4090 | 26/26 steps，reload finite loss；peak allocated 5.19 GiB | 量化本身是否可能是回退来源；全精度 adapter 的显存成本是多少？ |

训练完成不等于得到质量结论。每个 adapter 都必须和**相同加载精度的 base**按固定 Spider dev、greedy decode、SQLite diagnostics 和 pinned Test Suite 评测成对比较；之后还要抽检 changed cases 的指标/Join/结果语义。

本轮的启动状态如下。所有 prediction、SQLite/Test Suite 原始输出和完整日志均保留在仓库外；在 `completed` 文件和聚合报告产生前，不把进程启动或显存占用当成质量结果。

| 对照 | `screen` 会话 | 运行顺序 | GPU 守卫 | 外部聚合目录 |
| --- | --- | --- | --- | --- |
| QLoRA-26 | `daa-qwen15b-qlora26-eval-v1` | 已完成的 4-bit Base -> 新 26-step adapter | logic `0` -> physical `2`，UUID `GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52` | `qwen25coder15b-qlora-coverage26-pair-v1-20260825` |
| bf16 LoRA-26 | `daa-qwen15b-bf16lora26-eval-v1` | bf16 Base -> bf16 adapter（同卡顺序） | logic `1` -> physical `3`，UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` | `qwen25coder15b-bf16-lora-coverage26-pair-v1-20260825` |

## 阅读顺序

1. [学习指南](post-training-learning-guide.md)：先理解 Token/SFT/LoRA/QLoRA/评测，不夹杂实时实验数字。
2. [实验台账](post-training-experiment-log.md)：再看每次实验为何运行、配置是否可比、结果如何解读。
3. [数据协议](post-training-data-protocol.md)：理解训练数据、脱敏、切分与永久 holdout。
4. [Base/Adapter 评测协议](post-training-base-adapter-evaluation.md) 和 [负向诊断](post-training-base-adapter-analysis-v1.md)：查看首轮失败的完整证据边界。

## 看日志与停止任务

外部实验目录不进入 Git。两条已完成训练的日志分别是：

```bash
tail -f /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-v1-20260825/screen-run.log
tail -f /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-v1-20260825/screen-run.log
```

这两个 `screen` 会话已在脚本正常退出后自动关闭。后续完整评测会使用新的、命名不同的会话；不要通过终止其他用户的 GPU 进程腾卡。

本轮完整评测的实时日志为：

```bash
tail -f /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-pair-v1-20260825/screen-run.log
tail -f /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-pair-v1-20260825/screen-run.log
screen -ls
```
