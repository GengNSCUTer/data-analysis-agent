# 后训练代码审阅指南 v1

**状态：** 代码阅读阶段；未创建领域数据、未启动 GPU 训练、未运行完整评测或接入生产运行时。

本指南的目标不是再讲一遍 LoRA 原理，而是让你亲自打开代码、核对数据是否真的按文档所说流动，并对每一个结论追问“是哪个函数保证的、哪份测试在验证、仍有什么不能保证”。

## 1. 先固定审阅边界

后训练有两条相连但不能混淆的代码链：

```text
Spider 离线研究
train JSON + tables + SQLite -> SFT rows -> split -> LoRA training
-> Base/Adapter free generation -> SQLite/Test Suite/denotation -> safe comparison

Olist 业务迁移评测
protected case -> Router/Catalog/QueryPlan/ResultContract -> candidate SQL model
-> SqlPolicy -> PostgreSQL reader role -> ResultValidator -> redacted report
```

第一条说明模型在公开 Spider/SQLite 上能否提出更好候选；第二条检查这一能力能否迁移到当前中文 PostgreSQL 工作区。两条链都不替代生产安全执行链。

审阅时只看规范实现位置：`scripts/post_training/`。根目录 `scripts/` 中的同名 Python 与 shell 文件是兼容包装，不应作为业务逻辑阅读对象。原始数据、训练 JSONL、模型、Adapter、预测 SQL、结果行和完整实验日志都在仓库外，因此也不应尝试从 Git 中寻找它们。

## 2. 审阅方式

每次只审阅一个单元，按下面的顺序操作：

1. 在 IDE 打开“主文件”，先读模块 docstring、命令行参数、公开类/函数和 `main()`；
2. 再打开“配套测试”，确认正常路径、错误路径和不变量是否都被断言；
3. 你用自己的话回答“输入是什么、输出是什么、最重要的失败保护是什么、仍不能保证什么”；
4. 我再以代码审查方式指出遗漏、潜在风险、测试空白或可重构点，并给出精确文件/行号；
5. 未共同完成当前单元的代码阅读前，不进入下一单元，不构造数据，不调整训练参数，不启动 GPU。

测试通过只能证明测试覆盖到的行为；它不能替代阅读，也不能自动证明业务语义、数据隔离或模型泛化成立。

## 3. 后训练文件总览

| 层 | 规范文件 | 作用 | 对应主要测试 |
| --- | --- | --- | --- |
| Prompt 格式 | `src/data_analysis_agent/spider_sft_format.py` | Spider schema、question、SQL target 的版本化序列化。 | `tests/test_spider_sft_format.py` |
| 候选构建 | `scripts/post_training/data/build_spider_sft_candidates.py` | 只读 Spider train，EXPLAIN、token budget、覆盖选择，写仓库外候选。 | `tests/test_post_training_candidate_builder.py` |
| 数据切分 | `scripts/post_training/data/split_post_training_candidates.py` | 按 `db_id` 将 Spider 训练数据 schema-disjoint 切为 train/validation。 | `tests/test_post_training_splitter.py` |
| 前向/训练 | `scripts/post_training/training/run_post_training_forward_smoke.py`、`run_post_training_sft_smoke.py` | 验证 labels/LoRA 工程，随后执行受控 SFT。 | `tests/test_post_training_sft_smoke.py` |
| Adapter 工件 | `validate_post_training_adapter.py`、`verify_post_training_sft_artifacts.py` | fresh reload 与外部工件 hash/边界核验。 | `tests/test_post_training_sft_artifact_verifier.py` |
| 自由生成 | `scripts/post_training/inference/generate_post_training_text_to_sql.py` | Base 或 Adapter 在不读取 dev gold SQL 的前提下生成候选。 | `tests/test_post_training_generation.py` |
| SQLite 诊断 | `src/data_analysis_agent/sqlite_benchmark.py`、`scripts/post_training/evaluation/run_sqlite_benchmark.py` | 只读 SQLite policy、超时、执行状态；不冒充官方 EX。 | `tests/test_sqlite_benchmark*.py` |
| 官方 Test Suite | `src/data_analysis_agent/spider_test_suite.py`、`run_official_spider_test_suite.py` | 准备外部 evaluator 输入、钉住 evaluator commit、保存原始输出证据。 | `tests/test_spider_test_suite.py` |
| 结果等价性 | `run_spider_bounded_denotation_audit.py`、`post_training_comparison.py` | 生成冻结后才比较结果关系，并对 Base/Adapter 做脱敏状态迁移分析。 | `tests/test_spider_bounded_denotation_audit.py`、`tests/test_post_training_comparison.py` |
| Olist 迁移 | `candidate_sql_generator.py`、`olist_candidate_sql_evaluation.py`、`run_olist_candidate_sql_evaluation.py` | 用当前可信运行时构造业务候选输入、执行并输出安全聚合报告。 | `tests/test_olist_candidate_sql_evaluation.py` |
| 启动器 | `scripts/post_training/launchers/*.sh` | 经用户确认后创建 `screen` 任务；不包含新的训练逻辑。 | shell syntax、CLI smoke |

