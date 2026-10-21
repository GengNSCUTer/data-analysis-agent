# Agent 项目代码学习记录

本记录承接 [`四周完善与学习计划`](../../plan/process-agent-internship-improvement-v1.md)，只放代码理解与问答。项目进度见 PROJECT.md，后训练知识继续使用既有 post-training/learning 目录。每轮阅读 1–3 个关键函数，问题答完再进入下一段。

当前学习位置（2026-10-20）：Day 1尚未闭卷复述完成。下文保留旧版本学习原文，旧Router
和examples入口不再代表现行代码。现在先读本文末尾“恢复学习：模型理解提案与服务端校验”，
只审阅两个位置；工程六项已做完，不因此跳过用户理解、也不继续插入新功能或训练。

## Day 1：沿一条问题认识请求链

场景：“按客户州统计有效订单数前五名，并生成柱状图”。完整调用关系和本次请求证据见 [`产品请求架构`](../architecture/product-request-architecture-v1.md)。目标是用户能画出请求链，区分代码/模型职责，以及离线 QuerySpec 与在线 QueryPlan；入口和 Router 复述已完成，当前进入 QueryPlan 与模型上下文小段，Day 1 尚未完成。

### 第一小段：启动装配与请求入口

先阅读两处，不要求读完整文件：

1. `examples/trusted_olist_web_demo.py:create_app()` 的组件创建部分，以及 `register_chat_routes(..., BudgetedChatHandler(...))`。它在启动时构建可重复使用的组件，并把它们交给 HTTP 路由。
2. `src/vanna/servers/fastapi/routes.py:chat_sse()`。它接收 JSON 请求、补充 HTTP 上下文、调用 handler，并把 handler 产出的块编码成 SSE。它本身没有写 SQL 的逻辑。

前端通过 `frontends/webcomponent/src/services/api-client.ts:streamChat()` 发送请求。主要字段是：

```json
{
  "message": "按客户州统计有效订单数前五名，并生成柱状图",
  "conversation_id": "同一段会话的标识",
  "request_id": "这一次请求的标识"
}
```

`conversation_id` 用来串联多轮，`request_id` 用来追踪单次请求；两者不能证明用户身份。服务端从实际 HTTP cookies/headers 中解析认证凭据，再由身份解析器验证，不能信任请求体自称的用户或角色。

核心调用为 `async for chunk in chat_handler.handle_stream(chat_request)`。handler 持续产出状态、表格、图表或文本块，HTTP 路由逐块传给前端；SSE 数据块数量不等于模型调用次数。

2026-10-10 问答：

1. `create_app()` 和 `chat_sse()` 分别在什么时候运行？用户再发一条消息，会重新装配所有组件吗？
2. 为什么单凭请求里的 conversation_id 或 user_id，不能决定用户可查询哪些数据？

用户回答：

1. “create-app在服务启动时运行，chat-sse在用户发送问题模型返回回答时运行。router，agent，数据库runner只需要在启动时创建一次就够了。”
2. “因为身份信息由服务端的解析器验证登录凭证，可能包括cookie之类的？单靠id可能不行。”

反馈：启动装配和身份验证的理解正确。精确时序是后端收到 SSE 请求就进入 `chat_sse()`，再调用 handler，模型生成发生在后续处理过程中；拒绝或 Catalog 定义回答可能完全不调用模型。组件在本项目当前运行方式下由每个应用实例启动时创建并复用，多 worker 时各进程有自己的实例，不能理解为所有部署进程共享一个 Agent。cookies 或认证 header 是凭据载体，必须验证其有效性；请求体中的 user_id 既不是凭据，也不决定权限。

### 第二小段：Catalog 检索与 Router 分类/准入

本段只关注三个函数：

- `src/data_analysis_agent/semantic_catalog.py:CatalogRetriever.retrieve()`：用受控指标名称、别名和匹配规则选取用户角色可见的指标、表、列与 Join，返回 `CatalogSelection`，含定义对象、模型上下文 `prompt` 和可追踪的 `trace`。这里不是查询数据库里的业务结果，也不是模型生成 SQL。
- `src/data_analysis_agent/question_router.py:QuestionRouter.classify()`：判断用户是在查数据、问定义、追问结果、请求帮助还是提出不支持/越权操作，返回 `QuestionRoute`。
- 同文件 `QuestionRouter.admit_sql_request()`：对需要查库的请求再检查指标、维度/归属规则、查询形态、时间等是否足够明确；可把先前的查库意图改成澄清状态。通过不代表 SQL 已经正确或获准执行。

运行时的连接位置为 `chat_runtime.py` 中第一次 `retriever.retrieve()`、`classify()` 和查库分支中的 `admit_sql_request()`。中间还有受控历史复用和无指标匹配时的可选 LLM grounder；本段只跟踪首次单轮且明确命中指标的路径，不能推断所有请求均无模型辅助。

本问题中的“有效订单数”匹配 `data/catalog/olist_catalog_v3.yaml` 的 `paid_order_count`，定义为排除 canceled/unavailable 后按订单去重计数。按客户州需要选取 `fact_orders`、`dim_customers` 以及 `orders_customers` Join。Catalog 提供的是定义和关联依据，不含实际州订单数量。

本地直接执行现有检索与路由函数，三例核验通过；未请求 HTTP、数据库或模型：

| 问题 | classify 状态 | 最终状态 | 执行查库路径 |
| --- | --- | --- | --- |
| 按客户州统计有效订单数前五名，并生成柱状图 | answerable | answerable | 是 |
| 有效订单数的统计口径是什么？ | catalog_answered | catalog_answered | 否，定义来自 Catalog |
| 本月有效订单数是多少？ | answerable | missing_time | 否，要求补具体时间 |

第三例是在没有已确认时间范围的初始会话中运行的。当前实现不会自行把“本月”解析成明确日期窗口；这是当前产品行为，不能泛化成所有 Agent 都应这样处理。三个例子只验证这些问法，不证明规则覆盖所有中文表达。

2026-10-10 问答：

1. 两个问题都提到“有效订单数”，为什么问统计口径不用查数据库，而问订单有多少需要查？
2. “本月有效订单数是多少”已经被 classify 识别成数据查询，为什么 admit_sql_request 仍可以阻止生成 SQL？

用户回答：

