# Olist 领域 SFT Release v2 数据合同

## 1. 计数口径

本 release 同时报告三种数量，避免把语言改写误报成新的语义能力：

| 名称 | 含义 | 是否计入 release 行数 |
| --- | --- | ---: |
| `family` | 指标、结果形态、维度、时间模式、Join/聚合程序组成的独立语义程序族。 | 单独报告，不直接等同于行数。 |
| `query_instance` | 一个已验证 QuerySpec 实例；同一 family 使用不同合法时间窗口时，QuerySpec 和 Gold SQL 不同，可作为不同查询实例。 | 是，一行。 |
| `surface_variant` | 同一 query instance 的中文表述（正式、口语、管理者、简洁、结果导向）。 | 否；五种问法是同一实例的语言池。 |

训练数据的每一行绑定一个 `query_instance` 和一个确定性选择的主表述；五种问法保存在同一实例的 overlay/审阅资产中，不重复计入语义样本。后续若要在多个 epoch 轮换问法，应在 Dataset 层按 `epoch + stable_hash(instance_id)` 选择，不复制 Gold SQL 行数。

## 2. 目标规模与旧资产合并

正式 v2 目标为实际查询实例：

```text
train：2,400
validation：600
in_domain_test：600
```

旧 Medium v1 的 `train=720` 和 `validation=240` family 纳入新 release，并重新生成五种中文表述。旧 `in_domain_test=240` 不进入本 release 的训练或验证，也不作为新 test 的组成部分；它作为历史 final holdout 独立保留，避免此前模型选择造成测试污染。

当前已冻结 QuerySpec 空间在排除 protected family 和旧 test 后，最多有 2,114 个可用 family（旧 train/validation 960 个 + 新 family 1,154 个）。因此 2,400/600/600 的目标是“查询实例”目标：训练 split 在同一 train family 内使用多个合法日期窗口形成不同 QuerySpec 实例；validation/test 使用彼此 family-disjoint 的实例。日期窗口变化产生不同 Gold SQL 和不同结果范围，但不能被描述为新的 Join 程序或新的指标能力。

建议分配如下：

| split | 目标实例数 | family 组成 | 规则 |
| --- | ---: | ---: | --- |
| train | 2,400 | 旧 train 720 + 新 train family 约 194 | 允许同一 train family 的多个合法时间窗口；不跨 split。 |
| validation | 600 | 旧 validation 240 + 新 family 360 | 每个 family 默认一个实例；只用于模型选择。 |
| in-domain test | 600 | 新 family 600 | 每个 family 一个实例；与 train/validation family 完全不交集。 |

剩余新 family 不为凑行数强行复制；可留作下一轮扩展或额外 holdout。每个 split 的实际
`family_count`、`query_spec_count`、`query_instance_count` 都必须写入 audit。

## 3. 语言变体规则

每个 query instance 生成五个中文 surface form，但只保留一个确定性的主表述进入静态 SFT 行；其余四个属于同一实例的 overlay。五个表述必须通过 Router、Catalog、QueryPlan、ResultContract 重建并保持：

- `metric_ids`、结果形态、维度、时间模式和粒度不变；
- QuerySpec identity、Gold SQL hash 和 workspace 快照不变；
- 不凭空加入比较、同比/环比、Top-N、因果或未冻结筛选；
- “比较、趋势、变化”在缺少 baseline 时走澄清合同，不进入普通 SQL SFT。

语言审计分别报告 unique question、normalized template、family 内变体数量；`5 x family` 不作为能力覆盖率。

## 4. 质量门

正式 release 必须按以下顺序执行：

```text
结构种子
 -> QuerySpec validator
 -> deterministic PostgreSQL Gold renderer
 -> SqlPolicy
 -> daa_analytics_reader
 -> ResultContract / ResultValidator
 -> 五种中文 overlay
 -> Router/Catalog/QueryPlan/Contract 重建
 -> 长度审计
 -> split/family/holdout 审计
 -> 分层人工或 SiliconFlow 辅助口径抽检
 -> 只选一条主表述物化 SFT train/validation/test
```

LLM 只做辅助语言和口径抽检，不能决定 Gold SQL、指标公式、split 或准入。所有原始问题、Prompt、SQL、数据库结果、模型输入输出留在仓库外；Git 只提交构造脚本、合同和聚合报告。

## 5. 退出条件

在完整 Gold 准入、运行时 Prompt 重建和长度审计通过之前，不启动新的 GPU 训练。完成后先运行 matching Base/Adapter 对照，再决定是否把新 Adapter 接入产品运行时；离线 loss 或单一 split 通过不能替代 Olist 结果合同和 TheLook 跨 schema 回归。
