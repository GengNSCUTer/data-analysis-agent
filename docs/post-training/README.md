# 后训练研究总览

这是后训练文档的规范入口。根目录中的旧路径保留为兼容跳转页；新的文档、链接和学习记录应从本目录进入。

## 文档职责

本目录只承载离线 Text-to-SQL 后训练研究和学习材料，不替代产品运行时文档。为避免项目进度、训练原理和实验结果再次混杂：

- `learning/` 记录概念、真实代码审阅和用户问答；
- `data/` 记录数据协议、泄漏隔离和领域覆盖设计；
- `experiments/` 只记录已运行实验的配置、聚合结果和结论；
- `archive/` 保存历史路线和旧笔记，不作为当前状态依据。

本地后训练阶段地图和实验索引以本页为准；学习问答以 `learning/review-*.md` 为准。项目的对外进度、微调实验状态、结果、风险和下一步统一同步到飞书项目文档，不写入飞书学习笔记。根目录兼容入口只用于保持旧链接有效，不再追加内容。

这份文档是后训练分支的唯一入口。它把“数据分析 Agent 产品本身”和“离线 Text-to-SQL 后训练研究”分开：前者继续使用 Vanna、FastAPI、PostgreSQL 和服务器拥有的安全/结果合同；后者只在仓库外的 Spider/CSpider SQLite 实验资产上训练和评测一个候选生成模型。研究模型不能直接获得生产数据库权限，也不能替代 SQL Policy、PostgreSQL reader role、ResultValidator 或 ChartContract。

学习顺序从现在开始以 [`learning/code-review-guide-v1.md`](learning/code-review-guide-v1.md) 和 [`learning/walkthrough-v1.md`](learning/walkthrough-v1.md) 为主。先亲自审阅真实代码、测试与边界，再由用户确认后进入下一步；不再自动连续启动训练或完整评测。

## 先看这一页

| 问题 | 结论 |
| --- | --- |
| 项目主体是什么？ | 一个可嵌入业务网页的可信数据分析 Agent：自然语言问题经过路由、Catalog、QueryPlan、SQL AST Policy、只读 PostgreSQL、ResultContract/ResultValidator 后，返回表格、图表和证据。 |
| 后训练在解决什么？ | 提升离线模型从“问题 + 数据库 schema”生成 SQL 候选时的 schema linking、SQL 结构和格式稳定性。它不负责权限和业务口径的最终裁决。 |
| 为什么改用 Spider？ | Olist 是产品演示案例；Spider 是公开、结构化的跨 schema Text-to-SQL 研究数据，适合做可复现的候选生成和执行诊断。两者不混为一个数据集或一个指标。 |
| 现在处于哪一阶段？ | 已完成 Spider 3k 级 bf16 LoRA 的 1,034-case 离线对照，并完成 12 条 Olist PostgreSQL holdout 的独立业务迁移评测。Spider 证据正向，但 Olist 迁移为 ResultContract valid `2 -> 0`，因此 production integration 继续延后。 |
| 当前最重要的结论？ | 跨 schema/SQLite 的离线提升不自动迁移到中文、Catalog/QueryPlan 驱动的 PostgreSQL 业务工作区。下一实验应优先增加受控、领域对齐的 Olist PostgreSQL 训练/验证数据，不是盲目扩大通用 Spider 样本；不能写成业务准确率或生产可用性。 |

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
| R0 数据与隔离 | 可训练数据、许可证、哈希与 holdout 边界 | 已完成 | 历史 128 条 v1 与当前 3,600 条 v2 train-only Spider 候选均有 schema-disjoint 边界；CSpider 官方三切分输入已独立构造并隔离 test，项目 v2 60 条 golden 永久隔离。 |
| R1 环境与工程 | 单卡可加载、可训练、可恢复 | 已完成 | Qwen2.5-Coder-1.5B，Python 3.11/CUDA 12.1，QLoRA forward 和 8-step SFT smoke 通过。 |
| R2 SFT 质量门 | 先建立不回退的 SFT 基线 | 已完成但失败 | 官方 release 上 matching Base/26-step QLoRA Adapter 均 1,034/1,034；SQLite executed `829 -> 671`，Test Suite internal all `0.433 -> 0.376`，限定列错误 `15 -> 296`。详见官方专属分析。 |
| R3 数据与错误迭代 | 基于诊断补数据、改模板或超参 | 本轮完成 | 3,600 条 official train-only v2 corpus、2 epoch bf16 LoRA、164-case/17-schema 独立 smoke 和完整 1,034-case 对照均完成；SQLite `950 -> 961`、Test Suite internal all `0.507 -> 0.667`、denotation `570 -> 708`。 |
| R4 偏好/RL | DPO/GRPO 与执行反馈 | 未开始 | 前提是可复核的 SFT 非回退、可信 chosen/rejected 数据和成本可控的奖励。 |
| R5 受控接入 | 将候选模型作为运行时可选生成器 | 未开始 | Spider 与 CSpider 两个 Adapter 的 12 条 Olist 业务迁移子门均未通过；最新 CSpider Adapter 的 ResultContract valid 为 Base `2`、Adapter `0`。先构建领域对齐训练资产，安全、权限、结果与图表合同仍在服务器端执行。 |

