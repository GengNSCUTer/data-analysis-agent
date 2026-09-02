# 后训练学习审查手册 v1

## 这份文档解决什么问题

前面的工作已经完成了一个可运行的后训练实验，但执行过程不应该被理解成“写一个脚本，放入 3,000 条数据，跑出一个模型”。实际链路包含多个相互独立的质量门。本手册把从准备微调到 Olist 业务迁移评测的全部步骤拆开，说明每一步为什么存在、读哪些文件、调用哪些函数、产生什么输出，以及哪些结论可以说、哪些不能说。

这份文档是后训练学习的主入口。旧的 `archive/learning-notes-v1.md` 保留概念和历史记录，但不再承担当前学习顺序；实验数字以 `../experiments/log.md` 和对应评测报告为准。每次问答的学习证据见 `review-2026-08-28.md`。

## 1. 先建立正确的整体模型

当前项目有两条边界不同的链路：

```text
产品运行时
用户问题 -> QuestionRouter -> Semantic Catalog -> QueryPlan
          -> Vanna/SiliconFlow -> SqlPolicy -> PostgreSQL reader role
          -> ResultValidator/ResultContract/ChartContract -> 前端

后训练研究
Spider question + SQLite schema -> Qwen 1.5B 候选 SQL
                               -> SQLite 诊断/官方 Test Suite/denotation
                               -> Olist PostgreSQL 迁移实验
```

后训练模型只负责提出 SQL 候选。它不拥有数据库权限，也不负责最终决定指标口径、表范围、权限、结果合同或图表合同。即使模型训练成功，生产环境仍然必须保留服务器端的安全链路。

本次研究的真实假设是：

> 在单张 24GB 消费级 GPU 上，用 LoRA 对 Qwen2.5-Coder-1.5B 做 Text-to-SQL SFT，能否让候选 SQL 在固定评测协议中比同精度 Base 更稳定；这种提升能否迁移到当前中文 Olist/PostgreSQL 业务上下文？

Spider 不是 Olist 业务数据，也不是生产数据。它只是公开、结构化、可复现的 Text-to-SQL 研究数据。Olist 迁移评测用于回答“通用 benchmark 提升是否转化为我们业务链路中的候选质量”，不能把两套数字混为一个准确率。

## 2. 从环境开始，而不是从训练命令开始

### 2.1 为什么单独创建 Conda 环境

训练需要 `torch`、`transformers`、`peft`、`bitsandbytes`、`accelerate`、`datasets` 等包，而项目运行时还需要 Vanna、FastAPI、PostgreSQL 驱动和 `sqlglot`。把两套依赖混在一起会导致 CUDA、Python 或上游可选依赖互相影响。

本项目使用：

| 项目 | 当前选择 | 原因 |
| --- | --- | --- |
| 训练环境 | `data-analysis-agent-qlora` | 隔离训练依赖和模型加载方式 |
| Python | 3.11 | 与当前训练依赖和 CUDA wheel 兼容 |
| PyTorch wheel | CUDA 12.1 | 宿主驱动支持范围内的保守选择 |
| 运行时环境 | `data-analysis-agent` | 项目业务链路和 PostgreSQL 集成测试 |
| GPU | 单卡 4090 优先 | 24GB 显存，实验可复现且不抢占其他任务 |

GPU 的逻辑编号和 `nvidia-smi` 编号不能直接等同。当前约定是逻辑 `0,1,2,3` 对应物理 `2,3,0,1`；训练脚本内部看到的 `cuda:0` 只是 `CUDA_VISIBLE_DEVICES` 中的第一个可见设备。每个 launcher 还会用 GPU UUID 做二次守卫。

相关记录：

- `docs/qlora-environment.md`：环境版本、CUDA 选择和显存说明；
- `evals/manifests/qlora_environment_v1.yaml`：环境审计清单；
- `AGENTS.md` 的 GPU 映射：后续任务的强制约束。

### 2.2 模型下载和冻结