## 4. 单元 A：Prompt 和 Spider 训练数据

**先打开：**

1. `src/data_analysis_agent/spider_sft_format.py`
2. `tests/test_spider_sft_format.py`
3. `scripts/post_training/data/build_spider_sft_candidates.py`
4. `tests/test_post_training_candidate_builder.py`
5. `scripts/post_training/data/split_post_training_candidates.py`
6. `tests/test_post_training_splitter.py`

### 你要在代码中找到的关键点

- `serialize_spider_schema()` 与 `serialize_spider_schema_v2()`：为什么历史 v1 不能被悄悄修改，v2 又怎样补足 `table.column`、列类型、主键和完整外键；
- `render_sft_prompt()` 与 `render_sft_training_text()`：训练与推理共享同一个 `### SQL` 截止位置；
- `assert_train_only()`、`load_holdout_ids()`：它们是第一层输入防错，不是“仅因文件名含 train 就绝对安全”的完整数据来源证明；
- `read_only_explain()`：只检查 SQLite 解析和标识符解析，不执行目标 SQL、更不证明查询语义正确；
- `schema_stratified_round_robin()`：它如何避免大 schema 主导候选集；
- `choose_validation_groups()`：为什么 Spider 必须将相同 `db_id` 的样本整体放入同一个 split。

### 本单元的审阅问题

1. v1 和 v2 prompt 为什么要并存，而不能让 v2 覆盖 v1？
2. `EXPLAIN QUERY PLAN` 通过时，哪些 SQL 业务问题仍然可能存在？
3. `assert_train_only()` 有什么用、又为什么不能单独构成训练数据来源审计？
4. 如果同一 `db_id` 同时出现在 train 和 validation，模型到底提前看到了什么？

**结束标准：** 你能画出 `train JSON/tables JSON -> candidate JSONL -> train/validation JSONL` 的字段流向，并指出 gold SQL 在 Spider 的 train 侧为何允许、dev/test 侧为何禁止。

## 5. 单元 B：token、labels、LoRA 和实际训练

**先打开：**

1. `scripts/post_training/training/run_post_training_forward_smoke.py`
2. `scripts/post_training/training/run_post_training_sft_smoke.py`
3. `tests/test_post_training_sft_smoke.py`
4. `scripts/post_training/training/validate_post_training_adapter.py`
5. `scripts/post_training/training/verify_post_training_sft_artifacts.py`

### 重点函数和类

- `split_prompt_and_target()`：从完整训练文本重新得到 prompt 与 SQL，检查嵌入 target 和 `candidate_sql` 是否一致；
- `CausalSqlDataset`：拼接 `prompt_ids + SQL ids + EOS`，将 prompt 与 padding 位置置为 `-100`；
- `CausalSqlCollator`：对不同长度样本补齐，同时保证补齐 token 不参与 loss；
- `main()`：冻结 Base、按 `bf16_lora` 或 `qlora_4bit` 加载、`get_peft_model()`、`Trainer.train()`、evaluate、保存 Adapter 与记录 manifest；
- `validate_post_training_adapter.encode()`：新进程重建同样 labels，验证 Adapter 可 fresh reload。