1. “因为统计口径相当于是指标的定义，直接返回就可以了，订单数量则需要具体看数据库有多少。”
2. “admit是判断当前query是否足够明确的，检查分组维度，归属，时间等，本月并不是具体的日期，所以会被打回，让用户补充进一步信息。”

反馈：定义与实际数值、分类与准入的区别均理解正确。“本月”是相对时间，若有确定的时间锚点和时区，可以转换成明确日期窗口；当前初始会话的实现没有自动完成这一步，才要求补充，不是相对时间天然不可查询。

### 第三小段：在线 QueryPlan、模型上下文与离线 QuerySpec

本段目标是区分“服务端提出的查询要求”和“模型生成的 SQL”，并识别线上/离线输入差别。只核对当前例子的计划构造与上下文，不新增功能或调用在线模型。

先看 `src/data_analysis_agent/query_plan.py:QueryPlan.from_selection()`。输入是 CatalogSelection、当前问题、已准入的 QuestionRoute 和受控会话状态，输出 QueryPlan 对象。它通过代码整理指标、维度、时间、必要结果列和聚合策略；不返回 SQL，也不执行数据库查询。

本地按同一问题构造出的字段节选为：

```json
{
  "plan_type": "single_metric",
  "metric_ids": ["paid_order_count"],
  "dimensions": ["customer_state"],
  "time_range": null,
  "required_result_columns": ["customer_state", "paid_order_count"],
  "execution_strategy": "single_metric_aggregation",
  "top_n": {}
}
```

`required_result_columns` 意味着最终结果需要州和有效订单数，不能只给一个订单总数，也不能顺手返回客户标识。`execution_strategy` 描述聚合方式，并不是 SQL 文本。本例没有用户指定的时间窗口；`top_n` 为空是当前结构化排行覆盖缺口，不能擅自把例子写成 top_n=5。之前真实请求的 LIMIT 5 和 Gold 排序确实正确，但来自候选 SQL 对问题的遵循。

再看 `QueryPlan.prompt_context()`：它把对象转换成模型可读的查询要求，例如结果列白名单、维度、时间和聚合策略。`as_dict()` 则用于结构化状态与证据，两者用途不同。

上下文传递可在 `chat_runtime.py` 的 `usage.set_query_plan(query_plan.as_dict(), query_plan.prompt_context())` 和 `semantic_catalog.py:CatalogContextEnhancer.enhance_system_prompt()` 对照：后者将本轮 Catalog、受控 WorkingMemory 和 QueryPlan 文本附到系统提示词。模型随后依据系统提示词、用户问题和工具定义生成候选 SQL；计划文本不能取代后续的 SQL 检查。

与离线数据构建的区别：

| 对象 | 来源与用途 | SQL 如何产生 |
| --- | --- | --- |
| 在线 QueryPlan | 当前自然语言问题、Catalog 选择、路由和受控状态，经服务端代码整理 | 作为模型上下文的一部分；模型生成候选 SQL |
| 离线 QuerySpec | 预先设计并验证、锁定版本的结构化样本规格，规定指标、shape、时间和 join program 等 | deterministic Gold renderer 将已验证规格编译为 Gold SQL |

离线 `olist_queryspec.py:render_gold_sql()` 不解析自然语言；需要已经构建好的 QuerySpec。当前多 Schema SQL-only 训练模板 `multi_schema_sql_only_sft.py:render_multi_schema_prompt()` 明确包含 Catalog、QuerySpec、Result Contract 和中文问题。在线使用 QueryPlan 文本与 Agent 工具调用，二者不是同一个对象，也不是完全相同的输入模板；已有离线成绩不能直接证明 Adapter 能替换当前线上模型。

本次直接运行计划构造和 prompt 断言通过，核对指标、维度、结果列、聚合策略和实际 top_n；没有请求数据库或在线 LLM。这只证明该例子的代码输出，不是模型质量评测。

2026-10-11 问答：

1. 当前 QueryPlan 是谁生成的？它已经是一条 SQL 吗，候选 SQL 又由谁生成？
2. 既然离线有 renderer 能生成 Gold SQL，为什么不能说当前用户提交自然语言后，就由这个 renderer 直接生成线上 SQL？

用户回答：

1. “queryPlan由服务端代码生成整理，包括维度，需要的结果列之类的。不是sql，候选sql由模型生成。”
2. “因为render是基于一个queryspec生成的，他是一个设计好的结构化样本规格，然后render去解析生成，render本身不能理解用户的中文问题。而线上生成sql，模型是需要理解queryplan来生成。二者是有差别的吧，只不过具体差别是什么好像你也没说。”

反馈：两者职责理解正确。renderer 对结构执行的是验证与编译，不承担自然语言理解；线上模型还会读用户问题、Catalog 和工具定义，不是只读 QueryPlan。此前讲解只比较了来源与用途，缺少实际字段和模型输入对照，本轮补充如下。

#### 同一需求的字段、输入和能力差别

为集中比较统计语义，使用“按客户州统计有效订单数”，暂不带“前五”和绘图要求；不把离线 QuerySpec 没有的排行字段伪装成已支持。通过当前代码分别构造在线 QueryPlan 和 Olist v3 QuerySpec，验证指标、维度、结果列一致，并生成 Gold SQL；全部在本地完成，没有查库或调用模型。

| 同一概念 | 在线 QueryPlan | 离线 Olist QuerySpec |
| --- | --- | --- |
| 指标 | metric_ids=[paid_order_count] | 相同 |
| 维度 | dimensions=[customer_state] | dimension=customer_state，result_shape=state_grouped |
| 查询类别 | plan_type=single_metric 表示单指标 | result_shape=state_grouped 表示按州分组；分类角度不同 |
| 时间 | time_range=null，time_grain=null | time={mode: all_time, start: null, end_exclusive: null, grain: null} |
| 组织 SQL 的提示 | execution_strategy=single_metric_aggregation；metric_plans 携带指标事实粒度/来源/过滤 | join_program_id=JP05_customer_geo_order；renderer 使用已实现的指标表达式和程序规则 |
| 结果列 | required_result_columns=[customer_state, paid_order_count] | 相同 |
| 溯源 | 在线 run/会话/workspace 另有版本与证据 | QuerySpec 内含 workspace 版本 pin、schema_version 和由内容派生的 query_spec_id |

`plan_type` 不是 `result_shape` 的简单改名，`execution_strategy` 也不是 `join_program_id` 的简单改名。它们表达的业务需求可以重叠，但数据结构和执行消费方不同。

