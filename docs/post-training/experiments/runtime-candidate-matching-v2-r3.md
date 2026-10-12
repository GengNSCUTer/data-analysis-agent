# Runtime Candidate v2 Base / Adapter Matching：r3 评测结果

## 运行范围

本次评测使用已经封存的 runtime-aligned 输入和已经完成的 Base / Adapter
生成物，没有重新生成模型输出：

- Olist：750 条，其中 736 条进入候选生成，14 条因 Router 合同拒绝；
- TheLook：750 条，其中 717 条进入候选生成，33 条因 Router 合同拒绝；
- 合计：1,500 条固定分母，1,453 条可生成输入。

输入、模型生成物和原始问题没有被修改。r3 只修复评测阶段对 PostgreSQL
statement timeout 的处理，并通过 `generation_contract` 明确复用了 r2 的
prepare/generation marker 和 raw completion。r3 合同 SHA-256 为
`6602344363eec518089045a868c8f9d9cc56ca5427347d87f754ecc0f01b0513`，复用的
r2 生成合同 SHA-256 为
`c639a1fd71368e7b2e10ce0af199052a72bb69cb36f6b3ec83ea6e0eea46408c`。

## 评测修复

原评测在一条候选 SQL 超过 5 秒 `statement_timeout` 后，把 PostgreSQL
`QueryCanceled` 当作数据库连接基础设施故障，导致整轮中止。现在
`execute_sql()` 对 `QueryCanceled` 或 SQLSTATE `57014` 返回：

```text
state=postgres_execution_error, sqlstate=57014
```

当前连接随后关闭，下一条候选使用新连接继续执行。权限错误、普通连接/接口
错误以及 Gold SQL 执行失败仍然会阻断评测，避免把数据库故障伪装成模型失败。

新增回归用例覆盖该分类；runtime matching 定向测试为 46 passed，Ruff 通过。

## 结果

| workspace | 可生成输入 | Base ordered Gold match | Adapter ordered Gold match | Adapter valid execution | Adapter EOS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Olist | 736 | 0（另有 1 条 bag match） | 430（58.4%） | 736（100%） | 736（100%） |
| TheLook | 717 | 41（另有 12 条 bag match） | 717（100%） | 717（100%） | 717（100%） |
| 合计 | 1,453 | 41（另有 13 条 bag match） | 1,147（78.9%） | 1,453（100%） | 1,453（100%） |

这里的 `ordered_denotation_match` 才是严格的结果顺序与值均匹配；`bag` 只表示
忽略行顺序后匹配，不能和 ordered 结果混写成同一个数字。路由拒绝样本保留在
1,500 条总分母中，没有被当作模型成功。

### Base 暴露的问题

在 1,453 条可生成输入中，Base 只有 1,041 条观察到 EOS，另有 412 条达到
1,024 token 上限后停止。其最终状态包括：

- `postgres_execution_error`：404；
- `policy_rejected`：380；
- `result_contract_rejected`：112；
- `length_stopped`：412；
- `valid`：140。

因此 Base 的低匹配率同时受到输出边界、SQL 安全/可执行性和结果合同失败的
影响，不能只把它解释成语义能力不足。

### Adapter 的剩余问题

Adapter 的所有可生成输出都能结束、通过 Policy、使用 reader-only 事务执行并
通过 ResultContract；但可执行不等于业务语义正确。全量 1,453 条中仍有：

- `ordered_denotation_match`：1,147；
- `denotation_mismatch`：177；
- `row_count_mismatch`：129。

这 306 条失败全部来自 Olist；TheLook 这次 717 条可生成样本全部匹配。Olist
按问题形状看，时间序列更容易出现行数/粒度问题：

- `single_purchase_series`：37 / 100 匹配；
- `single_review_series`：9 / 19 匹配；
- `multi_time_series`：110 / 187 匹配；
- `structural_hard`：65 / 116 匹配；
- `multi_scalar`：97 / 142 匹配；
- `multi_dimension`：80 / 117 匹配。

按时间粒度看，week 为 22 / 74，day 为 34 / 77，month 为 46 / 82，year 为
41 / 77，quarter 为 51 / 78；无时间粒度的 scalar/dimension 类问题为
236 / 348。这个分布说明下一步应优先检查时间边界、时间桶、聚合粒度以及
结构化难例的数据覆盖，不能通过 SQL 文本格式修复代替语义训练。

## 证据位置与限制

安全报告位于仓库外：

`/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-final-20261011-r3/evaluation/safe-report.json`

报告只保存 source identity、模型/Gold hash、状态、reader 和 denotation，
不保存问题、原始 SQL、结果行或数据库错误原文。评测结果证明这轮固定输入上
Adapter 的生成边界和受控执行能力明显改善，也证明了 TheLook 这组输入上的
结果匹配；它不能推出所有电商 schema、自由问题或线上模型调用的泛化准确率。