### 审阅问题

1. 把 SQL marker 或 EOS 放错一个位置，会怎样影响 loss 与自由生成？
2. `forward smoke` 调用了哪些操作，又刻意没有调用 `backward()` / `optimizer.step()`？
3. `CausalSqlDataset` 为什么对超过长度限制的样本报错，而不是悄悄截断？
4. adapter reload 得到有限 loss 为什么仍不足以证明 SQL 候选质量提高？

**结束标准：** 你能用一个假想样本写出 `input_ids` 与 `labels` 的布局，并解释 `per_device_train_batch_size`、`gradient_accumulation_steps` 与 optimizer step 的关系。

## 6. 单元 C：冻结输入后的 Base/Adapter 自由生成

**先打开：**

1. `scripts/post_training/inference/generate_post_training_text_to_sql.py`
2. `src/data_analysis_agent/frozen_sqlite_baseline.py`
3. `src/data_analysis_agent/external_artifacts.py`
4. `tests/test_post_training_generation.py`
5. `tests/test_frozen_sqlite_baseline.py`

### 重点

- `require_dev_cases_without_gold()` 应只保留 dev 的 case ID、问题和 schema 定位信息，不将 gold SQL 送进模型输入；
- `load_model()` 必须让 Base 和 Adapter 使用相同模型 revision、同一 tokenizer、相同 prompt、seed、greedy decode 和 token 上限，唯一变量是是否挂载 Adapter；
- `append_jsonl()` 每条候选后 `flush/fsync`，让中断后的任务可恢复；
- `ensure_path_outside_repository()` 强制模型、预测和原始输出不写入 Git 工作区。

**结束标准：** 你能解释 Base/Adapter 对照为什么不能在生成阶段读取 dev gold SQL，也能指出“生成成功”与“SQL 可执行/语义正确”之间还缺少哪些阶段。

## 7. 单元 D：三层离线评测

**先打开：**

1. `src/data_analysis_agent/sqlite_benchmark.py`
2. `scripts/post_training/evaluation/run_sqlite_benchmark.py`
3. `src/data_analysis_agent/spider_test_suite.py`
4. `scripts/post_training/evaluation/run_official_spider_test_suite.py`
5. `scripts/post_training/evaluation/run_spider_bounded_denotation_audit.py`
6. `src/data_analysis_agent/post_training_comparison.py`

| 层 | 重点实现 | 证明什么 | 明确不证明什么 |
| --- | --- | --- | --- |
| SQLite diagnostics | `SqliteBenchmarkPolicy`、`ReadOnlySqliteExecutor.execute()` | 候选是否在受限 SQLite 下被策略允许、执行、超时或报错。 | SQL 是否符合 gold 语义。 |
| Official Test Suite bridge | `prepare_complete_spider_test_suite_inputs()`、`verify_unmodified_official_evaluator()` | 输入完整、官方 evaluator 固定、原始输出可追溯。 | 当前项目内部数字是官方排行榜成绩。 |
| Bounded denotation | `execution_state()`、`paired_records()` | 生成冻结后，候选与 gold 在当前 SQLite 快照的结果关系。 | 所有可能数据库实例上的逻辑等价。 |
| Paired analysis | `analyze_paired_sqlite_diagnostics()` | Base 到 Adapter 的状态迁移与错误类型变化。 | 单条错误已经被彻底解释。 |

**结束标准：** 你能说出一条 Adapter SQL “SQLite executed”但 denotation 不匹配的例子，并解释为什么必须同时看 Base/Adapter 的状态迁移，而不是只看一个总体比例。

## 8. 单元 E：Olist 业务迁移和生产边界

