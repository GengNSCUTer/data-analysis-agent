# Olist v3.1｜数据平衡与中文 Surface 修复合同

## 1. 目的、版本边界与非目标

本合同是 `olist-v3-balanced-release-v1` 的**后继 release 合同**，用于修复已审计确认的四类
数据偏差：五问法/八问法不一致、`hash % 5` 非配额选择、中文主训练集混入英文指标别名，以及测试只
验证实现细节而没有验证发布合同。同时提高 9 个新增 v3 指标的训练曝光，修复 `category_grouped`
训练覆盖，并均衡 validation/final-test 的时间粒度。

旧 release 及其 `4,500` 条 Gold SQL、全量 PostgreSQL 准入证据、runtime overlay、SFT JSONL 和 hash
均保持不可变，目录仍为：

```text
/disk2/gengnan/data-analysis-agent-data/evals/olist-v3-balanced-release-v1/
```

v3.1 只能写入新的仓库外目录 `olist-v3-balanced-release-v1.1/`。它不改变默认产品的
`olist-demo-v2` runtime，不启动 GPU 训练，不改变 QuerySpec/renderer 的安全边界，也不为了提升
`category_grouped` 行数建立未冻结的订单/评价品类归因规则。

## 2. 结构规模与隔离不变量

总行数、三 split 和八个互斥主 bucket 均保持 v3 的既有冻结配额：

| bucket | train | validation | final test |
| --- | ---: | ---: | ---: |
| single_scalar | 110 | 25 | 25 |
| multi_scalar | 600 | 150 | 150 |
| single_dimension | 122 | 33 | 33 |
| multi_dimension | 478 | 117 | 117 |
| single_purchase_series | 400 | 100 | 100 |
| single_review_series | 72 | 19 | 19 |
| multi_time_series | 750 | 188 | 187 |
| structural_hard | 468 | 118 | 119 |
| **total** | **3,000** | **750** | **750** |

每条新 row 都必须重新以 v3 workspace pin 验证 QuerySpec、重新 deterministic render Gold SQL；
`family_id`、`query_spec_id`、canonical Gold SQL hash 跨 split 均为零交集。所有新结构 row 都必须
通过完整的：

```text
SqlPolicy -> daa_analytics_reader -> ResultContract / ResultValidator
```

旧 v3 的全量准入结论不能替代新 v3.1 row 的执行证据。

## 3. v3.1 结构平衡合同

### 3.1 新增 v3 指标的行/程序曝光

新增指标为：`unique_customer_count`、`review_count`、`canceled_order_count`、
`delivered_order_count`、`unavailable_order_count`、`average_items_per_order`、
`average_item_price`、`approval_latency_days`、`carrier_handoff_days`。

对每一个新增指标，v3.1 的最低要求是：

| split | 最少行曝光 | 最少独立 family |
| --- | ---: | ---: |
| train | 230 | 20 |
| validation | 50 | 5 |
| final test | 50 | 5 |

这里的 `family` 按 `family_id` 去重。行曝光和 family 覆盖一起检查：8 个日期窗口不能单独靠同一
程序族满足“程序更广”的要求。`230` 是在真实候选容量上试算得到的可达下限，较旧 release 中新增
指标的 `151–180` 条 train 行有实质提高；并不主张将所有指标机械训成同频。

### 3.2 `category_grouped` 的有限空间与分配

当前 validator 只允许单一 item-grain 指标按商品品类分组；order/review 指标仍会因缺少归因/分摊
合同被拒绝。合法 family 一共只有 8 个，不能通过多指标拼接、复制 SQL 或增加未冻结的品类订单口径
来“补齐”。

为改善 train 的 9 条低覆盖，v3.1 不修改 v1 seed fixture，而是在新 release 的选择配置中将：

```text
olist-v3-coverage-in_domain_test-031
freight_amount × category_grouped × absolute_range
```

明确重分配到 train。它有 8 个不重复、合法的日期实例。最终最小目标为：

| split | 最少 category 行 | 最少独立 category family |
| --- | ---: | ---: |
| train | 17 | 3 |
| validation | 8 | 1 |
| final test | 11 | 4 |

final test 仍保留 4 个不同品类 family，覆盖 `item_count`、`average_item_price`、`gmv`、
`freight_amount` 四类 item 指标；这比保持旧 test 的 5 family 但让训练只有 9 行，更符合本轮
“训练可学习、测试仍有跨程序检查”的目标。

