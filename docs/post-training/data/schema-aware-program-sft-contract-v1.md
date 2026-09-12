# Olist Schema-aware Program SFT 数据合同 v1

**状态：** Phase 1 的接口、确定性 `SchemaLinkPlan` registry / derive / validate、Task A/B 外部物化与单元回归已完成；Phase 2 的 1.5B Base pair-aware Trainer 已通过最长序列 GPU smoke，完整两 epoch 训练已在外部 screen 中启动，尚未结束或评测。<br>
**训练基座：** `Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b` 的新 LoRA Adapter。<br>
**基座决策：** 1.5B Instruct 在同一 TheLook v2 任务内容下提高了 Policy/执行通过数，却没有提高最终 Gold 语义正确率；且其 chat-template 包装不同，不能作为严格单变量替换证据。为与历史 SQL-only Adapter 保持可比，本轮冻结既有 Qwen2.5-Coder 1.5B Base。

## 1. 最小任务卡

| 项目 | 冻结内容 |
| --- | --- |
| 目标 | 从 Olist 已验证的 `QuerySpec`、指标注册表、Catalog 和静态 Join 规则确定性派生 `SchemaLinkPlan`，把 schema linking / 程序结构变成训练期辅助监督。 |
| 主要能力缺口 | 表列绑定、最小 Join、事实粒度、时间字段归属、状态过滤、订单去重/二层聚合、维度分组和结果 alias。 |
| 运行时不变项 | 在线仍只有一次 `Prompt -> SQL` 模型调用；再经 `SqlPolicy -> readonly PostgreSQL -> ResultContract/ResultValidator`。不新增“规划一次、生成一次”的第二次 LLM 调用。 |
| 训练期任务 | 每个 Olist train/validation query instance 派生原 SQL Task A 与 JSON Task B；这是两个监督事件，不是两个业务查询。 |
| Gold 来源 | 既有 `QuerySpec -> deterministic PostgreSQL renderer -> Policy -> reader -> ResultContract/ResultValidator` 准入链。计划不生成 Gold、不替代 SQL、不执行数据库。 |
| 非目标 | 不修改生产 Prompt、Router、Catalog、SQL Policy、reader role、ResultContract、Vanna 前端或默认模型；不做 DPO/GRPO、repair 训练或运行时多 Agent。 |
| 验收 | 同一 QuerySpec 的计划可纯确定性重建、字节稳定且可追溯；Task A 不发生 Prompt/SQL 漂移；Task B 与 TheLook 完全隔离。 |

## 2. 训练期辅助监督，不是运行时两阶段 Agent

```text
Task A（生产形态）
  真实 runtime Prompt -> canonical PostgreSQL SQL + EOS

Task B（仅训练期）
  受控 Olist 上下文 + "emit SchemaLinkPlan JSON" task header
    -> canonical SchemaLinkPlan JSON + EOS

部署
  仍仅使用 Task A 的真实 runtime Prompt
    -> 一次 SQL 生成
    -> Policy -> reader role -> ResultContract/ResultValidator
```

Task B 让同一组 LoRA 参数在训练期看见 SQL 的结构原因；它不要求线上先输出计划 JSON，也不改变 SQL-only 输出边界。

## 3. 允许来源与永久隔离

仅可使用 Olist Release v2 的 **train / validation** 构造资产及其已通过的 Olist Catalog、QuerySpec、renderer、Gold 准入证据。每条记录必须追溯到：

```text
split, family_id, query_spec_id, workspace pin,
runtime Prompt hash, canonical Gold SQL hash, renderer version
```

Release v2 `in_domain_test` 继续是 final-evaluation-only，不训练、不用于 checkpoint 选择。

TheLook v2 的 question、schema、Catalog、QuerySpec、Gold SQL、candidate、错误状态、结果行与 case 级统计，一律不得进入静态 registry、Olist seed、语言改写、Task A/Task B Prompt/label、训练、验证、超参或 checkpoint 选择。TheLook 仅在新 Adapter 生成证据冻结后作为最终跨 schema 评测。

一条 `query_instance` 仍是一条业务语义样本。其一个 SQL event 和一个 structure event 只能被描述为 `Task A/B event_count`，不得说成业务查询或能力覆盖翻倍。