模型输入/输出的实际区别：当前 `render_multi_schema_prompt()` 将 Catalog、QuerySpec、Result Contract 序列化为 JSON，附上中文问题，要求模型只输出 SQL。Gold renderer 生成的 SQL 是监督标签，不会作为答案写进该输入。线上 `CatalogContextEnhancer.enhance_system_prompt()` 将检索出的 Catalog、受控 WorkingMemory 与 QueryPlan.prompt_context() 拼到系统提示词；Agent 同时提供用户消息/历史和工具定义，模型产出 run_sql 工具调用中的 SQL。

因此，离线题目里的指标、结果形态、时间和关联程序由构建者预先决定，并经过数据准入。线上 QueryPlan 要从当前真实问法和受控状态推导，可能澄清、遗漏或只覆盖部分要求；例如前五尚未进入本例 top_n。两边都有指标语义和表列信息，不能把区别误说成“只有离线给 Schema”或“只有线上理解中文”。输入内容的确定程度、表达格式与输出协议都需要对齐；仅凭对象名字不同，不能断言它导致了过去的某项模型退化。

理论上，如果某条线上请求已经被完整解析成 renderer 支持且验证通过的 QuerySpec，就可以直接确定性生成 SQL；不是原理上永远不能。当前实现没有提供一个覆盖任意用户问题的 QueryPlan→QuerySpec 转换器，renderer 也不会自行补出缺失语义，因此不能把它当成现成的线上自然语言入口。

本轮仍停留在第三小段补充理解，没有开始新训练、输入适配改造或下一段执行链。本段补充复述题：离线完整 QuerySpec 的模型评测通过后，为什么仍需验证它在当前线上 QueryPlan 上下文和工具协议下的表现？

2026-10-11 补充复述与新任务：

用户正确指出输入组织和输出协议不同：离线 Catalog/QuerySpec/ResultContract JSON + 问题 → SQL，线上系统提示词/Catalog/受控状态/QueryPlan/消息与工具 → run_sql 调用；提出复用既有数据改造后再训练验证。反馈：公平比较要求同一评测输入、解码与评测规则，但并非需要小模型学习全部 Agent 对话和工具职责。可以将 SQL-only 输出由服务端适配为工具调用，重点对齐候选输入的真实可用信息和语义。

这次实际回放 train/validation 后发现结果列序、TheLook 维度/粒度等输入错配，不能只改字段名或包装格式。具体事实与阶段计划见 [`runtime 候选训练对齐计划`](../../plan/refactor-runtime-candidate-sft-alignment-v1.md)。已经实现 `candidate_sql_generator.py:build_runtime_candidate_request/render_runtime_candidate_prompt`，运行时与离线共用构造器；运行时先记录候选输入 hash，默认 API 仍使用完整 Agent 协议。当前插入该任务，尚未生成新 release 或启动训练，Day 1 的执行链小段仍待学习；本轮不增加复述题。

这一步值得一起读的是 `workspace.py:DimensionBinding`、`query_plan.py:_dimensions/from_selection` 和 `query_time.py:extract_absolute_range`：业务维度名不必等于真实字段名；计划来自问题解析，不能由 Gold 规格补答案。新候选计划把日期明确写成 `start <= time < end`，例如“2017-01-01 至 2017-01-31”按包含结束日转为 `< 2017-02-01`，而“至 2017-01-31，不含结束日”保留 `< 2017-01-31`。旧问题只给起止日、标签却按排除结束日写 SQL 时，换 Prompt 并不能消除监督矛盾；数据重建应先补清问题，保留修改来源，再重新经过实际解析链。

### 后续小段（尚未讲解）

- 工具执行到结果证据：为什么 SQL 可运行、合同通过、业务正确是不同层次。

本次真实请求已核验，但运行成功不能代替用户读代码。QueryPlan 本段、结果证据小段和闭卷链路复述完成后，才将 Day 1 标记完成。

### 2026-10-14：结果预算小单元——先明确预算由谁生成

继续沿四周学习计划理解执行链。本次实现只增加 QueryPlan/ResultContract 的模式和预算，
没有再开训练；阶段进度和测量数据见 PROJECT，不把项目流水账混入知识讲解。

例如“按客户州统计有效订单数”，底层可能读取十万条订单，但只返回几十行州统计。
`execution_max_rows` 数的是返回行，不是读过的事实行；`preview_rows` 控制浏览器一次
看到多少行；`llm_context_rows` 控制模型摘要取多少行。这几个数字不能互相代替。
在线 aggregate 已按结果形态派生基数；例如一年按日 × 27 州最多 9,855 组。
进程兜底默认 100,000 行/32 MiB，不能代替每个业务请求的基数合同；旧离线合同保持原值。

本次亲自阅读三个位置即可：

1. `src/data_analysis_agent/result_budget.py:ResultBudget.aggregate()`：从服务端配置生成
   一份不可变预算。在线资源兜底为 100,000，计划再收紧到业务基数；预览 200、摘要 8 行/1,200 字符。
2. `src/data_analysis_agent/chat_runtime.py:BudgetedChatHandler.handle_stream()`：浏览器
   传来的模式/预算/计划先删掉；已准入的指标请求得到服务端预算，同一份传给
   QueryPlan 和 ResultContract。问题和 Catalog 负责业务含义，预算负责执行资源。
3. `src/data_analysis_agent/result_budget.py:validate_runtime_result_budget()`：查库前
   对比计划、合同、真实服务端配置；不一致就停止。若 export 模式未实现，不能让
   模型写一个 SELECT 便顺便启用下载任务。Runner 记录拒绝原因，不让 SQL 修复改变模式。

冻结的候选生成 Prompt 不需要知道工件下载或前端预览预算。构造器校验计划与合同的
预算一致，再投影原 SQL 语义字段；因此旧数据/评测 Prompt hash 保持相同。

前一轮四个回答的纠正记录：`SELECT *` 返回所有列，`COUNT(*)` 只是聚合计数，
不是同一个安全含义，仍需遵守指标口径。策略自动补 LIMIT 时，内部多读一行是为了
确认是否真的执行截断；探测行不进入展示/摘要/工件，不把“正好 1,000 行”一律拒绝。
ResultValidator 用代码检查列、类型、范围和完整性等合同，比让模型自己宣称正确可靠，
但不能完全证明公式正确。AOV 是 Average Order Value（客单价）；Olist 这里精确定义为
有效订单的平均商品金额，不含运费，必须先把商品价格按订单汇总，再对订单金额取平均。
直接 `AVG(price)` 是平均商品行价格。识别这类错误需要核对 Catalog、SQL 的聚合粒度
以及独立 Gold 结果，不能只看列名叫 AOV 或结果为正数。

