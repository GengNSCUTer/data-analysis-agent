# Olist Protected-Family Summary 受限导出 v1

**状态：** 导出器与合成回归已实现；尚未读取真实 protected holdout，也未生成真实 summary。

**规范入口：** [`../../../scripts/post_training/data/export_olist_protected_family_summary.py`](../../../scripts/post_training/data/export_olist_protected_family_summary.py)。

## 1. 为什么需要一个单独的导出器

Olist 领域训练必须避免与永久 holdout 共享同一个语义 family 或完整 SQL program。但让
`materialize_olist_queryspecs.py` 读取 `post_training_holdout_v1.yaml`、问题、Gold SQL 或评测结果来判断
碰撞，本身就造成了训练数据构造流程接触 protected 内容。

因此边界分成两段：受限人工流程在隔离环境里先把 protected case 映射成结构化 `family_id`；本导出器只接收
该映射的受限、仓库外批准输入，并把 family ID 单向转换成 fingerprint。之后 materializer 只读取 fingerprint
summary，永远看不到 family 原文或 protected case 原文。

```text
protected case / Gold / result            materializer
           |                                  |
           v                                  v
  isolated human family review  ->  approved family IDs  ->  fingerprints only
           |                                  |
           +-- source-manifest hash           +-- collision rejection
```

## 2. 责任边界

| 组件 | 可以读取 | 不可以读取 | 输出 |
| --- | --- | --- | --- |
| 受限人工审阅 | protected case 所在受控环境 | 训练 seed、Prompt、训练 JSONL | 排序去重后的 family ID 批准输入。 |
| `export_olist_protected_family_summary.py` | 批准输入 | case ID、问题、Prompt、SQL、结果、数据库路径 | fingerprint summary 和 evidence。 |
| `materialize_olist_queryspecs.py` | 静态 coverage seed、fingerprint summary | protected 原文、批准输入中的 family ID | QuerySpec/Gold 中间产物或碰撞拒绝。 |

导出器不会且不能验证人工 family 映射是否完整或正确。它验证的是该输入的格式、当前 WorkspacePin、source
manifest hash、人工 review reference、排序和去重。人工复核仍是完整性与业务分类的责任方。

## 3. 外部输入与输出合同

批准输入必须在仓库外，格式严格为：

```json
{
  "approved_input_version": "olist-approved-protected-family-ids-v1",
  "workspace": {"workspace_id": "...", "catalog_version": "..."},
  "protected_source_manifest_sha256": "64-char sha256",
  "review_reference": "non-sensitive review reference",
  "family_ids": ["family_<24-lowercase-hex>"]
}
```

`family_ids` 必须非空、排序、去重，并且由当前 `family_id()` 合同产生。它不允许 case ID、问题、Prompt、SQL、
结果、seed 或任何额外字段。导出器拒绝输入或输出落在 Git worktree 内，也拒绝覆盖既有输出目录。

输出目录也在仓库外，包含：

| 文件 | 内容 |
| --- | --- |
| `protected_family_summary.json` | `summary_version` 与排序后的 SHA-256 `family_fingerprints`。这是 materializer 的最小输入。 |
| `protected_family_summary_evidence.json` | 批准输入 hash、summary hash、workspace、source-manifest hash、family 数、生成时间和 review reference。没有 family ID 原文。 |

输出先写入同级 staging 目录，再原子 rename；失败不会留下不完整的目标目录。

## 4. 已验证与尚未证明

合成测试证明：成功输出只含 fingerprint/evidence、固定时间戳下字节稳定、未知原文字段/未排序 ID/workspace
漂移/仓库内路径都会被拒绝。导出器没有 holdout 文件路径参数，代码也没有读取 `evals/`、数据库或训练资产。

这不能证明真实 protected case 的 family 覆盖完整，也不能证明 protected summary 已经建立。真实导出仍需一个
经用户批准的独立受限操作，先由人工在隔离环境审阅 family 映射，再把批准输入交给本工具运行。

## 5. 接下来的实际数据路线

在真实 summary 可用之后，仍不能直接说“训练集已经准备好了”。后续按单独任务逐项推进：

1. 将静态 15 条 seed 和真实 summary 做一次小批外部结构物化，核对 family/程序碰撞、版本和 hash；不生成 Prompt 或训练行。
2. 对通过的 Gold SQL 逐条走 `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator`，并人工抽查指标口径、Join 粒度和 AOV 等关键公式。
3. 将已准入的 QuerySpec 派生为真实运行时 `QueryPlan` 与 `olist-candidate-sql-v1` Prompt；人工写并审查中文业务 query 的受控语言变体。此时才会形成含 `question/prompt/canonical_sql` 的仓库外候选行。
4. 依据 `family_id` / `sql_program_id` 整组制定规模化 split。现有 15 条只是接口 fixture，不能直接承担最终 train/validation/test 分布；必须扩展独立 semantic family/program，并冻结 split audit。
5. 对完整外部候选行进行 tokenizer 长度审计，严格执行 `1536` 无截断合同；超长行进入不含原文的 exclusion manifest。
6. 再物化正式 train/validation/in-domain-test JSONL，运行训练入口的 hash、行数、split、holdout、长度和版本门；之后才另立训练与 matching Base/Adapter 评测任务。

每一步都只增加一类证据，不能由“SQL 渲染成功”跳到“模型已可训练/可上线”。
