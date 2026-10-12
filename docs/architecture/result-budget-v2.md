---
title: Result Budget Contract v2
status: frozen-design
date: 2026-10-12
---

# Result Budget Contract v2

## 1. 目的

v1 把数据库执行上限、业务结果完整性、模型上下文和前端预览都近似成了同一个
`1,000` 行数字。这个合同把四类预算拆开，避免因为底层数据量很大就拒绝一个最终
只有几十行的安全聚合，也避免把明细查询的前 1,000 行误认为完整数据。

本合同是设计冻结，不会因为写入本文档就自动放宽现有在线 SQL policy。v1 的在线
默认执行上限仍保持 1,000 行；后续实现必须按本合同和回归测试逐步迁移。

## 2. 三种查询模式

每个由服务端生成的 `QueryPlan` 必须落入以下一个模式。模式由用户意图、Catalog、
结果形态和权限共同决定，不能由模型在 SQL 中自行选择。

### `aggregate`

用于标量、分组统计、时间序列和明确的 Top-N。

- 底层可以扫描很多事实行，但最终结果必须满足 `ResultContract` 的列和基数要求。
- 不因为底层扫描行数大而强制截断最终聚合结果。
- 普通分组需要声明允许的维度和最大展示基数。
- 高基数维度必须由用户明确指定筛选或 Top-N；不能无序截取前 N 组冒充完整结果。
- 超出结果合同进入 `result_cardinality_exceeded` 或 `needs_clarification`，不静默改写业务含义。

### `detail_page`

用于订单、商品、客户等明细浏览。

- 返回的是一页，不是完整数据集。
- 必须有稳定排序；大偏移量优先使用 keyset cursor。
- 页面默认预算是 200 行，单页上限暂定 500 行，最终值在性能基准后确定。
- 响应必须标明页码/cursor、当前行数和是否还有下一页。
- 模型只接收摘要和少量样例，不接收全部明细。

### `export`

只在用户明确请求下载，并且权限、workspace 和审计条件满足时启用。

- 导出是独立任务，不是普通 `run_sql` 的模型返回值。
- 完整结果写入受控 `ResultArtifact`，记录行数、字节数、schema、内容 hash、workspace、
  query hash 和 audit ref。
- 前端获得一次性或短期有效的下载引用；模型上下文永远不包含完整文件。
- 当前版本继续保持 `full_export_allowed=false`，直到下载鉴权和审计路由冻结。

## 3. 预算维度

| 字段 | 所属层 | 作用 | v2 初始策略 |
| --- | --- | --- | --- |
| `statement_timeout_ms` | 数据库执行 | 限制单次交互执行时间 | 继续沿用现有值，按模式拆分前先测量 |
| `estimated_scan_cost` | 执行预检 | 拦截明显全表扫描/笛卡尔积风险 | 通过 `EXPLAIN` 预检实现，阈值另行冻结 |
| `aggregate_result_rows` | 业务结果 | 限制聚合结果的可验证基数 | 由 ResultContract/维度合同决定，不使用全局 1,000 |
| `detail_page_rows` | 分页 | 限制单页明细大小 | 默认 200，硬上限暂定 500 |
| `export_max_bytes` | 导出 | 限制异步导出资源 | 实现导出路由前冻结 |
| `llm_context_rows` | 模型上下文 | 控制传给模型的样例行 | 继续使用有界摘要，默认最多 8 行 |
| `llm_context_chars` | 模型上下文 | 控制摘要文本长度 | 继续使用 1,200 字符初始值 |
| `preview_rows` | 前端展示 | 控制一次渲染数量 | 固定 200 行 |

行数不是唯一的性能指标：一百万行可以聚合成一行，一个包含很长文本的几百行结果也
可能超过字节预算。后续实现必须同时记录 row count、byte size 和 elapsed time。

## 4. 截断和完成状态

结果状态必须区分以下含义：

- `complete`：业务结果按合同完整返回；
- `display_truncated`：查询完整，前端只展示部分预览；
- `detail_page`：当前是分页结果，可能还有下一页；
- `execution_truncated`：执行层确实遇到了预算上限，结果不完整；
- `result_cardinality_exceeded`：聚合结果超过结果合同，不能当作完整答案；
- `export_pending` / `export_ready`：异步导出状态；
- `needs_clarification`：需要用户补充维度、Top-N、筛选或时间范围。

`display_truncated` 不得被解释为 `execution_truncated`。同样，SQL 自动补充的
`LIMIT` 不能把任意高基数分组变成合法的 Top-N。

## 5. 结果输出边界

一次可信查询分为三条输出通道：

```text
数据库执行结果
├─ ResultArtifact：完整、受权限保护的内部结果
├─ Presenter：最多 200 行前端预览
└─ LLM summary：最多 8 行/1,200 字符的确定性摘要
```

`ResultValidator` 负责验证结果是否满足列、时间、关联粒度和数值合同；它不负责
推断用户真正想问什么。模型可以解释可信摘要，但不能自行证明 SQL 的业务正确性。

## 6. 执行前检查

迁移到 v2 后，执行链路为：

```text
QueryPlan
→ SQL AST policy
→ ResultContract shape check
→ EXPLAIN 预检（成本/扫描/笛卡尔积）
→ PostgreSQL reader role + timeout
→ ResultValidator
→ ResultArtifact / Presenter / LLM summary
```

`EXPLAIN` 只判断资源风险，不替代指标口径、权限和结果语义校验。任何预检无法
可靠判断的情况都必须保守记录并交给执行后的确定性结果校验。

## 7. 当前迁移边界

本次冻结不做以下变更：

1. 不直接把在线全局 1,000 改成 10,000；
2. 不把 TheLook 或新的业务数据集接入默认在线 workspace；
3. 不开放未鉴权的全量 CSV 下载；
4. 不把完整结果送进模型上下文；
5. 不以性能夹具结果宣称模型语义能力提升。

下一步先用独立 `perf_shop` workspace 验证 10 万到 100 万级数据的聚合、分页、
高基数分组和资源风险，再依据测量结果实现 `QueryPlan` 的执行模式字段和分层预算。
