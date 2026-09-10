# TheLook v2 Base / Adapter matching 生成与评测合同 v1

**状态：** 已冻结实现与本地回归；尚未启动 v2 的 Base 或 Adapter GPU 生成。  
**评测集：** `thelook-cross-schema-final-test-v2`，600 条 protected cross-schema final test。  
**唯一目标：** 在同一未见 TheLook 电商 Schema、同一服务器语义上下文和同一解码条件下，对比冻结 Olist LoRA Adapter 与其 bf16 Base 的候选 SQL 生成质量。

这不是训练合同、模型接入合同或产品运行时变更。TheLook v2 的问题、QuerySpec、Gold SQL、原始候选、结果行与失败日志仍全部位于仓库外，不能进入 Olist train/validation/test、few-shot、Prompt 调优、Adapter 选择或错误驱动数据构造。

## 1. 最小任务卡

| 项目 | 冻结内容 |
| --- | --- |
| 目标 | 形成可复现、可比较且 Gold 后置的 v2 Base/Adapter 成对评测证据。 |
| 非目标 | 不重新训练、不改 Olist 默认 SQL 生成器、不根据 TheLook 输出改 Prompt/训练集、不宣称跨 Schema 泛化。 |
| 输入 | 外部 `cases.jsonl` / `manifest.json`、同一 Qwen base、冻结 Olist Release v2 adapter。 |
| 输出 | 两份外部 raw completion、两份安全生成报告、配对 marker、外部规范化候选及脱敏聚合评测报告。 |
| 关键不变量 | 600 条完全相同的 case / Prompt / decode；生成前不读取 Gold 或数据库行；Gold 仅在 pair marker 后访问；候选仍须通过 Policy、reader role、ResultValidator。 |
| 验收 | 合同字段、文件 SHA、case 顺序、600 条覆盖、Adapter 实际加载、Prompt hash、GPU UUID 均可回读；任一漂移 fail-closed。 |

## 2. 固定评测条件

| 项目 | 冻结值 |
| --- | --- |
| 评测集 / case 数 | `thelook-cross-schema-final-test-v2` / `600` |
| case 文件 SHA-256 | `e942367df04b7ed6e52db8455e4de681827f8741989c19d81cd88bd0479b0fa5` |
| Workspace | `thelook-cross-schema-eval-v2`，`thelook-catalog-v2`，指标版本 `0.2-frozen` |
| Candidate Prompt | `thelook-candidate-sql-v2`；完整有序 Prompt bundle SHA-256 为 `48bdc6763f8a7a5ead870d1ae9b25ba4dede6c99cf39fea0c5294cd4223ab123` |
| Prompt token 预检 | 真实 Qwen tokenizer：`600` 条，最小 `1948`、最大 `2075`；输入容量固定 `4096`，无截断、无样本到达上限 |
| Base | `Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b`，`bf16_lora` base mode |
| Adapter | `/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-domain-sft-release-v2-bf16-lora-2epoch-retry-v3-20260909/adapter_final`；实际模型/配置 SHA 在 Adapter 安全报告中回读 |
| Decode | greedy：`do_sample=false`、`num_beams=1`、`max_new_tokens=768`、seed `20260910` |
| 候选执行 | `SqlPolicy(sql-policy-v1)` → `SET LOCAL ROLE daa_thelook_reader` → `statement_timeout=5s` → `ResultValidator(max_rows=200)` |

输入上限 `4096` 是容量上限，并不是将真实 2075 token Prompt 填充或扩写到 4096；超过上限时生成器直接失败，不截断 Catalog、QuerySpec、问题或结果合同。

不同侧可在不同空闲 GPU 顺序或并行运行，但每一侧必须在启动前显式设置 `CUDA_VISIBLE_DEVICES`、物理 `nvidia-smi` 编号与 UUID guard，并在安全报告中保留实际设备证据。GPU 不同不是实验变量；可比较变量仅是“是否加载这个冻结 Adapter”。

## 3. 三阶段隔离

