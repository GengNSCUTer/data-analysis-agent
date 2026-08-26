# Spider SFT v2 全量评测分析

## 结论

规模化 Spider SFT v2 的 bf16 LoRA Adapter 已在冻结合同下完成 1,034 条官方 Spider 1.0 dev 的离线 Base/Adapter 成对评测。它通过了本轮“离线候选 SQL 生成质量”的质量门：相对同精度 Base，受限 SQLite 可执行候选从 950 增至 961，固定版本 Test Suite 的内部 `all` 输出从 0.507 增至 0.667，生成冻结后运行的全量 bounded denotation match 从 570 增至 708。

这不是公开 Spider 排行榜成绩，也不是生产数据分析准确率。Adapter 仍有 75 条从 denotation match 变为非匹配的回退，且存在多表连接、去重/分组、集合/重复度和结果投影形状错误。因此本轮决策是：**允许把该 Adapter 作为下一阶段受控接入设计的离线候选，不直接改写或替换 Vanna/PostgreSQL 运行时链路。**

## 冻结合同与证据边界

| 项目 | 固定值 |
| --- | --- |
| 模型 | `Qwen/Qwen2.5-Coder-1.5B`，revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| 对照变量 | 不加载 Adapter 的 bf16 Base 与加载 74 MB bf16 LoRA Adapter |
| Prompt | `spider-sft-schema-question-sql-v2` |
| 生成 | greedy，seed `42`，`max_input_tokens=1536`，`max_new_tokens=256` |
| 开发集 | 官方 Spider 1.0 release，1,034 条；`dev.json` SHA-256 `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| Schema | `tables.json` SHA-256 `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed` |
| 模型可见输入 | 仅 `db_id`、问题和对应 SQLite schema；生成时不读 dev gold SQL 或数据库行 |
| 运行设备 | Base logical CUDA `0` -> physical GPU `2`（RTX 4090）；Adapter logical CUDA `1` -> physical GPU `3`（RTX 4090） |

原始问题、gold SQL、候选 SQL、数据库标识、数据库行、预测、权重、checkpoint 和完整日志均留在仓库外。仓库中的清单和本文只记录可复核的配置、聚合数字、哈希与有限 source case ID。

SQLite diagnostics 是候选在只读 SQLite policy 下能否执行的本地诊断，不等于 SQL 正确率。Test Suite 数字来自冻结 evaluator commit 和当前固定本地资产组合的内部输出，不是当前官方排行榜主张。denotation audit 在所有候选生成结束后才本地只读执行 gold SQL；它不保存问题、SQL、结果行或数据库标识，且不能证明在所有可能数据实例下的等价性。

## 三层结果

| 证据层 | 指标 | Base | Adapter | 变化 |
| --- | --- | ---: | ---: | ---: |
| SQLite diagnostics | executed | 950 | 961 | +11 |
| SQLite diagnostics | execution error | 81 | 73 | -8 |
| SQLite diagnostics | policy rejected | 3 | 0 | -3 |
| Fixed Test Suite internal output | easy | 0.750 | 0.887 | +0.137 |
| Fixed Test Suite internal output | medium | 0.556 | 0.706 | +0.150 |
| Fixed Test Suite internal output | hard | 0.293 | 0.557 | +0.264 |
| Fixed Test Suite internal output | extra | 0.235 | 0.349 | +0.114 |
| Fixed Test Suite internal output | all | 0.507 | 0.667 | +0.160 |
| Post-generation denotation audit | exact ordered match | 541 | 694 | +153 |
| Post-generation denotation audit | bag match but order differs | 29 | 14 | -15 |
| Post-generation denotation audit | exact-or-bag match | 570 | 708 | +138 |
| Post-generation denotation audit | mismatch | 380 | 253 | -127 |
| Post-generation denotation audit | not executable | 84 | 73 | -11 |

Test Suite bridge 固定为 `taoyds/test-suite-sql-eval` commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`，两次均完整评测 1,034 条并正常返回。四个难度桶同方向提升，和全量 denotation 的净增形成相互印证；但两者都不能替代未来针对项目真实 PostgreSQL workspace 的独立评测。

## 成对诊断

SQLite 状态迁移为：`executed -> executed` 898 条、`executed -> execution_error` 52 条、`execution_error -> executed` 61 条、`execution_error -> execution_error` 20 条、`policy_rejected -> executed` 2 条、`policy_rejected -> execution_error` 1 条。执行层面净增 11 条，分布在 20 个开发数据库中：7 个改善、6 个回退、7 个不变，范围为 -5 到 +10。这说明总体可执行性正向，但不是每个 schema 都一致改善。

全量 denotation 的配对迁移表明，495 条在两侧都匹配；213 条从非匹配变为匹配；75 条从匹配变为非匹配，净增 138 条。该净增与 Test Suite 的提升方向一致，但 75 条回退仍是后续设计必须保留的风险样本。

