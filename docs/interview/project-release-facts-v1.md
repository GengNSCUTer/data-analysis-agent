# 面试项目事实与贡献

更新于2026-10-20。此页汇总能由代码和报告支持的说法，不代替用户亲自审阅代码与复述。
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
| 结果呈现 | `result_artifact_store.py`、`result_presentation.py`、`chart_contract.py` | 同一验证工件用于预览/分页/CSV与服务端图表，隐藏行不进入模型摘要 |
| 接库与身份 | `runtime_workspace.py`、`local_auth.py`、`server.py` | 工作区配置、Catalog和权限、服务端身份与来源绑定嵌入令牌 |
| 运行与交付 | `runtime_control.py`、`deployment.py`、`.github/workflows/tests.yml` | 单进程容量、取消、生命周期、wheel及合成数据初始化、分层 CI |

代码路径均相对 `src/data_analysis_agent/`，最后一行工作流除外。阅读顺序和提问仍由
[四周学习计划](../../plan/process-agent-internship-improvement-v1.md)与
[代码学习记录](../learning/agent-project-walkthrough-v1.md)管理，本页不标记已经掌握。

## 已有证据能证明什么

| 范围 | 实际结果 | 正确解释与来源 |
| --- | --- | --- |
| Olist 在线开发场景 | 60题；使用定向复测后的混合证据56/60，独立数值23/24 | 不是修复后完整重跑准确率；半年度比较、模糊问题等仍有缺口，见[报告](../../evals/results/product_release_dev_v1.md) |
| TheLook 在线新场景 | 原全量复核58/60，两项定向成功；50次新非空查询的混合数值证据一致 | 额外重复城市查询仍出现最终分组遗漏，不能称稳定60/60，见[报告](../../evals/results/thelook_product_queries.md) |
| 部署与恢复 | 新venv、wheel离仓库导入、独立PG18.3初始化/重启，含285行完整工件恢复 | 无训练框架；Docker配置已检查，但本机未实际运行容器，见[部署证据](../deployment.md) |
| 单实例容量 | 受控provider每调用100ms；1/5/10并发各20次通过，整轮P95约0.87/1.80/2.98秒 | 真实在线模型另测约14/17秒；不是公网吞吐或生产SLA，见[报告](../../evals/results/runtime_capacity.md) |
| 离线1.5B SQL-only LoRA | 6,000 train / 1,500 validation；两个750条留出集 ordered Gold为403/750、396/750 | 两个Schema均在训练中；未证明未见Schema泛化，也未接入在线默认路径，见[模型说明](../post-training/experiments/project-v1-model-baseline.md) |
| 后续运行时输入对齐实验 | 1,500固定分母，1,453条可生成输入，发现704条Olist时间端点与旧Gold不一致 | 749条语义一致子集的诊断结果不能宣称全量100%，也不据此构造DPO负例，见[r3审计](../post-training/experiments/runtime-candidate-matching-v2-r3.md) |
| 本轮交付回归 | 纯测试、真实PG集成、受控模型的真实Chromium分别记录 | 不调用付费模型、不重跑protected final；见[交付报告](../../evals/results/product_handoff.md) |

在线产品默认使用 SiliconFlow 的 `deepseek-ai/DeepSeek-V3.2`。旧材料中的V4-Flash是历史运行
记录。离线模型保留SFT、validation-best及Gold评测证据；DPO因缺少足够真实、可复核的错误pair
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
> 对业务口径使用独立SQL核对；另完成1.5B模型bf16 LoRA与validation-best、Gold对照研究。

具体数字只在能解释分母、条件和限制时加入。当前不声称任意数据库零配置接入、组织级行
权限、企业SSO、多进程协调、生产SLA、企业使用或稳定全题正确。接库仍需定义指标、关联
和权限；目前实测的是PostgreSQL的不同业务Schema，未证明其他引擎可用。

正式tag/release、外部开发者独立部署和商业授权审查尚未完成。真实数据/模型不随代码分发，
见[资产许可](../../THIRD_PARTY.md)。下一步按学习计划逐段审阅、复述和练习，不由本页自动启动训练。