实现不等于已经学完；结果预算专题已收尾，接着原 Day 1 的工具执行到可信结果小段。

### Day 1 继续：一次 SQL 调用怎样变成可引用的结果

这段只阅读三处：`trusted_sql_tool.py:TrustedRunSqlTool.execute()`、
`postgres_runner.py:SecurePostgresRunner._run_sql_sync()`、
`result_validator.py:ResultValidator.validate()`。先看调用关系和返回值，不用逐行通读文件。

模型提出 `run_sql(sql=...)` 工具调用。TrustedRunSqlTool 接收调用，Runner 先过 AST
策略，再用 PostgreSQL 只读角色执行；ResultValidator 对结果列、时间、值域及完整性
做确定性检查。通过后完整结果保存为 ResultArtifact，初始表格只拿 200 行，模型只拿
最多 8 行/1,200 字符摘要。用户浏览下一页或下载 CSV 都读这个工件，不再调用模型，
也不重新查数据库，因此它们引用的是同一次已经验证的结果。

要区分三层：SQL 能运行，只证明数据库接受语句；合同通过，证明返回结果符合可检查的
结构与约束；业务正确，还要证明筛选、Join 和聚合粒度符合指标定义。例如把
`AVG(price)` 错当客单价，即便列别名叫 `average_order_value` 且数值为正，也不能
仅凭 ResultValidator 证明正确，需要核对“先按订单求金额、再对订单平均”的口径。

接下来用户阅读代码后复述两点：

1. 5,000 行完整结果只展示 200 行，与执行只拿到前 200 行，为什么不同？
2. SQL 能执行、列名也正确，为什么仍可能把客单价算错？哪一层能提供哪些证据？

尚未完成闭卷链路复述，Day 1 不标记完成；此后不再自动插入新训练或完整评测。

### 2026-10-14｜执行链复述与自然语言入口调整

用户回答：计划与候选 SQL 的 mismatch 非空说明二者不一致，不能接着执行；保存验证结果
方便追问、分页和下载；展示预算决定哪些数据给前端或模型。三点理解正确。补充：
工件保存的是完整、已经验证的结果，预览和摘要分别裁剪；并不是只保存前 200 行。

用户体验反馈：固定问法、指标词和比较关键词门槛使对话不流畅。本轮调整为
“原问题 + 受控 WorkingMemory → 模型提议 SemanticIntent → 服务端校验 → QueryPlan
→ 候选 SQL → 既有执行与结果链”。模型理解用户想做什么，代码判断这个需求在当前
Catalog/权限/能力下能否执行。自然语言理解失败提示重试，不退回固定句式。

例如“2017年各月销售额，画条曲线”可被理解成 gmv、month、2017 年区间和 line；
“继续”且有验证结果可直接解释已有结果，不重复查询。“本月”由服务器算当前日历月，
不是要求用户再手写起止日期，也不是把历史数据最新月冒充当前月。

下一次只读 `src/data_analysis_agent/semantic_intent.py` 中的 `SemanticIntent` 与
`parse_semantic_intent()`：前者记录理解提案，后者校验真实可见指标、维度和日期。
先复述“为什么合法 JSON 仍不等于可执行 SQL”，再进入 Handler 的传递过程。
本轮实现不等于 Day 1 已学完；继续沿原四周计划学习，不启动新后训练。

### 2026-10-14｜Phase 2/3 修复复盘

模型返回合法 JSON 仍可能有业务范围错误，所以新增 `semantic_check`：它逐字段比较
期望的指标、分组、筛选和是否应查库；`status=parsed` 只能说明结构合法。真实回归中，
“各个州哪些地方卖得好”要求的只有 `customer_state`，而明确说“按州里的客户城市”
才允许 `[customer_state, customer_city]`。筛选 `customer_state=SP` 不是分组，最终计划
必须保留空 dimensions 和 `filter_customer_state:SP` 警告。

`SemanticIntentInterpreter` 对 provider 异常只尝试第二次。两次都失败会抛出
`semantic_service_unavailable`，Handler 不会构造 QueryPlan，也不会调用 SQL；重试不是
语义纠错，也不能把失败计为通过。Phase 2/3 的 8 条真实回归和两个 PostgreSQL scope
probe 通过后，服务重新加载在 32013。下一次继续阅读 `SemanticIntent` → `parse_semantic_intent`
→ `route_semantic_intent` 的数据流，再回答“为什么 Catalog 校验仍不能证明 SQL 业务口径正确”。

### 2026-10-19｜可信回答的展示时序：待阅读小单元

业务问题：已经决定拒绝一个数字结论，若先流式展示原文，再替换最终文字，用户仍可能看到错误数字。
本轮修复不改变查库规则，只调整这些已知不可信状态的展示时机；工程已验证，学习尚未标记完成。

下一次只读三个位置：`budget.py` 的 `BudgetSafetyMiddleware.should_stream_response()` 和
`_rejection_response()`（同一套判断决定是否暂存和最终显示什么）；
`vanna/core/agent/agent.py` 的 `_handle_streaming_response()`（收集分片与显示分片分开）；
`tests/test_budget_streaming.py` 的 `test_blocked_answer_never_enters_rich_simple_or_sse()`
（检查所有中间事件，而不只检查最终答案）。先复述这个先后关系，再进入业务质量用例。

### 2026-10-19｜业务正确不能只看执行成功：待阅读小单元

第二项工程审查已完成，结果见[质量报告](../../evals/results/product_release_dev_v1.md)，不把
工程通过视为学习完成。本段只看三个位置：`result_validator.py:ResultValidator.validate()`
如何区分 valid/degraded/refuse；`audit_product_release_dev.py:reference_sql()` 如何从独立业务
定义计算参考；同文件 `compare_frames()` 如何比较原工件的实际值。

本次真实错误是按州平均履约天数：SQL使用 `EXTRACT(DAY)` 去掉不足一天的小数，结果列名、
正数范围和执行状态全部正确，却不等于“连续时长天数的平均值”。因此执行校验解决能否安全
运行，结果校验解决形状和数值边界，独立口径核对才能发现这一类业务公式偏差。

