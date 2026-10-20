# TheLook：按通用清单填写的接入实例

配置入口：[workspace YAML](../../data/workspaces/thelook.yaml)、[Catalog](../../data/catalog/thelook_catalog_v2.yaml)。
按[通用模板](../templates/workspace/README.md)逐项填写；不是TheLook专用Agent。

| 项目 | 本库填写结果 |
| --- | --- |
| 来源 | TheLook公开电商Kaggle镜像快照；不是本轮BigQuery官方直接导出，不代表真实企业交易 |
| 库/Schema | 独立PostgreSQL `thelook_analytics`；在线只访问 `analytics` 安全视图 |
| 工作区 | `thelook-demo`；数据/指标版本来自已存在Catalog，不复制修改历史离线资产 |
| 时间/单位 | timestamptz按UTC解释，日期采用左闭右开区间；时长按总秒数/86400.0；比例为0–1；币种未知 |
| 账号 | `daa_thelook_reader`只读安全视图；`daa_thelook_app_writer`只写app元数据，不写或读取业务数据 |
| 关联原则 | Catalog的8条多对一关联；订单/商品行/库存不能混为同一粒度。商品行到库存必须使用inventory_item_id=id，不能用product_id造成多行放大 |
| 维度来源 | 品类/品牌/部门优先商品维度；库存指标可直接用库存快照列。注册用户来源和事件来源是不同概念 |
| 构成图 | workspace声明10个可相加金额/数量指标；比率、均值、去重客户数与会话数不作为构成图指标。图表仍受维度、非负值和分类数量限制 |
| 默认分组范围 | 只返回满足指标过滤条件的事实分组，不自动把无完成订单的用户城市补0；用户明确要求完整维度补全时再单独处理 |
| 未支持 | 利润、币种换算、广告归因/转化率、库存历史快照趋势、原始个人明细；没有可信定义时澄清或拒绝 |

本次口径依据是现有Catalog/指标定义与数据库核对，由项目开发者复核；没有外部企业业务负责人背书。
连接凭证、账号密码、原始日志在仓库外，使用接入说明中的私有环境文件。

2026-10-20接入诊断实测：7表声明列均存在，reader/writer权限检查通过；8条已配置关联的
目标键重复组数均为0，非空来源键未匹配行数均为0。这个结论仅针对当前快照与配置的关联键，
不能阻止模型另写一个错误关联，仍需运行时检查与实际数值对照。

## 每张表一行代表什么

| 安全视图 | 行粒度与键 | 主要用途 |
| --- | --- | --- |
| orders | 一个订单，order_id | 状态、订单创建/发货/送达/退货时间 |
| order_items | 一个商品行，id；order_id关联订单 | 商品行成交价及商品归属；不是一行一订单 |
| products | 一个商品，id | 品类、品牌、部门与默认配送中心 |
| inventory_items | 一个库存单元，id | 入库、售出、当前未售；不是商品总库存快照时间序列 |
| users | 一个脱敏用户，id | 注册时间、国家、州、城市、注册来源 |
| events | 一次行为事件，id | 事件数、session去重、浏览器/来源/行为；保留匿名事件 |
| distribution_centers | 一个配送中心，id | 库存仓网分组；不等于订单实际发货中心 |

关联键只允许内部关联/去重，不能输出用户ID、邮箱、地址、SKU或精确坐标。
查看完整列含义请读Catalog，避免再复制一份易漂移的表列清单。

## 20个指标需要确认的公式

| 指标 | 口径 | 时间归属 |
| --- | --- | --- |
| completed_sale_amount | Complete订单商品行sale_price之和 | orders.created_at |
| completed_item_count | Complete订单商品行数 | orders.created_at |
| completed_order_count | Complete订单数 | orders.created_at |
| average_order_value | 先按Complete订单求金额，再求每单均值 | orders.created_at |
| average_items_per_completed_order | 先按Complete订单计商品行，再求每单均值 | orders.created_at |
| cancelled_order_count | Cancelled订单数 | orders.created_at |
| returned_order_count | Returned订单数 | orders.created_at |
| average_fulfillment_days | Complete且合法非空的送达−创建时长均值 | orders.created_at |
| average_dispatch_days | 合法非空的发货−创建时长均值 | orders.created_at |
| average_transit_days | 合法非空的送达−发货时长均值 | orders.created_at |
| average_post_delivery_return_days | 合法非空的退货−送达时长均值 | orders.created_at |
| return_rate | Returned/(Complete+Returned)，不是所有订单作分母 | orders.created_at |
| completed_customer_count | Complete订单的去重user_id | orders.created_at |
| received_inventory_unit_count | created_at非空库存单元数 | inventory_items.created_at |
| sold_inventory_unit_count | sold_at非空库存单元数 | inventory_items.sold_at |
| current_unsold_inventory_unit_count | sold_at为空的快照库存单元数 | 仅全部快照，不做期间趋势 |
| average_days_to_sale | 合法非空的售出−入库时长均值 | inventory_items.sold_at |
| event_count | 事件行数，包含匿名 | events.created_at |
| unique_session_count | 事件session_id去重，不输出session明细 | events.created_at |
| registered_user_count | 注册用户行数 | users.created_at |

每个指标允许的分组以Catalog为准。例如订单级客单价没有“按商品品类”的分摊定义，不能擅自编造。
入库数和售出数按不同时间归属，即使同一个月份也不能把二者当作同一批库存。

## 本轮验证方式

先用 `scripts/check_workspace.py --database` 核对配置、实际列、角色，再运行新编60个在线开发问题。
这些问题不来自模型protected test。可计算查询使用单独编写的参考SQL对比已保存CSV，
覆盖所有20指标以及分组、时间、下钻、撤销、图表、无结果和安全边界；记录首次运行失败，
不以同题反复采样取最好一次计分。结果更新到仓库验收记录，原始SQL/SSE/CSV/截图留在仓库外。

实际结果见[60题报告](../../evals/results/thelook_product_queries.md)：首轮复核58/60，真实修复
两项已定向验证；额外重复城市题仍有一次漏分组而被拦截，保留稳定性缺口。
