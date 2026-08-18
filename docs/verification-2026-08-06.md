# 2026-08-06 嵌入式可靠性与延迟验证

本记录对应 `/embedded-demo` 四项反馈的修复回归，代码基于仓库内 Vanna Web Component 和
Trusted Olist PostgreSQL Demo。Olist 只是当前工作区适配器与展示案例；本记录中的链路设计
不依赖某个数据集名称。

## 变更范围

1. **窗口布局同步**：宿主页在最小化恢复、拖拽、八方向缩放和浏览器窗口变化后，通过双
   `requestAnimationFrame` 等待 Lit/Shadow DOM 布局稳定，再写入 `--vanna-chat-height`；
   `ResizeObserver` 负责捕获宿主尺寸变化。桌面端四边和四角都可拖拽，窗口位置/尺寸只在
   有效的 normal 状态持久化，避免最小化的 `0×0` 尺寸污染恢复缓存。
2. **非数据库定义问题路由**：仅询问指标定义或统计口径（例如“GMV 是什么”）时，
   `QuestionRouter` 从 Semantic Catalog 生成 Markdown 说明，终止原因是
   `catalog_answered`，不调用 LLM、SQL 或工具。包含“概览/统计数值”等数据意图的问题仍
   进入受控 Text-to-SQL 链路。
3. **历史 Markdown**：新增共享的安全、有限 Markdown renderer。它先完整转义模型文本，
   再渲染标题、表格、粗体/斜体、列表、引用、分割线和 fenced code；表头/分隔线和数据行
   之间允许模型常见的空行；历史 assistant 消息与流式文本使用同一实现，用户消息保持纯文本。
4. **SQL Policy 边界**：补充多 CTE 的显式输出列/外层别名识别；无法证明 CTE 输出列时保守
   拒绝；scalar subquery 单独校验，避免关联键被误判为外层敏感投影，同时继续拒绝原样
   投影敏感 ID。
5. **延迟证据**：在现有 `agent_runs.catalog_trace` JSONB 下增加受限的阶段聚合耗时，记录
   `route_catalog`、`llm_request`、`sql_policy` 和 `postgres_sql`，不新增数据库表字段。

## 验证命令与结果

```bash
# 后端专项
source /disk2/gengnan/miniconda3/etc/profile.d/conda.sh
conda activate data-analysis-agent
PYTHONPATH=src:. pytest -q \
  tests/test_sql_policy.py \
  tests/test_sql_repair.py \
  tests/test_question_router.py \
  tests/test_budget.py \
  tests/test_working_memory.py \
  tests/test_trusted_sql_tool.py \
  tests/test_trusted_routes.py
# 63 passed, 1 skipped

# 嵌入式浏览器回归（服务已运行在 127.0.0.1:32010）
PYTHONPATH=src:. RUN_VANNA_E2E=1 VANNA_E2E_BASE_URL=http://127.0.0.1:32010 \
  RUN_PROJECT_DB=1 pytest -m integration -q \
  tests/e2e/test_trusted_embedded_window.py
# 9 passed

# 前端与 Python 静态检查
cd frontends/webcomponent && npm run build
# 38 modules transformed; build succeeded
ruff check src/data_analysis_agent tests/test_sql_policy.py tests/test_sql_repair.py \
  tests/test_question_router.py tests/test_budget.py
python -m compileall -q src/data_analysis_agent examples/trusted_olist_web_demo.py
git diff --check
# all succeeded
```

## 真实链路观测

问题：`概览 GMV、有效订单数、平均履约天数和好评率，并说明统计口径`。

代表性成功请求（`req-multi2-1785988777202`）的台账观测：

| 阶段 | 次数 | 总耗时 | 说明 |
| --- | ---: | ---: | --- |
| `route_catalog` | 1 | 17 ms | Catalog 检索与路由，不是主要瓶颈 |
| `llm_request` | 2 | 77,233 ms | 首轮约 63.5 s，最终回答轮约 13.7 s |
| `postgres_sql` | 1 | 387 ms | 受控 reader role 执行 |

请求总耗时约 77.7 s，最终结果包含四个指标列、指标口径、数据集版本、指标版本和 Catalog
版本。Olist 当前真实库的本次结果为：GMV `13,494,400.74`、有效订单 `98,207`、平均履约
天数 `12.558702`、好评率 `0.770680`。这些数值用于本地技术回归，不构成在线模型准确率
或业务生产 SLA。

纯定义问题“GMV 是什么”的代表性请求约 89 ms，`llm_rounds_used=0`、`tool_calls_used=0`、
`sql_calls_used=0`、`termination_reason=catalog_answered`。

完整 `tests/e2e/test_trusted_embedded_window.py` 当前为 **9 passed**，额外覆盖八方向缩放和
窄窗口长历史恢复；前端 `npm run build` 为 38 modules transformed/build succeeded。

## 限制与下一步

- 当前耗时证据是单次/少量本地观测，尚未形成 P50/P95、token 成本和失败率基线。
- SiliconFlow 模型仍可能因首轮上下文过长或服务端排队产生高延迟。后续的 24 条真实模型
  人工标签已完成，详见 `docs/evaluation.md` 与 `docs/verification-text-to-sql-v2.md`；该小样本
  仍不足以声称 P50/P95。下一步只针对已发现的 Catalog slice、合同后多余 SQL、币种和支付
  归因问题做固定回归，再决定 Prompt 压缩、超时、流式反馈或模型切换策略。
- 真实认证、组织/行级权限、第二个真实数据集和生产部署仍未完成。
