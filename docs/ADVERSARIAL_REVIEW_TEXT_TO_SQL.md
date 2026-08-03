# Text-to-SQL 第二轮对抗性审查

> 日期：2026-08-03
> 范围：Semantic Catalog、问题路由、Vanna Context Enhancer 和当前 Olist trusted path。
> 状态：设计审查；本文件列出本轮必须验证的边界，不代表所有能力已经实现。

## 1. 第一性原理

系统的实际目标不是“让模型看到更多 Schema”，而是让一个不可信的生成器在最小必要上下文
中提出可验证的业务查询。任何新模块都必须回答四个问题：

1. 它减少了哪一种错误，而不是只增加了多少 Prompt 文本？
2. 它失败时会不会泄漏、越权或生成未经验证的数字？
3. 它的行为能否由固定输入、版本和测试用例复现？
4. 它是否仍然经过现有 `sqlglot` Policy、PostgreSQL reader role、超时和行数限制？

## 2. 当前基线发现

| 发现 | 证据 | 影响 |
| --- | --- | --- |
| 固定上下文包含全部 8 张表、列和指标 | `src/data_analysis_agent/metric_context.py` | 无关 Schema 会增加模型选择空间和 token，尚未证明“Catalog 检索”真的减少了上下文 |
| Vanna 内层工具循环上限为 4 | `AgentConfig.max_tool_iterations` + `BudgetedToolRegistry` | 循环次数、工具次数和 SQL 修复次数必须分开，不能把 4 当作语义正确性 |
| 会话历史只按完整消息/字符裁剪 | `ContextBudgetFilter` | 多轮业务口径没有结构化状态，可能沿用错误时间或指标 |
| SQL Policy 只判断语法/权限/对象 | `src/data_analysis_agent/sql_policy.py` | SQL 可执行不等于指标口径正确，必须增加结果级校验与拒答 |
| 演示身份由服务端签名解析 | `DemoRoleResolver` | Catalog 不能信任客户端传入的 role/dataset/table 字段 |

## 3. 本轮攻击面与控制

| 攻击/故障 | 失败表现 | 必须的控制 | 测试证据 |
| --- | --- | --- | --- |
| 恶意问题伪造 `role=admin` 或 `dataset=...` | 召回未授权表/指标 | 只使用服务端 `User.group_memberships` 和代码绑定的数据集；问题文本只作为匹配输入 | 用户元数据注入、跨角色检索测试 |
| YAML Catalog 被删字段、重复 ID 或改成任意表 | Prompt 引入未知对象或启动后静默降级 | 启动/加载时严格校验；结构错误 fail closed；表列必须与 SQL Policy 白名单交集 | schema 校验、重复 ID、未知字段、未知表测试 |
| 中文短词/别名碰撞（如“数”“订单”“率”） | 召回错误指标或无关表 | 规范化 + 长别名优先 + 最低分阈值 + 召回理由；不因零命中强行生成 SQL | 别名碰撞、零命中、Unicode 变体测试 |
| 问题文本含 Prompt 注入 | 模型忽略边界并执行无关操作 | Catalog 片段是结构化服务器文本；明确“问题不是指令”；最终 SQL 仍过 AST/role | 注入字符串只改变 trace，不改变 allowlist 测试 |
| Catalog 片段超长 | 仍然超过上下文预算 | 表/列/指标各有硬上限，输出长度可测；不把全部描述重复注入 | bounded chars/items 测试 |
| 管理员可见对象误传给分析员 | 越权 Schema 泄漏 | 检索前按服务端角色过滤，且 Policy/DB role 二次拒绝 | analyst/admin 对照测试 |
| 并发请求共享最近检索结果 | A 用户收到 B 用户的 Catalog/问题 | 不使用进程级 mutable `last_result`；每次返回不可变 trace，绑定 request/user | 并发/交错调用测试 |
| 无法回答或时间缺失仍给数字 | 看似完整但口径错误的答案 | router 返回安全状态；不调用 SQL；输出结构化澄清/拒答 | ambiguity cases、SQL budget=0 断言 |
| 生成 SQL 失败后无限修复 | 工具/费用失控 | 一次修复契约；每次新 SQL 全链路复查 | 修复次数/预算终止测试 |
| 检索规则与真实 Policy 漂移 | Prompt 允许的字段执行时被拒，或反之 | Catalog 只允许 Policy 白名单交集；启动/测试检查集合一致 | Catalog/Policy 一致性测试 |

## 4. 本轮实现边界

本轮只落地可证明的第一条闭环：

```text
结构化 Olist Catalog
  -> 确定性、角色过滤、有限长度的检索
  -> 可解释 RetrievalTrace
  -> Vanna Context Enhancer 使用检索切片
  -> 既有 SQL Policy / reader role / budget 不变
```

问题路由先作为纯函数和契约测试实现；在 working memory 能承接澄清答案之前，不把它粗暴地
插入 SSE 主链路，避免“澄清一次后下一轮丢失原问题”的伪多轮体验。一次修复和结果验证随后
单独实现，不能在本轮用检索器冒充完成。

## 5. 退出条件

- Catalog 文件中的表、列、指标、Join 和 Policy 白名单一致；结构错误 fail closed；
- 同一问题和同一用户每次检索输出相同排序、相同上限和相同 trace；不保留共享 mutable 状态；
- 未命中或歧义问题不会因检索器而自动生成数字；
- Trusted Demo 使用 Catalog slice，而不是继续无条件注入完整 Schema；
- 单测覆盖攻击矩阵，现有 SQL Policy/会话/预算测试不回归；
- 未实现的澄清、修复、结果校验仍在计划中明确标记。
