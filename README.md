# 可信数据分析智能体

面向电商经营分析的嵌入式 Copilot：用户提出中文问题，服务端结合指标目录、数据库结构和当前分析状态生成候选 SQL，经只读执行与结果校验后，返回表格、图表、中文结论及数据依据。

项目基于 Vanna 扩展，默认在线工作区是 Olist PostgreSQL；TheLook 已作为独立在线实例验证。服务端配置 Catalog、表列、指标、维度、权限和页面说明后，两套业务复用核心分析链路，参见 [业务库接入说明](docs/workspace-onboarding.md)。当前是个人开发的可自托管原型，未主张商业交付或生产 SLA。

## 当前能力

- **业务查询与澄清**：结构化语义解释器理解问题与上下文，AnalysisState 保存当前分析；Semantic Catalog 和 QueryPlan 校验指标、时间、分组与追问。缺少必要条件时做最小澄清。
- **可信执行**：`sqlglot` AST Policy 校验单条只读查询、Schema/表/列白名单、敏感字段投影和函数；PostgreSQL reader role、超时与行数预算提供数据库层约束；ResultValidator 校验结果合同。合同通过不等于业务语义已被证明。
- **结果与图表**：聚合结果按查询计划派生预算，另有进程行数/字节/超时上限；页面预览最多 200 行。模型在 token 预算内读取完整小结果或服务端统计，追问可补读同一授权工件；服务端计算、模型解释。支持柱状图、横向柱状图、折线图、面积图和环形图，服务端基于有效 ChartContract 直接绘图。
- **会话与上下文**：PostgreSQL 会话存储、workspace 隔离、状态版本冲突检测、可信结果追问与回答后后台摘要；历史按 token 读取，24K 触发压缩、约 16K 保留原文，摘要失败保留未压缩历史；摘要不能成为 SQL 条件或权限依据。
- **工件与下载**：完整 CSV/Plotly 工件保存在仓库外，绑定用户、工作区和版本；完整 CSV 下载检查归属与完整性并记录导出审计。通用预览导出按钮关闭。
- **嵌入与身份**：`/embedded-copilot` 提供会话列表、保存分析、查看依据和移动端适配；支持本地账号会话和绑定允许来源的 5 分钟嵌入令牌。产品身份解析器不接受旧演示角色 cookie。
- **运行保障**：单进程默认同时执行4个分析、等待8个、排队15秒、整轮180秒，前台与摘要共用4个模型调用槽；支持停止生成、SQL取消和退出后的凭证失效。断流不自动重发，完整结果/缓存默认保留7天。配置与实测见 [部署说明](docs/deployment.md#单实例容量取消与结果保留) 和 [容量报告](evals/results/runtime_capacity.md)。
- **模型实验**：完成 Qwen2.5-Coder-1.5B Base 的 bf16 LoRA SQL-only SFT、validation-best checkpoint 选择和完整 Gold 对照；在线页面仍使用 SiliconFlow，本地 Adapter 尚未接入。

偏好目前只保存供回看，尚未实际改变展示或 SQL；长期业务事实记忆和组织级行权限尚未实现。

## 请求链路

```text
业务页面 / Web Component
  -> 服务端身份与 workspace
  -> 语义解释器 / Catalog / AnalysisState / QueryPlan
  -> 在线模型生成候选 SQL
  -> 计划一致性检查 / AST Policy / PostgreSQL 只读执行
  -> ResultContract / ResultValidator
  -> ResultArtifact / 服务端图表 / 有依据的回答
  -> 保存会话与分析状态；需要时后台压缩历史
```

离线数据构造中的 QuerySpec 与在线 QueryPlan 职责不同；确定性 Gold renderer 不负责理解用户问题。

## 本地运行

新环境优先使用 Compose，初始化独立 PostgreSQL 和自构造合成电商数据，不分发真实 Olist 数据。需要 Docker Compose v2、Docker 权限和模型 API key：

```bash
cp .env.example .env
# 填入模型 key 与三个不同的随机数据库密码
docker compose --env-file .env -f infra/postgres/compose.yaml up -d --build
curl -f http://127.0.0.1:32013/health
```

打开 http://127.0.0.1:32013/embedded-copilot。首次密码读取、无 Docker 的 wheel 安装、配置、持久化、重启与排错见 [部署说明](docs/deployment.md)。安装后的命令是 `data-analysis-agent`，不需要 Conda、`PYTHONPATH=src` 或前端开发服务器。默认签名密钥持久化在应用状态目录，重启保留未过期登录。

## 评测与后训练证据

在线产品评测与离线小模型评测分别记录。确定性测试通过、SQL 能执行、ResultContract 有效、Gold denotation 一致是不同层次，不能互相替代。

主模型使用 Olist 与 TheLook 各 3,000 条训练、750 条 validation，共 6,000/1,500；最终测试各 750 条。两个 Schema 都进入过训练，因此这是已见 Schema 的留出评测。adapter_best 在 Olist ordered Gold 对照为 403/750，TheLook 为 396/750；详情见 [模型实验卡](docs/post-training/experiments/project-v1-model-card.md)。DPO 因缺少真实、可复核的 rejected pair 暂停，未训练。

```bash
# 确定性链路回归，不需要模型 key、数据库或 GPU
DATA_ANALYSIS_ENV_FILE=/dev/null python -m pytest -q \
  -m 'not integration and not postgres' \
  tests/test_sql_policy.py tests/test_semantic_intent.py \
  tests/test_analysis_state.py tests/test_analysis_context.py \
  tests/test_result_validator.py tests/test_result_artifact_store.py tests/test_local_auth.py

# 已初始化的独立合成数据库；测试会写应用元数据
RUN_PROJECT_DB=1 python -m pytest -q -m postgres \
  tests/test_postgres_runner.py tests/test_postgres_conversation_store.py \
  tests/test_local_auth.py tests/test_result_artifact_routes.py

# 当前产品演示，调用在线模型，需既有服务与凭据
RUN_VANNA_LIVE=1 pytest -q tests/test_w4_04_product_demo.py
```

GitHub Actions 在 push/PR 上分层运行确定性回归、Web Component/wheel构建、临时PostgreSQL
集成和受控模型的真实浏览器测试。真实在线模型及完整重启验收显式手动运行，不由CI消费
模型额度。完整命令与测试含义见 [测试说明](docs/testing.md)。

## 文档入口

- [PROJECT.md](PROJECT.md)：项目事实与进度，历史实验按日期保留。
- [文档导航](docs/README.md)：运行时架构、指标合同、评测与学习入口。
- [部署说明](docs/deployment.md)：安装、合成数据初始化、持久化与重启恢复。
- [新业务库填写模板](docs/templates/workspace/README.md)：需要准备的Schema、指标口径、关联与权限，附TheLook填写实例和检查命令。
- [当前产品请求架构](docs/architecture/product-request-architecture-v1.md)：模型与代码的分工。
- [后训练总览](docs/post-training/README.md)：当前主实验与历史实验的区别。
- [目录与代码审阅地图](docs/architecture/repository-map.md)：各模块位置和职责。
- [面试项目事实](docs/interview/project-release-facts-v1.md)：贡献、测量分母、失败修复、取舍与未验证项。
- [贡献与试用](CONTRIBUTING.md)、[安全说明](SECURITY.md)、[第三方资产](THIRD_PARTY.md)：开发、脱敏反馈与许可边界。

## 开源与数据边界

上游 Vanna 代码许可证见 [LICENSE](LICENSE)。公开数据的许可与代码许可分别管理，不能因为代码开源就假设可以重新分发数据或派生资产。Olist 来源与已记录许可见 [数据清单](data/manifest/datasets.yaml)；原始数据、数据库结果、模型权重、账号信息和 .env 均不提交至本仓库。

本地登录与来源绑定令牌是当前原型能力，不代表已完成企业 SSO、多租户治理、多实例协调或企业部署验收。

Olist真实数据的发布者标为CC BY-NC-SA 4.0，当前TheLook镜像许可为Unknown；演示只分发自构造
合成样本。资产来源与核对方式见 [第三方资产清单](THIRD_PARTY.md)。尚无外部开发者独立部署
或企业使用记录；没有正式tag/release，也不自动发布PyPI同名包。
