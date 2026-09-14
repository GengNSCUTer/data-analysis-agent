# Olist v3 Balanced SQL-only SFT Release v1

## 1. 结论与边界

Olist v3 的平衡 SQL-only SFT release 已完成物化。它在隔离的
`olist-demo-v3 / olist-catalog-v3 / 0.3-proposal` workspace 中，将冻结的指标合同、
family seed 与历史结构候选重新编译为可追溯的运行时 Prompt → canonical PostgreSQL SQL
训练资产。

这是一份**训练数据 release**，不是一次模型实验：本 release 尚未启动训练、未替换产品默认候选
模型，也不证明开放式业务 SQL 的语义准确率、跨 schema 泛化或生产可接入性。默认产品运行时继续使用
`olist-demo / olist-catalog-v2 / 0.2-frozen`。

## 2. 最终数据与隔离

| split | 行数 | QuerySpec | family | 角色 |
| --- | ---: | ---: | ---: | --- |
| train | 3,000 | 3,000 | 440 | 参数更新 |
| validation | 750 | 750 | 108 | 训练期模型选择 |
| in-domain test | 750 | 750 | 335 | 仅最终评测 |
| **合计** | **4,500** | **4,500** | — | — |

- `family_id`、`query_spec_id` 与 canonical Gold SQL hash 跨三个 split 均为零交集；
- `in_domain_test` 物理存放在 `final_evaluation_only/`，不得用于训练、验证、few-shot、错误回流或模型选择；
- 全部 4,500 条 Gold SQL hash 唯一；中文问法和日期窗口不额外计为独立 family；
- 每条训练行的输入为真实运行时重建的 `rendered_prompt`，标签为 canonical PostgreSQL SQL；训练时附加 EOS。

结构 release 的八个主类别、各 split 配额、有限日期窗口与历史 v2 结构重建规则，见
[`olist-v3-balanced-release-v1-contract.md`](olist-v3-balanced-release-v1-contract.md)。其中单指标维度分组
的真实容量为 `122/33/33`，而非初始设想的 `134/33/33`；少出的 12 个 train 行已转至容量充足的
多指标维度分组，未通过重复 SQL 或中文改写补齐。

## 3. 准入与可复核执行证据

### 小批准入

12 条分层 seed 覆盖八类主类别、三个 split 与九项新增 v3 指标，全部通过：

```text
SqlPolicy -> daa_analytics_reader -> ResultContract / ResultValidator
12 admitted / 0 needs_human_review / 0 rejected
```

records SHA-256：
`13eb2a01d7006c57d98ec6c9689e9caa25fbf652095abc191df946523cbd2c25`

### 全量准入

全部 4,500 条 canonical Gold SQL 均在实际 PostgreSQL reader role 下通过同一条受控执行链路：

```text
4,500 admitted / 0 needs_human_review / 0 rejected
reader role: daa_analytics_reader
SQL execution concurrency: 8
```

此外，按风险/类别分层抽取 48 条，由 `deepseek-ai/DeepSeek-V4-Flash` 作 advisory 语义复核，
结果为 `48 pass / 0 needs_human_review`。该 LLM 审阅不是准入门；SQL Policy、只读角色和结果合同才是
确定性放行门。

完整执行记录留在仓库外：

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1/admission-20260914/
```

每个 admitted record 记录 `seed_id`、split、family、QuerySpec、Gold SQL、`gold_sql_sha256`、
Policy 最终 SQL hash、reader role、结果合同摘要及摘要 hash。全量 records SHA-256：

```text
f3fee4f280a65619f28ce79928db2f4f50c15c0cc8c5160542f315d71a836ada
```

## 4. Prompt、中文问法与长度合同

每个 QuerySpec 受控生成 5 种中文 surface form，共 22,500 条；它们不增加结构样本数，也不改变
指标、维度、时间范围或结果合同。每个训练实例按 `sha256_seed_modulo_five_v1` 稳定选择其中 1 条
主问法，其余 4 条保留为 runtime overlay，以避免模板化中文问法又不虚增训练行。

22,500 条问法均经过同款运行时路径重建：

```text
question -> QuestionRouter -> CatalogRetriever -> QueryPlan
         -> ResultContract -> candidate SQL prompt
```

无 rejection，runtime overlay hash：

```text
81774c04069d46db7c9fd297966e9d195338bedb400a1534841e1db528935649
```

以 `Qwen2.5-Coder-1.5B Base` tokenizer 检查 `rendered_prompt + canonical SQL + EOS`：

| split | token 范围 | 超长排除 |
| --- | --- | ---: |
| train | 1,066–2,917 | 0 |
| validation | 985–2,906 | 0 |
| final test | 999–2,884 | 0 |

上限为 3,072；不允许静默截断。

## 5. 仓库外最终资产与文件指纹

```text
/disk2/gengnan/data-analysis-agent-data/evals/
  olist-v3-balanced-release-v1/
    structural-20260914/
    admission-20260914/
    surface-20260914/
    runtime-prompts-20260914/
    sft-release-v1/
      train.jsonl
      validation.jsonl
      final_evaluation_only/in_domain_test.jsonl
      split_audit.json
```

| 文件 | SHA-256 |
| --- | --- |
| `train.jsonl` | `719fd0a7bfe33122705f9625c9bb9fa679fbf3c11f28fe10537921542f2c79d4` |
| `validation.jsonl` | `0d8208f96fa310d41f5c812d85b2f3c61eef4213a4e9a8b3f0955c6294cd3705` |
| `final_evaluation_only/in_domain_test.jsonl` | `1942cb53dcf0e966cb8fa4bdca4c4e7018f2f4b22e8768aa69b19df1b32fef5e` |
| merged QuerySpec structural artifact | `57290af738c54ba1755cab5451eee90abf4a69d8f816c280792479fa75996b4e` |
| merged Gold SQL structural artifact | `41ae4ee710a843533f51b8d43a365cf2fdd8323362771627c14a199868ad567c` |

## 6. 下一步

下一件独立任务应是审阅并冻结适配这份 v3 release 的 SQL-only LoRA trainer 配置（基座、模板、
batch、optimizer、validation-best checkpoint 与 matching Base/Adapter 评测合同），再由用户确认是否
占用 GPU 启动训练。不得因为 Gold admission 通过而直接声称或假设 Adapter 会提升。
