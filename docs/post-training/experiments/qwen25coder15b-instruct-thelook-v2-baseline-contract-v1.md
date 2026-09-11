# Qwen2.5-Coder-1.5B-Instruct：TheLook v2 直接基座对照合同 v1

**状态：** 已冻结评测设计与模型 revision；尚未启动 GPU 生成。
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

结果不得用于 TheLook 反向造 Olist 样本、few-shot、Prompt 改写、训练样本筛选或 checkpoint
选择。若 Instruct 更好，也只形成“可进入 Olist-only Schema-aware SFT 基座审阅”的前置证据，
不代表可以接入产品默认路径。