错误类别也发生了结构性变化。Adapter 的 qualified `no_such_column` 从 57 降至 33，说明部分表别名/限定列 grounding 有改善；总 `no_such_column` 仅从 70 降至 66，因为 unqualified/other 形式从 13 增至 33。Base 的 3 条 policy parse failure 在 Adapter 中消失，但 Adapter 新增 2 条 function arity 错误。结论不是“schema linking 已解决”，而是错误从部分限定引用失败转移到表/列选择、连接、分组和函数形状等问题。

## 生成行为

| 指标 | Base | Adapter | 解读 |
| --- | ---: | ---: | --- |
| 总生成 token | 114,159 | 31,027 | Adapter 更快停止并较少继续生成展示文本。 |
| 单例平均生成 token | 110.41 | 30.01 | 不是生产吞吐或延迟基准。 |
| 触达 256 token 上限 | 264 | 1 | 说明 SQL 结束格式明显稳定。 |
| 直接 SQL 形态完成 | 370 | 1,034 | Adapter 不再产生 section continuation。 |
| 累计生成墙钟时间 | 2,852,330 ms | 1,524,347 ms | 两次独立运行，仅作运行记录，不报告为生产 latency/throughput。 |

这解释了 SQLite policy rejection 的下降，但不构成语义正确的充分条件。真正的质量判断仍来自全量 denotation、固定 Test Suite 和回退案例审核。

## Changed-case 审核

在 213 个“非匹配 -> 匹配”和 75 个“匹配 -> 非匹配”候选中，分别按 source order 取六个确定性分位点进行人工审核。审核只在仓库外进行；本文不写入问题、SQL、schema 或数据库行。

改善案例反复表现为：删除不必要的输出列，恢复标量聚合结果形状，正确使用 `ORDER BY ... LIMIT 1` 进行选择，避免多余 join，以及恢复预期的输出列顺序和 count/aggregate 选择。

回退案例反复表现为：不必要 join 造成无效引用或行数膨胀，表/列语义混淆，遗漏或误用 `DISTINCT`、`GROUP BY`、`HAVING`，投影顺序颠倒，以及集合/重复度语义不一致。这些属于通用 Text-to-SQL 结构能力问题，不应通过对某个 Spider schema 的硬编码修补。

审核还确认了 denotation 的固有限制：某些 SQL 与 gold 在当前数据库快照上结果相等，但使用了不同的比较符、逻辑连接或等价写法；它们未必在所有未来数据库实例中等价。因此本报告同时保留固定 Test Suite 输出作为独立佐证，且不把 denotation match 写成官方执行准确率。

## 审计健壮性修复

首次运行全量 denotation audit 时，有一个 Spider SQLite 文件含非 UTF-8 TEXT 值，Python 默认文本解码会使只读结果比较中断。这是审计器的数据解码健壮性缺口，而非候选 SQL 或模型输出错误。

审计器现在在只读连接上设置 `connection.text_factory = bytes`，按原始字节比较 TEXT 值。修复不改变 SQL、数据库数据、候选输出或报告载荷；报告继续不写原始文本、问题、数据库标识或结果行。新增回归测试向 SQLite 插入非法 UTF-8 TEXT，确认 Base/Adapter 比较可以完成且报告不泄漏原始值。

## 证据定位

所有路径均在仓库外，只用于复核：

| 工件 | SHA-256 |
| --- | --- |
| Base generation evidence | `c0bb9f596bbc87bf0f6ab3ebc18016053fb31540985b33de4998240529325451` |
| Adapter generation evidence | `704968b33355bcccbd2f1b9eaeb59666d88539209cea085e8c5171ff4f11d62f` |
| Base Test Suite evidence | `b43e202714b64c91da0f59d20a56cddf13876ce686b67c56eed25e9bcbe2c3f8` |
| Adapter Test Suite evidence | `c8c1312bdcc81a92076479afa0e1a85f3e613db3473841610518548cb57eccdf` |
| Paired SQLite analysis | `943c1a8c9ac1f32d56d04df7d1265da3675e6a8a3e4c50f661fb00465c208c81` |
| Full denotation audit | `c6226d3792e723e703303361e265f460536466fa5465ffb6100c06e760105c6e` |

完整合同、外部目录和决策状态见 [`post_training_spider_sft_v2_full_evaluation_v1.yaml`](../evals/manifests/post_training_spider_sft_v2_full_evaluation_v1.yaml)。

## 决策与下一步

本轮只完成 R3 的 SFT 质量评估：规模化 v2 bf16 LoRA 已有充分的离线候选质量证据，值得进入“受控 runtime candidate generator”的设计与独立业务评测阶段；R4 的 DPO/GRPO 仍未开始。

下一步不是直接把 Adapter 替换线上模型，而是设计一个可开关、可回退的 `CandidateSqlGenerator` 抽象，并用独立于 Spider 的项目工作区评测资产验证：候选 SQL 仍必须经过 `QuestionRouter`、Semantic Catalog/QueryPlan、`sqlglot` AST Policy、PostgreSQL reader role、`ResultValidator`、`ResultContract` 和 `ChartContract`。只有这些运行时边界与独立业务语义评测都通过，才讨论小流量受控接入；当前不进入 DPO/GRPO、多候选投票或执行反馈 RL。
