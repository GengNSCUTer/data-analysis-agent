# 后训练实验台账

本台账只记录已完成实验和待评测的受控实验，不在这里讲通用概念。原始训练样本、SQL、预测、数据库、模型权重、checkpoint 和完整日志都留在仓库外；这里仅记录可复核的配置、哈希和聚合结果。当前学习顺序在上级目录的 `README.md` 中维护。

## 固定边界

- 研究模型：`Qwen/Qwen2.5-Coder-1.5B`，revision `df3ce67c0e24480f20468b6ef2894622d69eb73b`。
- 训练数据：历史 v1 smoke 使用 Spider train-only 128 条、102/26 schema-disjoint split；当前 v2 使用 3,600 条候选、3,048/552 schema-disjoint split。两者均不读取 dev gold SQL。
- 评测数据：当前 v2 使用固定的官方 Spider 1.0 release `dev.json`（1,034 prompts）；生成不读取其 gold SQL。历史 2020-01 mirror 结果单独保留，不与当前 release 混用。Test Suite 输出只作为固定资产组合的内部对照，不写为当前官方榜单成绩。
- 永久隔离：项目的 60 条 v2 golden 不进入训练、偏好数据、示例或改写。
- 运行时隔离：生产 Vanna/FastAPI/PostgreSQL 代码不被离线训练改写；模型候选仍需通过服务器安全和结果合同。

## 最近完成：CSpider Adapter Olist PostgreSQL 迁移评测

2026-09-02，最终 CSpider 两 epoch bf16 LoRA Adapter 在当前 Olist 工作区的 12 条永久 protected
`answerable` holdout 上完成一次独立离线候选 SQL 评测。它只加载候选模型；每条仍经过
QuestionRouter、Semantic Catalog、QueryPlan、ResultContract、SqlPolicy、PostgreSQL readonly role
和 ResultValidator。未调用 Vanna/SiliconFlow、SQL repair、图表或生产模型切换，CSpider final test
也没有读取。

为避免重复运行 Base 而引入额外变量，本次复用 2026-09-01 中文 Base 的脱敏报告，但只在其
comparison contract 与本次 Adapter 逐字段一致后才做比较：相同 12 个 source ID、基座 revision、
bf16、`olist-candidate-sql-v1`、greedy decode、seed、256 新 token、中文源问题、PostgreSQL、禁用
repair，以及相同 manifest/source/holdout hash。Base 为 `12/12 generated, 6 policy accepted, 4
executed, 2 ResultContract valid`；CSpider Adapter 为 `12/12, 2, 1, 0`，变化为 `0/-4/-3/-2`。没有
`non-valid -> valid`，两条 Base 合同有效 case 分别变为 policy rejection 和 result-contract rejection。

Adapter 安全报告 SHA-256 为 `e159240a8d873907019d5d5382960b6b9101d10289d2cdcd05014c035e71a615`，
paired safe comparison 为 `0d8e032715097b241b0d2cbd88abefbfe5ee321a91f6b7ece6211a9610cba2f4`，adapter
权重为 `35eb45c0ebccaaeaf2cefb742473788031eb01bb3948412b2f35c5840c974983`。它们及完整日志、原始
问题、候选 SQL 和结果行均保留在仓库外的
`qwen25coder15b-cspider-bf16-lora-olist-transfer-v1-20260902/`。当前 runner 的合同冻结 Catalog、
数据集版本和策略版本，但未独立 hash PostgreSQL 内容快照，因此结论仅对该冻结合同和工作区版本负责。
这不是业务语义准确率，也不支持将 Adapter 接入生产默认路径。

## 最近完成：CSpider matching Base/Adapter 评测入口

2026-09-02，CSpider 成对生成与评测的工程入口已实现并完成本地回归，尚未启动完整 Base/Adapter
生成。生成器新增 `cspider_validation` namespace，运行前校验仓库外 acquisition manifest 的
`dev=validation_only` 角色、1,034 行数和 `dev.json`/`tables.json` 哈希；模型生成阶段只读取相同
顺序的 schema 和中文问题，禁止读取 gold SQL、SQLite 行、final test 或 Olist/PostgreSQL 上下文。

