---
goal: Design a controlled Olist QuerySpec batch materializer
version: 1.0
date_created: 2026-09-03
last_updated: 2026-09-03
owner: Data Analysis Agent
status: 'Design only'
tags: [feature, post-training, data-contract, materialization]
---

# Olist QuerySpec 受控批量物化器设计 v1

## 1. 目标与非目标

本任务只设计后续如何从已冻结 coverage seed 生成、验证和审计一批 `QuerySpec` 与 canonical Gold SQL。
它不是训练数据生成器，也不在本任务读取 protected holdout、构造自然语言 Prompt、执行 SQL 或启动训练。

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
- 相同 `family_id`、相同 `query_spec_id`、相同 `sql_program_id` 或受保护摘要碰撞必须 fail closed；
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

后续实现必须先用小型合成 seed 覆盖成功、重复、版本漂移、敏感维度、归因、family 泄漏、protected 摘要
碰撞和 renderer hash 篡改等路径；不得读取 Olist 训练/验证/测试原文或 protected holdout。只有用户审阅并确认
物化器接口、family 派生、split 分配、输出边界和失败策略后，才能开始编写物化脚本。