文件：`scripts/post_training/training/download_post_training_model.py`。

主要函数：

| 函数 | 作用 |
| --- | --- |
| `parse_args()` | 读取模型 ID、镜像地址、输出目录和 revision。 |
| `HfApi.model_info()` | 查询模型当前 revision；将可变分支解析为固定 commit。 |
| `snapshot_download()` | 把模型文件下载到 Git 仓库外。 |
| `sha256_file()` | 对下载文件计算 SHA-256。 |
| `main()` | 写 `download_manifest.json`，记录模型、revision、license、文件大小和哈希。 |

下载时使用 `HF_ENDPOINT=https://hf-mirror.com` 或脚本的 `--endpoint`。冻结 revision 的意义是：以后 Base、Adapter、评测和复现实验必须使用同一组基础权重，而不是某天重新下载后悄悄变化的模型。

本项目冻结的是 `Qwen/Qwen2.5-Coder-1.5B` 的 revision `df3ce67c...`。模型目录、权重和 tokenizer 不提交 Git；Git 只提交脚本、manifest 和哈希证据。

## 3. 数据是怎么构建出来的

### 3.1 数据来源和隔离原则

官方 Spider 数据包提供：

- `train_spider.json`：可用于构建训练候选的自然语言问题和 gold SQL；
- `tables.json`：每个数据库的表、列、类型、主键和外键元数据；
- 对应 SQLite 文件：用于只读解析检查和后续评测；
- `dev.json`：只用于评测，不能在训练构建阶段读取 gold SQL。

项目还维护一份 `evals/manifests/post_training_holdout_v1.yaml`，其中 60 条项目 v2 golden 永久禁止进入训练、改写、few-shot 示例或偏好数据。本轮 Olist 迁移使用的 12 条 case 也被保护；它们只在候选模型生成完毕后用于受控评测。

### 3.2 候选构建器

文件：`scripts/post_training/data/build_spider_sft_candidates.py`。

它读取 train split、schema 元数据、SQLite 数据库和 holdout ID，输出到仓库外的实验目录：

```text
candidates.jsonl     # 完整审计记录，包含训练目标
training_text.jsonl  # sample_id + 模型训练文本
audit.json            # 计数、哈希、特征覆盖、过滤原因
```

关键函数：

| 函数 | 作用 | 为什么重要 |
| --- | --- | --- |
| `assert_train_only()` | 检查输入路径看起来是 train，而不是 dev/test。 | 防止误把评测集传进训练构建器。 |
| `load_holdout_ids()` | 只读取 holdout 的 case ID。 | 检查碰撞，不需要读取受保护问题内容。 |
| `normalized_sql_shape()` | 替换字面量但保留 SQL 结构和标识符。 | 统计结构覆盖和做确定性分组。 |
| `sql_feature_flags()` | 粗粒度标记聚合、JOIN、GROUP BY、子查询、集合操作等。 | 记录数据覆盖，不能代替语义评测。 |
| `read_only_explain()` | 以 SQLite 只读模式执行 `EXPLAIN QUERY PLAN`。 | 过滤明显不可解析或列不存在的 gold SQL；不物化结果行。 |
| `serialize_spider_schema_for_version()` | 调用 v1/v2 schema 序列化器。 | 确保训练和推理使用同一 prompt contract。 |
| `schema_stratified_round_robin()` | 先覆盖不同数据库 schema，再轮询 SQL shape。 | 避免 3,600 条样本被大数据库主导。 |
| `training_sequence_token_count()` | 用冻结 tokenizer 计算 prompt+SQL+EOS 长度。 | 超预算样本被排除，不静默截断 SQL。 |
| `main()` | 读入、选样本、只读检查、构造记录、写 JSONL 和审计。 | 完成一个可复核的数据准入闭环。 |

