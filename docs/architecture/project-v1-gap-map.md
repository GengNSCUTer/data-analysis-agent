# Project v1 缺口与优化地图

> **状态：** Week 1 复盘完成版；仅汇总已有代码、测试或冻结实验摘要支持的缺口。  
> **更新时间：** 2026-10-08  
> **使用方式：** 本文是后续 Week 2–4 的候选 backlog，不是立即开工清单。每一项开始前仍须写最小任务卡并确认不变量、验收和停止条件。

## 1. 优先级原则

优先级以用户体验影响、已掌握证据、实现规模和安全依赖共同决定。这里不把“逻辑看起来相似”本身当作重构理由，也不把单测通过写成业务语义或模型泛化的证明。

| 优先级 | 含义 |
| --- | --- |
| P0 | 可能破坏安全、权限、数据隔离或将未验证数字展示给用户；发现后优先停止和修复。 |
| P1 | 直接影响主分析回合的正确性、可解释性或可用性，且已有明确运行证据。 |
| P2 | 影响体验、可观测性或长期维护；需要先冻结接口/合同再实施。 |
| P3 | 代码洁净、历史资产整理或未被证据证明的假设；不应抢占核心产品闭环。 |

## 2. 已确认优化项

| ID | 优先级 | 证据与现状 | 后续最小动作 | 验收 / 停止条件 |
| --- | --- | --- | --- | --- |
| GAP-RT-01 | P1 | 2026-09-22：当前轮优先使用已有的 `validated_result_summary`；没有摘要时 CSV 在 1,000 字符内的最后一个换行处截断。结果文件名仍交给图表。`ResultValidator`、CSV artifact 和 200 行 UI 预览未改。 | 已落地。三类真实结果的端到端图表回归不在本轮。 | 模型交接不再从半行 CSV 开始；摘要仍只来自已验证结果。 |
| GAP-RT-02 | P2 | 2026-10-08：策略把语句收到 1,000 行时，内部多读一行判断是否还有数据，然后丢掉这一行。正好 1,000 行且没有多出来的行，不再标成截断。合同见 [`execution-row-cap-v1.md`](execution-row-cap-v1.md)。 | 已落地。第 1001 行不进入结果、摘要、图表或模型上下文。 | 审计仍记录对外的 1,000 行，不记录被丢弃的那一行的内容。 |
| GAP-RT-03 | P1 | 2026-09-22：已冻结 `evals/cases/runtime_router_probe_v1.yaml`，覆盖在线 v3 Catalog 的拒绝、帮助、定义、建议、查库和准入。两条口语缺口（“最火的货”“支付方式的金额”）只记录观察，不引入 LLM router。 | 已落地。当前规则覆盖这组探针后停止扩展。 | 探针不是 protected test，也不能代替 SqlPolicy。 |
| GAP-RT-04 | P1 | `WorkingMemory` 能保存受控的指标、时间、比较、待澄清字段和可信摘要；Day 1 仅测试了基本 carry-forward，尚未对跨刷新、历史恢复、连续下钻、清空/过期行为形成完整状态矩阵。 | Day 3 审查 store、metadata 更新和多轮测试，先形成状态生命周期表。 | 所有保存字段均有来源、上限、更新触发、清理触发；不能从 assistant prose/client metadata 恢复不可信状态。若只是文档空白而代码已覆盖，记录即可。 |
| GAP-RT-05 | P2 | 2026-10-08：旧描述作废。M2 起 `app.conversations` 已持久化并校验 `workspace_id` 与 `state_version`；加载/更新/列表均按 workspace 过滤。当前默认页仍只有 Olist；TheLook 不上默认页。 | 多 workspace 同进程接入前再冻结跨 workspace 拒绝与迁移规则。 | 单工作区现有历史恢复回归继续通过；不把缺 `workspace_id` 写成当前缺陷。 |
| GAP-RT-06 | P2 | M5 已提供可见的“清空分析上下文”：保留聊天记录，清除指标/时间/维度和 active result。版本不匹配时不复用旧状态。v1 仍不做隐形 TTL。 | 已实现并有 `tests/test_analysis_lifecycle.py`。数据保留期限仍由后续合规策略决定。 | 清空后下一轮不得沿用旧指标或结果；版本漂移 fail closed。不因加入 TTL 而删除用户仍需要的历史。 |
| GAP-RT-07 | P2 | M2 起用 `state_version` 乐观锁；M5 将冲突记为 `state_conflict` 并返回可恢复提示。聊天组件在一次发送完成前拒绝第二条消息。 | 已有 stale update 测试。多实例锁和取消进行中的请求仍未做。 | 旧请求不能覆盖新状态；冲突提示可恢复。未定义产品语义前不引入分布式锁。 |
| GAP-UX-01 | P2 | 页面具备全边/四角 resize handle，且有既存 E2E；Day 1 只验证页面入口 HTTP 200，未重跑真实浏览器中的刷新、历史 Markdown、无 SQL 澄清完成后 loading 释放等场景。源码确认 SSE 正常结束时会清除遗留的 `working` 状态，但这仍不能替代浏览器验证。 | Day 4/UX 专项运行现有浏览器 case，按失败现象定位；不凭历史通过记录假定当前构建仍正确。 | 浏览器 E2E 覆盖最小化、缩放、刷新历史、状态卡完成、表格/图表尺寸；没有复现不新写大规模前端代码。 |
| GAP-UX-02 | P1 | 2026-09-22：`/embedded-copilot` 是唯一产品页，`/embedded-demo` 重定向到这里。旧宿主页已删除。演示角色和审计在「管理」后面。本地账号登录和 5 分钟跨域令牌已接入；这不是外部身份提供商。 | 已落地。外部 IdP 仍不在本阶段。 | 浏览器不可自报身份、角色或 workspace。 |
| GAP-RT-08 | P1 | 2026-10-08：`workspace_id` / `state_version` 已落库（见 GAP-RT-05）。剩余缺口是 `previous_result_summary` 仍是 WorkingMemory 里的自由文本；结果追问已用摘要数字门约束，但还不能按 `artifact_id` 重新打开表格或图表。 | 已保存分析回看若要做，先冻结 `ResultArtifact` 引用合同，再接列表恢复。 | 只有 ResultValidator 通过的摘要能支持结果追问；摘要外数字会被替换为复述摘要。不把缺 `workspace_id` 写成当前缺陷。 |
| GAP-RT-09 | P2 | 已实现 `HistorySummary v1` 的确定性历史边界与 `context_budget` 脱敏证据；它只在超字符/消息预算时替代被省略的完整 turn，不是语义摘要、长期记忆或 token 精确统计。 | 用定向长历史回归验证 summary 插入、当前回合异常压缩和 safe provenance；只有真实证据表明边界标记不足时，才单独评审受控语义摘要。 | 每次调用可说明保留/省略的 turn 数、范围 digest、字符预算和当前回合 fallback；summary 不能作为 SQL/权限/指标事实来源。 |
| GAP-RT-11 | P2 | 2026-09-22：M7 已有语言、时区、默认图表、数字格式和「保存本次分析」。这些记录按主体隔离，不进入 SQL。2026-10-08：页面写明偏好仍不会改变本轮语言、时区、图表或数字格式；回答与图表仍按服务器合同。 | 已落地入口与未生效说明。不把偏好接进 SQL/指标。真正生效要另立展示合同。 | 偏好可改可删；保存的结果带版本，不能变成下一问的指标或筛选；界面不得暗示偏好已改变展示。 |
| GAP-MODEL-01 | P1 | 选定的 Qwen2.5-Coder-1.5B Base + multi-schema SQL-only `adapter_best` 已有离线加载/匹配评测脚本，但 `trusted_olist_web_demo.py` 仅装配 SiliconFlow `OpenAILlmService`；无本地 `PeftModel` provider、模型选择开关、排队/超时/fallback 合同。 | 在 Week 2 先设计一个只生成候选 SQL 的本地 provider 接口与 runtime comparison harness；初版保持在线模型默认，Adapter opt-in。 | 同一 server-derived prompt 下可记录 Base/Adapter/online 的候选、Policy/reader/ResultContract 结果和延迟；本地失败安全 fallback；不绕过 SQL 链且不改变默认流量。 |
| GAP-MODEL-02 | P1 | 当前 Web 解释器 `data-analysis-agent/bin/python` 没有 `torch`/`peft`，虽可读取 tokenizer/config 却无法加载 Base 或 Adapter；CUDA-capable 训练/评测环境与 Web 运行环境不同。 | 在写 provider 前冻结最小 inference dependency lock 与进程部署边界：Web in-process extras 或独立内部 inference service 二选一。 | Base + adapter 只能以 `is_trainable=False` 加载；GPU/并发/超时被观察；Web 仍保持最小依赖；不能把训练环境直接当产品服务。 |
| GAP-MODEL-03 | P2 | 两批各 48 条、分层的 train-only / validation-only 诊断均为 Gold match，未产生可复核的 A--C rejected SQL；DPO pair 为 0，状态为 `stop_no_dpo`。这不是 DPO 代码失败，也不能靠手写坏 SQL 解决。 | 先建立与 train/validation/protected final test 都隔离的 developer challenge-source；仅在冻结 parent 自然产生足量 A--C pair 后重新审阅 DPO。 | 不读取/改写 protected test；chosen/rejected 均有模型、Prompt 与治理证据；达不到 pair 覆盖门则保持不训练。 |
| GAP-RT-10 | P2 | 当前 SQL repair 是同一在线 LLM 的一次、错误类别限定的机械修复；请求在候选/repair 前失败或 provider 不可用时没有第二模型/local adapter fallback。 | 在本地 provider 合同冻结后，再单独决定 provider fallback policy 与观测字段；默认不启用 silent fallback。 | 每次候选/repair 可追溯 provider/model；fallback 只发生在明确定义的错误类别；无验证 SQL 不展示数字；若成本或延迟不满足停止条件则保留单 provider。 |
| GAP-UX-03 | P1 | 已补齐 `ResultPresentation` v1：表格只接收 preview rows，同时显式携带 `total_row_count`、`preview_row_count`、`display_truncated`、列 label/unit、workspace/version 和 audit reference。 | 已实现 `docs/architecture/result-artifact-presenter-v1.md`、组件字段、frontend renderer 适配和 focused/browser tests；不能借此扩展 SQL 执行上限。 | 250 行完整结果必须明显显示“预览 200 / 共 250”；1,000 行不完整结果仍不得展示为完整；不能向 UI/LLM 泄露第 201 行以后数据。 |
| GAP-UX-04 | P1 | 2026-09-22：通用预览导出仍关闭。已登录用户可下载自己当前已校验的结果 CSV；下载前校验文件完整性，并写入 `app.export_audits`。 | 已落地。不把 200 行预览当成完整文件，也不提高 1,000 行执行上限。 | 无登录或无当前结果时不能下载；下载行数与校验和进入审计。 |
| GAP-OBS-01 | P2 | 2026-09-22：`GET /api/project/diagnostics` 按 `termination_reason` 和修复成功次数聚合。分析员只看自己的计数；管理员看工作区合计。响应不含问题、SQL 或结果行。主耗时分布仍未做。 | 已落地计数。耗时分布留到以后。 | 管理面板只在打开「管理」后显示这些计数。 |
| GAP-CLEAN-01 | P3 | `QuestionRouter.IntentKind` 存在重复 literal `"data_analysis"`，运行时无行为影响。 | 在相关 Router 任务中顺手移除，并运行 Router 定向测试。 | 仅类型洁净变更；`tests/test_question_router.py` 通过。 |

