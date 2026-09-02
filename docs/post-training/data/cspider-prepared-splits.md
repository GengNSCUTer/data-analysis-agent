# CSpider 官方三切分 SFT 输入

## 目标与边界

本记录描述从已冻结的 CSpider full release 构造 SFT 输入的过程。目标是
提供可追溯的中文 Text-to-SQL `train`、`validation` 和最终 `test` 资产，
并保留官方切分边界。它不加载 tokenizer 或模型，不执行训练、评测或运行时
接入，也不改变 Vanna/PostgreSQL 的安全和结果合同。

输入是仓库外的
`/disk2/gengnan/data-analysis-agent-data/text-to-sql/cspider/cspider-1.0-official-2026-09-01/extracted/`，
其获取证据见 [CSpider 获取与预检](cspider-acquisition.md)。输出同样留在
仓库外的 `prepared/official-splits-v1/`，不进入 Git。

## 构造契约

规范脚本是
[`scripts/post_training/data/build_cspider_sft_splits.py`](../../../scripts/post_training/data/build_cspider_sft_splits.py)。
它复用 `spider-sft-schema-question-sql-v2` 的 schema 序列化、中文问题规范化和
SFT 文本格式：

```text
### SQLite schema
<版本化 schema 文本>
### Question
<中文问题>

### SQL
<gold SQL>
```

构造前，脚本核对 acquisition manifest 的 release、源文件 hash、解压树 hash、
官方记录/数据库数量、`db_id/question/query` 字段、表元数据和 train/dev/test
的 schema 无交集。每条候选在所属 SQLite 文件上进行只读 `EXPLAIN`。这是语法及
对象解析检查，不证明 SQL 的业务语义、执行结果或模型日后的生成质量。

官方角色映射保持如下：

| 官方输入 | 输出 split | 允许用途 | 物理位置 |
| --- | --- | --- | --- |
| `train.json` | `train` | 参数更新 | `train.jsonl` |
| `dev.json` | `validation` | 选择模型/超参数，不更新参数 | `validation.jsonl` |
| `test_data/test.json` | `test` | 最终一次性评测 | `final_evaluation_only/test.jsonl` |

`test` 不得进入训练、few-shot、数据合成、模型选择或超参数决策。输出 audit 的
`primary_group` 为真实的 `cspider_db_id`。Trainer 的输入审计已适配两种显式已知
协议：历史 Spider 候选切分保持原有 `spider_db_id` 与 v2 holdout 校验；CSpider
只接受 `official_cspider_train_dev_test`，并核验官方角色、三组零 schema 重叠、test
元数据、`final_evaluation_only/` 路径、test 禁训标记、SQLite `EXPLAIN` 摘要及当前
train/validation 文件的行数和 SHA-256。未知策略或任一证据缺失均拒绝。

## 产物与核验结果

构造时间固定为 `2026-09-01T00:00:00Z`，对应的 `split_audit.json` 如下：

| 输出 | SFT 行数 | 官方源行数 | schema 组 | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| `train.jsonl` | 8,656 | 8,659 | 146 | `542836d0e61a576af20a7e3b5575e430e0fa3c22cb6cfd10aa1669ce8d896732` |
| `validation.jsonl` | 1,034 | 1,034 | 20 | `50d5022bde8f496273fd2e87dd645e162702e7c43733954f8de10c820323e41b` |
| `final_evaluation_only/test.jsonl` | 2,147 | 2,147 | 40 | `cfd2e43034d7fc1bdb8d1cd53621045ae7149af222b8162e66ce00973501c2f0` |

train、validation、test 的 `db_id` 交集均为空。三份输出的实际行数和 SHA-256
均与 audit 一致；test 被物理放在 `final_evaluation_only/`。

### 官方 train 的三个来源质量排除项

构造器没有静默把 SQLite 无法解析的 gold SQL 加入训练监督。CSpider official
`train` 有 3/8,659 条在随 release 提供的 SQLite/schema 上无法通过只读
`EXPLAIN`，因此存入 `source_quality_exclusions/train.jsonl`，并记录错误证据和
exclusion reason。该文件 SHA-256 为
`54d842727d78f1ae4d5df2f9c10bbe5aba3de84572cbc85968c009822bad2c7d`。

| 官方索引 | `db_id` | SQLite 发现 | 处理 |
| ---: | --- | --- | --- |
| 3153 | `assets_maintenance` | 缺失 `Ref_Company_Types` 表 | 不参与 SFT 参数更新 |
| 4513 | `document_management` | SQLite 不接受 `INTERSECT` 前的 `ORDER BY` | 不参与 SFT 参数更新 |
| 4514 | `document_management` | SQLite 不接受 `INTERSECT` 前的 `ORDER BY` | 不参与 SFT 参数更新 |

这是一条通用的来源质量门：任一公开 gold 在其发布方提供的受控数据库上无法
`EXPLAIN`，均隔离而非修补、猜测或混入监督。它不宣称这三条在其他方言或评价器中
一定无效，也不修改原始 release。dev/test 均为全量保留，且 test 绝不因这一检查
转入训练。

## 验证与尚未覆盖项

- `pytest -q tests/test_build_cspider_sft_splits.py`：`3 passed`，覆盖官方角色映射、
  中文 prompt、test 隔离、源树漂移 fail closed 和无效 SQL 隔离。
- `ruff check`、`python -m py_compile`、`git diff --check`：通过。
- 真实构造完成；所有 11,840 条官方记录已被审计，`EXPLAIN` 结果为 train
  `8,656 pass + 3 error`、dev `1,034 pass`、test `2,147 pass`。

尚未执行 token 长度统计、token 截断策略、模型训练、验证集模型选择或 test 评测。
下一项工作只能在用户确认后单独进行，建议先在加载 tokenizer 前对 CSpider 的全量
train/validation 做 token 长度分布审计并定义“不截断、超长 fail closed”的训练长度
合同；最终 test 继续不参与该过程。