最初的 v1 smoke 构建了 128 条 train-only 候选，用于验证工程；之后的 v2 使用 schema-stratified 策略构建 3,600 条候选。最终 3,048 条进入 train，552 条进入 validation。这里的“3,600 条”不是从 3,600 条原始数据无条件复制，而是经过 train-only、SQLite EXPLAIN、token budget 和 schema/shape 选择后的候选规模。

### 3.3 Schema 序列化和训练文本

文件：`src/data_analysis_agent/spider_sft_format.py`。

`serialize_spider_schema()` 是历史 v1 格式，主要列出：

```text
TABLE table_name: column_a, column_b
FOREIGN_KEYS: left_id -> right_id
```

`serialize_spider_schema_v2()` 增加了明确的 `table.column` 身份、列类型、主键标记和完整外键两端：

```text
TABLE orders
  orders.order_id: integer [PRIMARY KEY]
  orders.customer_id: integer
FOREIGN_KEYS
  orders.customer_id -> customers.customer_id
```

`render_sft_prompt()` 生成以 `### SQL` 结尾的模型输入；`render_sft_training_text()` 在末尾拼上 gold SQL。`normalize_question()` 只清理空白和 NUL，不改变问题语义。版本常量 `PROMPT_FORMAT_VERSION` 与 `PROMPT_FORMAT_VERSION_V2` 防止历史实验被悄悄换 prompt。

训练文本的抽象结构是：

```text
### SQLite schema
<schema>

### Question
<question>

### SQL
<gold SQL>
```

schema 和 question 是条件输入，SQL 是监督目标。训练构建器不会把数据库行或查询结果放进 prompt。

## 4. 为什么必须按 schema 切分

文件：`scripts/post_training/data/split_post_training_candidates.py`。

如果随机按行切分，同一个数据库的表、列和外键可能同时出现在 train 和 validation。模型看似在验证集表现好，实际上可能只是记住了同一套 schema。项目以 Spider `db_id` 为主分组：同一个数据库的所有候选只能进入一个 split。

关键函数：

- `load_rows()`：确认每行是 train-only 且有只读执行证据；
- `db_id()`：从稳定的 split group 恢复数据库组；
- `rank_group()`：用 seed 对组进行可复现排序；
- `choose_validation_groups()`：选择约 20% 的数据库组作为 validation；
- `main()`：写 `train.jsonl`、`validation.jsonl` 和 `split_audit.json`。

`split_audit.json` 必须证明：train/validation 没有数据库重叠、没有 holdout 碰撞、输入样本有执行证据、原始数据没有进入 Git。不同数据库之间出现通用 SQL shape 重叠并不等于 schema 泄漏，所以 v2 将 shape overlap 作为审计计数，而不是误判为完全泄漏。

## 5. 先做 Forward Smoke，再做训练

### 5.1 Forward 是什么

文件：`scripts/post_training/training/run_post_training_forward_smoke.py`。

forward smoke 不更新任何参数。它只回答四个工程问题：

1. tokenizer 能否把 prompt 和 SQL 编码；
2. prompt 与目标 SQL 的长度是否在上限内；
3. 4-bit 基座和 LoRA 是否能加载；
4. 标签布局是否正确，能否得到有限的 loss。

关键流程：

```text
load_candidate()
-> split_prompt_and_target()
-> tokenizer(prompt), tokenizer(target)
-> labels = [-100] * prompt_tokens + target_tokens + [EOS]
-> BitsAndBytesConfig(NF4)
-> AutoModelForCausalLM.from_pretrained()
-> prepare_model_for_kbit_training()
-> get_peft_model(LoraConfig)
-> model(**batch)
```

`-100` 是 PyTorch/Transformers 交叉熵中的 ignore index。prompt token 被 mask，不参与 loss；只有 SQL token 和 EOS 被监督。这样模型学习的是“给定问题和 schema，继续生成 SQL”，而不是复述 schema。

本次 forward smoke 产生了有限 loss，但明确没有调用 `backward()`、`optimizer.step()` 或保存 adapter。因此它只能证明加载和标签工程，不是微调效果。

