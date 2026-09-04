---
goal: Implement a controlled Olist QuerySpec batch materializer
version: 1.0
date_created: 2026-09-03
last_updated: 2026-09-03
owner: Data Analysis Agent
status: 'Implemented; no real data materialized'
tags: [feature, post-training, data-contract, materialization]
---

# Olist QuerySpec 受控批量物化器设计 v1

## 1. 目标与非目标

本任务实现从已冻结 coverage seed 生成、验证和审计一批 `QuerySpec` 与 canonical Gold SQL 的受控中间产物。
它不是训练数据生成器，也不读取 protected holdout、构造自然语言 Prompt、执行 SQL 或启动训练。

物化器的目标是把“可安全覆盖的语义组合”转换为可复现、可审阅的离线中间产物，并在写出任何记录
之前完成结构校验、重复检查和版本锁定。

## 2. 输入与输出

### 输入

- 版本锁定的 `olist-domain-sft-coverage-matrix-v2`、十指标 Catalog 和指标合同；
- 仅包含结构字段的 coverage seed，例如有序指标、结果形状、维度、时间模式/粒度和固定 Join program；
- 当前 `WorkspacePin`、renderer 版本和 materializer 版本；
- 受保护 exclusion manifest 的不可逆 `family_id`/`sql_program_id` 摘要（只用于碰撞拒绝，不读取 holdout 原文或答案）。

### 输出（必须写在仓库外）

- `query_specs.jsonl`：通过 `create_validated()` 的 QuerySpec 结构；
- `gold_sql.jsonl`：QuerySpec ID、renderer 版本、SQL hash 和脱敏 evidence；SQL 正文只在受控实验目录保存；
- `materialization_manifest.json`：输入/代码/版本 hash、行数、QuerySpec/family/program 计数、覆盖分布和失败统计；
- `materialization_rejections.jsonl`：只记录稳定 ID、reason code 和脱敏输入摘要，不写问题、SQL、结果行或 holdout 内容。

## 3. 单条记录流水线

每条 seed 必须按以下顺序处理，任一门失败就拒绝该条；在 manifest 中计数，不能静默跳过：

```text
canonicalize seed
  -> QuerySpec.create_validated()
  -> validate_query_spec()（物化器仍需保留显式调用/证据）
  -> render_gold_sql()
  -> canonical key / alias / SQL hash 检查
  -> duplicate / family / protected-summary collision 检查
  -> 写出 QuerySpec 与 Gold artifact
```

`create_validated()` 是减少调用方遗忘校验的便捷入口，但 renderer 的二次验证必须保留；物化器不得
直接实例化 dataclass 后绕过这两个入口。

## 4. 稳定身份与去重

- `query_spec_id` 由 QuerySpec canonical JSON 派生，不能由随机数或自然语言生成；
- `sql_program_id` 取已冻结的 Join program/聚合程序 ID，不能从 SQL 文本模糊推断；
- `family_id` 由不含具体日期/措辞的有序指标、结果形状、维度、时间粒度/模式、Join program、聚合/去重策略、版本组成；
- 相同 `family_id`、相同 `query_spec_id`、相同 canonical SQL hash 或受保护 family 摘要碰撞必须 fail closed；
- 同一 `sql_program_id` 可以服务同一 split 的多个不同 family，但不能跨 split；否则训练/验证/测试会共享完整查询程序。
- 日期窗口、中文措辞、SQL 空白和别名改写不能制造新的 family；它们属于后续语言物化阶段，不能在本设计中用来虚增覆盖。

## 5. Split 与配额

本物化器不随机逐行切分。它先按 family/program 建立不可跨 split 的分组，再使用预注册的 coverage bucket 配额
分配 train、validation、in-domain test。分配完成后必须检查：

- family、program、QuerySpec 在 split 之间交集为零；
- 指标和物理 schema 可以共享，但完整查询程序不能共享；
- test 只生成受控结构和审计记录，不能被训练入口读取；
- 任一桶无法提供足够独立 family 时缩小规模并报告，不得用同义改写填满目标。

## 6. 失败保护与审计

物化器必须拒绝：未知指标/维度、版本漂移、归因或敏感维度、时间字段混用、结果列篡改、重复 family、
protected summary 碰撞、renderer hash 不一致和任何未声明字段。失败记录只含 `seed_id`、reason code、输入结构摘要和 materializer version。

manifest 至少保存：输入文件 hash、Catalog/metric/policy/prompt/workspace 版本、代码与 renderer 版本、
输出文件 hash、各 split 行数、QuerySpec/family/program 数、split 交集、拒绝原因计数、protected collision
计数、canonical SQL hash 计数和运行时间。写文件采用临时文件 + 原子 rename，避免中途失败留下“看似完整”的产物。

## 7. 后续准入边界

物化器成功只证明结构和确定性产物已生成，不能证明 Gold 业务语义正确。进入训练 JSONL 前仍必须独立经过：

```text
SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator -> 人工指标口径抽检
```

这些 gate 的执行器、结果审计、人工抽检和 token-length audit 另立任务，不在本设计中偷渡实现。

## 8. 验收证据与停止条件

实现位于 `scripts/post_training/data/materialize_olist_queryspecs.py`，测试位于
`tests/test_olist_queryspec_materializer.py`。它接受显式 split 的结构化 JSONL seed 和仅含 family fingerprint
的保护摘要；输出仅可写到仓库外目录，并使用 staging directory + 原子 rename。

合成测试覆盖成功物化、日期变体 family 重复、protected family 碰撞、SQL program 跨 split、敏感维度、
未知 `question` 字段和 renderer hash 篡改。实现后的 QuerySpec/物化器专项为 `53 passed`，与 Catalog/Router/
QueryPlan/ResultValidator/SqlPolicy/Trusted SQL Tool 的相关回归为 `169 passed`。未运行脚本处理任何真实 Olist seed，
未读取 protected holdout、未执行 Gold SQL，未生成 Prompt/训练 JSONL，未启动 tokenizer/GPU。

下一项只能先设计并审阅一个很小的结构化 coverage seed 清单和与其匹配的 protected family summary 的生成边界；
不得直接大规模物化或进入 SQL 执行、结果合同、token 审计和训练。
