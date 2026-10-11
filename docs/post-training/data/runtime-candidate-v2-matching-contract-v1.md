# Runtime candidate SQL v2 matching 评测合同

本合同用于输入对齐实验的 step 2,500 `adapter_best` 与原 Qwen2.5-Coder-1.5B Base。模型已由完整 validation 的最小 eval_loss 预选，不能根据 final test 改模板、条件解析、解码或权重。历史 QuerySpec 模板的结果保留原协议；本次是运行时问题派生协议的独立实验，不主张与旧成绩之差构成单变量因果证据。

## 输入与模型冻结

统一入口按 freeze → prepare → generate（Base/Adapter 各一次）→ evaluate 执行。正式合同绑定 best 冻结清单 SHA-256、Base manifest/文件、Adapter/config hash、原来源 hash、运行时/Catalog 资产及评测实现 hash。验证集 smoke 使用单独 scope/output，不与 protected final test 混用。

| 项目 | 冻结规则 |
| --- | --- |
| 模型 | 原 `Qwen/Qwen2.5-Coder-1.5B` revision `df3ce67c0e24480f20468b6ef2894622d69eb73b`，bf16、无量化 |
| Adapter | `adapter_best`，step 2,500，权重 hash `926d16e4218fb9f83dc99cb1ef79094dac90adc397e3c5e0ad9526c7eb834c9e` |
| Olist 来源 | 原 v3.1 `final_evaluation_only/in_domain_test.jsonl`，750 条；SHA-256 `67c1db3f9a3d7a74160306cd1680ea6f3c6be00035d2c95d860bb3d6e1266750` |
| TheLook 来源 | 原 v3 `cases.jsonl`，750 条；SHA-256 `bd1195c00dd68a884fc55eb8301184ef1492dd070f01fa26f96293ac13acef7c` |
| 模型输入 | 原问题经 CatalogRetriever/Router/WorkingMemory/QueryPlan/ResultContract 重建，再调用共同候选构造器；无 QuerySpec/Gold/父 Prompt/结果 |
| prefix | `runtime-candidate-sql-v2`，精确停在 `### SQL`，不额外补换行或 chat template |
| 解码 | greedy，`do_sample=False`、`num_beams=1`、`max_new_tokens=1024`、seed20261011、use_cache=True |
| 长度 | 输入上限4096，tokenizer 不截断；1024 输出预算高于训练最长目标778，不依据 test 调整 |
| 停止 | EOS 成功停止；达到输出预算且无 EOS 独立记 `length_stopped`，不作为完整候选继续执行 |
| 并行与重试 | 单条生成，模型 eval；不重采样、不自动修复，不续接部分生成文件重复补题 |

来源文件在 prepare 中按白名单投影 ID、workspace 与原问题。内嵌标签只存在于原文件，不能访问它们构造请求。结构元数据与 Prompt 都由实际问题推导；原问题不修改，不能使用训练物化器的 `repair_source_question()`。定义问题、含糊/不支持问题、超长请求全部保存状态，仍计入原总量。

## 生成与结果封存

Base/Adapter 消费同一份准备后的输入文件及其 hash。每条来源均写一条记录，包括路由拒绝、超长、空输出、正常输出或达到 token 上限；不存在“只保存成功 SQL”的分母缩减。raw completion（含前导换行）留在仓库外，完成后写记录数、ID、raw 文件 hash、合同/input hash 和模型身份，生成未完整结束不允许 Gold 阶段。

统一使用 `unwrap_sql_completion()` 去除已允许的展示外壳。保留 raw 与清洗后 SQL hash；不修复表列、公式、时间或 Join。CUDA/OOM、资产漂移、文件损坏等基础设施错误中止并保留未完成文件，不能把此类错误批量伪装为模型答错。输出目录禁止覆盖，原 protected 生成不提供 subset 参数。

## 治理与 Gold 结果对照

evaluate 先核对两个完整生成文件的合同、输入、身份、所有 raw hash、行数和模型角色，再打开原来源的 Gold 字段。候选先执行 QueryPlan Guard → SqlPolicy → 对应 workspace PostgreSQL reader role 的只读事务 → ResultValidator。固定执行预算1000、超时5000ms、预览200；通过 policy 上限多读一行判截断，截断结果不参与完整 denotation。没有修复或写业务数据库。