## 4. `SchemaLinkPlan` 的职责

`SchemaLinkPlan` 是离线训练标签，不是自然语言解析结果、自由 SQL 或线上 API。它显式记录：

1. 每个指标需要哪些表和列；
2. 每张表使用哪个固定 alias；
3. 哪些最小 Join 边是允许的；
4. 在订单、商品行、评价行或订单级中间聚合哪个 grain 计算；
5. 时间过滤作用在哪个事实字段；
6. 哪些状态/非空过滤、去重或二层聚合规则不可丢失；
7. CTE 如何合并，最终输出列/alias 的顺序是什么。

它不携带问题文本、SQL 文本、数据库结果、权限、模型输出、排序/Top-N、自由过滤或未冻结归因。

## 5. 规范 JSON 形状

```json
{
  "schema_version": "olist-schema-link-plan-v1",
  "schema_link_plan_id": "slp_<canonical-json-hash>",
  "query_spec_id": "qs_<source>",
  "workspace": {"...": "must equal QuerySpec.workspace"},
  "registry_version": "olist-schema-link-registry-v1",
  "join_program_id": "JP05_customer_geo_order",
  "result_shape": "state_grouped",
  "time": {"mode": "all_time", "start": null, "end_exclusive": null, "grain": null},
  "required_result_columns": ["customer_state", "paid_order_count"],
  "metric_programs": [
    {
      "metric_id": "paid_order_count",
      "cte_name": "m01_paid_order_count",
      "source_grain": "order",
      "relation_aliases": [
        {"relation": "analytics.fact_orders", "alias": "o"},
        {"relation": "analytics.dim_customers", "alias": "c"}
      ],
      "join_ids": ["orders_customers"],
      "required_column_refs": ["o.order_id", "o.customer_id", "o.order_status", "o.order_purchase_timestamp", "c.customer_state"],
      "time_owner": "o.order_purchase_timestamp",
      "filter_rule_ids": ["exclude_canceled_unavailable", "exclude_null_customer_state"],
      "dedup_rule_id": "count_distinct_order_id",
      "group_key_refs": ["c.customer_state"],
      "aggregation_rule_id": "count_distinct_order_id",
      "result_alias": "paid_order_count"
    }
  ],
  "final_merge": {
    "strategy": "single_metric_cte",
    "key_alias": "customer_state",
    "output_columns": ["customer_state", "paid_order_count"]
  }
}
```

上面是逻辑 schema，不是可自由填充的 SQL 模板。每个标识只能来自静态 registry、已验证 QuerySpec 或 Catalog，不能从用户问题、模型文本、SQL 或数据库行拼接。

### 5.1 顶层字段合同

| 字段 | 不变量 |
| --- | --- |
| `schema_version` | 固定 `olist-schema-link-plan-v1`；破坏性变更只能创建 v2。 |
| `schema_link_plan_id` | 对不含 ID 的 canonical JSON 做 SHA-256；字段篡改、顺序漂移或 ID 伪造都拒绝。 |
| `query_spec_id` / `workspace` | 必须来自已验证 Olist QuerySpec，workspace 版本逐字段相同。 |
| `registry_version` | 固定 `olist-schema-link-registry-v1`，防止未来规则变更后静默误读。 |
| `join_program_id` / `result_shape` / `required_result_columns` | 必须逐项等于 QuerySpec，不可补列、改 alias 或改形态。 |
| `time` | 必须逐字段等于 QuerySpec 的 `all_time` / 半开绝对时间范围 / series grain；`time_owner` 另固定该指标应当使用的底层时间字段。 |
| `metric_programs` | 与 `metric_ids` 一一对应且顺序一致；第 n 个 CTE 固定 `mNN_<metric_id>`。 |
| `final_merge.strategy` | 只能为 `single_metric_cte`、`cross_join_metric_ctes`、`full_outer_join_on_customer_state`、`full_outer_join_on_time`，由 QuerySpec 派生。 |

### 5.2 每指标程序字段合同