用户下一次读完后只复述两点：为什么按月列出12个订单数不等于已经完成上下半年比较；为什么
分类NULL可以标为“空值”画出来，而指标NULL不能自动当0。尚未收到复述，不标记已掌握。

### 2026-10-20｜恢复学习：模型理解提案与服务端校验

目标仍是理解业务与真实代码，用于面试表达。先从用户需求看：“哪个州销售额最高”不是
要求系统返回某个预设答案，而是先识别销售额口径、按州分组和排名，再查当前业务库。
当前线上主链为身份/workspace → 语义提案 → 分析状态/QueryPlan → SQL候选 → 受控执行
与结果验证 → 工件/图表/回答。历史离线QuerySpec与小模型不参与默认在线回答。

本次只看 `src/data_analysis_agent/semantic_intent.py`：

1. `SemanticIntent`：模型理解结果的类型。先关注 `request_kind`（查数字还是解释等）、
   `metric_ids`（指标）、`dimensions`（分组）、`filters`（限定范围）、`top_n`（排名数）、
   `action`（新任务/修改当前/复用）即可；不用一次记住所有字段。
2. `parse_semantic_intent()`：将模型输出解析为上述类型，检查指标/维度是否来自当前角色可见
   Catalog，并由服务器计算相对时间。它不写SQL、不查库，也不能证明业务计算公式正确。

例如首次问“哪个州销售额最高”，一个合理提案是 `metric_ids=[gmv]`、
`dimensions=[customer_state]`、`top_n=1`、`action=new_task`。销售额在此对应Catalog定义的GMV，
不是任意表里名为sales的列。下一轮“这个州哪些品类卖得好”属于修改当前任务，并须从上一
可信结果选中州；具体引用解析留到会话章节，不让模型凭记忆编造州值。

理解JSON、允许使用的业务对象、SQL可以安全执行、答案业务正确，是不同层次。示例提案
没有执行权；调用方还要处理状态、计划及后续SQL/结果检查。

本轮复述题（已回答，纠正见下）：

1. “按州比较销售额”与“只看SP州的总销售额”，`dimensions` 和 `filters` 有什么区别？
2. 模型生成合法JSON并通过Catalog校验后，为什么还不能直接说“查出的销售额一定正确”？

用户回答与纠正：

- 第一题回答“dimension分别使用state，以及商品种类；第一个不用filter，第二个只看SP州”。
  筛选判断正确，第二个问题的分组需纠正：单独看“只看SP州的总销售额”，并没有按品类
  展开的要求，`dimensions=[]`，`filters=[{dimension: customer_state, value: SP}]`，返回一个
  总额。“按州比较”才是`dimensions=[customer_state]`；如果问“SP州各品类销售额”，才同时
  使用`dimensions=[product_category_name]`和SP筛选。这里比较的是两个独立问题；多轮承接
  已有任务时还要看明确变更和既有状态。
- 第二题回答“这里只是分析，还没有真正生成SQL”。抓住了阶段区别。补充：即使指标ID和
  JSON都合法，也可能误解用户要的指标或范围；后续SQL还可能算错分母、状态或Join粒度。
  可解析、可访问、可执行与业务正确须分别判断。

下一小单元：本轮意图如何更新当前分析状态。

只读`analysis_state.py:AnalysisState.apply_patch()`，重点看`state=self`、列表字段的`set`
分支与`replace(state, **{name: value})`；再对照
`tests/test_analysis_state.py:test_scope_changes_preserve_untouched_fields_and_new_task_resets`。
`SemanticIntent`描述本轮想做什么，`AnalysisState`记录当前任务已经确认的指标、维度和筛选。
`StatePatch`是本轮修改清单，应用清单本身不调用模型或数据库，更新后的范围还要经过调用方校验。

示例：已有GMV、无分组、SP筛选；用户追问“按商品品类展开”，修改清单只将`dimensions`
设为`[product_category_name]`。应用后仍是GMV、SP筛选，只是按品类分组。`replace`返回新状态，
没有被修改的指标、筛选保留；若模型给错修改清单，代码不会自动理解中文来纠正它。

下一轮复述题（已回答，见下）：

1. “按商品品类展开”没有再提SP，为什么不应自动删除SP筛选？
2. 再说“取消州限制”，应修改哪个字段？指标和品类分组需要一起清空吗？

分组与筛选继续结合该例巩固，Day 1仍未完成，不自动进入新功能或训练。

用户回答与纠正：第一题“继承第一轮，前提还是SP州，不能删除筛选”，理解正确。第二题
“修改filter=sp，指标和品类分组不需要清空”，后半正确；操作应是从`filters`移除
`customer_state=SP`，而非重新设置SP。如果还有其他筛选，应只移除州条件。

### 2026-10-20｜LLM如何决定继承、修改或新开任务

本单元只按现有代码解释，不改运行时、不发起新模型测试。输入是本轮问题、当前任务状态
与预算内历史；输出是对实际prompt、状态操作和局限的理解。依据是
`semantic_intent.py:SemanticIntentInterpreter.prompt()`、`resolve_state_intent()`以及
`analysis_state.py:AnalysisState.apply_patch()`；Day 1仍等待最终整链口述。

`chat_runtime.py`先恢复当前`AnalysisState`、构建context packet，再调用语义解释器。
解释器组装本轮问题、当前角色可见Catalog（指标名称/别名/说明及允许维度）、结构化状态
（指标、分组、筛选、时间、Top-N、澄清与执行状态等）、近期叙事对话、可用的历史摘要、
结果引用元信息和输出JSON schema。`server.py:complete_semantic_intent()`将这些内容作为
一次专用LLM请求发送，temperature=0、tools=None；该调用只返回语义JSON，不生成SQL。
结果引用不包含可直接采用的实体值；“这个州”等需要由服务端从可信工件解析。

继承判定依靠LLM理解上述输入与prompt中的任务说明，没有另一个关键词分类器或学出来的
独立任务分类模型。说明要求：省略条件的承接追问默认修改当前任务；独立新分析不继承旧
范围；仅解释/换图用keep；清空用clear_task。模型输出`action`与`state_patch.operations`，
代码按它们应用。update_task只改patch指定字段，未改字段从当前状态保留；new_task在patch
前加replace_task，创建新task并重置业务范围，保留workspace/版本，不删除会话记录。