新的 matching verifier 在 SQLite 前比对 Base/Adapter evidence 与 prediction case IDs：完整 1,034
条 source order、模型 revision、bf16、prompt、token 上限、greedy decode 和数据 hash 必须相同，唯一
允许差异是 adapter 从 disabled 到 enabled。安全报告不写问题、候选/Gold SQL、库标识或结果行；CSpider
SQLite、paired analysis 与生成后 denotation 均只能在 verifier 成功后执行。专用 sequential launcher
不调用 Spider Test Suite，也不引用 CSpider final test。15 项相关回归、ruff、CLI、shell 语法和 diff
check 通过；这些只是入口正确性证据，不是 SQL 语义、可执行性或模型质量结果。

## 最近完成：CSpider matching Base/Adapter 完整离线评测

2026-09-02，CSpider official validation 的 Base 与最终 bf16 LoRA Adapter 均完成 `1,034/1,034`
候选生成，并通过 matching verifier。两侧使用相同的基座 revision、bf16、prompt v2、greedy
decode、seed、token 上限、source hash 和 case 顺序；Base 使用 physical GPU `3` 的 RTX 4090，
Adapter 使用 physical GPU `0` 的 RTX 3090。不同 GPU 只影响耗时，未用于质量对照。

只读 SQLite diagnostics：Base `911 executed / 7 policy rejected / 116 execution errors`，
Adapter `932 / 0 / 102`；执行数净增 `21`。生成冻结后 bounded denotation：Base exact-or-bag
match `525/1034 (50.77%)`，Adapter `743/1034 (71.86%)`，严格有序 match 为 `507 -> 725`。
状态迁移中 `289` 条由不匹配/不可执行变为匹配，`71` 条原匹配 case 退化，`674` 条匹配状态不变，
净增 `218`；SQLite status 有 `93` 条 execution error -> executed 与 `7` 条 policy rejected ->
executed，同时存在 `79` 条 executed -> execution error。结果是当前 CSpider validation SQLite
快照上的正向离线证据，但不是无回退通过。

Adapter 平均生成 token `108.29 -> 29.61`，Base 有 `257` 条达到 256 token 上限，Adapter 为 `0`；
Base 仅 `406` 条直接 SQL 开头，Adapter 为 `1,034`。这些是输出形态证据，不是跨 GPU 的生产延迟
或吞吐结论。CSpider final test、官方榜单、Olist/PostgreSQL 迁移和生产运行时均未使用或改变。
完整原始产物留在仓库外 pair 目录，safe paired analysis 和 denotation 报告只保存聚合/状态，不
保存问题、候选 SQL、gold SQL、数据库标识或结果行。下一步应先审查 71 条 denotation 回退及错误
类别，再决定是否设计后续训练实验。

## 最近完成：CSpider bf16 LoRA 两 epoch 训练与 reload

2026-09-02，CSpider 正式长度物化 train/validation `8,574/1,034` 在 logical CUDA `1` ->
physical GPU `3` 的 RTX 4090 上完成 bf16 LoRA 两 epoch。实际 `4,288` optimizer steps、
batch `4`、accumulation `1`、普通 AdamW `1e-4`、weight decay `0.01`；final test 未传给
runner。训练正常退出，峰值 allocated/reserved 约 `15.35/23.16 GiB`，无 OOM；末尾 full
validation loss 为 `0.318318`。最终 LoRA adapter 已在独立进程 fresh reload 并得到 finite
validation forward loss `0.018230`。

validation loss 最低值为 step `1,072` 的 `0.278697`，末尾比它高 `14.22%`，所以不能把完整
两 epoch 自动叙述为更优模型，也不能从 loss 推导 SQL 语义能力。CSpider 成对 Base/Adapter
生成、SQLite 诊断和生成后 denotation 尚未运行；历史 Spider 结论不能代入。详见
[`cspider-bf16-lora-2epoch-v1.md`](cspider-bf16-lora-2epoch-v1.md)。

## 最近完成：Olist 候选 SQL 英文 prompt 隔离对照

2026-09-01，以当前代码、当前 PostgreSQL 工作区和同一物理 RTX 4090 刷新 12 条受保护 Olist case 的中文 Base/Adapter，再仅将候选生成器看到的自然语言问题替换为外部、哈希固定的英文 overlay。中文原问题继续独占 Catalog、QuestionRouter、QueryPlan、ResultContract 和审计上下文，因此实验只测 candidate generator 的 prompt-language sensitivity，不把中文 workspace 的英文语义层缺口误归因给 LoRA。

