# 在线业务任务质量优化：2026-11-09

当前处于Phase 1，尚未形成全量业务成功率。主计划见
[业务质量优化计划](../../plan/process-agent-business-quality-v1.md)。不启动后训练，不修改业务事实数据。

## 已完成的准备

- 新增60项独立业务任务：Olist、TheLook各30项，基础查询、公式/JOIN、比较、连续追问、历史复用、解释展示六类各5项；连续追问每项4–5轮。
- 独立参考由原问题及业务定义编写，复用部分已有人工SQL；不读取实际QueryPlan或训练renderer。全部参考已在两套只读数据库上执行成功。
- 新入口走真实登录、Web Component、SSE、SQL与完整CSV；记录每轮实际回答、来源、状态、图表、模型调用、耗时、token和未知费用。检查图表实际序列和分页数据。
- 评测器验证错配实体金额、行数冒充金额、时间桶、精度、NULL/NA、数字形状维度和任务完成判定。自动检查通过仍需正文语义复核，不把pending算通过。
- 相关回归78通过、2跳过，其中新评测器19项通过；Ruff与diff检查通过。CI已纳入新测试。该数量不是业务题通过率。
- 随后补充“分区Top-N的当前上限”负例后，评测器20项通过；旧采集错误可离线重审，不重跑模型。工具提交5c1e01b的Actions运行42586731798，build与postgres-and-browser均通过。

## 12项试跑结果与评测器修正

12项试跑覆盖两套库各类别一项，共24轮，已执行完并逐项审阅正文，3项通过、9项未完成。
证据位于仓库外`evals/agent-business-pilot-20261109/`，日志为同名`.log`。
这次试跑用于检查评测与采集，不与后续全量成绩混算。

通过项为Olist商品成交额、Olist切换任务后回看销售排行差额、TheLook完成订单客单价。
这些结果只描述本次试跑，不代表线上总体准确率。

| 已观察问题 | 实际证据与归因 | 后续阶段 |
| --- | --- | --- |
| 错增长率被放行 | 2017/2018有效订单44379/53532，差额9153；正文用53532作分母写17.1%，正确基期应44379，增长约20.62%。CSV正确不代表回答正确。 | Phase 4 |
| 解释核验降级 | Olist客单价、TheLook年度比较/州排行等数据可用，但正文未完成核验。 | Phase 4 |
| 首轮超时带来连续追问失败 | 读trace可见模型首字节120秒超时；没有最高州，后续实体指代失去依据。不是把每个后续失败都归因成独立记忆错误。 | Phase 3/6 |
| Top10改Top5未完成激活 | TheLook本地计算5行已产生，QueryPlan重新绑定customer_state时Catalog selection缺字段，抛ValueError；输出工件未提交，默认下载仍指向旧10行，显式新ID为404。 | Phase 3/5 |
| 已有数据解释被误分为通用问题 | “概括差异、给建议”走general_business，脱离刚才的数值并要求再提供条件。 | Phase 3/4 |
| 不存在的旧排行比较未友好收口 | 首轮排行超时后，旧结果比较读到了当前订单结果并触发行号越界异常；实际SSE为失败状态卡，不能把会话最后一条旧正文当作本轮模型回答。 | Phase 5 |

评测器采集问题也已分开纠正：Plotly压缩数组从实际渲染的_fullData读取；派生下载明确绑定
output_artifact_id；正文从本轮SSE采集，数据库最近消息只作持久化证据。以上修正不改产品数据。

评测器曾把合法`EXTRACT(YEAR)`输出的`2017.0`当日期字符串解析失败，已补年份桶归一测试。
通过`--review-directory`重新核对保存的CSV，无需重新调用模型；原始失败文件不覆盖。

## 正在执行的全量

全量清单共60项、122轮。修正California输入后在
`data-analysis-agent-business-full-r2` screen运行，证据为仓库外
`evals/agent-business-full-20261109-r2/`，日志为同名`.log`。每次启动保存本次实际tasks.json。
此前部分运行保留于`agent-business-full-20261109/`，附有停止原因，不混入全量成绩。
当前两个业务服务保持原实现，尚未改线上规则或解释生成，以免污染初次质量测量。
全量结束后的12项×3次稳定性检查由`data-analysis-agent-business-stability-wait`等待脚本接续；
仅当全量report确有60项后才运行。脚本及独立日志在仓库外，不增加产品后台任务。

## 复核入口

```bash
PYTHONPATH=src python scripts/evals/run_agent_business_tasks.py --audit-references
PYTHONPATH=src python -m pytest -q tests/test_agent_business_tasks.py
PYTHONPATH=src python scripts/evals/run_agent_business_tasks.py --review-directory /absolute/external/run
```

后续依次完成试跑复核、60项全量、12项各3次稳定性检查和失败优先级，再进入具体修复。
本文件不以本地测试数量代替业务成功率，不将定向重测拼成一次完整运行。
飞书项目文档已同步本轮试跑事实（revision 321），该次同步时相关回归为77项，随后补非有限数字负例为78项。