| 字段 | 允许值与用途 |
| --- | --- |
| `source_grain` | `order_item`、`order`、`review`、`order_total`。AOV 必须是 `order_total`。 |
| `relation_aliases` | 只允许 `analytics.fact_orders`、`fact_order_items`、`fact_reviews`、`dim_customers`、`dim_products` 及 renderer 固定 alias `o/i/r/c/p`。 |
| `join_ids` | 当前 Catalog 的静态 `orders_items`、`orders_customers`、`items_products`、`orders_reviews` 最小路径，不能加无关表。 |
| `required_column_refs` | 必需 alias-column 的有序并集；不能包含未登记列、敏感投影或调试列。 |
| `time_owner` | 必须等于指标定义：购买类 `o.order_purchase_timestamp`，评价类 `r.review_creation_date`。 |
| `filter_rule_ids` | 只引用静态 rule，如 `exclude_canceled_unavailable`、`delivered_with_complete_dates`、`valid_review_score`、维度非空规则；不允许携带任意 WHERE SQL。 |
| `dedup_rule_id` | `none`、`count_distinct_order_id`、`preaggregate_order_price`；显式保护订单计数和 AOV grain。 |
| `group_key_refs` | scalar `[]`；州 `["c.customer_state"]`；品类 `["p.product_category_name"]`；时间序列为 time owner，输出 alias 仍是 `time`。 |
| `aggregation_rule_id` | 静态规则，如 `sum_item_price`、`average_order_total`、`positive_review_fraction`；不是 SQL 字符串。 |
| `result_alias` | 必须等于对应 metric ID。 |

## 6. 确定性派生与 fail-closed 验证

```text
validated QuerySpec
  + pinned Catalog
  + METRIC_SQL_REGISTRY
  + explicit SchemaLinkRegistry
    -> derive_schema_link_plan()
    -> validate_schema_link_plan()
    -> canonical JSON / slp_ ID
```

新的静态 `SchemaLinkRegistry` 必须独立、可审阅。它不能解析 renderer 输出 SQL 或调用 renderer 私有函数去猜结构，否则共同错误会被隐藏。

验证器必须：先调用 `validate_query_spec()`；核对静态 registry 的关系/Join ID 与当前 Catalog；从同一 QuerySpec 重新派生期待计划；要求传入计划的字段与 canonical ID 全等；拒绝未知字段、除 scalar 的空 group key 和无 Join 的空 `join_ids` 以外的空必需数组、重复 relation/Join/column、SQL 片段、任意 filter 字符串、非 Olist relation、无关 alias 和敏感投影。它不调用 LLM、数据库、renderer、模型、Olist final test 或 TheLook 资产。

## 7. Task A / Task B 布局

### Task A：保持不变的 SQL-only SFT

```text
输入：当前真实 runtime rendered_prompt
标签：canonical PostgreSQL Gold SQL + EOS
```

Task A 必须复用 `olist-candidate-sql-v1` 的 Prompt 字节、SQL target、EOS/masking 和长度合同。新训练不得通过改写生产 Prompt 获得表面提升。

### Task B：仅训练期 JSON 任务

```text
输入：同一 Olist QuerySpec 对应的受控 Catalog / QueryPlan / ResultContract 上下文
      + training-only "emit canonical SchemaLinkPlan JSON" 任务头
标签：canonical SchemaLinkPlan JSON + EOS
```

SQL 与 JSON 不拼在同一 assistant target。默认是每个 train query instance 一个 Task A event 加一个 Task B event，稳定排序交错；验证也成对生成并分别报告 SQL / program loss。重新做 token audit，任何超长行进入外部 exclusion manifest，绝不静默截断。

Task B 的 prompt 从同一条已验证 runtime prompt 中确定性提取 Semantic Catalog、Query Plan 和 Question 三个语义上下文，并在前后加入固定的 training-only selector，明确“不要生成 SQL、只输出 canonical JSON”。它不改写或重建 Task A 的任何 prompt 字节，也保留结果合同所需的列、粒度和时间信息；去掉重复的 SQL 生成前言，避免两个 `### SQL` 选择器互相干扰。Task B 的 target 是包含稳定 `schema_link_plan_id` 的完整 plan JSON（排序键、紧凑编码）；其中不得出现 SQL 字段或 SQL target。所有 Task A/B 都由同一个 `sample_id` 派生稳定 `pair_id`，并在 length audit 中逐一记录是否成对可训练；存在任一超长 event 时，物化审计为 blocked，Trainer 不得启动。