例：当前GMV、按品类分组、SP筛选。用户说“取消州限制”，合理输出是data_query、
update_task，以及`remove filters [{dimension: customer_state, value: SP}]`。品类与GMV不需要
在patch里重写。“换个问题，统计全部时间总订单数”则可new_task，并设置Catalog对应订单
指标；不能仅因指标变化就断定换任务，“销售额换成订单数，其他不变”仍是update_task。

`resolve_state_intent()`应用后会用当前Catalog重新验证完整范围。校验能发现未知/不可见
指标与非法结构，不能保证“这句话到底算不算新任务”判对。LLM若误把追问标成new_task，
合法范围仍可能被重置；误标update_task则可能残留旧条件。temperature=0不消除这种错误。
历史和摘要虽随prompt提供以理解承接关系，执行条件按设计应来自本轮明确修改、受控状态
或可信工件；摘要禁用为SQL事实是信任边界与提示约束，不能据此宣称LLM永远不受叙事误导。

建议本轮只读`prompt()`中的action/patch几行（当前334–345行），对照
`resolve_state_intent()`的new_task分支。待复述：同样改成订单数，为什么“其他不变”和
“换个问题，全部时间总订单数”需要不同的状态处理？

用户回答：因为“其他条件不变”表示遵循上一轮任务，所以是`update_task`，不是
`new_task`。回答正确。

### 当前问题对应的代码地图

一次请求中，相关判断按这个顺序发生：

1. `chat_runtime.py:262-315` 读取当前`AnalysisState`和上下文包，再调用
   `_interpret_semantic_intent()`；这里把上一轮的指标、分组、筛选、时间和可信结果引用
   放进本轮语义解释的输入。
2. `server.py:357-370`定义专用语义请求：只发一个不带工具的LLM请求，
   `temperature=0`、`max_tokens=800`、system prompt要求只输出JSON，不生成SQL。
3. `semantic_intent.py:78-123`的`SemanticIntent`定义模型可以提出的结果：
   `action`决定新任务/修改/保留/清空，`state_patch`写本轮具体字段变化。
4. `semantic_intent.py:276-404`的`SemanticIntentInterpreter.prompt()`组装提示词。状态
   位于293-319行；继承和patch规则位于328-345行。模型据此判断“其他不变”是
   `update_task`，并把变化写成最小patch。
5. `semantic_intent.py:406-434`的`interpret()`执行LLM调用并把文本交给
   `parse_semantic_intent()`。后者在166-208行做JSON、字段、角色可见Catalog、指标和维度
   校验。它只判断提案是否合法，不判断SQL业务结果是否正确。
6. `semantic_intent.py:211-253`的`resolve_state_intent()`应用提案：`update_task`只处理
   patch指定的字段；`new_task`会在patch前插入`replace_task`，重置业务范围并创建新的
   `task_id`。应用后249-250行再次用完整范围做Catalog校验。
7. `analysis_state.py:121-158`的`AnalysisState.apply_patch()`真正执行`set/add/remove/clear`
   和`replace_task`。它从旧状态开始，只替换patch指定字段，返回一个新的不可变状态对象。
8. `chat_runtime.py:497-522`把通过校验的状态用于当前路由；535-571行生成`QueryPlan`。
   677-681行把更新后的`working_memory`写回会话。到这里仍然只是形成受控计划，之后才进入
   候选SQL、Policy、数据库只读执行和结果验证。

因此，真正的“继承/修改/新任务”决定来自LLM返回的`action + state_patch`，代码负责提供
状态、约束格式、应用修改和重新校验；代码没有另一个隐藏的关键词分类器替它做语义判断。
这也是当前系统的能力边界：LLM误判action时，合法的patch仍可能导致范围保留错误或任务被
重置，所以必须靠多轮测试观察，而不能只看JSON解析成功。

下一小步先不读更多文件：请你根据上面的代码地图，口述“换成订单数，其他条件不变”
从LLM输出到`AnalysisState`写回的五步过程。口述完成后再进入`QueryPlan`如何消费这个状态。

### 2026-10-21｜用户串联请求链，回顾执行与结果职责

用户复述：先由LLM提出继承、修改或新任务，服务端解析并校验JSON、应用patch到
AnalysisState，再形成QueryPlan、让模型生成SQL；后面经过SqlPolicy、PostgreSQL只读角色、
结果合同与校验、ResultArtifact再回答。主线正确，已能串联意图、状态和执行阶段。仍需
结合实际代码理解ResultContract的生成时机：它在SQL生成前就由服务端确定，查库后才由
ResultValidator按要求验收，而非结果回来才临时制定标准。Day 1其余阅读与口述仍按计划继续。

用户要求简要回顾以下组件：

- SqlPolicy：应用侧SQL安全检查，解析AST，限制只读单语句、允许访问的表列及敏感字段，
  并按执行预算规范查询；不只是检查语法，也不证明业务公式正确。
- PostgreSQL只读角色：数据库自身的权限边界，只允许读取授权对象，禁止写入、删除等。
  即使应用检查漏掉操作，数据库仍有权限限制；它不能判断GMV口径是否正确。
- ResultContract：服务端提前规定本次结果应有哪些列、指标及约束。例如按品类查GMV，
  就要求品类列和GMV列。这是验收标准，本身不是执行校验的组件。
- ResultValidator：对实际DataFrame按合同做代码检查，如缺列、指标空值、范围和结果截断，
  决定能否用于可信回答或只能降级展示。通过仍不证明所有Join和业务公式都正确。
- ResultArtifact：给已经验证的结果保存摘要、查询范围、版本及完整CSV/图表的外部引用，
  供后续追问、换图、分页、下载和刷新恢复复用；不把完整表塞进对话上下文。

本轮仅记录问答并核对现有代码，没有更改运行时或执行新在线评测。

### 2026-10-21｜QueryPlan：把当前需求整理成查询计划

本单元目标是读懂更新后的分析状态怎样进入查询计划；不修改运行时，不启动训练或新评测。
输入是已经确认的当前范围、选中的Catalog指标与路由信息，输出是对计划字段和构造代码的
理解。验收方式为用户阅读两个小片段后回答下面两题，未回答前不标记掌握或完成Day 1。

业务例子：“只看SP州，按商品品类统计有效订单数”。当前状态记住订单指标、品类分组和SP
筛选；`QueryPlan.from_selection()`再结合Catalog形成这次查询的计划：