### 5.2 LoRA 与 QLoRA

训练脚本和 forward smoke 都使用 PEFT 的 `LoraConfig`，目标模块为 Qwen 的 attention 和 MLP 投影：`q_proj`、`k_proj`、`v_proj`、`o_proj`、`gate_proj`、`up_proj`、`down_proj`。

LoRA 不直接更新基础权重，而是对某个线性层的更新近似为：

```text
W' = W + (alpha / r) * B * A
```

其中基础权重 `W` 冻结，`A/B` 是低秩可训练矩阵，`r` 是 rank，`alpha` 控制缩放。当前实验使用 `r=16`、`alpha=32`、dropout `0.05`。

QLoRA 在此基础上把冻结基座以 4-bit NF4 保存，计算时用 bf16；`prepare_model_for_kbit_training()` 为量化基座准备训练，`BitsAndBytesConfig` 开启 double quant。它节省显存，但不意味着训练目标或质量天然更好。

本次 v2 主实验使用 bf16 LoRA；QLoRA 是显存受限路径。1.5B 模型在 24GB 卡上不需要为了“显得专业”强制 QLoRA，选择依据是显存、稳定性和对照公平性。

## 6. SFT 训练脚本逐函数阅读

文件：`scripts/post_training/training/run_post_training_sft_smoke.py`。

### 6.1 输入校验

- `load_rows(path, expected_split)`：读取 JSONL，检查 sample ID 唯一、split 名称正确、每条有 SQLite EXPLAIN 证据；
- `prompt_format_version(rows)`：要求 train 和 validation 使用同一个 prompt 版本；
- `split_prompt_and_target(row)`：从 `training_text` 的 `### SQL` 标记处分离 prompt 和 target，并确认嵌入的 SQL 与 `candidate_sql` 一致。

这些校验的作用是防止“训练文本和实际 target 不一致”“train/validation 混用”“切分后换了 prompt”这类静默错误。

### 6.2 `CausalSqlDataset`

`CausalSqlDataset.__init__()` 对每行执行：

1. tokenize prompt；
2. tokenize SQL target；
3. 拼接 `prompt_ids + target_ids + [eos_token_id]`；
4. 构造 `labels = [-100] * prompt_len + target_ids + [eos]`；
5. 超过 `max_seq_length` 时直接报错，不截断目标 SQL；
6. 记录样本数、最长序列和最长 target。

`__len__()` 和 `__getitem__()` 让它符合 PyTorch Dataset 接口。

### 6.3 `CausalSqlCollator`

一个 batch 中的序列长度可能不同。`CausalSqlCollator.__call__()` 将 `input_ids` 用 pad token 补齐，`attention_mask` 用 0 补齐，`labels` 用 -100 补齐，然后 `torch.stack()` 成 batch tensor。padding 位置不参与 loss，也不会被 attention 使用。

### 6.4 模型和 Trainer

`main()` 的核心顺序是：

1. 读取参数、检查 CUDA、输出目录和 split audit；
2. 固定 Python、NumPy、PyTorch 和 Transformers seed；
3. 加载 tokenizer；
4. 用 `AutoModelForCausalLM.from_pretrained()` 加载 bf16 或 4-bit 基座；
5. 关闭 `use_cache`，启用 gradient checkpointing；
6. 用 `prepare_model_for_kbit_training()`（仅 QLoRA）准备基座；
7. 用 `get_peft_model()` 注入 LoRA；
8. 用 `count_parameters()` 记录总参数和可训练参数；
9. 构造 `TrainingArguments` 和 `Trainer`；
10. 调用 `trainer.train()`；
11. 调用 `trainer.evaluate()`；
12. 保存 `adapter_final` 和 checkpoint；
13. 写 `sft_smoke.json`，记录 loss、step、显存、GPU UUID、数据哈希和参数。

关键训练参数含义：