## 8. 评测与停止条件

新 Adapter 必须从原始 Base 重新训练；当前 SQL-only Adapter 只保留为对照，不能作为父 Adapter。后续对照为：

```text
原始 Base
vs 当前 SQL-only Olist Adapter
vs 新 Schema-aware Program Adapter
```

loss、JSON 精确率或 Olist 协议拟合都不足以说明跨 schema 改善。必须先完成 Olist train/validation layout audit 和 matching 生成证据，再依次做 Olist 后验治理评测、TheLook generation、pair evidence verification、TheLook final evaluation。任何 workspace、Catalog、Prompt、registry、split、SQL hash 或长度证据漂移必须停止后续训练，先修复并重建外部 artifact。

## 9. 当前实现证据与下一小步

`src/data_analysis_agent/olist_schema_link_plan.py` 已实现 immutable dataclass、canonical
JSON / `slp_` ID、独立静态 registry、`derive_schema_link_plan()` 与
`validate_schema_link_plan()`。它在派生前复用 `validate_query_spec()`，并校验静态 relation / Join
ID 与当前 Catalog：此前发现初稿的 Join 名称不等于 Catalog 实际 ID，现统一使用
`orders_items`、`orders_customers`、`orders_reviews`、`items_products`，避免训练期标签和运行时
Catalog 脱节。模块不导入 renderer、不读取数据库、不调用 LLM。

`tests/test_olist_schema_link_plan.py` 覆盖十个 scalar 指标、scalar 多指标、州分组 review、品类
分组 item、AOV 订单级中间粒度、时间序列多指标、mapping 往返、immutable、篡改、跨 QuerySpec
复用与直接 SQL 字段拒绝。materializer / auditor 回归额外覆盖 Task A 字节保持、Task B canonical
target、source Gold 漂移、family 与 QuerySpec 跨 split 泄漏、过长不静默截断、完整 event sequence、
重写文件哈希后的内容漂移和重派生隔离失败。相关集合为 `82 passed`；Ruff、compile 和 diff 检查通过。

外部 Task A/Task B materializer 已完成一次真实 Olist Release v2 train/validation 物化：
`2,400 + 600` 个 query instance 形成 `3,000` 个稳定 pair、`6,000` 个训练事件，Task A
Prompt/SQL 字节哈希与源 release 一致，Task B 全部由 QuerySpec/Catalog 确定性派生并通过
plan validator。紧凑的结构上下文使 Task B 最大长度为 `3,046`（上限 `3,072`），无超长排除。
物化后的独立只读 audit 会再次从可信 Olist Release v2 输入重建 3,000 个 instance，并比较所有
6,000 个 Task A/B event、pairing、source hash、length accounting 与交错顺序；它不会读取 TheLook、
in-domain test、数据库、模型或 GPU。2026-09-11 的真实 audit 通过，报告位于
`/disk2/gengnan/data-analysis-agent-data/experiments/olist-schema-aware-program-sft-v1-audit-20260911/audit-report.json`，
其中 `3,000` pair、Task A/B 各 `2,400/600`、交错 event `4,800/1,200`、length exclusion `0`，且 source
hash、family/QuerySpec isolation、Task A byte identity、Task B re-derivation 与 pairing/order 均为 `true`。物化结果位于
`/disk2/gengnan/data-analysis-agent-data/experiments/olist-schema-aware-program-sft-v1-materialization-20260911c/`。
随后 Olist-only 有界分层 review 通过：train/validation 分别有 `2,400/600` 个 instance、
`914/600` 个 family；当前十个冻结指标、scalar/state/category/time-series 四种形态和十条
Join program 都有覆盖。Task B token 的 min/p50/max 为 train `1,272/2,484/3,046`、validation
`1,268/2,535/3,025`，均未越过 `3,072`。review 仅保存计数、形态/Join/长度分布和哈希型
pair/plan ID，不保存问题、Prompt、SQL、plan JSON 或结果行；报告位于
`/disk2/gengnan/data-analysis-agent-data/experiments/olist-schema-aware-program-sft-v1-review-20260911/review-report.json`。
仍未读取 TheLook、不训练、不访问 GPU。Phase 1 数据准备现已完成；下一步才为 Trainer 增加读取
已审计 pair、按 Task A/Task B 分别记录 validation loss 的入口。

