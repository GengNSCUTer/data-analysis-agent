# TheLook 600 条跨 Schema final test v2

**状态：** 已冻结、未运行任何 v2 Base/Adapter 生成；它是 Olist LoRA 的 protected cross-schema holdout，不是训练、验证或 Prompt 资产。

**版本 pin：**

| 项目 | 冻结值 |
| --- | --- |
| Evaluation | `thelook-cross-schema-final-test-v2` |
| Workspace | `thelook-cross-schema-eval-v2` |
| Catalog / metric | `thelook-catalog-v2` / `0.2-frozen` |
| QuerySpec / prompt | `thelook-query-spec-v2` / `thelook-candidate-sql-v2` |
| Renderer / Policy | `thelook-postgres-gold-renderer-v2` / `sql-policy-v1` |
| 数据快照 | `thelook-kaggle-mirror-v1-20260908` |
| 物化时间 | `2026-09-10T05:04:56+00:00` |

## 1. 产物位置与隔离

完整问题、五种中文问法、QuerySpec、Gold SQL 和逐条执行证据只在仓库外受保护目录：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  thelook-cross-schema-final-test-v2-20260910/
    cases.jsonl
    manifest.json
```

`cases.jsonl` 为 600 行，SHA-256 是：

```text
e942367df04b7ed6e52db8455e4de681827f8741989c19d81cd88bd0479b0fa5
```

仓库只提交构造器、指标合同、QuerySpec/renderer 合同、测试和聚合事实；不提交评测问题、Gold SQL、原始结果行、模型输出或运行日志。该资产不得被读取到 Olist train/validation/test、few-shot、Prompt 调优、错误驱动样本生成或 Adapter 选择中。

## 2. 构造与准入链路

构造器是 [`scripts/post_training/evaluation/build_thelook_cross_schema_evaluation_v2.py`](../../../scripts/post_training/evaluation/build_thelook_cross_schema_evaluation_v2.py)。每一个 case 都从已冻结的结构化计划开始，而不是从自然语言或模型 SQL 反推：

```text
Catalog / 指标合同
  -> 已验证 TheLookV2QuerySpec
  -> deterministic PostgreSQL Gold renderer
  -> SqlPolicy
  -> SET LOCAL ROLE daa_thelook_reader + 5 秒 statement_timeout
  -> ResultValidator（精确列、值域、时间边界、200 行预算）
  -> protected cases.jsonl
```

一次失败就停止物化，staging 目录被清理，不会产生部分正式集。问题在 QuerySpec 通过后才由 5 条受控中文 surface form 生成；每个 QuerySpec 用 `sha256(query_spec_id) mod 5` 稳定选择一条主问法。五种问法是同一语义样本的表达覆盖，不能计为五条评测样本。

## 3. 冻结覆盖

| 维度 | 实际覆盖 |
| --- | ---: |
| 唯一 case / QuerySpec / family | `600 / 600 / 600` |
| 标量 / 维度分组 / 时间序列 | `163 / 303 / 134` |
| 覆盖指标 | `20` |
| 同事实域多指标 case | `152` |
| 同事实域多指标时间序列 | `29` |
| 时间粒度 | 日 `1`、周 `1`、月 `69`、季度 `53`、年 `10` |
| 最大真实返回行数 | `194` |
| 结果合同 valid | `600 / 600` |

指标覆盖订单商品、订单/履约、库存入库、库存售出、库存快照、行为/会话与用户注册。`event_state` 在真实快照中有 `228--231` 个分组，超过 200 行可展示合同，因而从 v2 Catalog 和评测中移除；替换为订单完成/取消、库存销售/售出周期、事件/会话的多指标时间序列，避免通过 `LIMIT` 截断或同义改写补足 600 条。`average_dispatch_days` 的状态分组同样改为已实际验证的 2019 窗口（181、116、176 行）。

## 4. 物化验证与结论边界

manifest 声明并已回读验证：case ID、QuerySpec ID、family ID 全部唯一；每条都有 5 个问法；所有 case 都通过 QuerySpec、renderer、Policy、reader role 与结果合同；不读取 Olist 训练/Prompt 输入；未运行 Base 或 Adapter 生成。独立回读还确认 manifest 哈希与文件一致、600 条的执行状态均为 `valid`、问题中不含 SQL 或 `analytics.`、Gold SQL 不访问 `thelook_raw`。

这些事实只证明测试资产、Gold SQL 和数据库准入是可重放的，并不证明任何模型的 SQL 生成质量。v2 matching 的独立 Base/Adapter 合同、三阶段隔离、真实 Prompt token 预检和实现入口已冻结在 [`thelook-v2-matching-evaluation-contract.md`](thelook-v2-matching-evaluation-contract.md)，但尚未启动任何 v2 模型生成；不能用 v1 的 206 条结果外推到本 v2 集，也不能改变产品运行时默认路径。
