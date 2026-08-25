# 后训练候选审计清单

本文件描述 `post_training_candidates_v1.yaml` 从模板变成实际数据集前的准入检查。
截至 2026-08-25，清单状态为 `prepared_external_only`：已从 Spider `train_spider.json`
确定性抽取 128 条候选，产物位于仓库外，Git 只保留脚本、manifest 和审计规则。

## 每条样本必须回答的问题

1. 来源和许可证是什么，是否允许训练和必要的内部共享？
2. 问题是否已脱敏，是否仍然保留理解 SQL 所需的最小语义？
3. 使用的是哪个 workspace、Catalog、role 和策略版本？
4. 是否属于永久 holdout，或是其同义改写、相同 SQL shape 的近邻？
5. 路由、澄清、QueryPlan、SQL、执行状态、结果合同和人工复核结论是否分别记录？
6. 失败样本是否说明了失败阶段，而不是只保存模型最终文本？

## 准入门槛

- 没有 API key、cookie、访问令牌、原始结果行和未脱敏业务上下文；
- 不含 `text_to_sql_v2.yaml` 的 60 个 case，也不含它们的改写/派生答案；
- SQL 已经在固定数据库快照上通过 AST、reader role 和结果合同检查，或被明确标记为可训练负例；
- split 按 SQL shape、语义模板、Catalog/workspace 和业务时间分组，不能只随机切分；
- 每个标签可追溯到确定性生成器或人工复核人和时间；
- 训练集、验证集、holdout 的 manifest 与哈希可复核；
- 原始数据放在仓库外部目录，Git 只提交 manifest、schema、转换脚本和小型 fixture。

## 推荐记录结构

```json
{
  "sample_id": "manual_failure_0001",
  "source": "manually_reviewed_failure",
  "license": "project-owned",
  "timestamp": "2026-08-24T00:00:00Z",
  "workspace_id": "olist_demo",
  "catalog_snapshot": "catalog-olist-v3",
  "role_scope": "analytics_reader_v1",
  "question_redacted": "按区域比较订单数和成交额，并说明统计口径",
  "working_memory": {"time_range": "fixed_demo_window"},
  "target_route": {"intent": "data_query", "requires_database": true},
  "query_plan": {"metrics": ["orders", "gmv"], "dimensions": ["region"]},
  "candidate_sql": "SELECT ...",
  "execution_outcome": {"ast": "pass", "role": "pass", "result_contract": "pass"},
  "review": {"semantic_correct": true, "reviewer": "human_2026_08_24"},
  "label_provenance": "manual_review",
  "split": {"name": "train", "group": "region_compare_shape_v1"}
}
```

示例中的 SQL 仅为结构占位，不应直接复制到训练文件。真实业务问题和结果行必须继续留在受控外部存储。

## 2026-08-25 候选构造结果

- 来源：Spider 1.0 Kaggle v1 镜像的 `train_spider.json`，共 7,000 条；没有读取 dev/test gold SQL。
- 选择：按 `db_id + 归一化 SQL shape` 分组，使用固定 seed `20260825` 排序后轮询抽取 128 条。
- 执行检查：128/128 通过只读 SQLite `EXPLAIN QUERY PLAN`；脚本不物化或保存结果行。
- holdout：与 v2 永久隔离清单的 60 个 case ID 碰撞数为 0。
- 外部产物：`/disk2/gengnan/data-analysis-agent-data/experiments/spider-sft-candidates-v1-20260825/`。
- 产物 hash：以 `evals/manifests/post_training_candidates_v1.yaml` 中的 SHA-256 为准。
- 训练边界：这批样本是“可用于下一步 tokenizer/forward smoke”的候选，不等于已经完成人工复核、SFT 或模型效果提升证明；正式训练前仍需 tokenizer 检查、训练/验证分组和小批量 SFT smoke。
