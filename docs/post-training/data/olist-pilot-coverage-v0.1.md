# Olist Candidate SQL 领域 Pilot 覆盖矩阵 v0.1

**状态：** 设计草案，待用户共同审查；未创建训练/验证样本，未启动训练。

## 1. 要回答的问题

这不是要让小模型立刻覆盖任意 Olist 分析需求，而是先验证一个可反驳的假设：在与当前
PostgreSQL、Catalog、QueryPlan 和结果别名合同一致的训练输入下，领域 LoRA 是否能比 matching
Base 更稳定地产生**单指标、无归因歧义**的 SQL 候选。

本 pilot 优先覆盖现有业务迁移评测暴露的基础错误：指标别名遗漏、把派生指标当物理列、
PostgreSQL 日期/聚合形状错误、Catalog Join 路径遗漏、以及顶层结果列不符合合同。它不试图在
第一轮解决多指标 CTE、支付归因、多轮上下文或任意分析解释。

## 2. 固定边界

| 项目 | v0.1 决定 |
| --- | --- |
| 模型职责 | 仅生成候选 SQL；不训练或替代 QuestionRouter、SqlPolicy、PostgreSQL reader role、ResultValidator。 |
| 工作区 | `olist-catalog-v1`、`olist-kaggle-v2-2026-08-03`、PostgreSQL、`sql-policy-v1`。每条样本记录完整版本快照。 |
| 路由 | 仅 `state=answerable` 且 `route.should_generate_sql=true` 的单轮数据库问题进入 SQL SFT。 |
| 输入 | 用户问题、同轮显式时间范围、服务器 Catalog slice、QueryPlan、required result columns、PostgreSQL SQL-only 合同。 |
| 目标 | 已审查的单条只读 PostgreSQL SQL，加 EOS；不把答案文本、图表、结果行或安全策略当生成目标。 |
| 原始资产 | 训练 JSONL、SQL、执行摘要和模型产物均在仓库外；Git 只保留本设计、manifest、脚本和聚合证据。 |
| 永久隔离 | `post_training_holdout_v1.yaml` 的 60 条 v2 case 及其派生表达不得进入任何训练、验证、示例或人工照抄语料。 |

“共享同一个 Olist Catalog”不等于泄漏。模型必须在训练中见到 `gmv`、`customer_state` 等基础
业务词汇；隔离的对象是完整的**问题表达 + 指标组合 + 查询形状 + 维度/时间/排名/归因方案**，
不能把 holdout 的同义改写、同一 gold SQL 程序或只改表面措辞的题目加入训练。

## 3. v0.1 允许的基础覆盖

当前 Catalog 的四个核心指标如下：

- `gmv`：订单商品行粒度；默认排除 canceled/unavailable；不含运费；
- `paid_order_count`：订单粒度的去重计数；
- `average_delivery_days`：订单粒度，只统计购买和实际送达时间均存在的订单；
- `positive_review_rate`：评价行粒度，`review_score >= 4` 除以有效评分评价数。

v0.1 只选择已被 Catalog 判定为安全直连、且结果形状容易审查的维度：

| 覆盖单元 | 可用指标 | QueryPlan / 顶层结果形状 | 为什么纳入 |
| --- | --- | --- | --- |
| S0：总体标量 | 四个核心指标 | `single_metric`；`[metric_id]` | 学习每个指标的事实粒度、默认过滤和严格 metric alias。 |
| S1：显式时间过滤的标量 | 四个核心指标 | `single_metric`；`[metric_id]`；问题中明确年份或 `YYYY-MM-DD 至 YYYY-MM-DD` | 学习使用每个指标自己的时间字段，而不是默认使用订单时间。 |
| S2：按月时间序列 | 四个核心指标 | `single_metric`；`[metric_id, time]` | 学习 PostgreSQL 月度截断、`time` 别名和时间分组。 |
| S3：按客户州汇总 | 四个核心指标 | `single_metric`；`[customer_state, metric_id]` | `customer_state` 对四个指标均为安全直连，是跨订单/评价事实表的最小共同维度。 |
| S4：按品类汇总 | 仅 `gmv`、`paid_order_count` | `single_metric`；`[product_category_name, metric_id]` | 覆盖订单商品行和品类 Join，但不让订单/评价指标在多商品订单上被重复计数。 |

共计 `4 + 4 + 4 + 4 + 2 = 18` 个基础覆盖单元。每个单元可使用多个非 holdout 的时间范围和
中文表达，但报告必须分别统计“覆盖单元数”和“materialized rows”，不得把同一语义的同义改写
伪装成独立业务能力。

## 4. v0.1 明确排除的内容

