# 在线指标：首批八项扩展

本页说明在线业务口径，不是训练数据或Gold renderer合同。在线仍由模型理解问题、生成候选
SQL，随后通过计划检查、SqlPolicy、只读角色、JOIN证据与ResultValidator；没有用确定性
renderer替代线上SQL生成或验证。

Olist共23个指标，TheLook共24个。入口是`resources/workspaces/*.yaml`；运维可复制
`data/workspaces/*.yaml`。在线Catalog为[Olist](../../data/catalog/olist_online_v1.yaml)与
[TheLook](../../data/catalog/thelook_online_v1.yaml)，版本分别`olist-online-v1 / 0.4.2-online`、
`thelook-online-v1 / 0.3.2-online`。历史v2/v3实验Catalog仍保留原样；切换后的旧版本工件
不能继续当作当前可信结果使用，聊天历史不删除。

## 新指标口径

| 工作区 / 指标 | 业务用途和算法 | 时间、维度与局限 |
| --- | --- | --- |
| Olist `total_order_count` 全部订单数 | 所有状态的订单去重数，包含取消/不可用 | 购买时间；州/城市/品类/支付方式。一个订单可属于多个品类/支付方式，这些分组不可相加当总体 |
| Olist `payment_amount` 有效订单支付金额 | 排除取消/不可用订单后，支付记录`payment_value`之和，每条支付只计一次 | 购买时间，不是支付发生日期；州/城市/支付方式。不支持品类/卖家分摊，不是GMV或退款后净收入 |
| Olist `freight_share` 运费占比 | 同一有效商品行范围内，总运费÷(总商品价+总运费) | 购买时间；客户/卖家地域、品类。不是逐行占比的平均，不按支付方式分摊 |
| Olist `period_repeat_customer_rate` 期间复购客户率 | 期间内≥2笔有效订单的客户数÷≥1笔有效订单的客户数，用`customer_unique_id`识别人 | 购买时间；客户州/城市。各月/组内独立计算，不是终身老客率；客户标识仅内部聚合 |
| TheLook `total_order_count` 全部订单数 | 所有状态订单去重数 | 订单创建时间；客户州/城市/注册来源。与Complete订单数不同 |
| TheLook `cancellation_rate` 取消率 | Cancelled订单数÷全部状态订单数 | 同一创建期间/分组，不能先筛掉非Cancelled订单，也不能用Complete作分母 |
| TheLook `period_repeat_customer_rate` 期间完成订单复购客户率 | 期间内≥2笔Complete订单的用户数÷≥1笔Complete订单的用户数 | 创建时间；客户州/城市/注册来源；每个时间桶独立算，不输出user_id |
| TheLook `current_unsold_inventory_cost` 当前未售库存成本 | 当前数据快照中`sold_at IS NULL`的库存单元`cost`之和 | 品类/品牌/部门/配送中心；一行一个库存单元。无历史库存快照，不支持期间/趋势；币种未知，不能称人民币/美元或利润 |

比率统一返回0–1小数、由展示层转百分比。比率不能跨组求和或直接平均来还原总体；分母为0
返回NULL，当前校验器将全NULL指标标为degraded，不让模型把它说成真实0。新金额指标的
空范围标量合计为0。复购的分子、分母均先应用同一期间、地域和状态范围；不使用一笔订单
一个不同值的Olist `customer_id`代表真实复购人群。

## 在线链路的改动

1. `CatalogLoader`允许“无默认过滤”的指标，避免全部订单数被强行套上有效订单状态。
2. `MetricDefinition.temporal_mode`表达事件或当前快照；`QueryPlan.preflight()`在SQL生成前
   拒绝给快照指标指定历史日期/时间分组。语义解释器也读取该能力，正常当前快照分组仍可用。
   原有未售库存数与新增库存成本共用此规则，不用问题关键词判断。
3. Olist显式配置6个维度绑定；TheLook沿用13个绑定。金额/数量进入可加列表，比例不进入；
   全部订单数按品类/支付方式的重叠分组仍由原`multi_membership`策略排除相加。
4. 支付金额及库存成本复用已有金额来源检查，分别约束`fact_payments.payment_value`和
   `inventory_items.cost`。不让商品价冒名支付金额或用商品零售价冒名库存成本。
5. 状态、工件、图表、分页/CSV和解释复用已有组件，不增加新模型或SQL执行通道。
6. 业务时间分桶的数据库会话明确采用UTC，避免宿主时区导致跨月订单误归属；虚拟`date`
   与`time_grain`归一，避免把同一个时间分组重复估算，误拦合法月度查询。
   wheel资产构建和新安装的合成Olist Catalog也使用最新在线指标定义。
7. 分组路径中自行添加的`维度 IS NOT NULL`检查交给计划审查，原候选与一次修复候选共用；
   保留指标自身非空规则、已确认的维度筛选和未使用CTE，不将任意额外筛选都声称已覆盖。
   本次对应的实际错误是模型从运费占比分组里丢掉未分类商品。
8. 结果解释及从工件恢复后的追问携带本次指标的Catalog定义；定义与观测值分开引用，
   指标公式中的阈值不能直接成为业务结果。验收器独立记录正文核验状态，文字回退标degraded，
   不再仅因CSV正确就标整轮回答通过。没有放宽单位、数字或复核失败保护。

来源检查、范围检查与JOIN诊断仍不是完整的SQL业务语义证明。例如错误的比率公式可能落在
0–1内；因此数值验收必须保留独立参考SQL，不能只检查返回列名和校验器状态。

## 验收入口

- `tests/test_online_metrics.py`：新指标选择/计划/合同、维度、快照、可加性、金额来源、
  比率边界、空总体和原实验Catalog隔离。真实两库SQL测试用`RUN_ONLINE_METRICS_DB=1`启用。
- `evals/cases/{olist,thelook}_online_metrics_v1.yaml`：每库17轮真实浏览器/模型问题，
  每库一段5轮连续分析，独立SQL核对完整CSV，并检查图表、刷新、复用与不支持的请求。
- `scripts/check_workspace.py --database`：实际列、角色权限和声明关联检查。
- `scripts/verify_workspace_onboarding.py`：沿用真实网页验收器，不接入训练评测。

实际结果见[首批八指标验收记录](../../evals/results/online_metrics_20261104.md)。成功仅限已测
场景，不据此宣称任意中文问题或任意SQL公式都正确。
