# Olist Medium v1 SFT 数据集

## 定位

`Olist Medium v1` 是 Olist 电商领域 Candidate SQL 后训练的首个中等规模数据 release。它不是通用 Spider/CSpider 训练集，也不是产品运行时的权限边界；训练目标只是让候选生成模型在产品真实 Prompt 上输出 canonical PostgreSQL SQL。运行时仍强制经过 `SqlPolicy`、PostgreSQL reader role、`ResultContract` 和 `ResultValidator`。

40 条 `Olist Pilot v1` 保留为工程链路验收集，不能作为本 release 的质量或规模结论。

## 冻结目标

| split | 行数 | 用途 |
| --- | ---: | --- |
| train | 720 | 参数更新 |
| validation | 240 | 训练中模型选择与诊断 |
| in-domain test | 240 | 最终评测，物理放在 `final_evaluation_only/` |

每一行对应一个独立 `family_id`。时间端点、问题措辞和输出列顺序不用于规避 family 隔离。目标总量为 1,200，不用同义改写重复样本凑数。

## 构造链路

```text
冻结十指标/QuerySpec 合同
  -> 受 protected-family 哈希摘要过滤的结构化 seed
  -> deterministic PostgreSQL Gold SQL
  -> SqlPolicy + reader role + ResultContract 全量准入
  -> 受控中文问题 overlay
  -> 生产 Router/Catalog/QueryPlan/ResultContract Prompt 重建
  -> Prompt + Gold SQL + EOS 长度审计
  -> train / validation / final_evaluation_only
```

- 每条 QuerySpec、Gold SQL、Prompt、workspace pin、split、长度和哈希都可回溯。
- protected holdout 只以仓库外不可逆 family fingerprint summary 参与碰撞检测；构造脚本不读取 protected QuerySpec、问题或 SQL 原文。
- 数据、完整 SQL、Prompt、数据库结果和模型审阅内容都保存在 `/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-medium-v1/`，Git 仅保存脚本、合同和小型测试。
- Olist Medium v1 的长度合同为 `3072`：完整 release 的实测最大值为 `2915`，这是最小的 256 对齐上限。Pilot v1 的 `2304` 只适用于旧 40 条验收集。不得静默截断；任何超长记录都必须进入脱敏 exclusion manifest，并使要求精确行数的 release 失败。

## 准入策略

所有 1,200 条记录必须通过 deterministic `SqlPolicy -> daa_analytics_reader -> ResultContract/ResultValidator`。外部 LLM 语义审阅只按指标、结果形态和时间粒度做确定性分层抽样，作为口径风险提示，不冒充 1,200 条逐条人工审阅，也不替代数据库结果合同。

release 仅当三切分精确为 `720 / 240 / 240`、family/QuerySpec 跨 split 重叠为空、protected collision 为零、所有 Gold 行被准入且长度合同无排除时，才可以进入 Base/Adapter matching 生成评测和后续 LoRA 训练配置冻结。

## 冻结证据

2026-09-04 的最终构造满足 release 条件：1,200 条 Gold 均为 `admitted`，40 条分层 advisory semantic review 均为 `pass`，Prompt 重建为 `1200/1200`，length exclusion 为 `0`。

| split | 行数 | SHA-256 | 最大 token |
| --- | ---: | --- | ---: |
| train | 720 | `acb807e731fb9a9401ea5043830870fdbc3782166f8def20ef2db3f3c88c5d45` | 2915 |
| validation | 240 | `2a1893ce310864383e50d80ac387c4499243b38dd2895e967ad43e61b046a1ae` | 2902 |
| in-domain test | 240 | `7054d137938387a06cd2d1c2e5e9b835962245864d2c17e4647215784838d6b0` | 2912 |

外部 release 根目录为 `/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-medium-v1/sft-splits-length3072-v1/`。最终 test 文件位于 `final_evaluation_only/in_domain_test.jsonl`，构造及后续训练入口不得读取它作为训练或模型选择输入。