| 排除项 | 原因 | 后续条件 |
| --- | --- | --- |
| 多指标标量和多指标分组 | 需要每个事实粒度先聚合、再 CROSS JOIN 或按受控键合并；这是下一阶段独立变量。 | 单指标 pilot 先有正向领域验证证据。 |
| Top-K、排名、同比/环比、异常/因果解释 | 引入 `ORDER BY`/`LIMIT`、比较基线或答案证据约束，不能与基础 SQL 生成混成一个变量。 | 作为 v0.2 单独覆盖。 |
| 多轮时间补充、结果追问 | 需要验证 WorkingMemory 和会话状态迁移，不属于单轮 candidate SQL pilot。 | 单轮数据合同和 SQL 质量门先稳定。 |
| `payment_type` 分组 | GMV/订单按支付方式需要归属或分摊规则，当前 Catalog 为 `requires_attribution`。 | 管理员实现并注册服务器归因规则。 |
| 按品类的履约天数/好评率 | 一笔订单/评价可能关联多个商品品类，直接 Join 会放大订单/评价事实。 | 冻结订单/评价归属或分摊口径。 |
| `customer_city`、`seller_id` | 城市和卖家高基数；卖家标识还不适合作为基础展示目标。 | 单指标安全矩阵通过后再单独评估展示、隐私和行数边界。 |
| 路由、拒答和澄清 SFT | 当前 Adapter 是 SQL candidate generator，不应被训练为“所有问题都输出 SQL”。 | 若要训练路由器，另建分类/对话数据和评测。 |

## 5. 切分与样本规模

### 5.1 切分单位

每条样本需记录一个 `family_id`。建议由下列成分组成：

```text
metric_set + result_shape + dimensions + time_mode + filter_class
+ join_program + ranking_mode + attribution_mode + catalog_version
```

同一 `family_id` 的自然语言改写、同一 SQL 程序的日期/文字轻微替换、或同一 gold 的格式改写必须
整体进入一个 split。train 与 validation 可以共享基础原子词（例如 `gmv` 或 `customer_state`），
但不能共享完整查询程序或其表面改写。60 条永久 holdout 不参与任何 split 分配，只在训练配置冻结
后作为最终外部 gate。

### 5.2 建议的渐进规模

| 阶段 | 规模 | 作用 |
| --- | --- | --- |
| coverage seed | 18 个覆盖单元，约 120--160 个 materialized rows | 先检查 Catalog/Plan/SQL 渲染、执行、合同与 split 审计；不据此宣称模型质量。 |
| basic pilot | 在上述范围内扩展至约 300--500 个 rows，并保留 80--150 条独立 validation | 用 matching Base/Adapter 对照验证基础领域适配是否有信号。 |
| 扩展 | 1,000 条以上前先完成错误迁移分析 | 只有证明是覆盖不足，才加入多指标、排名或其他新维度。 |

`120--160` 是数据合同的 materialization 检查规模，而非“训练已经足够”的判断；`300--500` 是
基础 pilot 的上限目标，也不等同于 300--500 个完全不同的业务指标。每轮报告都必须把逻辑覆盖
单元、语义 family 数和实际 SFT 行数拆开报告。

## 6. 构造与质量门

一条样本只有依次通过以下步骤才能成为 SQL SFT target：

```text
coverage matrix cell
-> Catalog / QueryPlan / ResultContract 生成受控输入
-> PostgreSQL gold SQL
-> SqlPolicy
-> readonly reader-role execution
-> ResultValidator / ResultContract
-> 人工语义抽检与 split / holdout 审计
```

数据生成可以用 Catalog 驱动的确定性 SQL 模板降低劳动量，但模板不是质量证明。必须额外验证：

1. 每条 SQL 使用正确事实粒度和默认过滤；
2. `positive_review_rate` 的时间序列使用评价创建时间，其他三个核心指标使用其 Catalog 指定时间字段；
3. 顶层列严格匹配 QueryPlan / ResultContract，指标别名必须为 metric ID；
4. 不泄漏订单、客户、卖家等原始标识和结果行；
5. 没有样本与永久 holdout 或其他 split 属于同一 `family_id`；
6. 每个覆盖单元至少抽取人工复核样本，不把“SQL 能执行”写成“业务语义正确”。

## 7. 评测顺序

1. 先冻结 train / validation family 与版本 hash；
2. 使用 validation 选择 prompt、epoch、learning rate 和 LoRA 配置，不能用 60 条 holdout 调参；
3. Base/Adapter 固定同一模型 revision、prompt、decode、seed 和 SQL repair 状态；
4. 对 validation 记录生成、Policy、PostgreSQL 执行、ResultContract 和人工语义抽检；
5. 配置冻结后才运行已有 12 条 protected candidate transfer case，作为外部迁移 gate；
6. 运行完整 60 条确定性路由/合同回归，证明 Adapter 试验没有改动服务器可信链路。

v0.1 的通过不代表可切换生产默认模型。只有基础领域 validation 与 protected holdout 都出现可复核
的正向状态迁移、没有安全回退、并通过人工语义抽检，才有资格设计 v0.2 的多指标或 runtime shadow
candidate generator。

## 8. 学习检查

在创建数据模板前，用户应能回答：

1. 为什么 `customer_state` 可以进入四个指标的 v0.1，而 `payment_type` 不能？
2. 为什么 `positive_review_rate` 的按月 SQL 不能机械复用 GMV 的时间字段？
3. 为什么“同一 SQL 只把 2017 改成 2018”不应同时出现在 train 和 validation？