自然英文直接进入当前 Catalog/Router 的预检为：12 条中 8 条可回答数据库路由、4 条保留与中文 grounding 相同的指标集合、8 条保持维度数一致，说明现有中文 alias/规则尚非端到端英文工作区。隔离生成对照中，Base 从中文 `12/12 generated, 6 policy accepted, 4 executed, 2 contract-valid` 变为英文 `12/12, 6, 2, 1`；Adapter 从中文 `12/12, 6, 2, 0` 变为英文 `12/12, 4, 2, 0`。Adapter 在两种语言下均未通过业务迁移子门，继续不接入生产默认路径。完整合同、哈希和限制见 [`olist-english-prompt-transfer-v1.md`](olist-english-prompt-transfer-v1.md)。

## 最近完成：Olist PostgreSQL 业务迁移 Base/Adapter 对照

2026-08-26，Spider SFT v2 的 bf16 Base/Adapter 在当前 Olist 工作区完成 12 条受保护、`answerable` 数据库 holdout 的离线候选 SQL 对照。每条均先经 QuestionRouter、Catalog、QueryPlan 和 ResultContract 构造 PostgreSQL SQL-only prompt，再经 SqlPolicy、项目 PostgreSQL reader role 和 ResultValidator 执行；未调用 SiliconFlow、Vanna agent loop、图表或 SQL repair，生产默认模型未改动。12 条均属于 `post_training_holdout_v1.yaml`，永久禁止训练、改写和提示样例使用。

Base/Adapter 都 12/12 生成，SqlPolicy 为 `6 -> 6`、PostgreSQL executed 为 `4 -> 2`、ResultContract valid 为 `2 -> 0`。状态迁移无 `non_valid -> valid`，有两条 `valid -> non-valid`。Base 总生成 token `2,481`（5 条达到 256 上限），Adapter 为 `801`（0 条达到上限）；更短的输出没有转化为业务候选质量。受控人工复核确认 Base 的两条合同有效样本符合当前平均履约天数口径，但不能由两条样本外推业务准确率。

失败模式是 SQLite/英文 schema 训练格式和当前中文业务 Catalog、QueryPlan、PostgreSQL、结果别名合同之间的迁移差异：Adapter 常遗漏 metric alias、把派生指标作为物理列、丢失 Join 路径或多指标事实粒度。结论为业务迁移子门未通过，不将 Adapter 接入运行时，也不直接扩大通用 Spider 数据；下一步先构造与 60 条 holdout 严格隔离的 Olist PostgreSQL 领域训练/验证资产。详见 [`post-training-olist-business-transfer-evaluation-v1.md`](../../post-training-olist-business-transfer-evaluation-v1.md)。

## 最近完成：官方 Spider release Base/Adapter 成对质量评测

2026-08-26，官方 Spider release 的 Base 与 26-step QLoRA Adapter 均完成 1,034/1,034 候选生成、只读 SQLite 诊断、固定 Test Suite bridge 和脱敏 paired analysis。Base 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090；Adapter 使用 logical CUDA `0` -> physical GPU `2` 的 RTX 4090。两条 `screen` 已正常退出。

SQLite executed 为 `829 -> 671`，execution error 为 `201 -> 360`，policy rejected 为 `4 -> 3`；Test Suite internal all 为 `0.433 -> 0.376`。状态迁移显示 240 条 Base 可执行候选在 Adapter 中退化，81 条错误候选恢复为可执行，净损失 158 条 executed。`no_such_column` 为 `182 -> 322`，其中限定列引用为 `15 -> 296`。Adapter 平均生成 token 从 `123.86` 降至 `36.9`，但规范化 SQL 中位长度从 `86` 增至 `121`，说明“更早停止”不是质量提升。

本轮结论是该 26-step Adapter 质量门失败。该结果只说明当前小数据、prompt 和超参组合回退，不归因于 QLoRA 本身；下一步转入官方 train-only 数据扩展与 Schema prompt v2，不进入 DPO/GRPO。完整报告见 [`post-training-official-base-adapter-analysis-v1.md`](../../post-training-official-base-adapter-analysis-v1.md)。

## 最近完成：Spider SFT v2 独立 Schema-Stratified 复验

