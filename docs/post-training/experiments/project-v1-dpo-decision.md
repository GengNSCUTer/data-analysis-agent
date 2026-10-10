# Project v1：DPO 当前不启动的证据与重新准入门

> **状态：** `stop_no_dpo`；这是一次有证据的停止，不是 DPO 实现故障。  
> **审查日期：** 2026-09-18  
> **范围：** 只读取 train/validation 诊断的脱敏审计摘要；不读取 protected final test、问题、Gold SQL、原始 completion 或结果行。

## 1. DPO 在本项目原本要学什么

SQL-only SFT 的监督是：

```text
prompt -> canonical Gold SQL
```

DPO 需要的是同一字节级 Prompt 上可复核的一对回答：

```text
prompt + chosen Gold SQL + rejected parent-model SQL
```

其中 `rejected` 不能是随手写的坏 SQL、不能是把 Gold 改几个字符的伪负例，也不能来自 protected test。它必须是冻结的 parent Adapter 真正生成过、并在治理/Gold 对照中有明确失败证据的 SQL。否则训练到的只是人工制造的格式偏好，而不是模型当前真实会犯的业务错误。

## 2. 本轮真实诊断结果

所有候选均来自当前 multi-schema SFT release，严格区分 train 和 validation 用途：

| 来源 | 选择方式 | 生成数 | Policy / PostgreSQL / ResultContract | Gold 结果 | DPO pair |
| --- | --- | ---: | --- | --- | ---: |
| train-only stratified diagnostic | 每 workspace 6 个 source、每 source 4 次采样；按 result shape、scenario family、risk tag 分层 | 48 | 48 / 48 / 48 | 48/48 Gold match | 0 |
| validation-only diagnostic | 同样的 workspace / shape / family / risk-tag 分层，仅作 checkpoint/泛化诊断 | 48 | 48 / 48 / 48 | 48/48 Gold match | 不写 pair |

两批均使用冻结 parent、同一 decode 合同与相同的基本采样参数：`temperature=0.7`、`top_p=0.9`、`max_input_tokens=3072`、`max_new_tokens=768`。Train 诊断的 raw completion 与 pair 文件均在仓库外；`pairs.jsonl` 的 SHA-256 是空文件哈希，符合预期。

这不是“模型从此不会错”。它只说明：在当前合成/受控 release 所抽到的、与训练分布高度接近的 96 次采样里，没有出现符合 A/B/C 级证据的真实 rejected SQL。因此并不存在可合法用于 DPO 的错误对。

## 3. 为什么不把 validation 或 final test 的错误拿来训练

validation 的角色是训练期模型选择和诊断，不应进入梯度更新；两套 protected final test 更是永久隔离。若把它们的 Gold 或错误回流，最终分数就不再能说明模型面对未见问题和 Schema 时的能力。

更重要的是，本轮 validation 采样也没有产生错误。即使有，也只能用于 validation preference loss/质量门，不能作为 DPO train pair。

## 4. 当前停止判断

现有 DPO 合同规定小批至少要在**每个 workspace**得到：

- 3 条 A--C 级 pair；
- 至少 3 个 scenario family；
- 至少 2 种 result shape；
- 至少 2 种 risk tag；
- 每条 pair 都有 Prompt/Gold/rejected hash、parent provenance 和唯一的可审计失败类别。

本轮两个 workspace 的 A--C pair 数均为 0，所以四项门全部不满足，审计状态为：

```text
stop_no_dpo
```

因此当前不应启动 DPO Trainer，也不应通过以下方式“补足”数据：

- 手工伪造 rejected SQL；
- 将正确 SQL 假装成 rejected；
- 仅换空格、注释、大小写的形式差异；
- 从 Olist/TheLook protected final test 借错误；
- 为了获得错误而把原有运行时安全链拿掉。

## 5. DPO 何时可以重新考虑

不是“先训一次看看”，而是先满足下面的重新准入顺序：

1. 建立一个**新的、非 protected、非当前 SFT 实例复写**的开发挑战来源；它必须与最终测试隔离，并有自己的 workspace、family、program、question、Gold 与 hash 审计。
2. 让冻结 parent Adapter 在相同线上候选 Prompt 语义下生成；先保存 raw completion，再在后置阶段读 Gold 做 `Policy -> reader -> ResultContract -> denotation` 分类。
3. 若自然产生的 A/B/C pair 达到上节两 workspace 的覆盖门，先做人审 pair-quality review，确认 chosen/rejected 的差异确实对应 schema linking、join、时间/状态过滤、指标口径或结果合同，而不是 transient database error 或展示包装。
4. 只有 review 通过，才冻结独立的 `train_pairs.jsonl`；DPO validation pair 必须从独立 validation source 生成。随后才讨论 parent/reference 多 adapter 加载、显存和 checkpoint 合同。
5. 训练后只选一个 validation-best checkpoint，在两套已冻结 final test 上做**一次** Base / SFT parent / DPO delta 对照。

这条路线的重点是：DPO 可以是后续可学的技术，但它不是目前项目最缺的东西。当前更高价值的是缩小“合成训练合同高度拟合”与“真实产品运行时/用户体验”之间的距离。

## 6. 证据位置

- DPO 数据与停止条件合同：[`../data/multi-schema-dpo-v1-contract.md`](../data/multi-schema-dpo-v1-contract.md)
- 主 SFT 模型与最终评测：[`project-v1-model-baseline.md`](project-v1-model-baseline.md)
- train-only 诊断：仓库外 `evals/multi-schema-dpo-candidate-stratified-v1-20260917/safe-audit.json`
- validation-only 诊断：仓库外 `evals/multi-schema-validation-generation-diagnostic-v1-20260917/safe-audit.json`
