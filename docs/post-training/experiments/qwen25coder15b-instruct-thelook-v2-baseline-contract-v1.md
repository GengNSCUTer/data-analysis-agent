# Qwen2.5-Coder-1.5B-Instruct：TheLook v2 直接基座对照合同 v1

**状态：** 已完成。600 条生成、生成证据回读和生成后 Policy / reader / ResultContract / Gold denotation 评测均已结束；TheLook 仍保持 protected。
**唯一问题：** 官方 `Qwen/Qwen2.5-Coder-1.5B-Instruct` 在同一受保护 TheLook v2
候选 SQL 任务上，是否比当前历史 `Qwen/Qwen2.5-Coder-1.5B` Base 更适合后续
Schema-aware Program SFT 的候选基座。

这不是既有 1.5B Base / Olist Adapter 的 matching pair，也不替代已有结论。Instruct
权重与官方 chat template 都不同，因此结果只能表述为“同一任务内容下的基座方案对照”，而
不能把参数量相同误写为只有一个变量发生变化。

## 最小任务卡

| 项目 | 冻结内容 |
| --- | --- |
| 目标 | 对未微调的官方 Instruct 权重生成 600 条 TheLook v2 candidate SQL，并在生成证据冻结后评测 Policy、只读执行、结果合同和 Gold denotation。 |
| 非目标 | 不训练 Adapter、不改生产路由/Prompt/Catalog/Policy、不使用 TheLook 结果构造任何 Olist 数据或调参。 |
| 输入 | 已冻结的 v2 generation-safe case 投影；其完整 SQL、Gold 与数据库行均不进入生成进程。 |
| 模型 | `Qwen/Qwen2.5-Coder-1.5B-Instruct@2e1fd397ee46e1388853d2af2c993145b0f1098a`，官方权重、bf16、无 Adapter。 |
| 输出 | Git 外 raw completions、安全生成报告、规范化候选与脱敏聚合评测报告。 |
| 验收 | 600/600 完成、模型/权重/官方模板/Prompt/decode/GPU UUID 可回读；后验阶段只在生成证据验证后读取 Gold。 |

## 可比内容与明确不可比项

下列内容与已完成的 1.5B Base / Adapter TheLook v2 实验保持一致：

- 600 条 `thelook-cross-schema-final-test-v2`、case / manifest SHA、服务器 Candidate Prompt 内容；
- `SqlPolicy → daa_thelook_reader → ResultValidator → Gold denotation` 后验链路；
- bf16、greedy (`do_sample=false`、`num_beams=1`)、`max_input=4096`、`max_new=768`、seed `20260910`；
- 生成阶段禁止读取 Gold SQL 与数据库结果，所有原始模型文本继续只保存在仓库外。

唯一有意改变的是官方 Instruct 权重和其 `tokenizer.apply_chat_template()` 形成的输入包装。
服务器完整 Prompt 字节串仍会单独 hash；Instruct chat 文本、chat template 和 token 长度也会
分别 hash / 回读，防止把模板变化误归因给模型。

因此最终报告应并列三项历史/新证据：1.5B Base、1.5B Olist Adapter、1.5B Instruct
Base。只有前两者才可作严格 Base/Adapter uplift；Instruct 对照仅用于决定后续是否值得为
Schema-aware Program SFT 准备 Instruct 训练入口。

## 三阶段隔离

```text
阶段 A：只读 generation-safe case 投影
  question + QuerySpec + ResultContract + 完整服务端 Catalog
    -> 原样 server prompt 作为一个官方 Qwen user turn
    -> Qwen2.5-Coder-1.5B-Instruct greedy candidate completion
    -> Git 外 raw-completions + 不含题目/SQL 的 safe-report

阶段 B：无 Gold 的证据回读
  下载 manifest + official chat template + Prompt hash + token preflight
  + raw-completion hash + 600 case 顺序
    -> 验证通过后才允许阶段 C

阶段 C：统一后验评测
  unwrap_sql_completion
    -> SqlPolicy -> readonly PostgreSQL -> ResultContract
    -> Gold denotation（唯一读取 Gold 的阶段）
```

## 最终聚合结果与基座决策

三条路线均在同一 600 条 protected TheLook v2 case 上经 `SqlPolicy -> readonly
PostgreSQL -> ResultContract -> Gold denotation` 后置链路统计。历史 Base / Olist Adapter 是
严格 matching pair；Instruct 使用官方 chat template，因而只是基座方案对照，不能视作单变量
消融。

| 路线 | Policy accepted | PostgreSQL executed | ResultContract valid | ordered-or-bag Gold match |
| --- | ---: | ---: | ---: | ---: |
| 历史 Qwen2.5-Coder 1.5B Base | 259/600 (43.2%) | 173/600 (28.8%) | 127/600 (21.2%) | 67/600 (11.2%) |
| Qwen2.5-Coder 1.5B-Instruct，无 Adapter | 354/600 (59.0%) | 228/600 (38.0%) | 159/600 (26.5%) | 64/600 (10.7%) |
| 历史 Qwen2.5-Coder 1.5B Base + Olist Adapter | 467/600 (77.8%) | 335/600 (55.8%) | 313/600 (52.2%) | 225/600 (37.5%) |

Instruct 比历史 Base 多通过了前序 Policy / 执行 / 合同闸门，但最终 Gold 正确数没有实用提升，
且其合同有效候选中的 ordered-or-bag match 为 `64/159 (40.3%)`，低于 Base 的 `67/127
(52.8%)`。特别是 Instruct 有 `42` 条 column mismatch（Base 为 `5`），表明结果 alias / 列合同和
schema binding 仍是主问题。故下一阶段不为 Instruct 新建训练入口，选择原始
`Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b` 作为新的
Schema-aware Program LoRA 的受控起点；当前 Olist Adapter 仅保留为固定对照。

外部运行证据：`experiments/qwen25coder15b-instruct-thelook-v2-baseline-v1-20260911/` 下的
`generation/safe-report.json` 与 `execution-evaluation/evaluation-report.json`。没有回读或修改
TheLook case 来构造 Olist 数据、few-shot、Prompt、checkpoint 选择或错误驱动规则。
