# JOIN 诊断与金额来源检查

日期：2026-11-04。范围是在线候选 SQL 的执行前来源约束、同快照 JOIN 诊断、结果准入与观测。
不使用确定性 Gold renderer 判断在线 SQL，也没有证明任意 SQL 与 Catalog 公式语义等价。

## 实现与职责

`join_evidence.py`只遍历最终结果依赖的作用域，忽略未被使用的CTE。普通诊断用事实行主键
检查重复输入；事实身份缺失、未知窗口/聚合和无法合并的派生来源标为未覆盖，不再当成安全证据。
无JOIN直接返回not_applicable，避免无关的资格化开销。

| 状态 | 含义与处理 |
| --- | --- |
| checked | 生成的诊断均已执行且没有发现重复或分摊超额；仅针对诊断覆盖的关系 |
| amplified | 实际探针发现重复输入或分摊权重超额，ResultValidator拒绝 |
| partial | 存在未覆盖项；若SUM跨来源且无法沿唯一键证明保留行身份，则拒绝；其余保留状态并继续 |
| not_applicable | 本SQL没有需要诊断的聚合，例如无JOIN或COUNT DISTINCT |

`partial`风险判断检查JOIN完整主键、单行聚合和CTE最终分组键，不把普通订单级预聚合再关联维度
一刀切拒绝。不认识的复杂SUM不冒充已验证；这里只证明行数保持边界，不证明指标公式。
正常Catalog元数据存在但主键字典为空时仍诊断，不能因空字典跳过未识别身份的查询。

支付占比分摊仅在计划明确payment_share且有声明的金额来源时启用独立探针：针对可识别的
乘除表达式，按事实完整主键汇总权重，在所有实际结果分组一起检查是否超过1（容差1e-7）。
只返回违反事实数与检查事实数，不返回敏感键。合法分摊与额外CROSS JOIN复制两倍均有真实PG
回归。它不验证欠分配、负权重抵消或所有分摊公式；更复杂表达式仍partial，不自动豁免。

`ResultContract`从Catalog声明的可相加amount指标推导单一数值度量来源，放入仅在线预算元数据
的metric_value_sources。`TrustedRunSqlTool`在原候选和一次修复候选上调用PlanSqlGuard的输出
lineage检查：限定实际金额来源和SUM聚合，不接受payment_value改名gmv、CTE别名洗列、常数、
无关CTE/WHERE中出现price、混入其他金额或改用MAX/SUM DISTINCT。条件SUM的判断表达式和
窗口分区不算新的金额来源，避免误伤。非唯一金额来源的复杂指标不自动推导来源规则；历史
无预算离线元数据不变。

这不是完整表达式证明：SUM(price)*2、遗漏状态筛选、复杂不可解析lineage、任意加权/空值
处理仍可能超出本次检查。没有据此声称所有口径一致或通过就必然业务正确。

## 成本与可复核观测

Runner将建探针AST、策略审查、执行/读取纳入join_evidence.elapsed_ms，统计planned_checks、
executed_checks、covered_aggregates、uncovered_aggregates和coverage_ratio。覆盖率按去重后的
探针/未覆盖单元计，不是全部SQL语义覆盖率；发现放大提前停止时，未执行探针不计已覆盖。
超时、策略/数据库失败仍保存partial、成本和rejection_reason。探针继续使用原reader、同一个
REPEATABLE READ事务及主查询剩余deadline，不新增权限或独立时间预算。

请求trace的join_evidence_attempts保留每次重试的诊断，不只保留最后一次。
summarize_join_evidence汇总状态、覆盖、拒绝率和耗时；误拒绝率只能由独立expected_safe标签
计算，线上未标注数据返回null，不能把“没投诉”当作无误拒绝。

复跑入口（会读取业务库并写现有查询审计，不调用LLM）：

```bash
PYTHONPATH=src python scripts/evals/verify_join_diagnostics.py \
  --output /absolute/path/outside-repo/diagnostics.json
```

25条受控SQL包含Olist的5条查询和TheLook60题中的20项指标总量。TheLook两条含敏感内层键的
FROM子查询改成同算术的具名CTE，以满足已有安全Policy；原参考题文件不改。此实验评估诊断
准入，不重新评估中文理解、自然回答或逐条业务数值准确率。

最终执行25/25准入；checked=5、partial=3、not_applicable=17。8个需诊断单元覆盖5个，
覆盖率62.5%；25条独立标注关联安全SQL未被诊断误拒绝（0/25），不是生产误拒绝率，也不是全部指标口径正确率。
平均诊断耗时46.519ms，P95 362.338ms；包括无需探针的not_applicable样本，不包含模型耗时，
不是容量压测、速度提升或SLA。原始报告：
`/disk2/gengnan/data-analysis-agent-data/evals/join-catalog-review-20261104/diagnostics-final.json`。

## 验证范围

确定性测试覆盖真实来源、别名/CTE/UNION/无关子查询、保留CASE/FILTER合法条件、结果状态及
超时/数据库失败观测。真实PG覆盖普通关联、重复输入、缺身份、正常预聚合、合法与重复分摊，
以及支付金额冒名的工具链拒绝。受控修复provider验证原候选和修复候选遵守同一金额规则。
修正一条旧测试：Top5返回行合计应比较前五州总和，不能在真实27州数据上比较全国总和。

没有修改已有在线模型或重启服务，没有新跑真实LLM/浏览器问答；SQL控制层与真实PG通过
不等价于端到端回答准确率。新增诊断失败测试加入已有无凭据CI。

最终联合回归271项通过（含上述真实PG用例）；定向Ruff、diff检查通过。复跑命令：

```bash
RUN_PROJECT_DB=1 PYTHONPATH=src python -m pytest -q \
  tests/test_join_evidence.py tests/test_join_runtime_evidence.py \
  tests/test_plan_sql_guard.py tests/test_semantic_catalog.py tests/test_query_plan.py \
  tests/test_result_validator.py tests/test_trusted_sql_tool.py tests/test_sql_policy.py \
  tests/test_postgres_runner.py tests/test_query_filters.py \
  tests/test_runtime_workspace.py tests/test_workspace_onboarding.py
```
