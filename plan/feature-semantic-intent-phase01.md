# 自然语言意图入口：Phase 0 / Phase 1

目标：解除在线固定句式门槛，让模型理解问题后直接产生可校验的结构化意图。
输入：用户原问题、服务端角色可见 Catalog、版本有效的 WorkingMemory。
输出：SemanticIntent、精确 Catalog 切片、QueryPlan/ResultContract，以及可回读路由证据。

本轮包含：32条自然中文问题的旧链路基线；结构化语义解析；相对日期服务端运算；
角色/指标/维度/归属/执行预算校验；真实provider小批验证与Handler回归。
不启动后训练，不改历史离线Prompt或数据，不新增任意明细分析能力。

关键不变量：

- 在线生产配置启用SemanticIntent后，不再调用旧classify/admit或口语正则扩大检索。
- 解析失败返回可重试提示，不能退回同一组固定句式门槛，也不能默许SQL。
- 校验不静默丢弃非法指标、维度、筛选；模型不能授予身份/workspace/表列权限。
- 未经验证的摘要、任意metadata不作为SQL条件；上下文来自受控WorkingMemory。
- 查询形态直接读取已验证结构，不把模型输出拼成自然语言再正则解析。
- 当前Catalog外指标和未实现筛选明确澄清，不猜利润等业务定义。
- 保留Policy、reader角色、ResultValidator、Artifact和执行预算。

验收：同一批问题旧路由/新意图对照，实际模型调用与stub逻辑测试分开；
日期/权限/非法JSON/新问题清旧条件/追问继承/Top-N/图表/服务失败回归；
历史离线candidate协议测试仍通过。SQL正确率不由路由成功率推出。

## 2026-10-14 完成记录

Phase 0：`evals/cases/semantic_router_baseline_v1.yaml` 保存 32 条自然问法及追问状态。
旧 classify/admit 链路只允许其中 6 条进入数据库生成路径，结果保存在
`evals/results/semantic_router_baseline_v1.json`。这批用例包含定义、建议、拒绝与澄清，
不能期待所有题都查库，也不把准入数当准确率。

Phase 1：新增 `semantic_intent.py`，使用现有 DeepSeek-V4-Flash 提议结构化意图；
`chat_runtime.py` 的默认在线装配直接消费校验后的结构，绕开旧 classify/admit、
口语检索扩展、WorkingMemory 日期/比较正则及图表关键词匹配。相对日期由服务器
Asia/Shanghai 日历计算，新查询覆盖旧条件，追问继承由显式语义提案表达。
Catalog 切片使用精确指标 ID，QueryPlan/ResultContract/ChartContract 使用同一结构。
解析失败返回重试提示，不启用旧关键词回退。按日全时段查询服从可验证的结果预算，
不再因为没有手写日期就一律拦截。

旧语言路由仅保留给冻结离线协议和历史调用，不在默认产品入口执行；这不删除
SQL AST、角色/表列权限、指标归属、结果合同与资源预算。图表仍由服务器从验证
结果绘制，无第二次模型绘图工具调用。

### 实测证据

- 同批 32 条真实语义调用：32 条结构解析成功，20 条通过查询形态准入，2 条已有结果
  追问不查库；其余为定义、建议、拒绝或澄清。语义调用延迟中位数约 3.73 秒。
- 真实账号认证 + SSE Handler + PostgreSQL + 在线模型六场景全部通过：2017 年销售额、
  按州追问、继续、画曲线、指标口径、写库拒绝。前三条共用会话；“继续”使用上一轮
  验证结果且 SQL 调用数为 0；图表从验证工件自动渲染。
- 243 项 Python 定向回归通过，8 项需显式启用的数据库测试跳过；上述真实 HTTP
  smoke 单独覆盖实际数据库链。测试明确禁止语义路径调用旧 Router 和 Memory.apply。
- 定向 Ruff 与 diff check 通过。复跑脚本为 `scripts/evals/run_semantic_router_baseline.py`
  和 `scripts/evals/smoke_semantic_runtime.py`；完整在线证据不进入 Git。
- 现有 `data-analysis-agent-m4` 服务加载新代码，仍监听 127.0.0.1:32013；未改其他项目进程。

仓库外证据目录：
`/disk2/gengnan/data-analysis-agent-data/evals/semantic-intent-phase01-20261014/`。
最终语义对照为 `live-intents-final.json`，最终 HTTP 验收为 `runtime-smoke-final.json`，
网页服务日志为 `web-service.log`。

### 已知限制与下一小步

这不是在线 SQL 准确率评测。真实语义模型仍有过度澄清/多选维度：例如“各个州哪些
地方卖得好”被额外理解成城市分组；SP 州筛选有时仍追问买家/卖家归属。新增语义调用
也增加延迟，本轮不宣称性能提升。

指标仍来自已定义 Catalog；利润等缺失口径不编造，额外筛选当前只编译单客户州，
任意明细和复合筛选尚未开放。比较请求能返回涵盖两个期间的序列，不能据此宣称已实现
通用增长率、归因或异常算法。下一步先由用户体验当前入口，再按具体错误改进；继续
Day 1 时只审阅 SemanticIntent 与验证函数，不自动开始新训练或大批评测。