原 Gold 使用原 QuerySpec 的指标/时间/列合同验证，版本必须与冻结 workspace 一致；通过相同 AST/reader/ResultValidator 安全边界。Gold 顶层投影只规范为“维度、时间、按 metric_id 排序的指标”，保留父 hash；不改公式、关联、过滤或日期。其业务元数据只进入后置审计，不返回生成阶段。

ResultContract 允许的时间列别名仅在结果 DataFrame 上规范为 `time`，不修改候选 SQL；其余指标/维度别名及列序仍必须匹配合同。比较复用既有 `compare_denotations()`，绝对/相对数值容差均为 `1e-6`，先比 ordered，再比 bag。empty/empty 只有通过合同才可匹配；非法、缺列、NaN/Inf 或执行截断不计语义成功。

若原问题推导的指标、维度、粒度或时间端点与原 Spec 不同，在输出冻结后单独标注 `source_semantics_mismatch`，仍保留原分母与原 Gold。不能修问题、反向补计划或把这种差异直接归因为模型错误。Gold 在原标签合同下执行失败是评测阻塞，需要停止定位，不能换标签或当作模型失败。

## 输出指标与验收

逐 workspace 和合计报告总量、runtime 准入/超长、EOS/空/长度停止、Guard/Policy/执行/ResultContract 状态、ordered/bag Gold 匹配和成对变化。主分母固定各750；另给已准入与 source semantics 一致子集的诊断分母，但不能用子集成绩替代主成绩。safe report 只保留 ID、状态、数量与 hash，不含问题、SQL、错误原文或结果行；仓库外 raw 保留审计证据。

入口回归覆盖 Gold/Spec 不影响 Prompt、原问法不修改、两模型共用输入、错误 scope/hash/版本/权重拒绝、缺失/重复/篡改记录拒绝、未完成生成禁止读 Gold、拒绝/超长保留分母、EOS 与长度停止、只读/截断、别名/列序和数值容差。实际 GPU smoke 只使用 validation，正式来源在实现与合同冻结后才物化。完整 protected 评测启动另属下一小单元。

## 实现入口与已完成验证

[`run_runtime_candidate_matching.py`](../../../scripts/post_training/evaluation/run_runtime_candidate_matching.py) 提供四个子命令。`freeze` 写固定模型、数据、代码和解码合同；`prepare` 只用问题重建并封存输入；`generate` 在受 UUID 检查的空闲 GPU 上执行一次 Base 或 Adapter 生成；`evaluate` 先检查两个生成完成标识，再读取 Gold 和执行后置判分。CLI 不覆盖已有输出，不提供 final subset 或失败题重采样参数。

[`runtime_candidate_matching.py`](../../../src/data_analysis_agent/runtime_candidate_matching.py) 中，`project_question()` 明确只取 ID/问题，`prepare_question()` 调用真实请求回放，`load_generation()` 验证完整性与模型身份，`execute_sql()` 复用受控执行组件，`gold_context()` 在后置阶段构造原标签合同，`evaluate_pair()` 给出成对 denotation。只读执行使用两个 workspace 各自 reader role，审计写入仓库外 JSON；没有调用应用元数据 writer，也不等于完整在线工具调用端到端测试。

[`test_runtime_candidate_matching.py`](../../../tests/test_runtime_candidate_matching.py) 的 30 项定向测试通过；70 项相关回归通过。实际边界测试包括 1,000 行恰好完整与多读到第 1,001 行、未结束/篡改生成禁止 Gold、前缀无额外换行、标签无法影响生成输入。定向 Ruff、compileall 与 diff check 通过。

最终 validation smoke 位于 `/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-validation-smoke-20261011-r3/`：合同 SHA-256 `088ac1ebab23dba03702f3b10d63e8240f0d045c2495a27668a54ef73858074b`；Base/Adapter 输入 SHA-256 均为 `9748c1a632914f83bf5e67a3d2a7b5c62f39e97c9530096efb9e7b79eab8fe7a`。Base 使用逻辑2/物理0 RTX3090，Adapter 使用逻辑3/物理1 RTX3090，均记录 UUID。生成耗时约29/37秒，仅包含两条生成；不同 GPU 的耗时不作为模型速度对比。

| validation smoke | Base | step 2,500 Adapter |
| --- | --- | --- |
| Olist 一条 | 1,024 token 无 EOS，长度停止，不执行 | EOS、valid、ordered Gold 匹配 |
| TheLook 一条 | EOS；PostgreSQL 字段歧义，SQLSTATE 42702 | EOS、valid、ordered Gold 匹配 |

