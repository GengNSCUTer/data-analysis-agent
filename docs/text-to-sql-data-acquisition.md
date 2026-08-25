# Text-to-SQL 数据获取与官方评测替代方案

## 结论

2026-08-25，用户通过本地 VPN 的临时代理使服务器能够直接下载官方 Spider `spider_data.zip`。该包已通过 ZIP 与路径安全校验，保存在仓库外的 `spider-1.0-official-v1-20260825/`；其 `dev.json`、`tables.json` 和 SQLite tree 哈希已记录。历史 Kaggle 2020-01 镜像及其已完成的 Qwen/LoRA/QLoRA 实验仍原样保留，但不再可被描述为官方当前 release 的结果。

用户此前已用同一方式将官方链接提供的 `testsuitedatabases.zip` 下载并解压到仓库外。官方 `dev.json` 的 20 个数据库在该 Test Suite 资产中均存在，逐表/列/类型比较为零 mismatch。即便结构兼容，也不能把旧镜像上的预测或分数迁移成官方包分数；任何对官方包的报告都必须从官方包重新全覆盖生成、运行固定 evaluator，并单独保留输入/输出证据。Test Suite 独立资产的条款仍须在公开声明前核验。

Spider 1.0 的普通 JSON、SQLite 数据库和 Test Suite 数据库是不同资产，不能因为一个镜像包含 `train_spider.json` 就推断它包含 Test Suite。`taoyds/test-suite-sql-eval` 的 evaluator 代码可复现，但其 README 仍要求单独下载 Test Suite 数据库；`ruiqi-zhong/TestSuiteEval` 也指向 Google Drive，并只包含少量示例库；其他 fork 主要是 Python/LLM 时代的 evaluator 改造，没有证据表明重新发布了完整官方数据库。

## 已核验的来源

| 来源 | 能否用于当前项目 | 事实与限制 |
| --- | --- | --- |
| Spider 官方页面 | 作为版本与论文入口 | 说明 Spider 许可和官方评测边界；不解决本机下载问题。 |
| 官方 Spider `spider_data.zip` | 官方数据输入 | 2026-08-25 通过临时 VPN 代理直接下载；归档 SHA-256 为 `00636695...5b121b`，外部 manifest 已记录完整哈希、布局和 Test Suite 结构兼容性。 |
| `taoyds/test-suite-sql-eval`，commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c` | evaluator 可用 | 官方代码；配套 Test Suite DB 已下载到仓库外，但官方分数仍未运行。 |
| Kaggle Spider 1.0 镜像 | 普通 SQLite 诊断可用 | 已冻结 1,034 条本地基线；镜像 metadata 许可为 unknown、版本早于官方修订，不能用于官方排行榜比较。 |
| `ruiqi-zhong/TestSuiteEval` | 仅作获取线索 | README 仍要求另一个 Google Drive 文件；仓库中的数据库是示例，不是完整 release。 |
| GitHub modernized evaluator forks | 仅作代码研究 | 没有证据证明包含官方完整 Test Suite DB；不能替换官方 evaluator/数据。 |

已下载资产记录：`testsuitedatabases.zip` 大小 `1,269,456,098` bytes，SHA-256 `9ec24ea8debc6bd04abfe137b5f1a739b5a8836f32c0464e4dfc94eb7f41da96`；解压后 3,194 个 SQLite 文件、28 个数据库目录，SQLite tree SHA-256 为 `c9529ce837eeb68a7eb98af9dfa1caf721ff566ebb871835a9910e96b3d963bd`。当前 Spider dev 的 20 个数据库均存在，逐表/列/类型比较无差异。详细外部 manifest 位于 `.../test-suite-databases-official-2020-12-27/acquisition-manifest.json`。

Hugging Face 数据集查询使用 `HF_ENDPOINT=https://hf-mirror.com` 已可返回元数据；这些派生数据仍不能证明包含 Test Suite DB。ModelScope/Kaggle 页面同样必须在下载前逐项核对 release、许可、文件树和哈希。

## 推荐获取流程

如果需要官方 Test Suite 分数，优先在可访问 Google Drive 的本地电脑完成下载，然后上传到服务器：

```bash
# 本地电脑：下载后记录文件名和 SHA-256
sha256sum test_suite_database.zip

# 服务器：只放在外部数据目录，不提交 Git
scp test_suite_database.zip ligengnan@202.38.247.145:/disk2/gengnan/data-analysis-agent-data/text-to-sql/incoming/
sha256sum /disk2/gengnan/data-analysis-agent-data/text-to-sql/incoming/test_suite_database.zip
```

上传后必须记录：原始 URL、下载时间、文件名、SHA-256、解压目录树哈希、release/commit、许可证和与当前 Spider JSON 的兼容性。解压前先列目录，确认数据库数量、文件名和 `tables.json`/gold case 的版本匹配；不匹配就标记为 `diagnostic_only`，不得运行 official bridge。

## 没有官方资产时的可行路线

1. 继续使用已冻结的 Kaggle Spider mirror 做本地 SQLite 执行诊断，报告只能写 `execution_pass`，不能写 EX/EM/Test Suite Accuracy。
2. 用 Olist/Chinook 的 DDL、脱敏指标 Catalog 和人工复核 SQL 构建项目专属 benchmark，重点评测路由、schema linking、QueryPlan、ResultContract 和安全策略。
3. 用公开且许可证明确的 Spider 派生数据只做 SFT 研究；保留来源和版本，禁止把未经核验的派生集与官方分数混用。
4. 训练/验证数据和官方 holdout 分离。所有原始数据、数据库和用户业务数据留在外部目录，仓库只提交 manifest、转换脚本、schema 和小型 fixture。

## 证据等级

- `official`: 官方页面/官方 evaluator 和已校验资产；可用于官方指标。
- `verified_mirror`: 能校验文件树、哈希、许可和兼容性；只能按镜像说明使用。
- `diagnostic_only`: 只能支持执行、安全或回归诊断，不能替代官方指标。
- `unverified`: 只有搜索结果或他人声称；禁止进入训练和发布报告。