官方 `train_spider.json` 已构造为 3,600 条可训练候选：全量覆盖 139 个可满足 token 预算的 Spider schema，使用 `spider-sft-schema-question-sql-v2`，每个列以 `table.column` 形式序列化，并保留列类型、PK 和 fully-qualified FK。构造阶段通过对应 SQLite 的只读 `EXPLAIN`，排除 3 条执行不兼容样本；随后用 Qwen tokenizer 的 1,536 token 硬预算排除 29 条过长样本，最大保留序列为 1,443 token，不发生静默截断。

最终 split 为 3,048 train / 552 validation，分别来自 118 / 21 个不重叠 schema。跨 schema 的通用 SQL text shape 有 6 个重叠，作为计数证据保留但不阻断，因为 `COUNT`、`GROUP BY` 等结构重叠不等于 schema 泄漏；所有表、列、外键 identity 仍被 schema-disjoint 边界隔离。2 epoch bf16 LoRA 已在物理 GPU 3 的 RTX 4090 完成并 fresh reload；前缀 100-case smoke 的 SQLite executed 为 `94 -> 89`，但 post-generation bounded denotation audit 为 `56 -> 69`。

为避免按前缀选择的 3 个 schema 造成误判，独立复验以固定 seed 排除前 100 条及其出现的全部 schema，从 17 个未观察 schema 选择 164 条。生成输入不含 gold SQL，gold 仅在 Base/Adapter 输出冻结后用于仓库外 bounded denotation audit。复验结果为 denotation match `97 -> 122`、SQLite executed `153 -> 155`、`no_such_column` `9 -> 8`，所有预冻结质量门通过。它允许进入完整 1,034-case 对照，但不是官方 Spider 分数，也不允许 production 接入。完整合同、哈希与限制见 [`post-training-spider-sft-v2-plan.md`](../../post-training-spider-sft-v2-plan.md)。

## 最近完成：Spider SFT v2 bf16 LoRA 全量对照

2026-08-26，规模化 bf16 LoRA 的 matching Base/Adapter 均完成官方 Spider 1.0 dev 的 `1,034/1,034` 候选生成。两者使用同一 Qwen2.5-Coder-1.5B revision、v2 schema prompt、greedy decode、case 顺序、SQLite policy 和固定 Test Suite bridge；模型生成阶段均不读取 dev gold SQL 或数据库行。Base 使用 logical CUDA `0` -> physical GPU `2` 的 RTX 4090，Adapter 使用 logical CUDA `1` -> physical GPU `3` 的 RTX 4090，screen 均正常退出。

SQLite diagnostics 为 executed `950 -> 961`、execution error `81 -> 73`、policy rejected `3 -> 0`。固定 Test Suite 的内部 all 输出为 `0.507 -> 0.667`，easy/medium/hard/extra 四个桶同方向上升。候选冻结后运行的全量 bounded denotation audit 为 exact-or-bag match `570 -> 708`，其中 213 条非匹配变匹配、75 条匹配变非匹配，净增 138。该审计不保存问题、SQL、数据库标识或结果行，且只说明当前 SQLite 快照上的执行结果关系。

Adapter 的输出形态也更稳定：direct SQL-shaped completion `370 -> 1,034`，generation-cap hit `264 -> 1`，总生成 token `114,159 -> 31,027`。这不是生产延迟/吞吐量结论。changed-case 审核显示，改善通常是多余投影/连接消失和标量聚合或 top-selection 形状恢复；回退仍包括无谓 join、表列混淆、`DISTINCT`/分组/集合重复度、投影顺序和函数形状问题。完整分析见 [`post-training-spider-sft-v2-full-analysis.md`](../../post-training-spider-sft-v2-full-analysis.md)。

本轮状态为“离线候选生成质量门通过，运行时接入延后”。不把内部 Test Suite 写为官方榜单，不将 SQLite/denotation 写为生产业务正确率，不移除运行时的安全和结果合同，也不进入 DPO/GRPO。

## 当前运行

Spider SFT v2 训练、前缀 Base/Adapter smoke、独立 164-case 复验和完整 1,034-case 对照均已完成，SQLite executed `950 -> 961`、fixed Test Suite internal all `0.507 -> 0.667`、bounded denotation `570 -> 708`。其后 12 条 Olist PostgreSQL 业务迁移评测显示 Adapter 的 ResultContract valid `2 -> 0`，因此仍不会替换生产运行时。下一项是先构建领域对齐、与当前 Catalog/QueryPlan/PostgreSQL 合同一致的 train/validation 数据，并以同一业务 holdout 重新验证，而不是直接开始 runtime 接入、DPO/GRPO 或扩大通用 Spider 样本。

