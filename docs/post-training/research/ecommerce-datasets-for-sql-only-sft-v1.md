# 面向 SQL-only SFT 的电商数据集调研 v1

## 1. 选择标准

本项目要补的不是一张销售 CSV，而是能训练和评测以下能力的关系型 workspace：

- 多事实表和维度表的 schema linking；
- 订单、商品行、用户、评价/行为等不同粒度；
- 至少有时间、客户/商品维度和可解释的聚合指标；
- 能导入独立 PostgreSQL schema，建立只读角色、表白名单、Catalog、QuerySpec 和 renderer；
- 来源、许可证和下载方式可记录，不能把 GitHub 镜像的许可证当成原始数据许可证。

TheLook 继续作为 protected cross-schema holdout，不进入候选训练集、验证集或 Prompt 调整。

## 2. 候选比较

| 候选 | 关系结构与业务内容 | 对本项目的价值 | 获取/许可注意事项 | 建议角色 |
| --- | --- | --- | --- | --- |
| **dunnhumby – The Complete Journey** | `transaction_data`、`product`、`household`/人口统计、`campaign`、`coupon`、`coupon_redempt` 等多表；约 2,500 个家庭、两年购买与营销历史。可构造销售额、件数、折扣、客户、促销和优惠券指标。 | 最接近真实零售分析，既有多表 Join，又有客户/商品/促销归因，能补 Olist 没有的营销场景。 | Kaggle 可见镜像 `frtgnn/dunnhumby-the-complete-journey`，接口显示约 847 MB，许可证字段为 “Database: Open Database, Contents: Database Contents”。镜像并非许可证权威，正式使用前要保存来源页、条款、快照和 checksum。 | **首选第二训练 workspace**，先做独立 schema 审计，暂不下载训练。 |
| **Instacart Market Basket Analysis** | `orders`、`order_products__prior/train`、`products`、`aisles`、`departments`；约 300 万订单、20 万用户级别、商品与类目层级。适合复购率、购物篮、类目和订单时间。 | Schema 简洁清晰，特别适合训练最小 Join、订单 DISTINCT、商品行粒度和用户/类目聚合。 | 原始数据通常通过 Kaggle competition 获取；GitHub/Kaggle 镜像可能标成 CC0，但不能替代原始竞赛条款。没有真实价格/GMV，不能直接承担金额指标。 | **第二候选/第三 workspace**；适合 SQL 结构泛化，不宜单独代表完整电商经营分析。 |
| **AdventureWorks** | Microsoft 样例包含销售订单、订单明细、客户、产品、分类、采购、库存等完整企业关系。 | PostgreSQL 导入和 schema 约束容易工程化，适合验证复杂 Join、销售/库存/采购 SQL；有稳定 DDL 和 SQL 样例。 | 它是合成企业样例，不是真实电商行为数据。Microsoft 样例仓库 `microsoft/sql-server-samples` 的仓库许可证字段不是数据内容许可证；PostgreSQL 镜像的许可证只覆盖镜像代码/转换。 | **工程基准/受控补充**，不作为“真实电商泛化”唯一证据。 |
| **H&M Personalized Fashion Recommendations** | `transactions`、`customers`、`articles` 三大表，时间、用户、商品、价格和服装属性丰富，规模很大。 | 适合大规模时序、用户/商品维度和价格分析。 | 数据量、磁盘和预处理成本高；没有明确订单粒度，不能直接表达订单履约；Kaggle 竞赛条款和各镜像许可证需核实。 | 暂不作为下一步；待资源和许可证单独评审。 |
| **Retailrocket Ecommerce** | `events`、`item_properties`、`category_tree`；约 275.6 万事件、2,242 万商品属性行、1,669 类目节点，含 view/cart/transaction。 | 适合行为漏斗、事件时间和属性快照 Join；可补 Olist 没有的浏览→加购→购买。 | Kaggle API 显示约 987 MB、`CC BY-NC-SA 4.0`；字段大量 hash，交易金额/订单语义很弱，不能作为核心营收数据集。 | 行为分析的备选，不作为下一轮主训练集。 |
| **UCI Online Retail II** | 约百万级发票商品行，含客户、商品、数量、价格、日期和取消发票。 | 下载简单、适合做很小的 SQL smoke。 | 主要是一张明细表，关系结构和 Join 覆盖不足；不能解决 Olist 的跨 schema 泛化问题。 | 仅用于快速 fixture，不作为核心 workspace。 |
| **阿里天池 O2O 优惠券使用预测** | 多个 CSV 形成用户、商户、优惠券、折扣、距离、领取日期和核销日期之间的关系；适合优惠券领取→核销转化、商户/用户分组和时间分析。 | 国内电商/本地生活语境，能补营销转化和中文业务问法；结构规模适中，适合独立 workspace。 | 需要天池账号和竞赛数据条款核验，不能从 GitHub 实现仓库推断数据许可；金额和商品订单粒度较弱。 | 国内候选；在 Complete Journey 许可证不稳定时，可作为小型第二 workspace。 |
| **Alibaba/Taobao 用户行为（天池类）** | 点击、收藏、加购、购买等行为事件，国内电商语境强，规模通常很大。 | 可增加中文业务问题和行为漏斗场景。 | 多数是单事件表或分区文件，原始访问条款、字段定义和可公开再分发边界需要单独确认；金额、订单和维度关系不完整。 | 作为国内行为数据候选，待许可证和 schema 评审，不作为当前第一选择。 |