### 3.3 validation / final-test 时间粒度

对 `day / week / month / quarter / year`，validation 和 final-test 各自每种粒度至少 `70` 条
时间序列 row。train 仍完整报告所有粒度分布，但不额外施加刚性下限。该门专门修复旧 validation 的
`month=32` 与旧 final-test 的 `day=51`，不把日期表达改写当作新的时间粒度能力。

本轮暂不按 `distinct`、二层聚合、状态过滤、非负时长边界拆分新的独立 row 配额；它们继续保留为
`risk_tags`，在 family/Gold SQL 准入和分层 review 中检查。

## 4. 八类纯中文 surface 合同

每个已准入 QuerySpec 固定生成下列 8 条候选问法。它们是同一 QuerySpec 的语言 overlay，不增加
family、QuerySpec 或训练行计数。

| ID | 类型 | 说明 |
| --- | --- | --- |
| `v1` | formal_request | 正式请求 |
| `v2` | colloquial_request | 业务口语请求 |
| `v3` | manager_request | 管理者表达，不引入趋势/因果含义 |
| `v4` | concise_request | 关键指标、范围、切面均仍明确的短句 |
| `v5` | result_oriented_request | 表格/并列展示导向，不引入排名或比较基线 |
| `v6` | time_front_request | 时间范围前置 |
| `v7` | analysis_cut_front_request | 分组行维度前置、时序行粒度前置、标量行整体口径前置 |
| `v8` | catalog_alias_request | 使用 Catalog 已注册的中文受控别名 |

中文主 release 从 Catalog 的 `metric.name`/`metric.aliases` 中过滤所有含 ASCII Latin token 的候选；
因此 `paid orders`、`AOV`、`average items per order` 等不会进入主 train/validation/test。它们可以
以后通过独立的 multilingual robustness overlay 保存，但不能静默污染中文训练集。surface builder
不维护另一份指标别名表，仍由冻结 Catalog 作为唯一来源。

禁止自动引入同比、环比、趋势、变化、Top-N、排名、原因、影响、相对时间、自由筛选、支付归因、
品类订单归因或卖家归因。这些词会改变 QuerySpec 或需要新的合同，不能伪装成 SQL-only 正例改写。

## 5. 主问法的可复现分层配额

正式 SFT 每个 QuerySpec 只选一条主问法。选择策略为
`sha256_ranked_split_bucket_eight_way_quota_v2`：

1. 每个 `split × primary_bucket` 先给 8 个 form 分配差值最多为 1 的行数；
2. 各 bucket 的余数由稳定 SHA-256 排序分配，使整个 split 满足**精确** form quota；
3. 同 bucket 中的 seed 也按稳定 SHA-256 rank 映射到已分配 form slot，不依赖 JSONL 顺序或随机数。

因此 train 的 3,000 行为每种 `375`；validation/final-test 的 750 行分别为 `v1–v6=94`、
`v7–v8=93`。其余 7 条 form 只保留为 runtime/robustness overlay，不计作额外 SFT 样本。

## 6. 合同级质量门

发布前必须验证，而不是仅测试某段模板代码：

- 所有 QuerySpec 均有 `v1–v8` 恰好一次，`variant_id` 与 `variant_kind` 一一对应；
- 36,000 条 overlay 无 exact/normalized duplicate，每条主中文问题无 Latin token；
- 每一个 split 与每一个 `split × primary_bucket` 均有 8 类 form；全 split 是精确配额，bucket 内最大差为 1；
- Router → Catalog → QueryPlan → ResultContract 的指标、维度、时间范围、结果列与 QuerySpec 一致；
- 结构/Gold/Prompt 绑定、family/querySpec/SQL hash split isolation、3,072 token 无截断合同均通过；
- 第 3 节所有指标、品类、时间粒度分布都达到下限，并写入 structural manifest；
- 全量 Gold 真实 reader-role 准入通过后，才允许物化 SFT JSONL；本合同本身不授权训练。

## 7. 预期外部目录

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1.1/
    structural-<date>/
    admission-<date>/
    surface-<date>/
    runtime-prompts-<date>/
    sft-release-v1.1/
```

每一层的 manifest 必须绑定上游文件 SHA-256；新 release 的最终 hash、实际分布和任何未通过门只在运行
完成后写入项目状态文档，不能预先宣称通过。
