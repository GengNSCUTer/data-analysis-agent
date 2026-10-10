---
goal: Project v1 Weeks 2–4 — Close the trusted product loop, then package a defensible demo
version: 1.0
date_created: 2026-09-18
status: W4-04 packaging written; live demo script tested on 2026-10-08
tags: [process, product, agent, memory, evaluation, post-training]
---

# Project v1：Week 2–4 小任务路线

## 1. 总目标与边界

一个月内要收束的不是“再训练一次更大的模型”，而是一个可演示、可解释、可复核的可信数据分析 Copilot：

```text
嵌入业务网页的真实用户 / workspace 上下文
-> Router / Catalog / QueryPlan
-> 在线模型或显式 opt-in 的本地候选 SQL
-> SqlPolicy -> PostgreSQL reader -> ResultValidator
-> 明确范围的表格、图表、依据、审计与可追问结果
```

每个方块仍需单独立项、单独审阅、单独验收。以下是**依赖顺序和交付门**，不是一次性授权所有实现。

在执行本路线中的任一产品或模型改造前，必须先完成 [`feature-product-capability-baseline-test-v1.md`](feature-product-capability-baseline-test-v1.md) 的 Phase 0 与 Phase A，并将 B--D 按阶段执行、审阅。否则不得仅凭代码审查推断当前问答、记忆、图表和浏览器体验的主要缺口。

共同不变量：不动 protected final test；不把 adapter 变成默认 provider；不引入 Redis、多 Agent、MCP、**向量化业务事实长期记忆**或任意 Python 执行；不把 Demo 的演示身份误称为生产认证。

### 1.1 记忆与上下文是一条显式产品主线

| 能力 | 具体内容 | 当前状态 | 对应任务 |
| --- | --- | --- | --- |
| 短期记忆 | 当前请求页面约束、当前会话的指标/时间/维度/待澄清状态、当前可信结果引用 | `WorkingMemory` 已有基础实现，但缺 workspace/version/Artifact 合同 | M1、M2、M3 |
| 持久会话 | 原始历史、当前 AnalysisState、当前 ResultArtifact；刷新和历史会话恢复 | PostgreSQL history 已有；状态生命周期尚不完整 | M2、M3、M5 |
| 会话压缩 | 超预算时把早期完整回合压缩为带 source range/hash 的叙事摘要 | M6 已完成并有 Phase 3 回归证据；后续只做缺陷修复 | M6 |
| 长期记忆 | 用户可编辑的语言/时区/图表偏好；用户主动保存的历史分析 | 已有显式入口；不进入 SQL，也不自动沉淀业务事实 | M7 |

这四项的检索方式也不同：R2/R3 通过 `conversation_id + workspace/version` 精确读取，R4 按已验证的消息范围读取，R5 按 `(organization, subject, workspace, memory_kind)` 精确过滤。首版不使用“把全部历史 embedding 后按语义相似度召回”的方式。

## 2. Week 2：嵌入式 Agent 与受控会话底座

### W2-01（M1，已完成）：冻结宿主集成合同

| 项目 | 内容 |
| --- | --- |
| 输入 | 当前 `TrustedDemoHost`、Host Integration Contract 设计、现有 cookie demo 身份模型 |
| 输出 | `CopilotBootstrap`、`AnalysisSurfaceContext`、`ConversationScope` 的版本化 schema、server-side `WorkspaceAccessRegistry` 与同源 `/api/copilot/bootstrap` |
| 不做 | 不新建前端框架；不接受浏览器直传 `user_id`、角色或任意 workspace；不改 SQL 链 |
| 验收 | 定向测试 20 passed：伪造 role/workspace、SQL/DOM 注入被拒，合法 bootstrap 只返回允许字段；Demo 原会话接口未改动 |
| 停止条件 | 真实宿主认证/跨域形式仍未决定，故只交付同源 contract，不猜测 token 方案 |

### W2-02（M2，已完成）：把 workspace 与并发语义落入会话

| 项目 | 内容 |
| --- | --- |
| 输入 | W2-01 contract、`PostgresConversationStore`、WorkingMemory 测试 |
| 输出 | `workspace_id + state_version` 持久化、加载/列表/删除隔离与乐观并发合同 |
| 不做 | 不接入 TheLook 到当前产品默认页面；不做隐式 TTL；不做全量历史摘要 |
| 验收 | `security.sql` 幂等迁移回填旧数据到 `olist-demo`；Store get/list/delete/update 按 workspace 过滤；20 项现有回归 + 6 项真实 PostgreSQL store/route 回归通过；双对象 stale update 抛出 `ConversationStateConflict`，handler 返回可恢复提示 |
| 停止条件 | 真实认证/跨域 workspace 仍未决定，TheLook 不接入默认在线页面；后续多 workspace 需单独注册 grant 与迁移证据 |

