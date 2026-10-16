# 可信数据分析智能体

面向电商经营分析的嵌入式 Copilot：用户提出中文问题，服务端结合指标目录、数据库结构和当前分析状态生成候选 SQL，经只读执行与结果校验后，返回表格、图表、中文结论及数据依据。

项目基于 Vanna 扩展，当前在线工作区是 Olist PostgreSQL。TheLook 是另一个隔离电商工作区，参与离线训练与评测，尚未接入默认产品页。当前是个人开发的可运行原型，未主张商业交付或生产 SLA。

## 当前能力

- **业务查询与澄清**：QuestionRouter、Semantic Catalog、运行时 QueryPlan 和 WorkingMemory 约束指标、时间、分组与追问；缺少必要条件时先澄清。
- **可信执行**：`sqlglot` AST Policy 校验单条只读查询、Schema/表/列白名单、敏感字段投影和函数；PostgreSQL reader role、超时与行数预算提供数据库层约束；ResultValidator 校验结果合同。合同通过不等于业务语义已被证明。
- **结果与图表**：执行最多 1,000 行，页面预览最多 200 行；支持柱状图、横向柱状图、折线图、面积图和环形图，服务端基于有效 ChartContract 直接绘图。
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

前置条件：Python 3.12、Node.js 22、已初始化并加载分析表的 PostgreSQL，以及 OpenAI-compatible 模型凭据。以下流程用于既有开发环境；从全新机器一键安装仍是后续交付任务。

```bash
conda activate data-analysis-agent
pip install -e ".[test,fastapi,openai]" python-dotenv
cd frontends/webcomponent
npm install --package-lock=false
npm run build
cd ../..
PYTHONPATH=src DATA_ANALYSIS_DEMO_PORT=32013 python examples/trusted_olist_web_demo.py
```

本地 .env 配置模型，数据库连接、bootstrap 账号和会话密钥由服务端环境配置。不要提交真实值。长期运行应显式设置稳定的 DATA_ANALYSIS_DEMO_SESSION_SECRET；缺省随机密钥会使旧会话在重启后失效。

```dotenv
SILICONFLOW_API_KEY=your_key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V3.2
```

打开 http://127.0.0.1:32013/embedded-copilot；/embedded-demo 重定向到产品页。登录和演示顺序见 [演示脚本](docs/demo-script.md)，数据库准备见 [PostgreSQL 基础设施](infra/postgres/)。

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

GitHub Actions 当前覆盖一组确定性回归与 Web Component 构建，尚未覆盖全部新增产品路径。

## 文档入口

- [PROJECT.md](PROJECT.md)：项目事实与进度，历史实验按日期保留。
- [文档导航](docs/README.md)：运行时架构、指标合同、评测与学习入口。
- [当前产品请求架构](docs/architecture/product-request-architecture-v1.md)：模型与代码的分工。
- [后训练总览](docs/post-training/README.md)：当前主实验与历史实验的区别。
- [目录与代码审阅地图](docs/architecture/repository-map.md)：各模块位置和职责。

## 开源与数据边界

上游 Vanna 代码许可证见 [LICENSE](LICENSE)。公开数据的许可与代码许可分别管理，不能因为代码开源就假设可以重新分发数据或派生资产。Olist 来源与已记录许可见 [数据清单](data/manifest/datasets.yaml)；原始数据、数据库结果、模型权重、账号信息和 .env 均不提交至本仓库。

本地登录与来源绑定令牌是当前原型能力，不代表已完成企业 SSO、多租户治理、多实例协调或企业部署验收。