## 3. 明确不作为当前优化项的内容

| 事项 | 决定与理由 |
| --- | --- |
| 合并在线 repair 的 `extract_sql_candidate()` 与离线 `unwrap_sql_completion()` / SQLite baseline 清洗函数 | **不合并。** 在线 repair 需要无法可靠抽取 SQL 时返回 `None` 并 fail-closed；离线评测要尽可能保留不合规候选给 Policy 归因；历史 SQLite baseline 还受可复现评分约束。只有未来有明确迁移需求时，才可能抽有限 wrapper parser，并通过兼容合同迁移。 |
| 直接放开多次 SQL repair | **不做。** 结果合同失败不是语法失败；多次猜测不能证明指标口径正确，且会增加延迟和攻击面。是否扩展 repair 要先有错误分类与可验证成功条件。 |
| 因文档数量多而批量移动/删除历史 Markdown | **不做。** 历史报告、飞书和 GitHub 链接可能依赖原路径。当前仅补导航和当前基线；迁移必须是独立任务并运行链接检查。 |

## 4. 文档职责与当前入口

| 目的 | 当前规范入口 | 不应写入 |
| --- | --- | --- |
| 当前项目事实、阶段决策、变更记录 | `PROJECT.md` 与飞书项目文档 | `AGENTS.md`、学习笔记 |
| 当前架构审查、运行链路、缺口 | `docs/architecture/project-v1-baseline-review.md` 与本文 | 历史实验报告 |
| 代码目录和模块职责 | `docs/architecture/repository-map.md` | 实时实验状态 |
| 后训练学习和代码问答 | `docs/post-training/learning/` 与飞书学习笔记 | 项目进度 |
| 数据合同、切分、release 协议 | `docs/post-training/data/` | 临时运行日志 |
| 已运行实验的配置和聚合证据 | `docs/post-training/experiments/` | 下一步猜测 |
| 历史验证和实验报告 | 根目录旧报告与 `archive/` | 当前默认结论 |

本周只维护上述入口之间的链接和事实边界，不进行物理迁移。详细 Day 1/2 证据见 [project-v1-baseline-review.md](project-v1-baseline-review.md)。