## 3. 已核对的公开元数据

本轮使用公开 Kaggle API 元数据和 GitHub API 只做候选筛选，没有下载大文件或读取 TheLook：

- dunnhumby 镜像：`https://www.kaggle.com/datasets/frtgnn/dunnhumby-the-complete-journey`，接口显示约 847,048,344 bytes、2,500 households、两年、多个表，许可证字段为 Database Open Database/Database Contents。
- Retailrocket：`https://www.kaggle.com/datasets/retailrocket/ecommerce-dataset`，接口显示约 987,498,023 bytes、2,756,101 events、20,275,902 item-property rows、1,669 category rows，许可证 `CC BY-NC-SA 4.0`。
- UCI Online Retail II：`https://archive.ics.uci.edu/dataset/502/online+retail+ii`，官方页面可访问；本轮只核对入口，没有把下载文件放入项目。
- AdventureWorks 官方样例仓库：`https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/adventure-works`。仓库有 OLTP/warehouse 安装脚本，但数据内容和转换脚本的许可需按 Microsoft 条款复核。
- Instacart 原始入口：`https://www.kaggle.com/competitions/instacart-market-basket-analysis`。Kaggle 镜像虽可能标记为 CC0，正式使用仍以原始竞赛条款为准。
- 阿里天池 O2O 竞赛入口：`https://tianchi.aliyun.com/competition/entrance/231593/information`。本轮只确认入口可访问，并参考公开 GitHub 实现核对字段方向；没有下载或重新分发数据。

GitHub 搜索得到的仓库大多是分析代码或数据镜像，例如 `timchapman/postgresql-adventureworks`、
`renaud/adventureworks` 和若干 Complete Journey notebooks。它们可用于了解导入方式，但不能把
镜像仓库的 LICENSE 自动解释为原始数据集许可证。

## 4. 推荐决策

### 首选：dunnhumby Complete Journey

它能补 Olist 当前最缺的三类能力：

1. 多表营销/优惠券 Join，而不是继续围绕 Olist 三个多指标程序复制；
2. 客户、商品和促销的不同粒度归因；
3. 折扣、促销响应、客户消费趋势等电商/零售常见指标。

但第一步不是训练，而是建立 `dunnhumby_analytics` workspace：冻结来源快照、schema dump、
行数/checksum，设计 Catalog 和 只读 role，再选 20--30 个 QuerySpec family 做 Gold 准入。

### 为什么不是直接选择 Retailrocket

它的数据规模和事件量很吸引人，但金额、订单和商品属性大量 hash，业务指标语义不如 Olist/Complete
Journey 清晰。它更适合以后做行为漏斗分支，不适合现在作为 SQL-only 主领域训练集。

### 为什么 Instacart 仍值得保留

如果 dunnhumby 的原始许可或下载不可稳定复现，Instacart 是更小、更干净的替代：它能很好地验证
`orders -> order_products -> products -> aisles/departments` 的 schema linking 和去重。但需要诚实标注：
它缺少价格/收入，训练目标应改成订单、复购、购物篮和商品类目，而不是照搬 Olist 的 GMV 合同。

## 5. 推荐的数据集角色与切分

下一阶段的候选结构：

```text
Olist + dunnhumby（或许可证受限时的 Instacart）
    -> 各自独立 Catalog / Metric Contract / QuerySpec / Gold renderer
    -> workspace-aware SQL-only train/validation

TheLook
    -> 永久 protected cross-schema final holdout
```

不能把两个 workspace 的物理表名、指标 ID 或 Gold SQL 混成一套模板。训练/验证至少携带
`workspace_id`，按 workspace 和程序族分层采样；同一 family、QuerySpec 和结构签名不得跨 split。
每个 workspace 先做小批准入，再决定是否合并 SFT。若多 workspace 混合后 TheLook 或 Olist 退化，
应保留 workspace-specific adapter 或分阶段课程，而不是继续盲目加数据。

## 6. 当前不做的事

- 不因候选数据集多就立即下载、导入或训练；
- 不使用 TheLook 反向构造训练样本；
- 不把 GitHub notebook、Kaggle 镜像或单表数据集当作已确认可商用来源；
- 不把行数增长写成泛化能力增长。

正式进入接入前，先完成候选数据集的许可证记录和 schema 设计卡；然后只做首选数据集的原始快照与
独立 PostgreSQL workspace，不同时接入多个新数据集。