2026-08-25 18:06 CST 启动的两条独立完整质量评测已经完成；以下历史启动表保留用于追溯，不再表示任务仍在运行。

| ID | `screen` 会话 | 当前阶段 | 对照合同 | 状态 |
| --- | --- | --- | --- | --- |
| `spider_sft_v2_bf16_lora` | `daa-qwen15b-spider-sft-v2-train` | 2 epoch SFT + fresh reload | official train-only、v2 prompt、schema-disjoint split | 已完成；训练/reload 工程通过，尚无独立质量结论。 |
| `spider_sft_v2_prefix_smoke100` | `daa-qwen15b-spider-sft-v2-base-smoke` / `daa-qwen15b-spider-sft-v2-adapter-smoke` | 100-case Base/Adapter diagnostics + denotation audit | official dev 前缀、v2 prompt、greedy decode、跳过 Test Suite | 已完成；SQLite `94 -> 89`、denotation `56 -> 69`，不作全量放行结论。 |
| `spider_sft_v2_independent_smoke164` | 已完成 | 164-case Base/Adapter diagnostics + denotation audit | 排除前缀 schema、schema-stratified、v2 prompt、greedy decode、跳过 Test Suite | 通过；denotation `97 -> 122`、SQLite `153 -> 155`、`no_such_column` `9 -> 8`。 |
| `official_base_adapter_pair_v1` | `daa-qwen15b-base-official-v1` / `daa-qwen15b-adapter-official-v1` | 官方 release Base 与 26-step QLoRA Adapter | 同 prompt、greedy decode、normalizer、SQLite diagnostics、pinned Test Suite | 已完成；质量门失败，详见官方专属分析。 |

本轮的两个 screen 已正常退出。脚本在进程内核验 UUID，未抢占或停止其他用户 GPU 进程；原始证据与聚合报告均保留在仓库外。

## 已完成

| ID | 假设 / 目的 | 配置 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| `forward-smoke-v1` | 验证 1.5B 4-bit QLoRA 加载、mask 与显存 | QLoRA, LoRA r=16, 单 train-only 样本 | finite loss 0.884486，peak allocated 约 1.94 GiB | 工程链路可用；没有 backward 或质量结论。 |
| `sft-smoke-v1` | 验证训练、checkpoint、adapter reload | QLoRA, 8 steps, effective batch 4 | train/eval loss 0.556203/0.466989，adapter 74 MB，peak allocated 约 3.31 GiB | 只说明工程可跑；8 steps 约 0.31 epoch。 |
| `base-adapter-pair-v1` | 验证首轮 adapter 是否改善生成行为 | 相同 4-bit base、prompt、greedy decode、1,034 dev | SQLite executed 831 -> 666；Test Suite internal all 0.427 -> 0.215 | 负向 ablation；不支持“QLoRA 提升 Text-to-SQL”的说法。 |
| `analysis-v1` | 确认回退模式 | 上述 pair 的聚合诊断与受限人工 changed-case 审核 | 253 条从 executed 变失败，88 条反向恢复；20 个库中 17 回退、3 不变；抽检的 4 条表面恢复均不满足语义 | 不能用执行 recovery 代替正确率；下一步先控制变量。 |
| `qlora_coverage26_v1` | 验证约一轮训练覆盖后的 QLoRA 工程路径 | 4-bit NF4，26 steps，effective batch 4，LoRA r16/alpha32/dropout0.05 | train/eval loss 0.427482/0.290193；peak allocated 4.35 GiB；fresh reload finite loss 0.249638 | 训练和 reload 通过；尚未与对应 4-bit base 做全量质量对照。 |
| `bf16_lora_coverage26_v1` | 在同配置下验证直接 bf16 LoRA 是否可行 | bf16 base，26 steps，其余与 QLoRA 相同 | train/eval loss 0.426504/0.309192；peak allocated 5.19 GiB；fresh reload finite loss 0.245578 | 1.5B 可在 24GB 卡做 bf16 LoRA；尚未与对应 bf16 base 做全量质量对照。 |
| `cspider_bf16_lora_length1536_full2epoch_v1` | 验证正式中文 CSpider 的 bf16 LoRA 两 epoch 训练、显存与 artifact 链路 | 8,574/1,034、1536 上限、2 epoch/4,288 step、batch 4、AdamW、weight decay 0.01 | 完成、末尾 validation loss 0.318318、peak allocated/reserved 15.35/23.16 GiB、fresh reload finite；最终 Adapter 的 CSpider validation denotation `525 -> 743/1,034` | CSpider SQLite 上正向但有 71 条回退；独立 Olist 迁移为 ResultContract valid `2 -> 0`，不能接入运行时。 |
| `cspider_bf16_lora_batch4_smoke_v1` | 验证正式 CSpider 长度物化输入能否使用真实 batch=4、未量化 bf16 LoRA 和普通 AdamW 跑通 | 8,574/1,034 CSpider train/validation，1536 上限，1 step，bf16 LoRA，batch=4，accumulation=1，AdamW `weight_decay=0.01` | 1 step 完成、全 validation finite loss、peak allocated 10.76 GiB、adapter fresh reload finite；无 OOM | 只闭合 batch-4 工程/显存/产物链路；不构成 SQL 质量或完整训练结论。详见 `cspider-bf16-batch4-smoke-v1.md`。 |
| `spider_sft_v2_independent_smoke164` | 检验 v2 SFT 的独立非回退资格 | 164 条/17 未观察 schema，bf16 Base/Adapter、v2 prompt、greedy decode | denotation `97 -> 122`，SQLite `153 -> 155`，`no_such_column` `9 -> 8` | 通过预冻结 bounded smoke 门，进入完整 1,034-case 对照；不是官方分数。 |