## CSpider 当前检查点

CSpider 官方 train/validation 的两 epoch bf16 LoRA 已完成，最终 adapter 已 fresh reload。对应的
CSpider Base/Adapter 成对入口已实现并通过 15 项本地回归，随后已完成 `1,034/1,034` 两侧生成、
matching verifier、SQLite diagnostics、paired analysis 和生成后 bounded denotation。Adapter 的
denotation exact-or-bag match 为 `743/1034`，Base 为 `525/1034`；同时有 71 条匹配回退和 102 条
Adapter 不可执行，因此这是当前 SQLite validation 快照上的正向但有回退的离线证据，不是官方分数、
跨数据集泛化或生产业务结论。生成阶段受 acquisition manifest、source hash、validation-only、gold
SQL 不读取和 final-test 不读取约束；final test 仍未使用。合同见
[`data/cspider-sft-2epoch-evaluation-contract-v1.md`](data/cspider-sft-2epoch-evaluation-contract-v1.md)。

同一最终 CSpider Adapter 随后以受保护的 12 条中文 Olist PostgreSQL holdout 做独立迁移
评测。复用 comparison contract 完全匹配的 bf16 Base 脱敏报告后，Adapter 的 Policy /
PostgreSQL executed / ResultContract valid 为 `2/1/0`，Base 为 `6/4/2`；不存在
`non-valid -> valid`，有两条 `valid -> non-valid`。这否定了“CSpider validation 改善即可
迁移到当前业务工作区”的假设，但不等同于业务准确率统计。原始 SQL、问题、结果行和日志均在
仓库外，且当前 runner 未单独冻结数据库内容快照；生产默认路径没有改动。完整记录见
[`experiments/cspider-bf16-lora-2epoch-v1.md`](experiments/cspider-bf16-lora-2epoch-v1.md)。

## 已完成实验，按目的理解

