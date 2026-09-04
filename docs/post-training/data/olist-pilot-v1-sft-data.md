# Olist Pilot v1 正式 SFT 数据集

## 状态

**已冻结，尚未训练。** 本文记录 Olist 领域 Candidate SQL SFT 的第一版正式
Pilot 数据资产。它是后续训练配置与 Base/Adapter 对照的输入基线，不是模型质量
结论，也不代表通用电商领域覆盖已经完成。

## 任务卡

| 项目 | 内容 |
| --- | --- |
| 目标 | 将通过完整准入的 Olist QuerySpec/Gold SQL，与生产运行时重建的中文 Prompt 精确绑定，物化为可训练的 train/validation/in-domain-test。 |
| 非目标 | 不启动 GPU、训练、生成候选 SQL 或修改生产默认模型；不将该 Pilot 表述为准确率或业务泛化结论。 |
| 输入 | 已冻结的 40-family QuerySpec 结构物化、7 个 Gold admission 批次、2 条绑定 SQL hash 的人工口径审批，以及生产 Prompt 重建记录。 |
| 输出 | Git 外的 `24/8/8` JSONL、split audit 与空的长度排除清单。 |
| 不变量 | 训练输入保持生产 `olist-candidate-sql-v1` Prompt 原样；Gold SQL 不由自然语言模板拼接；family/QuerySpec 跨 split 隔离；in-domain test 永不参与训练；不静默截断。 |

## 已冻结资产

所有原始问题、Prompt、SQL、结果和执行证据均在 Git 外：

```text
/disk2/gengnan/data-analysis-agent-data/text-to-sql/olist-domain-sft/
  olist-pilot-v1-20260904/
    train.jsonl
    validation.jsonl
    final_evaluation_only/in_domain_test.jsonl
    exclusions/length.jsonl
    split_audit.json
```

`split_audit.json` 绑定下列来源 manifest 的 SHA-256：

- 40 条完整 Gold admission assembly；
- 40 条真实运行时 Prompt 重建；
- 本地 Qwen2.5-Coder-1.5B tokenizer。

## 构造链路

```text
40 个结构化 QuerySpec family
  -> deterministic PostgreSQL Gold SQL renderer
  -> SqlPolicy -> PostgreSQL reader role -> ResultValidator
  -> DeepSeek advisory review + 必要的人工口径审批
  -> 已批准 Gold admission assembly
  -> 中文业务问题 overlay
  -> Router/Catalog/QueryPlan/ResultContract 重建真实运行时 Prompt
  -> rendered Prompt + canonical Gold SQL + EOS
  -> family-isolated train / validation / in-domain test JSONL
```

模型训练时只见最后一行中的 Prompt，并学习续写 canonical SQL。它不会获得
QuerySpec、人工审批、数据库结果或 protected holdout 原文；上线时仍必须经过
SqlPolicy、只读 PostgreSQL role 与 ResultValidator。

## 准入与切分结果

| 项目 | 结果 |
| --- | --- |
| QuerySpec / family / canonical Gold SQL hash | 各 40 个且均唯一 |
| Gold 准入 | 40/40 通过 SqlPolicy、`daa_analytics_reader` 与 ResultValidator/ResultContract |
| Advisory review | 38 条直接通过；2 条 `needs_human_review` 均以 metric contract、QuerySpec ID 与 Gold SQL hash 绑定后人工批准 |
| 运行时 Prompt 重建 | 40/40；Router、Catalog、QueryPlan、ResultContract 均重建成功；未调用模型、未执行 SQL、未使用 GPU |
| train | 24 条 / 24 个 family，唯一用于参数更新 |
| validation | 8 条 / 8 个 family，只用于训练期模型选择 |
| in-domain test | 8 条 / 8 个 family，位于 `final_evaluation_only/`，禁止作为训练或验证输入 |
| 跨 split family / QuerySpec overlap | 均为 0 |

两个 advisory 疑点分别是 AOV 的“先按订单聚合再平均”以及 `item_count` 的商品行粒度。
二者均符合冻结的十指标合同；疑点来自 advisory prompt 未包含“有效订单统一排除
`canceled` 与 `unavailable`”的全局定义，不是 renderer 或数据库执行缺陷。

## 长度合同

历史 CSpider 的 `1536` 不是 Olist Prompt 的通用上限。真实 Olist 运行时 Prompt 包含
Catalog、QueryPlan、结果合同和业务语义，精确计数（`Prompt + Gold SQL + EOS`）的最大值为
`2076`。因此本 Pilot 独立冻结：

```text
max_seq_length = 2304
silent_truncation = false
```

`2304` 是覆盖当前最大值的最小 256-token 对齐上限。最终结果：train 最大 `2054`、validation
最大 `1970`、in-domain test 最大 `2076`，长度排除 `0`。训练入口会在加载模型前校验
`split_audit` 的长度合同；使用默认 `1536` 会被拒绝。

## 中文问题与生产解析边界

问题 overlay 使用生产解析器可稳定理解的 ISO 日期和既有维度别名，例如
`2017-01-01 至 2018-01-01`、`各客户州`、`各商品品类`、`按月`。第一次使用
中文日期自然表达时被 `WorkingMemory` 时间范围解析拒绝，因此未进入正式资产。
这是当前生产语言解析边界的诊断记录，不代表中文用户只能使用 ISO 日期；若要支持更自然的
日期表达，应单独改进运行时解析与回归集，再重新构造受影响训练数据。

## 验证与限制

已验证：外部资产 24/8/8 行数、40 个 family/QuerySpec 唯一性、训练文本等于
`rendered_prompt.rstrip() + "\\n" + candidate_sql.strip()`、每条不超过 2304、训练入口
audit 回读、Gold admission/Prompt materializer/SFT layout 专项测试。

仍未验证：24 行训练能否改善 SQL 生成、是否在 24GB GPU 上以 `2304` 和 batch 4 稳定训练、
validation 是否提升、8 条 in-domain test 是否优于 matching Base，以及更广电商业务/语言表达
的泛化能力。下一项应只做一个 bf16 LoRA 的 `2304` 单步或小步显存 smoke，冻结 batch size 后
再由用户决定是否运行完整 Pilot 训练与 matching Base/Adapter 评测。