| 参数 | 当前解释 |
| --- | --- |
| `per_device_train_batch_size=4` | 一次显卡前向/反向真正并行处理 4 个样本；动态 collator 将它们 stack 成一个 batch。 |
| `gradient_accumulation_steps=1` | 不再把 4 个样本拆成 4 次 micro-batch；每个 batch 完成后立即进行一次 optimizer update，effective batch size 为 4。 |
| `num_train_epochs=2` | 完整遍历正式物化的 8,574 条 train 两次；短 smoke 仍可显式使用 `--max-steps`。 |
| `learning_rate=1e-4` | LoRA 参数每次 optimizer update 的步长尺度。 |
| `bf16=True` | 训练计算使用 bfloat16，降低显存并保留较好数值范围。 |
| `optim=adamw_torch` | bf16 LoRA 默认使用未量化的普通 AdamW；QLoRA 历史模式才使用 `paged_adamw_8bit`。 |
| `weight_decay=0.01` | 显式启用 AdamW 解耦权重衰减；它不进入梯度历史 `m/v`。 |
| `gradient_checkpointing=True` | 用额外前向计算换显存。 |
| `max_seq_length=1536` | 序列上限；目标 SQL 不允许被静默截断。 |

一次 optimizer step 取决于“实际 batch × 梯度累积”。当前正式配置是 batch 为 4、梯度累积为 1，因此一次 forward/backward 处理 4 个样本后就产生一次参数更新；如果显存不足，可退回 batch 1、累积 4，但那会重新变成四次串行 micro-batch。正式 8,574 条、2 epoch 约为 4,288 个 optimizer steps。训练 loss 下降只说明目标 token 的平均负对数似然下降，不等于 SQL 正确率。

## 7. Adapter 重载和 artifact 验证

文件：`scripts/post_training/training/validate_post_training_adapter.py` 和 `scripts/post_training/training/verify_post_training_sft_artifacts.py`。

`validate_post_training_adapter.py` 的流程是：

1. `load_row()` 只取 validation split 的一个样本；
2. `encode()` 重建与训练相同的 prompt/target/labels；
3. 用相同的 QLoRA 或 bf16 基座加载；
4. `PeftModel.from_pretrained()` 挂载 `adapter_model.safetensors`；
5. 做一次前向，检查 loss 是 finite；
6. 写 adapter hash、validation 文件 hash、GPU 信息和 loss。

它证明“保存的 adapter 能被新进程正确重载”，不证明它在 dev 或业务上更好。

`verify_post_training_sft_artifacts.py` 只读取外部 artifact 的哈希和文件大小，验证：

- split audit 没有失败；
- train/validation JSONL 没被替换；
- `sft_smoke.json` 存在；
- adapter config/model/reload evidence 存在且 hash 匹配；
- 所有 artifact 在 Git 仓库外。

模型权重、checkpoint、训练 JSONL 和完整日志不能提交到 Git。

## 8. Base/Adapter 如何生成候选 SQL

文件：`scripts/post_training/inference/generate_post_training_text_to_sql.py`。

评测时必须让 Base 和 Adapter 只改变一个变量：是否加载 adapter。两者固定相同的模型 revision、prompt version、case 顺序、seed、greedy decode 和 token 上限。

关键函数：

- `require_dev_cases_without_gold()`：只保留 `db_id` 和 `question`，不读取 dev gold SQL；
- `table_mapping()`：将 schema metadata 按 `db_id` 建索引；
- `load_model()`：加载同一 Base，可选 `PeftModel.from_pretrained()`；
- `decode_sql()`：解码模型新生成的 token；
- `append_jsonl()`：每条候选生成后立即 flush/fsync，长任务中断时保留已完成前缀；
- `main()`：渲染 schema prompt、`model.generate()`、写候选和安全 evidence。

推理阶段模型看到的是 schema 和问题，不看到 dev gold SQL、数据库行或 gold 查询结果。`do_sample=False`、`num_beams=1` 是 greedy decode，使 Base/Adapter 对照更稳定。