报告为 [safe-report.json](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-validation-smoke-20261011-r3/evaluation/safe-report.json)，保存生成/EOS、候选/Gold hash、reader 与合同状态，以及成对结果。两条问题来源是 validation，不是 protected final；这只证明真实模型、治理与判分可运行，不能推出整体质量提高。

首份 [r1 contract.json](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-final-20261011-r1/contract.json) SHA-256 为 `f68fdeda3aff58e492f50a9935b23b7e52cb12550c8f6b7193e6709ec1ba97a3`，冻结时只计算两个 final 来源的文件字节 hash。随后正式 prepare 因历史 Olist 存档没有内嵌 question 字段而停止，没有产生完整输入或任何模型输出；该合同已由下述 r2 存档格式兼容合同取代。保留 r1 和失败日志，不覆盖原文件。

可用以下命令查看唯一入口。GPU prepare/generate 使用现有 Py3.11 模型环境，CPU evaluate 使用 Py3.12 产品环境；环境名含 qlora 不代表此次启用了量化。

```bash
PYTHONPATH=src /disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python \
  -m scripts.post_training.evaluation.run_runtime_candidate_matching --help
```

## 正式运行准备：r2 存档格式兼容

历史 Olist SFT 行保存 `primary_variant_id` 和 `query_spec_id`，问题与 QuerySpec 分别存于独立 sidecar。已补格式适配：prepare 仅按主问法 ID/seed 从冻结 question-only surface 档案恢复原问题，不访问旧 rendered_prompt、training_text、SQL 或 QuerySpec；evaluate 仍在核验两侧生成完成后，才读取独立 Spec 并核对 seed/Spec ID。来源 SFT、问题 sidecar 和 Spec sidecar 的 hash 均入合同。该变更只修存档字段读取，不修改问题、解析器、模型、解码或 Gold 语义，也没有基于模型输出调参。

实际使用 [r2 contract.json](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-final-20261011-r2/contract.json)，SHA-256 `c639a1fd71368e7b2e10ce0af199052a72bb69cb36f6b3ec83ea6e0eea46408c`，其中记录 supersedes/reason。共享输入 [inputs.jsonl](/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-final-20261011-r2/inputs/inputs.jsonl) 已生成，SHA-256 `b96a1a268498b1c572071927aea9ae6c36840591e7cd3202deb2ecd80a870bb8`；共1,500条，模型输入长度1,180–2,884，无超长、无截断。

| workspace | 固定总量 | ready | 路由拒绝 |
| --- | ---: | ---: | ---: |
| Olist | 750 | 736 | 14 |
| TheLook | 750 | 717 | 33 |

拒绝为12条缺明确查询动作、2条指标定义问题、33条缺时间粒度。拒绝仍写生成状态记录并留在固定分母，本轮不修问题或解析规则。尚未读取这些正式来源的 Spec/Gold 字段来判分。

[`start_runtime_candidate_matching_screen.py`](../../../scripts/post_training/launchers/start_runtime_candidate_matching_screen.py) 为一次性正式运行启动器：Base 使用逻辑0/物理2 RTX4090，Adapter 使用逻辑1/物理3 RTX4090；启动前检查空闲和UUID。独占 launch 文件防止重复启动，两个 screen 各自保存生成日志/完成标识/退出状态。Adapter worker 成功后等待 Base 完成，核验两侧完整输出，再自动启动 evaluate；任一生成失败、worker 异常或等待超时均阻止 Gold。等待上限24小时，每10秒检查一次，不重采样或续接部分输出。status JSON 原子发布，避免等待进程读到半写入文件。启动器实际 argv 和自身 hash 写 launch.json。

启动器命令如下（只允许一次，不覆盖已有运行）：

```bash
PYTHONPATH=src /disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python \
  -m scripts.post_training.launchers.start_runtime_candidate_matching_screen start
```

兼容和启动编排定向测试共45项、相关回归70项通过；包含真实 CLI argv 解析、重复启动、GPU忙、生成失败阻断、先核验两侧再Gold、失联/超时和独立Spec绑定。没有重跑模型 smoke；此前真实GPU smoke的生成、执行和判分组件未修改，新增存档适配由合成边界测试及全量输入物化验证。
