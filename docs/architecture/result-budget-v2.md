---
title: Result Budget Contract v2
status: implemented
date: 2026-10-12
last_updated: 2026-10-14
---

# Result Budget Contract v2

在线结果预算已完成：按查询的结果形态决定可返回行数，完整结果存工件，浏览器分批查看，模型只读摘要。底层扫描百万订单和最终返回十万组是两种资源问题，不能都用全局 1,000 行限制替代。

## 三种模式的实际职责

| 模式 | 当前实现 | 完整性与权限 |
| --- | --- | --- |
| `aggregate` | Catalog 准入的指标 SQL，经 QueryPlan、Policy、只读 Runner、ResultValidator | 返回完整聚合或明确 Top-N；超出合同不把截取结果当答案 |
| `detail_page` | 浏览现有已验证 CSV 工件的一页，不重新执行 SQL | 默认 200 行，最多 500；行位置 cursor 固定在不可变工件中，用户/会话/workspace/版本隔离 |
| `export` | 用户点击现有鉴权 CSV 下载接口，服务端 FileResponse 返回完整工件并记录导出审计 | 下载已经通过验证的结果，不重新执行查询，文件不进入模型上下文 |

本版本不开放任意原始订单/客户明细 SQL，也不建立异步导出任务平台。`run_sql` 只执行 aggregate；分页和下载是独立的工件读取接口，复用现有认证和审计。

## 预算与完整性

| 数值 | 默认策略 | 作用 |
| --- | --- | --- |
| `execution_max_rows` | 在线资源兜底 100,000，可配置 | 防止服务保留无限结果，不是每条业务查询的配额 |
| `aggregate_result_rows` | 按维度基数 × 时间桶数，结合可用事实行数边界、明确 Top-N 派生 | 单个请求应有多少组；标量为 1，州分组最多 27 等 |
| `statement_timeout_ms` | 5,000，可配置 | PostgreSQL statement timeout；执行与读取另有相同墙钟预算，逐批检查 |
| `result_max_bytes` | 32 MiB，可配置 | 读取时估算 JSON UTF-8 字节并拒绝超界；不是精确 Python RSS 预算 |
| 前端初始预览 | 200 行 | 不把完整结果一次塞入组件 |
| 工件分页 | 默认 200，硬上限 500 | 每次仅读取/返回一页，稀疏 CSV 索引避免深页逐行跳过 |
| 模型摘要 | 最多 8 行 / 1,200 字符 | 样例用于解释与追问，不代表完整统计明细 |

例如“2017 年按日统计各州有效订单数”最多 365 × 27 = 9,855 组，不再因超过 1,000 被预检拦截。“按客户城市统计有效订单数”使用当前 Olist 快照的 4,119 城市边界，不能擅自改成前 20 名；用户明确说“前五名”才使用 Top-N=5。其他 workspace 使用自己的 DimensionBinding 基数配置。

在线 SQL 的顶层模型附加 LIMIT/OFFSET 会被完整聚合模式去除，再由服务端计划应用资源上限或明确 Top-N；内部 CTE/子查询 LIMIT 不被改写。这不证明内部 SQL 语义正确，指标粒度和条件仍需要合同与语义审核。没有预算 envelope 的旧离线调用保持原策略，不重写冻结 Prompt、历史数据和评测成绩。

Runner 使用 PostgreSQL named cursor，每批最多 1,000 行；资源 LIMIT 多读一行区分“正好达到合同”与“确实溢出”。探测行不进入 DataFrame、CSV、图表或摘要。如果确实溢出，返回 `needs_clarification`，不返回部分成功。字节/读取时间超界同样拒绝，不调用模型修复资源问题。

`complete` 表示完整返回且预览未截短；`display_truncated` 表示查询完整、预览只显示部分；`detail_page` 表示一页工件。`result_cardinality_exceeded` 是聚合超界的诊断状态，不能作为正常答案。未实现的异步 `export_pending` 不作为当前能力宣称。

## 实际执行链与代码

```text
Router / QueryPlan：确定指标、维度、时间、明确 Top-N、结果预算
→ QueryPlan 与 ResultContract 携带同一服务端预算
→ SqlPolicy：AST、表/列白名单、敏感投影和本请求 LIMIT
→ PostgreSQL reader：named cursor、分批、时间/字节/行数边界
→ ResultValidator：列、时间、数值、粒度与完整性
→ 完整 CSV 工件 + 200 行 Presenter + 8 行/1,200 字符摘要
→ 工件分页或鉴权 CSV 下载（不调用模型、不重跑 SQL）
```

- `query_plan.py:preflight/from_selection`：估计结果基数并派生请求预算。
- `result_budget.py`：预算 DTO、配对和资源兜底检查；客户端不能扩大权限或预算。
- `sql_policy.py:evaluate`：保留安全检查，应用完整聚合/Top-N 规则。
- `postgres_runner.py:read_bounded_rows/_run_sql_sync`：分批读取、完整性及资源边界。
- `result_validator.py:validate`：完整 aggregate 超界拒绝；旧离线 degraded 语义不变。
- `result_artifact_store.py:save_result_csv/read_csv_page`：每 200 行索引与稳定页读取，旧无索引工件仍可读取。
- `examples/trusted_olist_web_demo.py`：鉴权 `/artifacts/{id}/rows` 和 `/result.csv`。
- `examples/embeddable_copilot_shell.html`：更多 → 分页查看完整结果；当前范围、总行数和翻页操作。

分页和下载都要求当前登录用户拥有会话及当前 workspace/version 的工件，并验证文件完整性。完整 CSV 内部保存不等于开放 raw SQL、隐藏敏感字段或未鉴权下载。

## 验证与边界

百万订单的生产 Runner 实测完整返回 1 行汇总、1,095 行日统计和 100,000 行分组；明确 Top-5 返回 5 行；合同/字节超界拒绝。10 万行工件首尾分页核对一致。完整证据见 [性能记录](result-budget-v2-performance-baseline.md)。

这是一轮本机功能/资源验证，不是并发压测或生产 SLA，也不证明模型 SQL 语义准确率。EXPLAIN 成本门、任意敏感明细查询和异步大型导出未实现，本轮不再为这些新增前置任务。后续回到四周代码学习计划。
