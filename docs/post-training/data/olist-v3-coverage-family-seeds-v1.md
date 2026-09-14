# Olist v3 Coverage Family Seed 清单 v1

## 1. 状态与目的

**状态：** 已冻结为仓库内小型 fixture；尚未物化为正式 QuerySpec instance、Gold SQL、中文问题、
运行时 Prompt 或 SFT JSONL。

**文件：** [`data/fixtures/olist_v3_coverage_family_seeds_v1.jsonl`](../../../data/fixtures/olist_v3_coverage_family_seeds_v1.jsonl)

**构建器：** [`scripts/post_training/data/build_olist_v3_coverage_family_seeds.py`](../../../scripts/post_training/data/build_olist_v3_coverage_family_seeds.py)

**固定 SHA-256：** `79beeabb07f07d1b618116887e1e251b49b610d93f4fa683f6a6a56a4245e925`。

Family seed 是一张结构化的“能力施工卡”。它只声明一种独立 SQL 能力应该具备的指标、结果形态、
维度、时间模式、Join 程序、风险与 split，不承载实际语言和实际 SQL。因此它不能直接送入 Trainer，
也不能用于宣称模型已经具备对应能力。

```text
Family seed
  -> 同 split 内派生合法日期窗口的 QuerySpec instance
  -> deterministic Gold SQL renderer
  -> SqlPolicy -> PostgreSQL reader role -> ResultContract/ResultValidator
  -> 中文 surface 与 runtime Prompt 重建
  -> 长度 / split / 去重审计
  -> SFT JSONL
```

例如，一条 seed 可以固定为“`approval_latency_days`、按月、购买时间序列、v3 workspace”。
它不含“请按月统计订单确认时长”这句中文，也不含编译出来的 SQL；不同的合法日期窗口仍是同一个
family 的实例，而不是多个独立能力。

## 2. 输入边界

所有 seed 固定使用隔离 workspace：

```text
catalog_version = olist-catalog-v3
metric_version  = 0.3-proposal
dataset_version = olist-kaggle-v2-2026-08-03
```

默认 Agent 继续使用 `olist-catalog-v2 / 0.2-frozen`；该 fixture 不会改变生产运行时。

每行字段为：

| 字段 | 含义 |
| --- | --- |
| `seed_schema_version` | 此 fixture 的结构版本。 |
| `seed_id` | 仅供构造、审计和定位的稳定编号。 |
| `split` | `train`、`validation` 或 `in_domain_test`。 |
| `primary_bucket` | 本行唯一的主结构类别；不与其他类别重复计数。 |
| `family_id` | 忽略具体日期端点和自然语言后的语义身份；同一 family 不能跨 split。 |
| `risk_tags` | DISTINCT、状态过滤、二层聚合、时长边界等附加风险标签。 |
| `instance_window_policy` | 允许派生日期窗口的上限策略。 |
| `query_spec` | 已通过 validator 的代表性 QuerySpec 原型。 |

明确不允许出现：`question`、`prompt`、`sql`、`result`、`completion` 或模型输出。

## 3. 300 个 family 的配额

这是 **family 覆盖** 配额，不是最终训练行八等分目标。固定 Olist 指标空间不对称：例如新增 9 个指标
在 `all_time/absolute_range` 下只有 18 个单指标标量 family，不能靠复制日期或中文措辞凑到几百条。

| 主类别 | family 总数 | train | validation | test | 主要能力 |
| --- | ---: | ---: | ---: | ---: | --- |
| `single_scalar` | 18 | 12 | 3 | 3 | 新指标单独标量与时间范围。 |
| `multi_scalar` | 54 | 38 | 9 | 7 | 2--4 指标的独立 CTE、标量合并。 |
| `single_dimension` | 20 | 12 | 3 | 5 | 单指标客户州/商品品类分组。 |
| `multi_dimension` | 42 | 28 | 7 | 7 | 同一客户州下多指标合并。 |
| `single_purchase_series` | 40 | 27 | 6 | 7 | 新购买域指标 × 日/周/月/季/年。 |
| `single_review_series` | 5 | 3 | 1 | 1 | `review_count` 的评价时间序列。 |
| `multi_time_series` | 75 | 50 | 13 | 12 | 同一时间域多指标时间序列。 |
| `structural_hard` | 46 | 30 | 8 | 8 | DISTINCT、状态过滤、二层聚合、时长边界。 |
| **合计** | **300** | **200** | **50** | **50** | — |

为修复 Release v2 的品类测试空白，`category_grouped` 的 8 个独立 family 均固定在 v3 workspace。
它们由 `gmv`、`item_count`、`freight_amount`、`average_item_price` 四个商品行指标与两种时间模式组成，
其中 test 分到 5 个 family。继承 v2 的三个商品行公式在此仅用于补齐**结构测试边界**，并不改变 v3
新增指标定义。

每个新增 v3 指标的 family 覆盖门已通过：train 至少 10 个、validation/test 至少 3 个；实际每项为
train 25 个、validation 和 test 各 6 或 7 个。这个门避免某个新指标只在训练集出现。

## 4. 与最终 4,500 行 release 的关系

历史 Release v2 的 train 并不均衡：`JP09_scalar_multi_metric`、`JP10_state_multi_metric`、
`JP11_purchase_time_multi_metric` 合计 2,347/2,400（97.8%）；其中多指标购买时间序列为
1,049/2,400（43.7%），而品类 test 为 0。因而不能将 v3 新行直接 append 到旧文件后就声称最终数据
均衡。

后续正式 release 将从 v2/v3 的合法候选重新物化并审计，目标为 `train=3,000`、`validation=750`、
`test=750`。它同时遵循：

1. family 覆盖：每个稀缺结构、指标、粒度和 test 边界都有代表；
2. 行曝光：多指标时间序列最多约 25%，不再复刻 v2 的 43.7% 偏置；
3. 语义独立性：日期窗口和中文改写不会冒充新 family；
4. split 隔离：同一 `family_id` 不跨 train/validation/test。

因此 300 个 seed 是下一版的**结构骨架**，不是“新增 300 条训练数据”；正式行数必须在 Gold、
长度和 split 准入后才可确定。

## 5. 已验证与下一步

已验证：fixture 可由构建器确定性重建；300 个 `family_id` 全局唯一；bucket 与 `200/50/50` split
配额精确；每条 QuerySpec 均通过 v3 validator；v3 指标的真实 PostgreSQL Gold 回归已覆盖分组、时间
序列、二层聚合与空窗口。

下一步仅做小批（6--12 条）seed 的 QuerySpec/Gold admission：根据 workspace pin 加载 v3 Catalog，
渲染 SQL，依次经 SqlPolicy、`daa_analytics_reader`、ResultContract/ResultValidator，并留存 SQL hash
与执行证据。该小批通过前，不生成中文问题、不物化完整 JSONL、不启动 GPU。
