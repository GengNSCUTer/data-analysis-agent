# 可信数据分析智能体

面向电商经营分析的嵌入式 Copilot：用户提出中文问题，服务端结合指标目录、数据库结构和当前分析状态生成候选 SQL，经只读执行与结果校验后，返回表格、图表、中文结论及数据依据。

项目基于 Vanna 扩展，当前在线工作区是 Olist PostgreSQL。TheLook 是另一个隔离电商工作区，参与离线训练与评测，尚未接入默认产品页。当前是个人开发的可运行原型，未主张商业交付或生产 SLA。

## 当前能力

- **业务查询与澄清**：结构化语义解释器理解问题与上下文，AnalysisState 保存当前分析；Semantic Catalog 和 QueryPlan 校验指标、时间、分组与追问。缺少必要条件时做最小澄清。
- **可信执行**：`sqlglot` AST Policy 校验单条只读查询、Schema/表/列白名单、敏感字段投影和函数；PostgreSQL reader role、超时与行数预算提供数据库层约束；ResultValidator 校验结果合同。合同通过不等于业务语义已被证明。
- **结果与图表**：聚合结果按查询计划派生预算，另有进程行数/字节/超时上限；页面预览最多 200 行、模型摘要最多 8 行。支持柱状图、横向柱状图、折线图、面积图和环形图，服务端基于有效 ChartContract 直接绘图。
- **会话与上下文**：PostgreSQL 会话存储、workspace 隔离、状态版本冲突检测、可信结果追问与回答后后台摘要；历史按 token 读取，24K 触发压缩、约 16K 保留原文，摘要失败保留未压缩历史；摘要不能成为 SQL 条件或权限依据。
- **工件与下载**：完整 CSV/Plotly 工件保存在仓库外，绑定用户、工作区和版本；完整 CSV 下载检查归属与完整性并记录导出审计。通用预览导出按钮关闭。
- **嵌入与身份**：`/embedded-copilot` 提供会话列表、保存分析、查看依据和移动端适配；支持本地账号会话和绑定允许来源的 5 分钟嵌入令牌。产品身份解析器不接受旧演示角色 cookie。
- **模型实验**：完成 Qwen2.5-Coder-1.5B Base 的 bf16 LoRA SQL-only SFT、validation-best checkpoint 选择和完整 Gold 对照；在线页面仍使用 SiliconFlow，本地 Adapter 尚未接入。

偏好目前只保存供回看，尚未实际改变展示或 SQL；长期业务事实记忆和组织级行权限尚未实现。

## 请求链路

```text
业务页面 / Web Component
  -> 服务端身份与 workspace
  -> Router / Catalog / WorkingMemory / QueryPlan
  -> 在线模型生成候选 SQL
  -> 计划一致性检查 / AST Policy / PostgreSQL 只读执行
  -> ResultContract / ResultValidator
  -> ResultArtifact / 服务端图表 / 有依据的回答
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
# 确定性链路回归，不调用在线模型
pytest -q tests/test_sql_policy.py tests/test_question_router.py \
  tests/test_result_validator.py tests/test_analysis_lifecycle.py \
  tests/test_result_artifact_store.py tests/test_local_auth.py \
  -k 'not login_and_embed_route_keep_subject_on_the_server'

# 已初始化项目数据库的定向集成验证
RUN_PROJECT_DB=1 pytest -q tests/test_postgres_runner.py tests/test_postgres_conversation_store.py

# 当前产品演示，调用在线模型，需既有服务与凭据
RUN_VANNA_LIVE=1 pytest -q tests/test_w4_04_product_demo.py
```

GitHub Actions 覆盖确定性回归、Web Component 构建和离开源码目录的 wheel 安装检查；真实模型与数据库重启验收需显式运行，不纳入无凭据的 CI。

## 文档入口

- [PROJECT.md](PROJECT.md)：项目事实与进度，历史实验按日期保留。
- [文档导航](docs/README.md)：运行时架构、指标合同、评测与学习入口。
- [部署说明](docs/deployment.md)：安装、合成数据初始化、持久化与重启恢复。
- [当前产品请求架构](docs/architecture/product-request-architecture-v1.md)：模型与代码的分工。
- [后训练总览](docs/post-training/README.md)：当前主实验与历史实验的区别。
- [目录与代码审阅地图](docs/architecture/repository-map.md)：各模块位置和职责。

## 开源与数据边界

上游 Vanna 代码许可证见 [LICENSE](LICENSE)。公开数据的许可与代码许可分别管理，不能因为代码开源就假设可以重新分发数据或派生资产。Olist 来源与已记录许可见 [数据清单](data/manifest/datasets.yaml)；原始数据、数据库结果、模型权重、账号信息和 .env 均不提交至本仓库。

本地登录与来源绑定令牌是当前原型能力，不代表已完成企业 SSO、多租户治理、多实例协调或企业部署验收。
