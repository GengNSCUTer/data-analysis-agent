# Olist v3.1 Balanced Pure-Chinese SQL-only SFT Release

## 1. 结论与版本边界

Olist v3.1 是当前离线 SQL-only SFT 数据 release。它在隔离的
`olist-demo-v3 / olist-catalog-v3 / 0.3-proposal` workspace 中，从已验证的 QuerySpec、指标合同和
deterministic PostgreSQL Gold renderer 构造训练输入；每条输入仍经真实运行时的
`QuestionRouter -> CatalogRetriever -> QueryPlan -> ResultContract` 重建，标签仅为 canonical PostgreSQL SQL
加 EOS。

本 release 替代历史 v3.0 的训练前入口，专门修复四项发现的偏差：五问法与八问法合同不一致、只靠
`hash % 5` 选择主问法、英文指标别名混入中文主集，以及测试只验证模板而未验证发布合同。旧 v3.0 的
外部资产、初始准入记录和 hash 保留不可变，不删除、不改写；它们不再是当前训练输入。

这是一份**训练数据 release**，不是模型实验。v3.1 尚未启动训练、未替换产品默认候选模型，也不证明
业务 SQL 语义正确率、跨 schema 泛化或生产接入资格。默认产品运行时继续使用 v2 workspace。

## 2. 数据规模、隔离与结构平衡

| split | 行数 | 角色 |
| --- | ---: | --- |
| train | 3,000 | 参数更新 |
| validation | 750 | 训练期模型选择 |
| in-domain test | 750 | 最终评测；物理隔离在 `final_evaluation_only/` |
| **合计** | **4,500** | — |

`family_id`、`query_spec_id` 与 canonical Gold SQL hash 跨三个 split 均为零交集。每条结构样本均重新以
v3 workspace pin 验证 QuerySpec、重新渲染 Gold SQL，并通过：

```text
SqlPolicy -> daa_analytics_reader -> ResultContract / ResultValidator
```

新加入的 9 个 v3 指标在 train 均至少有 230 行、20 个独立 family；在 validation 和 final test 均至少有
50 行、5 个 family。`category_grouped` 受当前归因合同限制，只允许 item-grain 指标：train/validation/final
test 分别为 17/8/11 行、3/1/4 个 family。validation 与 final test 的 day/week/month/quarter/year 五种
时间粒度分别均不少于 70 行。结构难例仍保留 risk tag，本 release 没有额外按 distinct、二层聚合、状态
过滤或非负时长边界再拆新的硬配额。

## 3. 全量 Gold 准入与 advisory 复核

完整确定性准入结果为：

```text
4,500 admitted / 0 needs_human_review / 0 rejected
```

每条 admitted record 都保留 QuerySpec、Gold SQL、Gold SQL hash、Policy 最终 SQL hash、reader-role 执行和
结果合同摘要。对风险和类别分层的 48 条 `deepseek-ai/DeepSeek-V4-Flash` advisory review 中，45 条为 pass，
3 条因 `APITimeoutError` 无法返回结论。它们被记录为 `provider_error_advisory`，不会把已经通过确定性链路
的记录降格为失败；同时也不被误写成语义 pass。真实 semantic review objection 为 0。

初始 advisory 输出和其后的 reconciliation 都保留在仓库外，后者是下游输入：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1.1/
    admission-20260914-v3_1/
    admission-20260914-v3_1-reconciled/