### 9.1 TASK-007 Trainer 入口

实现位于 [`scripts/post_training/training/run_qwen25coder_schema_aware_sft.py`](../../../scripts/post_training/training/run_qwen25coder_schema_aware_sft.py)。入口只接受仓库外的 materialization、audit 和 review 路径，并在加载模型前检查：

- audit/review 版本与 `pass` 状态、物化目录绑定、event 文件 SHA-256、2,400/600 pair 数量；
- 每个 pair 恰好一个 Task A 与一个 Task B、split 标记、3,072 token 上限和 Prompt/target 边界；
- Qwen2.5-Coder-1.5B Base 的 model ID/revision 与 download manifest；
- TheLook、in-domain test、数据库、LLM 与 GPU 未参与数据准备的证据。

训练使用 bf16、冻结 Base、LoRA、`adamw_torch`、weight decay、gradient checkpointing 和真实的
gradient accumulation。Task A/B 事件按物化文件的稳定顺序交错训练；验证阶段额外分别在 SQL-only
和 SchemaLinkPlan-only 子集上计算 `sql_loss` / `schema_link_plan_loss`，避免总 loss 掩盖某一任务退化。
标签只监督各自 target 与 EOS，Prompt 和动态 padding 使用 `-100`，不静默截断。

本入口的逻辑回归为 `tests/test_qwen25coder_schema_aware_sft.py`（4 passed，使用 Dummy tokenizer，
覆盖 Prompt mask、EOS、空 target、超长拒绝和 right-padding）。下一小步是先用已经绑定的真实外部
materialization 做最小 GPU smoke，确认显存、LoRA 注入和两类 validation loss 都有限，再由用户确认是否
启动完整训练；本轮尚未启动任何 GPU 训练。

### 9.2 GPU smoke 与完整训练启动

2026-09-13 以 `CUDA_VISIBLE_DEVICES=1`（逻辑设备 1、物理 `nvidia-smi` 设备 3、RTX 4090、
UUID `GPU-10863af0-8588-7625-5609-640ba794f64b`）完成三次有界 smoke：首次修复了入口的
`src/` import 路径，第二次补回 RTX 40 系单卡所需的 `NCCL_P2P_DISABLE=1` /
`NCCL_IB_DISABLE=1`，两者均未进入有效训练；随后正式成功的 smoke 使用真实、已审计输入，且均
不读取 TheLook 或 Olist final test。

- `smoke-v4`：两个完整 pair（4 event）覆盖 micro batch 1、梯度累积 4 后的一次 optimizer step；
  train / aggregate validation / SQL validation / program validation loss 分别为 `0.7474` /
  `0.8456` / `0.2985` / `1.3928`，均有限；
- `smoke-v5`：按 pair 最大 token 选择最长训练样本，实际到达 `3,046 / 3,072` token；train /
  aggregate validation / SQL validation / program validation loss 为 `0.5859` / `0.4831` /
  `0.1209` / `0.8454`，均有限，峰值 allocated/reserved 为 `8.46 / 14.88 GiB`。

smoke 的 adapter、checkpoint、日志与证据在仓库外
`/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder-schema-aware-sft-smoke-v{3,4,5}-20260913/`。
最长样本 smoke 通过后，完整训练已以 `screen` 会话 `qwen25-schema-aware-full-v1` 启动，输出目录为
`/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder-schema-aware-sft-full-v1-20260913/`：
Olist train `4,800` event、validation `1,200` event、2 epoch、micro batch 1、accumulation 4、
有效 batch 4，预期 2,400 optimizer step；每 300 step 做 aggregate validation、每 600 step 保存
checkpoint。训练完成前不声明质量提升、不改生产默认路径；后续须先检查训练证据和独立 Base/Adapter
matching，再运行 Olist / protected TheLook 后评测。