Olist 迁移采用相同原则，但使用 `src/data_analysis_agent/candidate_sql_generator.py`：

- `CandidateSqlContext` 承载服务器生成的 question、Catalog、QueryPlan 和结果列；
- `require_database_route()` 在非查库问题上直接拒绝候选生成；
- `render_candidate_sql_prompt()` 只渲染服务器允许的上下文和 PostgreSQL SQL-only 合同；
- `unwrap_sql_completion()` 只移除一个外层 Markdown/`SQL:` 包装，不修复 SQL 内容。

这样可以把模型能力和服务器规则分开测量，避免修复器掩盖模型原始错误。

## 9. SQLite 诊断、Test Suite 和 Denotation 的区别

### 9.1 SQLite diagnostics

`scripts/post_training/evaluation/run_sqlite_benchmark.py` 调用 `src/data_analysis_agent/sqlite_benchmark.py`，对外部候选逐条执行：SQLite 只读连接、authorizer、AST policy、行数限制和超时。结果包括 executed、execution error、policy rejected 等。

“SQL 执行成功”只证明当前 SQLite 能解析并运行，不证明：

- 选择了正确的指标；
- Join 粒度正确；
- 时间过滤完整；
- 结果列顺序和别名满足合同；
- 当前数据快照之外仍然等价。

### 9.2 Official Test Suite

`scripts/post_training/evaluation/run_official_spider_test_suite.py` 负责准备固定 evaluator 输入，并调用未修改的官方 evaluator。当前项目只把它作为固定资产组合上的内部参考，不把内部结果写成官方 leaderboard 成绩。

### 9.3 Bounded denotation audit

`scripts/post_training/evaluation/run_spider_bounded_denotation_audit.py` 在所有候选生成完成后，才读取仓库外的 gold SQL，对 Base/Adapter 的查询结果做 exact ordered match 或 bag match。它用于检测候选和 gold 在当前 SQLite 快照上是否返回同一结果关系。

关键函数：

- `execution_state()`：对已执行候选和 gold SQL 进行结果关系比较；
- `paired_records()`：按固定 case 顺序比较 Base/Adapter；
- `main()`：只写 case ID 和状态迁移，禁止写问题、SQL、数据库标识和结果行。

Denotation 比执行成功更接近语义，但仍受当前数据快照影响，不能证明所有数据库实例上的逻辑等价。

## 10. 为什么要做多层评测

`scripts/post_training/evaluation/analyze_post_training_comparison.py` 和 `src/data_analysis_agent/post_training_comparison.py` 对 Base/Adapter 做脱敏聚合：

1. 生成是否成功；
2. SQLite policy 是否通过；
3. SQLite 是否执行；
4. Test Suite 固定 evaluator 输出；
5. denotation 是否匹配；
6. 哪些 case 从 Base 的成功变失败，哪些从失败变成功；
7. 生成 token 和输出形态如何变化。

同时观察多个证据是为了避免单指标误判。例如 Adapter 可能更早停止生成、SQL 形状更像 SQL，但 schema linking 反而退化；也可能某些 error 变成 executed，却仍然是错误 Join。最终结论必须基于预先写好的质量门，而不是挑一个好看的数字。

## 11. 本次 3,600 条 v2 的真实结果

官方 Spider dev 的 bf16 Base/Adapter 对照结果：

| 证据 | Base | Adapter |
| --- | ---: | ---: |
| SQLite executed | 950 | 961 |
| Fixed Test Suite internal `all` | 0.507 | 0.667 |
| Bounded denotation exact-or-bag | 570 | 708 |

这支持一个有限结论：在固定 Spider SQLite 协议下，v2 bf16 LoRA 通过了离线候选生成质量门。它不支持“生产 Text-to-SQL 已经准确”或“可以替换 Vanna”。Adapter 仍有 75 条从匹配退化为不匹配，说明还有回退。