- `metric_ids`：`paid_order_count`。
- `dimensions`：`product_category_name`。
- `filters`：绑定到当前workspace真实表列的`customer_state=SP`条件。
- `metric_plans`：携带Catalog规定的订单粒度、来源表、时间字段、默认状态过滤等。
- `required_result_columns`：品类列和`paid_order_count`列；州只是筛选条件，不因此自动成为
  必须返回的结果列。

阅读入口为`query_plan.py:QueryPlan.from_selection()`，先看365–380行取指标、维度和时间，
再重点读395–398行：先把分组维度放进结果列；存在时间粒度时加入`time`；最后追加指标列。
然后看`MetricPlan.from_metric()`的194–202行：这里复制Catalog已有定义，不调用模型重新
猜指标口径。当前Olist有效订单数会排除`canceled`和`unavailable`，来自Catalog定义，即使
用户没有在问题里再次说明，也应遵循；筛选条件和指标默认过滤属于不同来源。

`AnalysisState`服务于跨轮任务连续性，`QueryPlan`服务于本次查询的具体生成与验收。
构造计划本身不生成完整SQL，也不执行数据库。计划提供给后续SQL生成，并用于确定
ResultContract的必需结果列；计划正确仍不保证候选SQL已经正确实现了它。

本轮复述题（待回答）：

1. 该例是否必须返回`customer_state`列？为什么？
2. 若改成“按月、按品类统计有效订单数”，`required_result_columns`应包含哪三列？

用户回答与纠正：第一题“不需要，因为SP州只是filter”，正确。第二题回答“month、品类、
有效订单数”，业务含义正确，需区分时间粒度和输出列名：当前QueryPlan的标准时间结果列
叫`time`，`time_grain="month"`表达按月聚合；必需结果列为`product_category_name`、`time`、
`paid_order_count`（当前构造顺序）。不是要求所有SQL时间列都命名为`month`。

### 2026-10-21｜QueryPlan如何进入SQL生成提示词

本小单元只追踪计划到模型输入的衔接，不修改运行时、不运行新模型评测。输入为已有
QueryPlan和Catalog，输出为对提示词组装及其作用边界的理解；用户阅读下列两个小片段后
复述，未回答前不标记掌握。

1. `query_plan.py:QueryPlan.prompt_context()`把计划对象格式化成中文指令，例如必须返回
   哪些列、按什么维度和时间粒度统计、保留哪些筛选及指标粒度要求。这里只拼字符串，
   不调用LLM，也没有生成完整SQL。
2. `semantic_catalog.py:CatalogContextEnhancer.enhance_system_prompt()`读取本次请求已经
   准备的Catalog、状态和计划提示，追加到基础system prompt。重点读末尾返回表达式：
   `system_prompt + catalog_prompt + state_prompt + query_plan_prompt`。Catalog提供受限
   表列、Join和指标定义；计划说明本次要怎样查询。

Vanna Agent在`agent.py`中调用enhancer，再将增强后的system prompt、预算内消息和工具定义
组成模型请求。当前在线模型可以返回`run_sql`工具调用，SQL放在工具参数中；它不会因为
返回工具调用就自行获得数据库执行权，仍由服务端治理和执行。runtime中的
`build_runtime_candidate_request()`记录共享候选协议证据，不等于默认在线路径调用本地
Adapter；不要把它误当成当前真正的LLM生成调用。

提示词要求模型遵守计划，但不保证它一定遵守。SQL仍可能漏SP筛选、漏分组或使用错误
Join，后续计划核对、安全策略、数据库权限和结果验收各自处理其能覆盖的行为；不能
声称已有检查证明所有业务语义正确。

本轮复述题（待回答）：

1. `prompt_context()`返回字符串，是否意味着它已经调用模型生成SQL？
2. Catalog与QueryPlan分别给SQL生成模型提供什么信息？

用户回答：第一题“不是，只把结构化QueryPlan转成prompt”，正确；第二题“Catalog提供
表、列和指标，QueryPlan说明本次具体查什么”，正确。补充Catalog也包含关联与指标口径。
已完成该小单元的复述，Day 1整体仍按其余代码阅读与整链复述推进。

### 2026-10-21｜模型工具调用怎样进入受控执行

本单元只阅读现有SQL工具入口，不改运行时、不运行新评测。目标是区分“模型提出工具
调用”和“服务端允许执行”；输入是候选SQL及服务端QueryPlan，验收为用户阅读两个
小片段后回答下面两题。

在线模型可返回`run_sql`工具调用，SQL放在`arguments.sql`里；工具调用本身不是数据库
执行。Vanna Agent通过工具注册表分派到项目注册的`TrustedRunSqlTool.execute()`。
该入口先执行`sql_plan_mismatch(args.sql, context.metadata.get("query_plan"))`：返回错误
说明时，记录本次候选被拒绝，不把它交给数据库；没有发现偏差时才调用父类工具，后者
调用注入的SecurePostgresRunner。修复生命周期属于后续单元，不能把拒绝本条候选说成
整个请求绝对不会尝试其他候选。

本次先读`trusted_sql_tool.py:127`开头至首次`return result`，再读
`plan_sql_guard.py:24`的函数说明与筛选检查分支即可。计划核对主要检查已实现的筛选、
排名和部分关联/归属规则；SqlPolicy检查允许的语句、对象和操作，两者解决不同问题。
例如漏掉SP条件仍可能是安全的只读SQL，却统计了错误范围，需要计划筛选检查发现。
计划核对通过只表示没有发现其覆盖的偏差，不证明全部SQL公式或业务语义正确。

本轮复述题（待回答）：

1. 模型返回`run_sql`工具调用，是否意味着数据库已经执行？由谁决定是否继续？
2. SQL只读且只访问授权表，但漏了SP筛选，这属于安全问题还是查询范围问题？

用户回答：第一题“表示请求执行，尚未正式执行”，正确；第二题“属于查询范围问题”，
正确，并追问是否先对照QueryPlan、通过后再进入SqlPolicy。当前正常执行路径确实如此：
TrustedRunSqlTool先调用计划核对，再由父类工具进入SecurePostgresRunner；runner先校验
执行预算，再调用SqlPolicy，之后才用reader账号建立连接、设置只读事务与超时并执行。
这里只说候选SQL的执行顺序，不代表整个请求此前没有其他受控数据库读取。
计划检查通过表示没有发现已覆盖规则的偏差，不是完整证明SQL符合计划；语法解析失败
可留给SqlPolicy处理。结果回来后再由ResultValidator验收。

