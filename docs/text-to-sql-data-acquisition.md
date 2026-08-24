# Text-to-SQL 数据获取与官方评测替代方案

## 结论

当前服务器无法访问 Spider Test Suite 所需的 Google Drive 资产。我们没有找到一个能够同时证明“完整、来源明确、版本兼容、许可可用”的 GitHub 镜像，因此不使用第三方压缩包冒充官方 Test Suite，也不发布由它得到的官方分数。

Spider 1.0 的普通 JSON、SQLite 数据库和 Test Suite 数据库是不同资产，不能因为一个镜像包含 `train_spider.json` 就推断它包含 Test Suite。`taoyds/test-suite-sql-eval` 的 evaluator 代码可复现，但其 README 仍要求单独下载 Test Suite 数据库；`ruiqi-zhong/TestSuiteEval` 也指向 Google Drive，并只包含少量示例库；其他 fork 主要是 Python/LLM 时代的 evaluator 改造，没有证据表明重新发布了完整官方数据库。

## 已核验的来源

| 来源 | 能否用于当前项目 | 事实与限制 |
| --- | --- | --- |
| Spider 官方页面 | 作为版本与论文入口 | 说明 Spider 许可和官方评测边界；不解决本机下载问题。 |
| `taoyds/test-suite-sql-eval`，commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c` | evaluator 可用 | 官方代码；完整 Test Suite DB 仍需外部下载。 |
| Kaggle Spider 1.0 镜像 | 普通 SQLite 诊断可用 | 已冻结 1,034 条本地基线；镜像 metadata 许可为 unknown、版本早于官方修订，不能用于官方排行榜比较。 |
| `ruiqi-zhong/TestSuiteEval` | 仅作获取线索 | README 仍要求另一个 Google Drive 文件；仓库中的数据库是示例，不是完整 release。 |
| GitHub modernized evaluator forks | 仅作代码研究 | 没有证据证明包含官方完整 Test Suite DB；不能替换官方 evaluator/数据。 |

Hugging Face 数据集 API 在本服务器的请求超时，未得到可核验的 dataset ID、文件树、许可证和 Test Suite DB 信息。因此本轮不把 HF 搜索结果写成数据依赖。ModelScope/Kaggle 页面同样必须在下载前逐项核对 release、许可、文件树和哈希。

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