**先打开：**

1. `src/data_analysis_agent/candidate_sql_generator.py`
2. `src/data_analysis_agent/olist_candidate_sql_evaluation.py`
3. `scripts/post_training/evaluation/run_olist_candidate_sql_evaluation.py`
4. `evals/manifests/post_training_olist_business_adapter_evaluation_v1.yaml`
5. `tests/test_olist_candidate_sql_evaluation.py`

`run_olist_candidate_sql_evaluation.py` 是目前最值得仔细审阅的后训练脚本：它大约五百行，同时承担 manifest 读取、模型加载、服务器上下文重建、候选生成、受控执行和脱敏报告编排。它不是生产入口，但故意复用了生产中的 `QuestionRouter`、`CatalogRetriever`、`QueryPlan`、`ResultContract`、`SqlPolicy`、PostgreSQL reader role 与 `ResultValidator`，从而避免用一个脱离真实合同的“玩具评测”得出迁移结论。

### 重点函数

- `prepare_case_context()`：只有 `require_database_route()` 通过后才构造 `CandidateSqlContext`；模型获得的是服务器已确定的 Catalog、QueryPlan、required columns，而不是数据库连接；
- `generate_completion()`：固定 greedy decode；
- `unwrap_sql_completion()`：只移除一个 Markdown / `SQL:` 外壳，绝不修复 SQL 内容；
- `execute_candidate()`：依次记录 Policy、PostgreSQL 和 ResultContract 的状态；
- `build_safe_report()` / `build_safe_comparison()`：拒绝问题、SQL、结果行、连接串或密钥进入 Git 可共享报告。

### 审阅问题

1. 为什么 `require_database_route()` 必须发生在模型生成之前？
2. 为什么 `unwrap_sql_completion()` 故意保留错误 SQL，而不尝试补全/修复？
3. 这份评测为什么关闭 SQL repair？如果开启，会混淆哪一个变量？
4. `ResultContract valid` 比 PostgreSQL executed 多检查了什么，又为什么仍不等于完整业务正确？

**结束标准：** 你能按代码顺序讲出一条 protected Olist case 如何从 question 走到安全报告，并能指出模型在哪些位置被服务器规则限制。

## 9. 单元 F：启动器和历史兼容入口

最后才读 `scripts/post_training/launchers/*.sh`、根目录 `scripts/start_*` 包装器和 `scripts/_post_training_compat.py`。它们的职责是设置环境、记录逻辑/物理 GPU 映射、启动 `screen` 和转发历史命令，不应出现新的训练、评测或 SQL 业务逻辑。若发现启动器与规范 Python CLI 的参数不一致，应先修复这一一致性问题，再批准任何长任务。

## 10. 当前可记录的结构性观察

- 后训练规范实现已按 data/training/inference/evaluation/launchers 分层，当前没有必要再挪动；
- `src/data_analysis_agent/` 平铺模块和 `tests/` 平铺测试仍可优化，但这必须建立在“公开接口和依赖图已审阅”的基础上；现在不应为视觉整齐破坏可追溯性；
- `run_olist_candidate_sql_evaluation.py` 的编排职责较多。审阅完成后，可考虑将模型加载、上下文构建和执行循环抽到可单测库模块；在未建立针对真实边界的回归前，不应机械拆文件；
- `build_spider_sft_candidates.assert_train_only()` 是文件路径防呆层，不是可信 provenance 的唯一保证。真正的保障还来自输入 manifest/hash、holdout 测试、外部资产边界和人工审阅；
- 当前测试覆盖了许多正常/拒绝路径，但没有一项测试能替代你对 prompt、labels、数据切分和评测结论的逐行审查。

下一次从“单元 A：Prompt 和 Spider 训练数据”开始。先在 IDE 中打开 `spider_sft_format.py` 和 `tests/test_spider_sft_format.py`，不要跳去读 Trainer；Trainer 是否学对，首先取决于输入格式和数据边界是否正确。