### 2026-10-21｜SqlPolicy如何检查SQL结构

本小单元只读`sql_policy.py:SqlPolicy.evaluate()`的142–157行，不改运行时，不启动评测。
输入为候选SQL，目标是理解解析和安全检查的区别，验收为阅读后复述。

`sqlglot.parse(sql, read="postgres")`把SQL文本解析成AST（描述语句结构的树），然后代码
检查是否恰好一条语句、是否是查询、树中是否存在禁止的写入或其他操作。后续同一函数
继续检查表列白名单、敏感字段、危险函数并按预算处理LIMIT；不是仅检测SQL语法。
AST按结构区分直接投影全部列和聚合计数：`SELECT *`会展开全部字段，analyst路径禁止；
`COUNT(*)`只统计输入行数，不返回全部字段，不因星号本身被禁止。后者仍可能因Join
复制行而算错有效订单数，安全策略通过不等于业务公式正确。

本轮复述题（待回答）：

1. 为什么禁止`SELECT *`不代表也应禁止`COUNT(*)`？
2. SqlPolicy允许执行`COUNT(*)`，能否据此认定有效订单数一定算对了？

用户回答：第一题“前者禁止返回敏感列，后者只返回行数”，理解正确；禁止全部列投影也
能避免返回计划之外的明细。第二题“不行，需要先分组聚合”，抓住重复计数问题，补充
分组键必须明确：先按订单ID整理成每单一行再计数可行，仅按州分组后`COUNT(*)`仍可能
把一单的多个商品行计成多个订单。也可以在合适的查询范围内使用`COUNT(DISTINCT order_id)`。

### 2026-10-21｜巩固统计粒度：订单不等于商品行

本次只纠正计数理解，不继续增加运行时阅读范围，不改代码或启动评测。合成例子为订单
A包含三条商品行，订单关联商品后出现三行：A/手机、A/手机、A/家居。此时`COUNT(*)`为3，
`COUNT(DISTINCT order_id)`为1。订单数按订单计，商品行数按商品行计；不能把所有指标
都改成DISTINCT。上述例子只说明计数，还需满足指标的状态和范围过滤。

阅读`resources/catalog/olist_catalog_v3.yaml:180`的有效订单数定义，重点是
`grain: order`与`default_filters`：统计单位是一单，排除canceled和unavailable。
同一订单跨多个品类时，各品类分别按订单去重计数，但组间相加不一定等于整体去重订单数，
这一归属规则也写在同一指标定义中。安全策略不会自动证明模型实现了这些统计口径。

本轮只需复述（待回答）：同一有效订单有3条商品行，查整体有效订单数和有效商品行数，
分别应该得到多少？

用户回答“1，3”，即1笔有效订单、3条有效商品行，正确。该计数例子的理解已确认；不因此
推定所有指标粒度、Join放大和归属规则都已掌握，这些留到Day 2结合真实指标代码阅读。

### 2026-10-21｜Day 1收尾复述（待回答）

本轮只串联已有请求链，不引入新模块、训练或评测。用“SP州各商品品类有效订单数”
作为输入，请用户不看前面的答案，用自己的话说明：生成SQL前怎样理解需求和形成计划，
执行SQL前怎样核对范围与安全，结果回来后怎样验收、保存并用于回答。不要求背函数名，
但需分清模型与服务端代码的职责。

再区分QueryPlan与离线QuerySpec：哪一个由当前用户问题与受控状态派生，用于在线链路；
哪一个用于离线结构化样本及Gold生成。SQL遗漏SP筛选的拒绝路径已经过复述，可在整链中
作为一次失败例子。用户完成口述后再核对Day 1完成条件，不因这条收尾安排自动勾选完成。

用户收尾复述：生成前由模型理解问题，服务端更新状态并结合Catalog/问题生成计划；执行
前检查QueryPlan偏差、SqlPolicy和PostgreSQL；结果由ResultValidator校验，随后保存到
AnalysisState并基于返回结果回答，但明确表示不懂最后一段。前两段主线正确，补充数据库
这层是reader账号权限和只读事务等，不是理解业务问题的另一次模型审查。结果保存与回答
仍待补课；QuerySpec/QueryPlan区别本轮尚未回答，Day 1不标记完成。

### 2026-10-21｜结果验收、保存与回答（补充学习）

本单元只厘清结果去向，不改运行时、不启动评测。输入为数据库返回的DataFrame，目标是
分清当前查询状态、可信结果摘要与完整结果文件；验收为用户阅读小片段后复述。

- SecurePostgresRunner调用ResultValidator，对照提前形成的ResultContract检查列、指标
  值和完整性等。只有满足相应回答条件，代码才调用`build_result_summary()`提取受控摘要；
  这份摘要来自实际DataFrame，不是让LLM猜测或生成一组业务数字，也不同于对话压缩摘要。
- TrustedRunSqlTool对valid结果调用`ResultArtifactStore.save_result_csv()`保存完整查询结果。
  小型ResultArtifact包含摘要、查询范围、版本和文件引用。AnalysisState保留当前指标、
  维度、筛选，并通过`with_result_artifact()`记住上一可信结果的摘要和引用，不把完整
  DataFrame塞进状态或模型上下文。完整结果受执行预算约束，不等于业务数据库全部原始行。
- 工具将受控摘要放进`result_for_llm`；Agent将其作为role=tool消息加入对话，再构造模型
  请求生成中文解释。结果验收失败或不允许回答时，不能按正常成功路径输出确定数字。
  UI预览、完整工件和模型摘要有各自用途及预算，不能彼此冒充。

本轮只读两处：`analysis_state.py:168`中`previous_result_artifact`与
`previous_result_summary`的赋值；`vanna/core/agent/agent.py:1006`中取
`result_for_llm`并在1018行之后写入tool消息、重建模型请求的几行。完整CSV与会话结果
引用分两步保存，后者由chat_runtime在本轮收尾写回，不要求背其持久化时序。

本轮复述题（待回答）：

1. 查回100行结果时，AnalysisState是否需要直接保存完整100行？后续下载从哪里读取？
2. 生成最后中文回答时，模型使用的数字依据来自哪里？