Olist PostgreSQL 迁移的 12 条对照结果：

| 证据 | Base | Adapter |
| --- | ---: | ---: |
| 生成成功 | 12 | 12 |
| 通过 `SqlPolicy` | 6 | 6 |
| PostgreSQL 执行完成 | 4 | 2 |
| `ResultContract` 有效 | 2 | 0 |

这支持另一个结论：Spider/SQLite 离线提升没有迁移到当前中文 Olist/PostgreSQL/Catalog/QueryPlan 业务上下文。主要差异不是简单“数据少”，还包括 prompt 结构、语言、方言、指标别名、Join 路径和多指标结果合同。因此不能仅把 Spider 扩到更大规模后期待自动解决。

## 12. 当前项目做到哪一步

已经完成：

- 官方 Spider train-only 数据准入和数据版本冻结；
- 3,600 条候选构建，3,048/552 schema-disjoint 切分；
- Qwen 1.5B 的 QLoRA/bf16 LoRA forward 和 SFT 工程 smoke；
- 2 epoch bf16 LoRA 训练、Adapter 保存和 fresh reload；
- 1,034 条 Spider dev 的 Base/Adapter 生成、SQLite diagnostics、固定 Test Suite 和 denotation audit；
- 当前 Olist PostgreSQL 12 条受保护 holdout 的迁移评测；
- 生产安全链路没有被离线模型替换，SQL repair 在迁移评测中关闭。

尚未完成：

- 与当前 Olist Catalog/QueryPlan/ResultContract 对齐的领域训练集；
- 领域 Adapter 在更大、独立业务评测集上的正向证据；
- 将小模型以 shadow candidate generator 接入生产运行时；
- DPO、GRPO 或其他偏好/RL 后训练；
- 面向业务语义的全面人工标注。

## 13. 以后每次实验都用同一张检查表

在运行任何 GPU 任务前，先回答：

```text
假设：我认为改动什么，会改善哪个指标？
基线：和哪个完全相同的 Base 比？
数据：来源、许可证、版本、哈希是什么？
隔离：哪些 holdout、schema、模板和结果不能进入训练？
输入：模型实际看到哪些字段？是否包含 gold SQL 或数据库行？
目标：哪些 token 参与 loss？是否可能截断 target？
资源：逻辑 GPU、物理 GPU、UUID、显存预算是什么？
质量门：生成、执行、denotation、业务语义和安全分别怎么判？
停止条件：什么结果出现时必须停止，而不是继续堆实验？
结论边界：哪些只能叫工程证据，哪些才是质量证据？
```

实验完成后按顺序记录：配置和数据 hash、训练/reload 证据、Base/Adapter 成对生成、SQLite/denotation/Test Suite、错误迁移、人工语义抽检、结论和下一步。任何一步缺证据，都不能把“训练完成”写成“模型变好”。

## 14. 学习顺序和每节验收

后续学习不再一次性跑完整实验，而按以下顺序停下来审查：

1. **数据与 prompt**：你能解释一条 JSONL 训练样本的每个字段，以及为什么不能用 dev gold SQL。
2. **tokenizer 与 labels**：你能手算 prompt token 被 mask、SQL token 被监督的布局。
3. **LoRA/QLoRA**：你能解释冻结基座、低秩矩阵、4-bit NF4、bf16 compute 和显存权衡。
4. **Trainer 参数**：你能由 batch、accumulation、epoch 算出 optimizer steps，并解释 checkpoint。
5. **Base/Adapter 评测**：你能区分 loss、executed、denotation、Test Suite 和业务语义。
6. **Olist 迁移失败**：你能指出 Spider schema linking 与当前 Catalog/QueryPlan/PostgreSQL 合同的差异。
7. **领域数据设计**：在没有复用 holdout 的前提下，自己设计一条合法的 Olist 训练样本。

每一节先读代码和回答问题，再决定是否运行一个最小命令。没有你的确认，不启动新的训练、扩数据或评测任务。
