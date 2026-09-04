# Data Analysis Agent 协作与质量约束

本文件只维护稳定的协作规则、接口不变量和安全边界，不维护项目进度、实验流水账或临时状态。当前项目事实以 `PROJECT.md`、`docs/README.md`、`docs/post-training/README.md` 和飞书项目文档为准。

## 1. 第一性原则

- 先定义问题和验收证据，再写代码。每项实质任务先写最小任务卡：目标、非目标、输入、输出、关键不变量、验收方式。
- 复杂度必须服务于明确风险。安全、权限、数据泄漏、SQL 执行、模型训练和数据质量需要强校验；低风险样板、格式化和重复性转换不做无收益的过度审查。
- 测试通过只证明被覆盖的行为；loss 下降、单次成功、模型生成更短或 AI 审阅通过，都不能直接推出业务语义正确、泛化有效或可上线。
- 任何自动化扩展都必须可复现、可回读、可回滚，并明确记录实际覆盖量与未覆盖项。不能为了达到目标行数复制样本、伪造执行证据或放宽失败门。

## 2. 每次迭代闭环

1. 开始前读取相关 `PROJECT.md`/规范文档，明确本轮范围和不做项。
2. 只实现当前一个独立目标；运行与风险相称的单元、集成或端到端验证。
3. 更新本地事实文档；项目进度和实验状态写入 `PROJECT.md` 或后训练实验文档，不写入本文件。
4. 如形成项目事实，追加飞书项目文档；学习问答只写飞书学习笔记和 `docs/post-training/learning/`。
5. 检查 diff、密钥和大文件后使用 Conventional Commit 提交并推送 `origin`；失败时如实记录，不假称同步完成。
6. 交付时说明改动文件、调用方影响、保持/改变的不变量、测试证据、测试空白和停止条件。

## 3. 代码审阅节奏

- 先看模块地图、输入输出契约和失败保护，再深入涉及 SQL、权限、数据隔离、模型训练和即将修改函数的关键代码；不要求逐行通读整个仓库。
- 讲解和审阅按一个小单元推进：用户先看真实代码与测试并复述，我再纠正；未完成当前单元时不启动新的长训练或完整评测。
- 对已被明确契约和回归测试覆盖的重复转换，采用抽样检查和聚合证据即可；发现新风险再升级为逐条审查。
- 每次审阅区分“已验证行为”“测试空白”“待确认设计”，不要因为目录或文档视觉整齐进行无关重构。

## 4. 数据与安全边界

- API key、数据库密码、访问令牌、`.env`、模型密钥和含敏感信息的日志永不提交 Git；使用 `.env.example`。
- 原始第三方数据集、大型 CSV/Parquet、数据库 dump、模型、Adapter、checkpoint 和完整运行日志放在仓库外 `/disk2/gengnan/data-analysis-agent-data/`；仓库内只放 manifest、schema、转换定义和小型 fixture。
- 数据分析使用独立只读账号；应用元数据账号和分析查询账号不得混用。
- 所有用户/模型 SQL 必须经过 AST 策略、对象白名单、单语句、LIMIT/超时和只读数据库角色。不得用字符串黑名单替代 AST 安全策略。
- 受保护 holdout 不得作为训练、验证、in-domain test、few-shot 或合成种子。切分时按 `family_id`/程序族隔离，日期、措辞、别名等表面变体不得跨 split。
- Gold SQL 的业务公式来自冻结 Catalog/指标合同和 deterministic renderer；模型候选仍必须经过 SqlPolicy、reader role、ResultContract/ResultValidator，不能以训练数据绕过运行时治理。

## 5. 项目架构边界

- 产品运行时是 Python 3.12、FastAPI、Vanna、PostgreSQL、SQLAlchemy/Alembic、`sqlglot` 和原生 `<vanna-chat>`；不另建独立前端，不引入没有实际需求的 Redis、多 Agent、MCP 或任意 Python 执行。
- Olist 领域 SFT 的训练输入必须复用真实 `olist-candidate-sql-v1` Prompt（Catalog + QueryPlan + ResultContract），目标仅为 canonical PostgreSQL SQL 加 EOS；不能退化为 SQLite Schema-only 模板。
- `QuerySpec` 是离线、版本锁定的结构化施工图，不是自然语言解析结果，也不是 SQL 字符串；renderer 只编译已验证结构，不解析问题、不执行 SQL、不调用 LLM。
- 训练数据、模型实验和生产运行时分开；后训练 Adapter 未通过匹配 Base/Adapter 业务质量门前，不接入生产默认路径。

## 6. 文档职责

- `PROJECT.md`：项目需求、架构、当前阶段、决策和变更记录。
- `docs/README.md`：文档导航；`docs/post-training/README.md`：后训练阶段地图。
- `docs/post-training/data/`：数据合同、切分、覆盖和物化协议；`experiments/`：已运行实验证据；`learning/`：概念、代码审阅和问答。
- 飞书项目文档记录项目进度、实验状态、风险和下一步；飞书学习笔记只记录后训练知识与问答，不混入项目进度。
- 文档中的计划、通过和质量结论必须使用准确措辞，不能把工程 smoke 写成模型提升。

## 7. GPU 约定

逻辑 CUDA 设备与 `nvidia-smi` 物理编号固定映射如下：

| `CUDA_VISIBLE_DEVICES` 逻辑编号 | 实际 GPU | `nvidia-smi` 物理编号 |
| --- | --- | --- |
| `0` | RTX 4090 | `2` |
| `1` | RTX 4090 | `3` |
| `2` | RTX 3090 | `0` |
| `3` | RTX 3090 | `1` |

- 启动占用显存的任务前重新执行 `nvidia-smi`，不得停止、抢占或重启其他进程。
- 训练/评测命令必须显式设置 `CUDA_VISIBLE_DEVICES`，并记录逻辑设备、物理设备和 UUID；不假定四张卡可以同时使用。

## 8. 数据集构造的最小质量门

- 先锁定指标、维度、时间、Join 和结果列合同，再生成自然语言问题和 Prompt。
- 每条训练行必须能追溯到 QuerySpec、Gold SQL、Prompt 版本、workspace 快照、split 和长度统计。
- 不静默截断 Prompt 或 SQL；超长样本进入脱敏 exclusion manifest，除非合同明确允许并记录原因。
- 中等规模数据必须同时报告行数、QuerySpec 数、family 数、程序族覆盖和每个 split 的哈希；行数不能替代独立语义能力。
- 自动生成的大批量样本使用确定性 renderer 和全量确定性契约检查；对高风险指标/粒度做分层人工或模型辅助抽样，不为低风险重复样本逐条制造昂贵人工流程。

## 9. 变更停止条件

- 发现 workspace、指标合同、Prompt、SQL hash、split、权限或长度证据漂移时立即停止并修复来源，不继续训练或发布。
- 测试失败、外部依赖缺失、GPU/数据库资源不满足或推送失败时如实报告；保留可复现的本地证据，不删除用户已有修改。