```

reconciled admitted records SHA-256：
`7bf88889eb6c160f9617fa21afb7cff6601fbbf593e2ef7d6b4fc67cd6bf204c`。

## 4. 八类纯中文问法与主问法配额

每个 QuerySpec 生成 8 个受控中文 surface form：正式请求、业务口语、管理者表达、简洁短句、结果导向、
时间前置、分析切面前置和 Catalog 中文别名。它们不增加 QuerySpec、family 或训练行数量；完整语义边界见
[`olist-v3-1-balance-and-surface-repair-contract.md`](olist-v3-1-balance-and-surface-repair-contract.md)。

主 release 只允许 Catalog 的中文名称或中文受控别名，过滤含 ASCII Latin token 的候选。因此 `GMV`、
`AOV`、`paid orders`、`average items per order` 等英文/拉丁缩写不会混入 train、validation 或 final test。
它们若要用于多语言鲁棒性，只能以未来独立 overlay 引入。

全量 overlay 为 36,000 条，无 normalized duplicate。正式 SFT 对每个 QuerySpec 只选择一条主问法，策略为
`sha256_ranked_split_bucket_eight_way_quota_v2`：先在每个 split×主 bucket 中均分八类问法，再通过稳定
SHA-256 rank 分配余数并满足 split 总配额，因而不依赖 JSONL 读入顺序或随机数。

| split | v1–v6 | v7–v8 | bucket 内最大差 |
| --- | ---: | ---: | ---: |
| train | 各 375 | 各 375 | 1 |
| validation | 各 94 | 各 93 | 1 |
| final test | 各 94 | 各 93 | 1 |

question variants SHA-256：
`3603902f707900cbd2e8adce40469f93ad61819d05f3c5f9410e7921645a1b8f`。

## 5. Prompt 重建、长度合同与最终 JSONL

所有 36,000 条 surface case 已重建 runtime Candidate SQL Prompt，Router、Catalog、QueryPlan 与
ResultContract 均与原 QuerySpec 一致；无 rejected row。runtime candidates SHA-256：
`c1f85ef80eafb9a4dba470c50c3d812aaad4fc838c9b5c38b17de6d2846e2a59`。

以 Qwen2.5-Coder-1.5B Base tokenizer 检查 `rendered_prompt + canonical SQL + EOS` 后，最终 SFT JSONL
无超长排除、无静默截断：

| split | token 范围 | SHA-256 |
| --- | ---: | --- |
| train | 1,066–2,836 | `be3a9276abb3690c223aa68e064e4fb8e1ab3055f282c56c727dadd46aa7c7c8` |
| validation | 986–2,906 | `b2544aa8cef3f98134944df3a957dc046938bb351fdc64735b3139f6deefde38` |
| final test | 1,004–2,875 | `67c1db3f9a3d7a74160306cd1680ea6f3c6be00035d2c95d860bb3d6e1266750` |

最大序列长度合同为 3,072。最终资产位于：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1.1/
    structural-20260914-v3_1/
    admission-20260914-v3_1-reconciled/
    surface-20260914-v3_1/
    runtime-prompts-20260914-v3_1/
    sft-release-v1.1/
```

## 6. 发布级审计与下一步

发布级审计将 structural QuerySpec/Gold、reconciled admission、8-form surface、runtime Prompt 和最终 SFT
JSONL 的 SHA-256 链路一起验证，而不只验证单个模板函数。它确认结构 4,500 条、admitted 4,500 条、surface
36,000 条、runtime 36,000 条、最终 SFT `3,000/750/750`，并验证纯中文、8-form 配额、split 隔离、运行时
身份一致性、平衡下限、长度合同与无静默截断；结果为 `pass`。

审计报告：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1.1/
    release-contract-audit-20260914-v3_1/release_contract_audit.json
```

报告 SHA-256：
`8fa7f4efab2c4d730aec5ce168a877ca7dad6e8650cd43d48303b8f513203133`。

之后针对 v3.1 P1 审阅发现，审计器已增加对**最终 SFT 文件本体**的 hash 复算以及逐行 split/family/QuerySpec/
canonical Gold SQL 身份验证；它不再只信任 `split_audit.json` 自报的隔离结论。新实现对同一外部 release 的复跑
仍为 `pass`，并显式记录 `sft_split_files_hash_bound=true` 与 `sft_split_identity_recomputed=true`：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1.1/
    release-contract-audit-20260914-v3_1-recomputed/release_contract_audit.json
```

该复跑报告 SHA-256 为
`58cc0aea5eb26e07bf7ef1bb751eb43ab15360b9f8f028bfb7c0f8313b88384a`；没有调用模型、GPU 或数据库。

训练入口、标签模板、optimizer、validation-best checkpoint 与三阶段 matching 评测协议现已冻结于
[`olist-v3-1-sql-only-training-evaluation-contract.md`](olist-v3-1-sql-only-training-evaluation-contract.md)。下一件
独立任务是 CPU preflight；只在用户确认后才可做一次 GPU smoke。Gold admission 通过不构成 Adapter 提升或生产
接入的证据。
