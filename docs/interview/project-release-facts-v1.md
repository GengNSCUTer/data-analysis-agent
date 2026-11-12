# 面试项目事实与贡献

更新于2026-11-12。此页汇总能由代码和报告支持的说法，不代替用户亲自审阅代码与复述。
在线产品、离线模型、工程回归分别计量，不拼成一个“准确率”。

## 如何介绍这个项目

面向电商经营分析开发了可嵌入业务网页的分析 Copilot。用户用中文连续提问，系统结合
Catalog、数据库结构和当前分析状态理解需求，生成候选 SQL，经计划校验、AST 策略和
PostgreSQL 只读执行后生成表格、图表、结论与数据依据。支持会话恢复、可信结果下钻、
后台摘要、分页下载，以及单实例的排队、取消和超时。

Vanna 提供 Agent、工具注册、HTTP/SSE 和 Web Component 底座；本项目的工作集中在下表。
并非从零实现了整个框架，也不是简单调用一次模型 API。

| 贡献 | 代码入口 | 关键职责 |
| --- | --- | --- |
| 业务理解与计划 | `semantic_intent.py`、`query_plan.py`、`chat_runtime.py` | 模型理解自然语言，代码校验可用指标、维度、时间和结果要求；不再用旧关键词入口决定能否查询 |
| 执行边界 | `plan_sql_guard.py`、`sql_policy.py`、`postgres_runner.py`、`trusted_sql_tool.py` | 候选与计划对照、AST、受限角色、取消/超时、结果校验及审计 |
| 连续分析 | `conversation_store.py`、`analysis_state.py`、`analysis_context.py`、`execution_memory.py` | 会话事务、分析状态、token历史、后台增量摘要和可信结果引用；摘要不授权 SQL 条件 |
| 结果呈现与解释 | `result_artifact_store.py`、`result_presentation.py`、`result_explanation.py`、`chart_contract.py` | 同一验证工件用于预览/分页/CSV和图表；服务端计算可信证据，模型自由组织正文，代码检查及按疑点复核 |
| 历史结果复用 | `artifact_history.py`、`artifact_coverage.py`、`execution_memory.py` | 自动检索同会话候选、重新核验归属和覆盖；充分时直接复用或本地计算，不靠聊天中的数字作比较 |
| 接库与身份 | `runtime_workspace.py`、`local_auth.py`、`server.py` | 工作区配置、Catalog和权限、服务端身份与来源绑定嵌入令牌 |
| 运行与交付 | `runtime_control.py`、`deployment.py`、`.github/workflows/tests.yml` | 单进程容量、取消、生命周期、wheel及合成数据初始化、分层 CI |

代码路径均相对 `src/data_analysis_agent/`，最后一行工作流除外。阅读顺序和提问仍由
[两周学习计划](../../plan/process-agent-internship-improvement-v2-2week.md)与
[代码学习记录](../learning/agent-project-walkthrough-v1.md)管理，本页不标记已经掌握。

## 已有证据能证明什么

| 范围 | 实际结果 | 正确解释与来源 |
| --- | --- | --- |
| 当前在线业务任务 | 两库60项122轮完整通过60/60，Olist与TheLook各30/30 | 2026-11-11固定14ab842和官方deepseek-flash连续全量运行；非选择性拼分，助手语义审阅非第三方盲审，见[收尾报告](../../evals/results/agent_business_quality_closeout60_20261111.md) |
| 独立结果与解释对照 | 122/122轮CSV独立对照，19/19份图表重放；115轮正文accepted、7轮仅换图 | 包含范围、形状、数值和正文核对；96次复核、2次修订、213次解释调用，不宣称成本下降或任意业务语义已证明 |
| 历史复用 | 34轮DIRECT_REUSE/LOCAL_COMPUTE均0业务SQL，完整任务通过 | 同会话授权、版本、TTL、完整性及覆盖检查；显式双工件比较各自重新取证，不提供任意多结果JOIN |
| 部署与恢复 | 新venv安装wheel、独立PG18.3、300行独立对照、分页下载/重启及数据库与工件联合恢复至新库新目录 | 同集群已有角色，不是集群灾难恢复；Docker实运行未验证，见[部署](../deployment.md)及最终报告 |
| 单实例容量 | 受控provider每调用100ms；1/5/10并发各20项通过，整轮P95约1.02/2.00/2.99秒 | 最新真实60项整轮P50 5.01秒/P95 6.77秒；不同条件分列，均不能宣传生产SLA |
| 离线1.5B SQL-only LoRA | 6,000 train / 1,500 validation；两个750条留出集 ordered Gold为403/750、396/750 | 两个Schema均在训练中；未证明未见Schema泛化，也未接入在线默认路径，见[模型说明](../post-training/experiments/project-v1-model-baseline.md) |
| 后续运行时输入对齐实验 | 1,500固定分母，1,453条可生成输入，发现704条Olist时间端点与旧Gold不一致 | 749条语义一致子集的诊断结果不能宣称全量100%，也不据此构造DPO负例，见[r3审计](../post-training/experiments/runtime-candidate-matching-v2-r3.md) |
| 本轮交付回归 | 纯测试、真实PG集成、受控模型的真实Chromium分别记录 | 不调用付费模型、不重跑protected final；见[交付报告](../../evals/results/product_handoff.md) |

在线产品当前使用官方DeepSeek的 `deepseek-flash`，OpenAI兼容接口，关闭thinking。
旧材料中的SiliconFlow模型是历史运行记录。离线模型保留SFT、validation-best及Gold评测证据；DPO因缺少足够真实、可复核的错误pair
暂停，不能写成已经完成偏好优化。

## 三个可以深入讲的实际问题

1. **多轮追问不只是把聊天拼长。** “哪个州最高→这个州哪些品类好→前十名”需要从可信结果
   解析实体，并单独继承/撤销分析状态；叙事摘要只能帮助理解，不能替代执行事实。
2. **SQL可执行仍可能算错。** 库存与订单行Join会放大计数，城市查询可能漏最终分组。
   需要关联基数、明确指标口径和独立数值对照；仅检查列名或能执行不足以证明答案正确。
3. **前端停止不等于SQL停止。** 取消协程或 `to_thread` 不能自动终止数据库工作，必须把取消
   传至实际连接并等线程/事务结束，再释放执行槽；否则页面已停，后端资源仍在占用。

## 简历草稿与边界

> 基于Vanna、FastAPI/SSE与Web Component开发可嵌入式电商分析Copilot，构建Catalog驱动的
> 语义计划、分析状态/可信结果记忆及PostgreSQL受控执行链；通过配置接入Olist和TheLook，
> 完成中文连续分析、服务端图表、归属校验的分页下载、单实例排队/取消与wheel部署验证。
> 对业务口径使用独立SQL和真实多轮任务核对，分开记录数据正确性、完整交付、复用收益和失败边界。

具体数字只在能解释分母、条件和限制时加入。当前不声称任意数据库零配置接入、组织级行
权限、企业SSO、多进程协调、生产SLA、企业使用或稳定全题正确。接库仍需定义指标、关联
和权限；目前实测的是PostgreSQL的不同业务Schema，未证明其他引擎可用。

正式tag/release、外部开发者独立部署和商业授权审查尚未完成。真实数据/模型不随代码分发，
见[资产许可](../../THIRD_PARTY.md)。下一步按学习计划逐段审阅、复述和练习，不由本页自动启动训练。