| 实验 | 为了回答什么 | 结果 | 正确解读 |
| --- | --- | --- | --- |
| Frozen 3B Ollama baseline | 离线生成与 SQLite 受控执行能否跑通 | 1,034 条候选完成；是历史参考 | 它与 1.5B Qwen 训练实验模型、推理引擎不同，不能直接当 SFT 效果对照。 |
| Qwen 1.5B forward smoke | 模型、token mask 和显存链路是否正常 | 通过 | 只验证单 batch 前向，不是训练或准确率。 |
| 8-step QLoRA SFT smoke | 训练、checkpoint、adapter reload 能否跑通 | 102 train / 26 validation，adapter 74 MB | 只验证工程；8 steps 只产生最多 32 次样本暴露。 |
| 8-step Base vs Adapter | 这个 adapter 是否比同一基座更好 | SQLite executed 831 -> 666；Test Suite internal all 0.427 -> 0.215 | 一个有效的负向 ablation，说明不能只看 validation loss 或 SQL 外观。 |
| changed-case diagnosis | 回退是否是个别 schema 或包装问题 | 20 个开发库中 17 回退；新 alias/schema-linking 错误增加 | 当前不能归因到单一因素；先做覆盖度/量化控制实验。 |
| SFT v2 independent smoke | 规模化 SFT 是否有资格完整评测 | 164 条/17 个前缀未覆盖 schema 的 denotation `97 -> 122`，SQLite `153 -> 155`，`no_such_column` `9 -> 8` | 通过 bounded 质量门，允许运行完整对照；不是 Test Suite 或生产结论。 |
| SFT v2 full evaluation | 规模化 bf16 LoRA 是否在完整冻结合同下超过 matching Base | SQLite `950 -> 961`、fixed Test Suite internal all `0.507 -> 0.667`、denotation `570 -> 708`；仍有 75 条 denotation 回退 | 通过离线候选生成质量门；保留运行时边界，下一步是受控接口和独立业务评测。 |
| Olist business transfer v1 | Spider Adapter 能否迁移到当前中文 PostgreSQL 业务上下文 | 12 条 protected holdout：Policy `6 -> 6`、PostgreSQL executed `4 -> 2`、ResultContract valid `2 -> 0`，0 条无效变有效、2 条有效变无效 | 业务迁移子门未通过；不接入默认运行时，不盲目扩大 Spider，先做领域对齐训练/验证。 |
| Olist English prompt transfer v1 | 仅改变候选生成器看到的问题语言是否会改变业务迁移结果 | fresh 中文/英文对照中 Base contract-valid `2 -> 1`，Adapter `0 -> 0`；英文直入中文 Catalog/Router 仅 8/12 可查库 | 英文 workspace 支持与 LoRA 语言敏感性必须分开评测，Adapter 仍不接入默认运行时。 |
| CSpider Olist transfer v1 | CSpider 两 epoch bf16 LoRA 是否能迁移到当前中文 PostgreSQL 业务上下文 | 12 条 protected holdout：Policy `6 -> 2`、PostgreSQL executed `4 -> 1`、ResultContract valid `2 -> 0`，0 条无效变有效、2 条有效变无效 | CSpider SQLite validation 的正向离线证据未迁移；不接入默认运行时，领域对齐数据仍是下一前置条件。 |

## 本轮最小对照：官方 release 质量评测已完成

两个训练任务使用相同的模型 revision、102/26 split、prompt、26 optimizer steps、seed、learning rate、batch/accumulation 和 LoRA `r=16, alpha=32, dropout=0.05`。唯一训练变量是冻结基座的存储方式。

| 任务 | 基座权重 | GPU | 已完成的工程证据 | 要回答的问题 |
| --- | --- | --- | --- | --- |
| `official_base_adapter_pair_v1` | 4-bit NF4 Base 与 26-step QLoRA Adapter | Base logic `1` -> physical `3`；Adapter logic `0` -> physical `2`，均 RTX 4090 | 两者均 1,034/1,034；SQLite `829 -> 671`；Test Suite all `0.433 -> 0.376` | 26-step 覆盖仍未消除回退，且限定列/别名错误显著增加。 |

训练完成不等于得到质量结论。每个 adapter 都必须和**相同加载精度的 base**按固定 Spider dev、greedy decode、SQLite diagnostics 和 pinned Test Suite 评测成对比较；之后还要抽检 changed cases 的指标/Join/结果语义。本轮已完成该流程，结论是 26-step Adapter 回退，不能进入下一训练范式。

本轮的完成状态如下。所有 prediction、SQLite/Test Suite 原始输出和完整日志均保留在仓库外；只将 release 指纹、聚合结果和边界写入 Git。

