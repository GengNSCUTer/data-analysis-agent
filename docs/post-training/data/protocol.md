# 后训练数据协议

当前学习入口在上级目录的 `README.md` 中维护。

## 1. 目的与边界

本协议定义可信数据分析 Agent 后训练数据的来源、结构、隔离方式和评测口径。它适用于对路由、`QueryPlan`、SQL 生成、澄清和拒答候选进行 SFT、偏好优化或其他训练实验时的数据准备。当前已完成一轮外部 Spider train-only 的 **8-step QLoRA SFT 工程 smoke**；它只验证数据/设备/adapter 链路，不构成 SQL 准确率或业务质量结论。

后训练模型只能生成候选，不拥有数据访问或安全决策权。无论训练样本、模型版本或推理策略如何变化，以下服务器机制必须继续独立执行，且不得被训练结果替代或绕过：

- `sqlglot` AST Policy、对象白名单、单语句和查询预算；
- PostgreSQL reader role、Schema/表/列权限与数据库超时限制；
- `ResultValidator`、`ResultContract` 与可信结果收口；
- `ChartContract` 的图表类型、字段、结果工件和展示边界；
- Catalog 声明的归因治理，以及仅由服务器真实实现的归属/分摊规则。

因此，训练 loss、模型偏好分数、AST 放行或 SQL 可执行都不等于业务语义正确。只有候选 SQL 经完整运行时链路验证，并在需要时经过人工业务复核，才能被视为可用结果。

## 2. 可训练样本协议

每条候选训练样本必须有稳定 `sample_id`，并保存以下最小字段。字段可用结构化 JSONL、Parquet 或版本化标注表存储，但不得把敏感原文和原始业务数据直接写入仓库。

| 字段 | 含义 |
| --- | --- |
| `sample_id` | 不可复用的稳定样本标识。 |
| `source` / `license` / `timestamp` | 来源、许可、采集或标注时间。 |
| `workspace_id` | 样本所属工作区，避免跨数据集误用。 |
| `catalog_snapshot` | 版本化 Catalog 快照引用，包含数据集、指标、策略和版本号。 |
| `role_scope` | 可见 Schema、表/列范围和角色约束的脱敏引用。 |
| `question_redacted` | 最小必要、去标识化的问题文本；不保留完整用户原文。 |
| `working_memory` | 当轮允许使用的结构化上下文，不包含无关完整历史。 |
| `target_route` | 标注的 intent、`requires_database`、evidence mode 和澄清/拒答目标。 |
| `query_plan` | 服务器计划的指标、维度、时间、粒度/归因需求与目标结果列。 |
| `candidate_sql` | 候选 SQL；无 SQL 的帮助、定义、澄清或拒答样本留空并说明原因。 |
| `execution_outcome` | Gold 执行、AST/权限/结果合同状态及可比较的结果等价性摘要。 |
| `review` / `label_provenance` | 标注人或确定性生成器、复核时间和判定依据。 |
| `split` | `train`、`validation` 或 `holdout`，以及防泄漏族群标识。 |

用于偏好优化的样本还必须记录 `chosen`、`rejected` 的产生规则和两者对应的同一 Catalog/角色/策略版本，禁止把跨版本或跨权限范围的输出当作可直接比较的偏好对。

## 3. 数据来源与准入顺序

训练语料按以下可信度顺序扩充：

1. 服务器确定性生成、且不属于 holdout 的 goldens；
2. 人工标注并复核的在线失败或澄清案例；
3. 在固定 Catalog、角色、Policy 和数据库快照下执行验证过的查询变体；
4. 仅在确定性 gate 之后保留的合成负例，例如应拒绝的越权 SQL、应澄清的归因歧义；
5. 未经执行、合同或人工核验的模型输出不得直接进入训练集。

负例应覆盖错误路由、缺失澄清、错误指标口径、越权对象、错误结果列、归因缺口和图表合同违例。它们用于学习候选的偏好或拒答，不用于放宽运行时 Policy。

## 4. 隐私、数据和泄漏控制

以下内容不得进入训练集、验证集、提交记录或可共享报告：

- API Key、访问 token、cookie、用户 ID、IP、完整审计日志；
- 原始受限数据、数据库 dump、完整结果行和可以反推出个人/订单的数据；
- 未脱敏的完整用户问题、会话历史或内部业务上下文；
- 受保护的 SQL、提示词或第三方数据中许可证不允许再分发的内容；
- `evals/cases/text_to_sql_v2.yaml` 的 60 条 golden，以及它们的同义改写、近似题和派生答案。

划分时不能仅随机切分文本。必须至少按语义模板、SQL 形状、工作区/Catalog 快照和业务时间范围分组切分，避免同一问题的改写、相同 join 模板或同一快照中的近似样本同时出现在训练与 holdout 中。对于 Spider 这类多问题共用同一数据库 schema 的公开基准，`db_id` 是首要分组边界：同一 schema 的表、列、外键和命名风格不得同时进入 train 和 validation；SQL shape 交集只作为额外检查，不能替代 schema 隔离。

## 5. 固定 Holdout

`evals/manifests/post_training_holdout_v1.yaml` 是后训练隔离清单。它只记录 `text_to_sql_v2.yaml` 的 case ID 和版本，不复制 question、预期路由、计划或答案，以减少被误收集为训练语料的机会。

清单中的 60 条用例永久仅用于 holdout：不得用于 SFT、LoRA、DPO、GRPO、偏好对构造、提示词示例选择、合成改写或人工标注训练集。新增训练样本前必须执行 holdout 完整性测试；若修改 golden 或 Catalog 版本，需要创建新版本的评测套件和新的隔离清单，不能悄然改变既有 holdout 的含义。

## 6. 评测与发布门槛

每次训练实验至少分别报告下列指标，并标明数据集、Catalog、策略、模型、提示和运行配置版本：

| 维度 | 指标 |
| --- | --- |
| 结构输出 | JSON schema validity、route Macro-F1、澄清 precision/recall。 |
| 计划与 SQL | `QueryPlan` / `ResultContract` pass、AST Policy pass、SQL execution pass、结果等价性。 |
| 业务质量 | 指标口径正确率、人工业务语义正确率、证据是否足以支持回答。 |
| 安全性 | unsafe false-allow rate、越权/写操作/归因缺口的拒绝或澄清召回。 |
| 效率 | 模型轮数、修复次数、token、端到端延迟；未知 usage 必须记为 `unknown`。 |

发布前还应检查：训练样本的许可和脱敏记录完整；服务器合同测试全部通过；holdout 未泄漏；安全回归没有退化。模型在某一训练 loss 或单一 pass 指标上的提升，不能被描述为 Text-to-SQL 业务准确率提升，除非对应 holdout 与人工语义核验均有可复核证据。
