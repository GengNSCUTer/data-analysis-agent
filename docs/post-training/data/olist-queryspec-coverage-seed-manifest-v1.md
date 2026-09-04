# Olist QuerySpec 静态 Coverage Seed 清单 v1

**状态：** 已设计并以小型静态 JSONL 提交；尚未调用物化器，未生成 QuerySpec、Gold SQL、Prompt 或训练行，未执行 SQL、未读取 protected holdout、未加载 tokenizer/GPU。

**清单位置：** [`../../../data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl`](../../../data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl)。
**上游合同：** [`olist-domain-sft-data-contract-v1.md`](olist-domain-sft-data-contract-v1.md)、[`olist-domain-sft-coverage-matrix-v2.md`](olist-domain-sft-coverage-matrix-v2.md)、[`olist-queryspec-renderer-design-v1.md`](olist-queryspec-renderer-design-v1.md)。

## 1. 清单是什么

每一行只是受控物化器所需的结构化输入：有序指标 ID、允许的结果形状、固定维度、确认的时间合同、固定
Join program 和显式 split。它没有用户问题、模型 Prompt、SQL、查询结果、排序、Top-N 或自由过滤。

例如 `olist-v1-train-009-item-metrics-monthly` 的含义是“商品行指标 `gmv`、`item_count`、
`freight_amount` 按购买月形成一个时间序列”，而不是一条中文问题或已经生成的 SQL。未来只有在独立
protected-summary 准入通过后，materializer 才会把它转换为 QuerySpec 和确定性 Gold SQL。

## 2. 为什么是 15 条

这是用于审查接口与覆盖边界的最小清单，不是最终 SFT 数据量，也不能据此评测模型。它有 10 条 train、
3 条 validation、2 条 in-domain test，覆盖了十项指标与当前允许的四种结果形状：

| 覆盖点 | 代表 seed | 作用 |
| --- | --- | --- |
| 商品行标量、州、品类、购买时间序列 | 001、002、006、007、009 | 覆盖 GMV、商品行数、运费的三种安全事实路径。 |
| 订单标量、州、购买时间序列 | 003--005、010、014 | 覆盖履约、客单价、取消率、准时率的订单粒度与 AOV 两层聚合边界。 |
| 评价标量、州、评价时间序列 | 011--013 | 覆盖好评率、平均评分和独立评价时间字段。 |
| 多事实标量 / 多指标州 | 008、015 | 验证独立 CTE 再组合，而不是跨事实明细裸 Join。 |

所有时间区间均为 `start <= t < end_exclusive`。绝对区间只用于检查时间合同，不能凭借不同日期端点制造
更多 family。

## 3. Split 设计和明确限制

当前受控物化器规定：同一个 `join_program_id` 只能出现在一个 split。清单据此人工分配：

| split | Join programs | 行数 |
| --- | --- | ---: |
| `train` | `JP01`、`JP02`、`JP04`、`JP07`、`JP09`、`JP11` | 10 |
| `validation` | `JP03`、`JP06`、`JP12` | 3 |
| `in_domain_test` | `JP05`、`JP10` | 2 |

这样可以先验证“程序不跨 split”的 fail-closed 规则，但也带来明显偏差：validation/test 不会同时拥有所有
查询程序与指标形状。因此它不是公平的最终模型评测划分，不得用来报告准确率、选择 checkpoint 或判断
领域 LoRA 的质量。最终规模化方案必须在不放松隔离规则的情况下，重新冻结更多独立 SQL program / family
与分布目标。

## 4. Protected Summary 边界

本仓库不会保存 protected family fingerprint summary 的真实内容，也不会保存空摘要冒充保护已经生效。
未来 materializer 的 `--protected-summary-json` 必须指向仓库外、受限生成的 JSON 文件，且只能具有：

```json
{
  "summary_version": "olist-protected-family-summary-v1",
  "family_fingerprints": ["64-character SHA-256 values only"]
}
```

它不得包含 case ID、问题、Prompt、SQL、执行结果或可逆 family 原文。受限导出器现已冻结权限来源、输入版本、
代码 hash、输出 hash、人工复核引用和保存路径；但真实 summary 尚未生成，因而仍禁止使用本清单实际物化。

## 5. 审阅和验收

[`../../../tests/test_olist_queryspec_coverage_seed_manifest.py`](../../../tests/test_olist_queryspec_coverage_seed_manifest.py)
只验证 JSONL 字段、`QuerySpec.create_validated()`、family/program/split 隔离和预期覆盖。它不调用 renderer、
materializer CLI、PostgreSQL 或 tokenizer。通过测试只说明静态输入满足当前结构合同，不说明指标口径、SQL
执行、结果合同或模型泛化已经通过。

下一项：单独设计并审阅带 evidence 绑定的小批 Gold 准入/结构物化；在真实 summary 导出并获批准前，不物化本清单。
