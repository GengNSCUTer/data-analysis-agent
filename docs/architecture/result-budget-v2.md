---
title: Result Budget Contract v2
status: frozen-design
date: 2026-10-12
last_updated: 2026-10-14
---

# Result Budget Contract v2

## 1. 目的

v1 已区分数据库执行 1,000 行、前端预览 200 行和模型摘要 8 行/1,200 字符，
但聚合结果与明细浏览仍受同一个全局输出行数上限。这个合同进一步拆开查询模式和
结果完整性要求：扫描百万条事实只返回一行聚合，与输出十万行明细是不同的问题。
普通明细的前 1,000 行不能被称为完整结果，高基数分组也不能无序截取后冒充完整统计。

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
- 新的异步、大结果 `export` 模式尚未启用，字节预算和任务生命周期需单独实现。
- 当前已有鉴权 CSV 接口 `/api/project/conversations/{conversation_id}/result.csv`，
  验证当前用户、workspace、工件完整性并记录导出审计，只下载已执行且验证过的有界结果。
  它不会重新执行无上限 SQL；通用 DataFrame 导出开关仍关闭。两者不能混为一项能力。

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

独立 `perf_shop` 的 10 万/100 万订单基线已经完成，见
[`result-budget-v2-performance-baseline.md`](result-budget-v2-performance-baseline.md)。
单次本机测量只指导设计，不直接推出生产阈值。

## 8. 2026-10-14 已接入的执行合同

本轮只接入模式/预算元数据和执行前一致性校验，尚未迁移聚合返回或实现分页/异步导出。
当前 Catalog/Router 准入的是指标聚合请求，服务端将其标为 `aggregate`；不会仅根据
用户或模型提交的 `query_mode` 启用新的模式。

`result_budget.py:ResultBudget` 是不可变 DTO。composition root 从 PostgreSQL settings
生成 aggregate 预算，Handler 清除客户端保留字段后，给 QueryPlan 和 ResultContract
传入同一对象。序列化的计划、工具 metadata 和 Agent Run 合同证据都带以下字段：

| 字段 | 当前默认值 | 实际含义 |
| --- | --- | --- |
| `query_mode` | `aggregate` | 当前只启用 Catalog 指标聚合 |
| `result_budget.contract_id` | `result-budget-v2` | 执行合同身份，独立于旧 SQL Prompt 版本 |
| `execution_max_rows` | 1,000 | 当前 runner 输出兜底上限，不是底层扫描行数 |
| `aggregate_result_rows` | 1,000 | 本阶段暂与执行兜底一致；后续由维度/结果合同派生 |
| `statement_timeout_ms` | 5,000 | 当前服务端数据库配置；没有提高超时 |
| `preview_rows` | 200 | 前端预览预算 |
| `llm_context_rows` / `llm_context_chars` | 8 / 1,200 | 模型确定性摘要预算 |
| `detail_page_rows` / `export_max_bytes` | null / null | aggregate 不携带未启用模式的预算 |

执行数值来自实际服务端配置，1,000/5,000 为默认值。在此迁移阶段，Runner 要求
计划预算、工具结果合同预算、runner 配置和 validator 的执行/预览配置一致；半缺失、
非法字段、模式不一致或配置漂移均在 Policy 和 reader 查询连接前拒绝。拒绝仍用应用
审计连接写 `query_audits` 的 `result_budget:...` 原因；它不是 SQL 语法问题，不触发模型修复。
`detail_page` 和 `export` 可以作为合同表示，但不能进入普通 `run_sql`。

旧离线调用同时不带两个预算时保持旧行为，浏览器不能借此降级：在线 Handler 每个
已准入 SQL 请求都会覆盖服务端预算。冻结的 `runtime-candidate-sql-v2` 构造器先核对
预算配对，再显式投影原 SQL 语义字段，预算不进入该 Prompt；对同一问题，新旧输入
字节与 SHA-256 完全一致。现有数据、模型和历史成绩无需因此重建。

下一小单元是 aggregate 的实际返回合同：区分完整聚合、明确 Top-N 与超出允许基数的
分组，在 `perf_shop` 验证后再替换全局兜底；本轮未解决高基数完整返回问题。