本轮的外部训练/reload evidence SHA-256：`qlora_coverage26_v1` 为 `c00413757f24c3bfc338b5eec3dfe689720ec03be9e55be8a415d6d0c96f587e` / `c83fa84ab2b845bbb8dc94a2a92d8b67aa786d59fccfa4d60f295ffb23cec36a`；`bf16_lora_coverage26_v1` 为 `266a6f9a4471ca9734ebd7a01829ef8a601e7628023570e08b441369bc039afd` / `1e68125d350364c8a6d8a1734cae6c4a9622576ae945e24810f936be2a877cdb`。每对依次对应 `sft_smoke.json` 和 `adapter_validation.json`。

## 下一道质量门

| ID | 已冻结的训练变量 | 必须新增的对照 | 质量门 |
| --- | --- | --- | --- |
| `qlora_coverage26_v1` | 102/26 split、26 steps、seed 20260825、lr 2e-4、effective batch 4、LoRA r16/alpha32/dropout0.05、4-bit NF4 | 使用同一 4-bit base 对 1,034 dev 做 base/adapter 成对生成、SQLite diagnostics 与 Test Suite bridge | 不能比对应 base 退化；随后做状态迁移和人工 changed-case 审核。 |
| `bf16_lora_coverage26_v1` | 其余完全相同，唯一改为 bf16 frozen base | 先建立 bf16 base，再与 bf16 adapter 使用相同 1,034 dev/greedy/evaluator 合同 | 不能把 bf16 adapter 与 4-bit base 直接比较；必须先消除加载精度这一混杂变量。 |

这两个任务并行只节省墙钟时间，不改变统计含义。它们分别在 logic 0 -> physical 2 和 logic 1 -> physical 3 的 4090 上运行，脚本在进程内校验 UUID，避免把 `CUDA_VISIBLE_DEVICES` 的逻辑编号误当成 `nvidia-smi` 物理编号。

## 结果写入规则

一个训练任务完成后，依次记录：

1. `sft_smoke.json` 中的模型 revision、数据 SHA-256、steps、loss、GPU UUID、peak memory 和 adapter hash。
2. 同精度 base 与 adapter 的完整 1,034-case 生成、SQLite diagnostics 和 Test Suite bridge。
3. 状态迁移、错误类别和有限人工 changed-case 审核。
4. 是否满足“不比对应 base 回退”的门槛。未满足则记录失败假设，不能跳到 DPO/GRPO。

只有这四层都有证据，才更新“模型质量”结论；训练完成、loss 变小或 SQL 外观更像 SQL 都不够。