### W2-03（M3，已完成）：结果交接与表格 presenter 合同

| 项目 | 内容 |
| --- | --- |
| 输入 | GAP-RT-01、GAP-UX-03、GAP-UX-04；现有 ResultValidator/CSV artifact |
| 输出 | [`result-artifact-presenter-v1.md`](../docs/architecture/result-artifact-presenter-v1.md) 与 `ResultPresentation`：`total_row_count`、`preview_row_count`、`display_truncated`、列标签/单位、audit reference、preview/full export 权限 |
| 不做 | 不把全表、原始 SQL 或第 201 行之后的内容塞给 LLM；不修改 1,000 行执行上限 |
| 验收 | scalar、分组、时间序列均有明确 artifact ownership；250 行时能表达“预览 200 / 共 250”；通用导出已 fail closed；focused presenter/tool tests 与真实 embedded-demo browser DOM 回归通过 |
| 停止条件 | 无法统一当前轮与下轮摘要语义时，先冻结两个明确边界，不凭文本描述硬合并 |

## 3. Week 3：执行引导的模型改进决策与本地候选接入设计

### W3-01：独立开发集与模型错误分类设计

| 项目 | 内容 |
| --- | --- |
| 输入 | 主模型基线、DPO 停止证据、现有 Olist/TheLook final test 的**脱敏聚合**错误类别 |
| 输出 | 不与 train/validation/protected final test 重合的 developer challenge-set 合同，覆盖 schema linking、join、时间/状态过滤、指标口径和结果合同 |
| 不做 | 不从 protected case 改写；不立即构造 DPO pair；不为数据量复制日期/措辞 |
| 验收 | hash/split/provenance 审计；每类有明确预期证据，不能读 final Gold 作为种子 |
| 停止条件 | 无法证明隔离时，不物化数据，更不训练 |

### W3-02：本地候选 SQL provider 的最小设计与可比 harness

| 项目 | 内容 |
| --- | --- |
| 输入 | `adapter_best`、现有在线 Demo、GAP-MODEL-01/02 |
| 输出 | 本地 inference process 或最小 Web inference extras 的二选一决策；统一 provider request/response、timeout、队列、模型/adapter hash 与审计字段 |
| 不做 | 不把训练 conda 环境直接当 Web 服务；不启用 silent fallback；不绕过 Policy/reader/validator |
| 验收 | 同一 server-derived Prompt 下能做一次 offline Base/Adapter/online 候选对比，记录延迟和每个治理关口 |
| 停止条件 | GPU、依赖锁或安全边界不清晰时，仅完成设计与预检，在线 provider 保持默认 |

### W3-03：只选择一种有证据的后训练下一步

| 决策输入 | 允许动作 |
| --- | --- |
| developer set 暴露 schema/指标覆盖缺口，且 SFT 数据可从非 holdout 来源补足 | 冻结小规模、覆盖导向的 SQL-only SFT 扩展合同，然后单独训练/验证 |
| 同一 parent 在合格 train-only source 上自然产生足量 A--C DPO pairs | 先做 pair-quality review；通过后才开 DPO Trainer |
| 两项证据都不足 | 不训练；把时间投入运行时 Prompt/Artifact/UX 和评测闭环 |

这里的“执行引导”是指用 Policy、reader、ResultContract、Gold/人工开发集证据定位模型缺口，而不是让模型反复自我修复后宣称准确。

## 4. Week 4：产品壳、端到端验收与项目表达

### W4-01（M4，已完成）：最小可嵌入 Copilot shell

- 新增 `/embedded-copilot` 与 [`embeddable-copilot-shell-v1.md`](../docs/architecture/embeddable-copilot-shell-v1.md)，提取不含 role switch、全局 SQL 和审计面板的 `EmbeddableCopilotShell`；保留 `/embedded-demo` 作为 `TrustedDemoHost` 开发验证宿主。
- 只显示当前 workspace、对话、澄清、可信结果卡、图表和按权限展开的“查看依据”。
- 验收：浏览器 E2E 覆盖产品壳入口、Demo 控制面隔离、查看依据、新分析、250 行 preview 文案和移动端无横向溢出；澄清结束不再卡 `Sending message...` 的旧宿主回归继续保留。

### W4-02（M5，已完成）：显式的状态生命周期