```text
阶段 A：Base / Adapter 各自生成（无 Gold、无 PostgreSQL）
  protected case 投影：question + QuerySpec + ResultContract
    -> 完整 v2 Catalog + 固定 SQL-only Prompt
    -> Qwen Base 或同一 Base + Olist Adapter，greedy completion
    -> 外部 raw-completions.jsonl + 不含 SQL/问题的 safe-report.json

阶段 B：配对验证（无 Gold、无 PostgreSQL）
  Base / Adapter safe report + raw completion hash
    -> 检查 600 条 case 顺序、Prompt bundle、decode、模型 revision、Adapter 状态
    -> matching-generation-marker.json

阶段 C：候选执行与 Gold denotation（唯一 Gold 读取点）
  marker 复核成功
    -> unwrap_sql_completion() 统一前缀/代码栏清洗
    -> SqlPolicy -> daa_thelook_reader -> ResultValidator
    -> 仅对 ResultContract valid 的候选重放 Gold 并比较列、行数、数值和顺序
    -> 外部 normalized candidates + 不含 SQL/问题/结果行的 evaluation-report.json
```

阶段 A 的 case 投影函数显式只读取 `case_id`、`question`、`query_spec` 与 `required_result_columns`。代码和回归测试都禁止它访问 `gold_sql` / `gold_sql_sha256`。阶段 C 若 marker 的版本、任一 hash、Base/Adapter 合同、600 条 case 顺序或 adapter 状态不匹配，会在读取 Gold 前拒绝。

## 4. 实现入口与外部目录

| 职责 | 仓库内入口 | 仓库外产物 |
| --- | --- | --- |
| 纯合同、Prompt、pair 校验 | `src/data_analysis_agent/thelook_v2_matching.py` | 无 |
| 单侧生成 | `scripts/post_training/evaluation/run_thelook_v2_matching_generation.py` | `.../base-generation/` 或 `.../adapter-generation/` |
| screen 启动器 | `scripts/post_training/launchers/start_thelook_v2_base_adapter_matching_screen.sh` | `.../logs/` |
| 两侧配对 marker | `scripts/post_training/evaluation/verify_thelook_v2_matching_generation.py` | `.../matching-generation-marker.json` |
| 执行与 Gold 对照 | `scripts/post_training/evaluation/evaluate_thelook_v2_matching_outputs.py` | `.../evaluation/` |

默认 run root 为：

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
  qwen25coder15b-thelook-v2-matching-v1-20260910/
```

所有完整 completion、规范化 SQL、问题、Gold SQL、结果行和日志留在这里；仓库仅可提交代码、合同、测试和后续聚合事实。`raw-completions.jsonl` 保存模型原始 completion；`unwrap_sql_completion()` 的规范化输出另外留在外部，保证格式清洗可审计，同时不会把模型文本带回 Git。

## 5. 结果解释与停止条件

- `generation_success` 只代表模型产出了非空 completion；不代表 SQL 合法。
- `policy_accepted`、`postgres_executed`、`result_contract_valid` 分别是 AST、只读数据库与服务器结果合同的不同门，不能相互替代。
- `ordered_denotation_match` 表示结果列、行数、值与顺序均匹配；顺序不同但多重集合相同才标为 `bag_denotation_match`。
- 无法通过 ResultContract 的候选标为 `not_result_contract_valid`，不进入 Gold 重放。这样不会让越权、缺列、截断或无意义结果凭偶然值“得分”。
- 测试资产通过、生成完成或 Adapter 胜过 Base 都只能说明这份冻结 TheLook snapshot / Prompt / decode 合同下的表现，不能自动说明开放式 Text-to-SQL 泛化、生产安全或默认运行时接入资格。

任何以下情况都停止，不启动阶段 C：v2 case/manifest hash 漂移、Prompt bundle 或 token 预检漂移、Base revision 或 bf16 模式漂移、Adapter 文件缺失/哈希不完整、生成缺少任一 case、raw completion hash 不一致、GPU UUID guard 不匹配，或评测输出目录已存在。

## 6. 本轮已完成的工程验证

- v2 Catalog、QuerySpec、renderer、600 条构造器及 matching 合同专项：`22 passed`；
- Ruff、Python compile 与 launcher `bash -n` 通过；
- 使用真实外部 600 条和本地 Qwen tokenizer 的只读预检确认完整 Prompt bundle SHA、`1948--2075` token 范围与 4096 容量上限一致；
- 本轮没有加载模型、没有占用 GPU、没有启动 Base/Adapter 生成、没有读取 Gold 做模型输入，也没有改变产品默认路径。
