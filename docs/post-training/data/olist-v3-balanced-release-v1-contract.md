# Olist v3 Balanced Release v1｜结构物化合同

## 1. 目的与边界

本合同定义 Olist v3 SQL-only SFT 的**正式结构物化阶段**。目标是得到一个重新平衡、可复核的
`3,000 / 750 / 750` QuerySpec / Gold SQL release；它不是将 Release v2 的训练文件直接追加，也
不是在这一阶段生成中文问题、运行时 Prompt、tokenizer 输入或模型训练任务。

本阶段的输入仅包含：

- 仓库内冻结的 v3 family fixture：`olist_v3_coverage_family_seeds_v1.jsonl`；
- 仓库外 Release v2 的结构化 QuerySpec 资产，且只读取其结构、旧 split 标记与 hash；
- 本合同中定义的有限补充 QuerySpec 模板。

本阶段的输出只能写到仓库外 `/disk2/gengnan/data-analysis-agent-data/`。每条正式候选都重新固定为：

```text
workspace_id     = olist-demo-v3
catalog_version  = olist-catalog-v3
metric_version   = 0.3-proposal
dataset_version  = olist-kaggle-v2-2026-08-03
prompt_version   = olist-candidate-sql-v1
```

历史 v2 仅是**结构候选来源**：其 QuerySpec 必须以 v3 pin 重建、重验、重新渲染并重新计算
`query_spec_id`、`family_id` 与 Gold SQL hash。不得复制 v2 Gold SQL、Prompt、中文问题或 SFT 行。

## 2. 发布规模与行曝光配额

| 主类别 | train | validation | in-domain test | 总计 |
| --- | ---: | ---: | ---: | ---: |
| 单指标标量 | 110 | 25 | 25 | 160 |
| 多指标标量 | 600 | 150 | 150 | 900 |
| 单指标维度分组 | 122 | 33 | 33 | 188 |
| 多指标维度分组 | 478 | 117 | 117 | 712 |
| 单指标购买时间序列 | 400 | 100 | 100 | 600 |
| 单指标评价时间序列 | 72 | 19 | 19 | 110 |
| 多指标时间序列 | 750 | 188 | 187 | 1,125 |
| 结构难例 | 468 | 118 | 119 | 805 |
| **合计** | **3,000** | **750** | **750** | **4,500** |

这里的类别是“行的主要学习目标”，互斥。`structural_hard` 用于多指标中以 DISTINCT、状态过滤、
二层订单聚合、非负时长边界为主的程序；单指标查询仍按其结果形态归类，避免把稀缺的单指标能力
从自己的覆盖桶中移走。

初始设计中的单指标维度分组为 `134 / 33 / 33 = 200`。实际候选容量审计证明，在 19 个 metric 的
客户州/品类有限 family、每 family 至多 8 个日期实例、v3 seed split pin 和历史 final-test 不泄漏
同时成立时，最多可物化 `122 / 33 / 33 = 188` 条互不重复实例。缺少的 12 条不以中文改写或重复 SQL
填充，而是转给容量充足、同样有独立 family 覆盖的训练侧多指标维度分组（`466 -> 478`）。总行数和
所有其他 bucket 配额不变。这是一次由容量证据触发的最小调整，不是放宽隔离规则。

## 3. 候选来源与补充规则

300 个 v3 family 是正式 release 的强制覆盖骨架：每个 seed 至少物化一个 QuerySpec instance，且
必须保留原始 split、bucket、风险标签和 v3 workspace。它们最多只能产生约 `1229 / 316 / 267` 个
train / validation / test 日期实例，无法独立满足发布规模。

因此构建器按下列优先级合并候选：

1. **v3 frozen seed**：不可降级；若和历史来源发生同一 v3 family 冲突，优先保留它。
2. **v2 reconstructed structure**：历史 `in_domain_test` family 永远只允许进入新的 final test；历史
   `train` 与 `validation` 则组成一个非测试结构候选池，在本 release 内按新的 family 隔离规则重新
   分配到 train/validation。旧 release 本身不被改写，且历史 test 不会流入新 train/validation。
3. **有限补充模板**：由所有已冻结 v3 metric 生成单指标标量、客户州/品类分组、购买/评价时序，
以及合法的二指标标量组合；每个候选都先通过 v3 `validate_query_spec()`。

补充模板只填补达不到的 bucket 配额，不以中文改写、重复 SQL 或无意义日期轮换凑数。固定的八个
日期窗口为：

```text
[2016-10-01, 2017-01-01)
[2017-01-01, 2017-04-01)
[2017-04-01, 2017-07-01)
[2017-07-01, 2017-10-01)
[2017-10-01, 2018-01-01)
[2018-01-01, 2018-04-01)
[2018-04-01, 2018-07-01)
[2018-07-01, 2018-10-01)
```

`all_time` 只能生成一个实例；`absolute_range` 和 `series` 最多生成八个。日期端点不属于 family
身份，但不同端点必须形成不同 `query_spec_id` 和不同 canonical SQL hash；相同 hash 绝不重复进入
release。

## 4. 不变量与准入

- 每个 family、`query_spec_id`、canonical Gold SQL hash 只能属于一个正式 split；
- v2 历史 test family 不得流入 train 或 validation；历史 train/validation family 也不得被搬进新 test；
  历史 train/validation 之间允许在新的 v3 release 内重新分配，但同一 family 仍只能属于一个新 split；
- 同一实例不能重复计数，中文 surface overlay 也不参与本阶段行数；
- 全部候选经过 v3 QuerySpec validator 和 deterministic renderer，记录 renderer 版本及 SQL hash；
- 物化后才进入全量 `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator` Gold admission；
- full deterministic admission 通过前，不生成 SFT JSONL，不启动 GPU 训练；
- DeepSeek 只做分层抽样的 advisory 语义复核，不能放行 SQL、权限或结果合同。

## 5. 物化产物与后续闸门

结构物化目录将至少包含：

```text
query_specs/{train,validation,in_domain_test}.jsonl
gold_sql/{train,validation,in_domain_test}.jsonl
materialization_manifest.json
```

每条记录带有 `seed_id`、候选来源、`primary_bucket`、风险标签、v3 QuerySpec、family ID、Gold SQL
及其 SHA-256。manifest 记录源文件 hash、窗口合同、split/bucket/family/metric 覆盖、冲突排除和
跨 split 零交集证据。

后续依次为：全量 deterministic Gold admission → 受控中文问法 → runtime Prompt 重建 → tokenizer
长度审计 → train/validation/final-test SFT JSONL。每一步必须绑定上一步 manifest 的 hash。
