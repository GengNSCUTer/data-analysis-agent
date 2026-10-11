# Agent 项目代码学习记录

本记录承接 [`四周完善与学习计划`](../../plan/process-agent-internship-improvement-v1.md)，只放代码理解与问答。项目进度见 PROJECT.md，后训练知识继续使用既有 post-training/learning 目录。每轮阅读 1–3 个关键函数，问题答完再进入下一段。

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

### 后续小段（尚未讲解）

- 工具执行到结果证据：为什么 SQL 可运行、合同通过、业务正确是不同层次。

本次真实请求已核验，但运行成功不能代替用户读代码。QueryPlan 本段、结果证据小段和闭卷链路复述完成后，才将 Day 1 标记完成。