- 已实现“清空分析上下文但保留历史”、workspace/dataset/metric 版本失效，以及同会话 `state_version` 冲突的可恢复提示。
- `POST /api/project/conversations/{id}/analysis-context` 只清空 R2 和 active R3 引用，保留消息和 R4 history summary；版本不匹配时下一轮不复用旧指标、时间、结果或语义历史摘要。
- 验收：`tests/test_analysis_lifecycle.py` 覆盖匹配复用、版本失效、清空保留历史、stale `state_version`；上下文过滤器在失效回合跳过语义摘要。浏览器确认产品壳按钮和提示。

### W4-03（M6，已完成）：历史摘要与工件恢复

- 已完成：超预算时生成带 source message range/hash 的 R4 summary，摘要失败回退到完整最近回合；CSV/Plotly 工件通过 R3 引用恢复。
- Phase 3 证据位于仓库外 `data-analysis-agent-data/evals/product-capability-baseline-v1/phase3-context-artifacts/`，当前仍需在产品基线汇总中单独说明测试覆盖范围。

### W4-02B（2026-09-22，已完成）：本地登录、跨域令牌、完整结果下载

- 本地账号登录。没有本地会话或显式演示 cookie 时，分析接口返回 401。浏览器提交的 `user_id` 或角色会被拒绝。这不是外部身份提供商。
- 跨域嵌入使用允许列表中的 origin 签发 5 分钟令牌，由 `postMessage` 交给 iframe。令牌来源不匹配时不接受。
- 「下载完整结果」读取当前用户已校验的结果文件，下载前重算校验和，并写入 `app.export_audits`。通用预览导出按钮仍然关闭。执行上限仍是 1,000 行，界面预览仍是 200 行。

### W4-03A（M7）：最小长期记忆，而不是自动业务事实库

- **R5 用户偏好：** 用户可查看、编辑、删除的语言、时区、默认图表、展示格式等偏好；按 `(organization_id, subject_id, workspace_id, memory_kind)` 精确隔离。
- **保存分析：** 用户显式将某次已验证的 R3 `ResultArtifact` 固定为“已保存分析”；恢复时只显示其历史 workspace/version、口径与审计依据，不能静默作为当前 SQL 的指标、筛选或结果事实。
- **不做：** 不从聊天自动抽取“长期业务事实”，不把模型结论写入向量库，不做跨用户推荐。
- **验收：** 偏好 CRUD、范围隔离、删除与 workspace/version 漂移测试；保存 artifact 能被查看但不会污染新的查询。
- **状态：** 2026-09-22 已有 API 和产品入口。版本不匹配的会话不能把旧结果再保存成当前事实；已保存记录只回看，不进入 SQL。

### W4-04：项目闭环与面试材料

- 演示脚本即当前产品的全量功能测试：[`docs/demo-script.md`](../docs/demo-script.md) 与 [`evals/cases/w4_04_product_demo_v1.yaml`](../evals/cases/w4_04_product_demo_v1.yaml)。`tests/test_w4_04_product_demo.py` 执行登录边界、安全拒绝、澄清、结果追问、黄金表/图合同和展示不变量。浏览器步骤在服务可用时走同一份脚本。
- 架构图：[`docs/architecture/product-request-architecture-v1.md`](../docs/architecture/product-request-architecture-v1.md)。本地 Adapter 画在离线位置，不在当前网页路径上。
- 模型实验卡：[`docs/post-training/experiments/project-v1-model-card.md`](../docs/post-training/experiments/project-v1-model-card.md)。数字回链到主模型基线和 DPO 停止记录。
- 面试问答：[`docs/interview-qa-v1.md`](../docs/interview-qa-v1.md)。

验收不是“PPT 好看”，而是每一个数字都能回链到本地 manifest/报告，并能解释一个明确限制。

## 5. M1–M7 依赖图

```text
M1 Host contract
  -> M2 workspace + state_version
    -> M3 ResultArtifact
      -> M4 EmbeddableCopilotShell
        -> M5 clear / invalidation / concurrency
          -> M6 provenance-backed history summary（仅超预算时）
          -> M7 explicit preferences + saved analyses
```

M1–M7 的产品入口已具备。M7 只保存用户改过的展示偏好和主动保存的分析结果，不进入 SQL。W4-04 的演示脚本、架构图、模型实验卡和面试问答已经写好。整条链与模型改进并行但不互相替代：M1–M7 解决“用户在真实产品中如何安全地连续分析”；SFT/DPO 解决“本地模型如何提出更好的候选 SQL”。

## 6. 每周收束原则

每周末只做以下三件事：更新一个事实文档、运行与改动相称的测试、明确下周的一个首任务。不得因为计划存在而自动启动训练、全量评测、数据库迁移或前端重构。