| 对照 | `screen` 会话 | 运行顺序 | GPU 守卫 | 外部聚合目录 |
| --- | --- | --- | --- | --- |
| QLoRA-26 | `daa-qwen15b-qlora26-eval-v1` | 已完成的 4-bit Base -> 新 26-step adapter | logic `0` -> physical `2`，UUID `GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52` | `qwen25coder15b-qlora-coverage26-pair-v1-20260825` |
| bf16 LoRA-26 | `daa-qwen15b-bf16lora26-eval-v1` | bf16 Base -> bf16 adapter（同卡顺序） | logic `1` -> physical `3`，UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` | `qwen25coder15b-bf16-lora-coverage26-pair-v1-20260825` |

## 阅读顺序

1. [代码审阅指南](learning/code-review-guide-v1.md)：以真实文件、函数、测试和审阅问题为单位，从数据到 Olist 迁移链路逐节阅读。
2. [当前学习问答审查记录](learning/review-2026-08-28.md)：先回顾本轮已讲清的运行时边界、SFT、LoRA、评测与实验决策。
3. [逐步学习审查手册](learning/walkthrough-v1.md)：按真实代码从环境、数据构建、prompt、labels、LoRA/QLoRA、SFT 到评测逐节学习。
4. [原理与面试指南](learning/fundamentals.md)：理解不夹杂实时实验数字的 Token/SFT/LoRA/QLoRA 基础。
5. [实验台账](experiments/log.md)：查看每次实验为何运行、配置是否可比、结果如何解读。
6. [数据协议](data/protocol.md)：理解训练数据、脱敏、切分与永久 holdout。
7. [CSpider 获取与预检](data/cspider-acquisition.md)：查看中文 CSpider full release 的来源、三切分、SQLite 资源和训练隔离边界。
8. [CSpider 官方三切分 SFT 输入](data/cspider-prepared-splits.md)：查看 JSONL 构造、哈希、三条来源质量排除项和 Trainer 接入前提。
9. [CSpider SFT 训练长度合同 v1](data/cspider-token-length-contract.md)：查看与真实 Dataset 一致的 token 计数、1,536 上限、超长样本门和 test 隔离。
10. [CSpider 正式长度物化结果](data/cspider-token-length-contract.md#正式物化结果)：查看 8,574 train、1,034 validation、2,147 final test 及 82 条外部排除清单。
11. [CSpider 训练入口配置审阅](data/cspider-training-entry-review-v1.md)：核对 bf16 LoRA、普通 AdamW、weight decay、batch/gradient accumulation 与 test 隔离。
12. [CSpider 两 Epoch 训练与成对评测合同](data/cspider-sft-2epoch-evaluation-contract-v1.md)：查看 4,288-step 训练、GPU UUID guard、Base/Adapter 唯一变量与生成后评测边界；当前只冻结，尚未启动。
12. [Olist 基础领域覆盖矩阵 v0.1](data/olist-pilot-coverage-v0.1.md)：审查只含单指标、安全维度和单轮显式时间的领域 pilot 范围；尚未构造数据。
13. [Spider SFT v2 规模化计划](../post-training-spider-sft-v2-plan.md)：查看当前 3k 级数据、Schema prompt v2、训练与质量门设计。
14. [SFT v2 全量评测分析](../post-training-spider-sft-v2-full-analysis.md)：查看这轮完整对照、三层证据、回退模式和决策边界。
15. [Olist 业务迁移评测](../post-training-olist-business-transfer-evaluation-v1.md)：查看本轮 PostgreSQL/Catalog/QueryPlan 对照、失败模式和下一实验边界。
16. [Olist 英文候选 prompt 对照](experiments/olist-english-prompt-transfer-v1.md)：查看冻结中文 server grounding、仅替换英文候选提示的当日 Base/Adapter 对照及其语言边界。
17. [CSpider 两 Epoch 实验与 Olist 迁移结果](experiments/cspider-bf16-lora-2epoch-v1.md)：查看 CSpider SQLite 对照，以及最终 Adapter 在受保护 Olist 工作区的独立迁移结果。
18. [Base/Adapter 评测协议](../post-training-base-adapter-evaluation.md)、[首轮负向诊断](../post-training-base-adapter-analysis-v1.md) 和 [官方 release 成对分析](../post-training-official-base-adapter-analysis-v1.md)：查看评测合同与历史失败实验。

## 看日志与停止任务

外部实验目录不进入 Git。两条已完成训练的日志分别是：

```bash
tail -f /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-v1-20260825/screen-run.log
tail -f /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-v1-20260825/screen-run.log
```

这两个 `screen` 会话已在脚本正常退出后自动关闭。后续完整评测会使用新的、命名不同的会话；不要通过终止其他用户的 GPU 进程腾卡。

本轮完整评测的外部日志和聚合报告为：

```bash
tail -40 /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-base-spider-official-v1-20260826/screen-run.log
tail -40 /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-adapter-spider-official-v1-20260826/screen-run.log
cat /disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-official-base-adapter-pair-v1-20260826/analysis-v1/safe-comparison.json
```
