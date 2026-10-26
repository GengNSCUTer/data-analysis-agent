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

用户回答：第一题“不需要，从ResultArtifactStore下载”，正确；下载仍由服务端检查权限
后读取已保存工件，不是让模型或浏览器任意读取文件。第二题“来自受控摘要、ResultArtifact”，
主线正确。精确区分：本轮模型收到的是`result_for_llm`工具消息里的受控结果摘要；后续
复用旧结果时可读取可信ResultArtifact摘要与引用，模型不自行访问ResultArtifactStore。
结果保存与回答这部分已完成复述。

### 2026-10-21｜Day 1最后区分：QuerySpec与QueryPlan

本单元不改运行时或重新评测，仅对照已有两个类型，确认离线样本与在线需求的区别。
`olist_queryspec.py:QuerySpec`用于预先设计结构化样本，已有明确指标、形态和时间等规格，
由确定性renderer生成Gold SQL；renderer本身不理解中文问题。
`query_plan.py:QueryPlan.from_selection()`则结合当前问题的语义理解、受控状态和Catalog
形成本次请求计划，默认在线SQL仍由模型生成并接受后续治理。两者字段可相似，来源和
用途不同，不能把预先已知答案的离线规格当作模型已经理解用户问题的证据。

补充实现边界：仓库也有从QueryPlan生成Gold的评测适配器，内部复用QuerySpec公式；这
不等于默认在线聊天用Gold renderer代替模型，不能将两个类型理解为技术上互不关联。

最后复述题（待回答）：哪个是离线造样本时预先设计的规格，哪个是理解真实用户问题后
形成的在线计划？两者为什么不能仅因字段相似就当成同一种输入？

用户回答：QuerySpec是预先规定好的查询要求，可以直接交给renderer生成Gold SQL，因此
不能证明系统理解了中文。正确。它测试的是renderer把规格编译为SQL；中文理解必须用真实
用户问题经在线语义理解得到计划，再独立检查计划与用户意图是否一致。Day 1的主要请求
链和该区别已经完成复述；四周计划中的身份、会话与历史质量基线仍需按各自单元学习，
不因完成这一条就把整周任务标记完成。

### 2026-10-21｜聊天请求中的身份来自哪里

本单元只读本地登录会话的解析，不展开嵌入式Bearer token，不改运行时或运行测试。
`server.py:_request_user_context()`把HTTP请求的cookies与headers传入RequestContext；
`ManagedChatHandler.handle_stream()`在处理用户问题时调用ProductIdentityResolver。
resolver读取签名的`daa_session` cookie，检查会话类型、subject和角色，再通过AccountStore
核对当前账号角色与session_version。验证成功才构造Vanna的User；缺少或过期会话得到
anonymous身份，而ManagedChatHandler会在进入聊天处理前要求登录。

因此，用户身份/角色不是信任浏览器请求体里自报的`user_id`或`role`。签名cookie证明
服务器曾签发会话；再次核对AccountStore可让角色变化或已撤销的旧版本失效。这是当前项目
的本地会话方案，不等同企业SSO。相关代码只看`server.py:836-837`、`runtime_control.py:199-208`
以及`local_auth.py:305-333`。

本轮复述题（待回答）：

1. 为什么不直接相信浏览器请求里传来的role字段？
2. 即使cookie签名有效，如果AccountStore里的session_version已经更新，旧会话会怎样？

用户回答：第一题“可能角色实际已经没有权限”，方向正确，补充更直接的风险是浏览器
请求可被用户修改，攻击者可以自报admin；既有权限发生变化也是必须重新核对账号的原因。
第二题“旧cookie因版本不一致自动失效”，正确理解服务端的失效判定。精确说法是浏览器
可能仍保存cookie，但resolver不再接受其旧版本；若没有其他有效凭据，会得到anonymous，
聊天入口要求重新登录，并非浏览器自动删除cookie。

### 2026-10-21｜角色怎样约束Catalog与SQL

本单元只追踪角色进入现有查询链的两个位置，不改运行时或测试。输入是已验证User，
目标是分清提供给模型的可见业务上下文与执行时权限检查，验收为阅读小片段后复述。

1. `semantic_catalog.py:920-923`按角色过滤`role_visibility`，选出可见表与指标，再构造
   给模型的上下文；不能把全部表都交给模型再只靠提示词要求它保密。
2. `postgres_runner.py:334-335`从受控User确定应用角色，并传给SqlPolicy。
   `sql_policy.py:170-184`检查真实候选访问的表是否在该角色白名单；后续敏感投影检查
   也按角色执行。即使模型猜出隐藏表名或从旧对话看到名字，也不能因此获得执行权限。

当前应用的admin不等于PostgreSQL超级用户：runner仍用workspace配置的reader账号连接，
不是把用户传来的角色拼成数据库账号。Catalog可见性与SQL执行权限各有职责，前者不是
最终授权执行层，后者也不能撤回模型此前已经收到的数据。

本轮复述题（待回答）：

1. 如果模型猜出一个当前角色不可访问的表名，Catalog没给它这个表名是否就足够安全？
2. 应用里的admin能不能因此绕过只读账号，直接执行DELETE？

用户回答：第一题“不行，还需检查SQL越权、删除操作、业务准确性”，结论正确，但需区分
职责：SqlPolicy检查允许的语句、表和敏感列等执行规则，数据库reader权限与只读事务继续
限制写入；业务口径则由指标定义、计划覆盖检查与结果验收等共同约束，这些检查通过也
不能证明全部业务语义正确。第二题“不行，只有读取权限”，正确；应用admin不因角色名
获得数据库写权限。本单元完成，Day 1按累计代码阅读与复述情况收尾。

### 2026-10-21｜Day 2：先读GMV的业务口径

本单元只读`src/data_analysis_agent/resources/catalog/olist_catalog_v3.yaml:162-170`，
理解一个指标怎样定义，不改运行时、不启动训练或评测。输入是Catalog中的指标定义，
输出是用户能按定义计算一个小例子并说清统计粒度。

- 本项目的GMV称为“商品成交额”：有效订单的商品行`price`之和，不含运费。这里的
  有效订单按定义排除`canceled`与`unavailable`，不能从中文名字推断成仅已支付或已送达。
- `grain: order_item`表示按商品行计金额，不是一张订单只取一条，也不是对价格去重。
- `time_field`使用订单购买时间；`default_filters`记录默认订单状态条件。
- 按品类筛选时，只累加该品类的商品行，不能把包含该品类的整张订单金额都算进去。
  这是项目选择的业务口径，不能不看定义就当成所有公司的GMV公式。

本轮复述题（待回答）：一张有效订单有两行手机商品，各100元，一行家居商品50元，另有
运费10元。

1. 按当前定义，这张订单的GMV是多少？
2. 如果问题只问手机品类的GMV，应该是多少？为什么不能把整单金额都算进去？

用户回答：整单250元，手机品类200元，“因为限定了品类”。两题正确。限定品类意味着
筛选参与金额累加的商品行；家居商品不属于查询范围，运费本来就不在本项目GMV口径内。

### 2026-10-21｜Day 2：关联表为什么会把GMV算大

本单元只理解商品行与支付记录直接关联时的重复计数，不改运行时或运行训练/评测。先读
`src/data_analysis_agent/resources/catalog/olist_catalog_v3.yaml:30-62`：商品表的粒度是
`order_item`，支付表的粒度是`payment`，同一个订单可分别有多条商品和支付记录。

假设一张有效订单有两条商品行，价格100和50；它有两条支付记录。若只按`order_id`直接
关联商品表与支付表，每条商品行会匹配两条支付记录，关联后商品价格是100、100、50、50。
这时对商品价格求和会把真实150算成300。问题在于关联后的行重复参与计算，不是SUM语法
错误。不同商品本来也可能同价，因此`SUM(DISTINCT price)`不能作为通用修复。

本项目GMV定义已区分“筛选使用某支付方式的订单”和“按支付方式分组分摊”：前者可用
`EXISTS`只判断订单是否存在匹配支付记录，后者另有金额分摊口径。本单元不展开分摊公式。
已有`plan_sql_guard.py`对特定关联键和归因要求做有限检查，不能据此声称所有关联放大
都会被自动发现。

本轮复述题（待回答）：

1. 上述订单真实GMV是150元，为什么关联支付表后可能算出300元？
2. 如果两件不同商品恰好都是100元，能否用`SUM(DISTINCT price)`避免重复？为什么？

用户回答：第一题“多算了一次”，正确；精确说是每条商品行都匹配到两条支付记录，因而
每条商品金额参与SUM两次。第二题“不能”，结论正确，补充原因：DISTINCT按价格数值去重，
两件各100元的真实商品也会只剩一个100，错误少算。用户追问同一订单为何有两条支付记录。

本次只补充订单与支付的一对多关系：一个订单可使用多个支付方式或多条支付记录完成
付款。例如构造一个无运费、应付150元的示意订单，用信用卡付100元、代金券付50元，对应
同一order_id下的两条支付记录；这是教学示例，不是对某条真实Olist订单的取证。
项目建表定义`src/data_analysis_agent/resources/sql/analytics.sql:94-104`以
`(order_id, payment_sequential)`为联合主键，允许同一订单存在多个支付序号。
`payment_installments`单独记录分期数，不能把分3期直接等同为表中有3条支付记录。

补充复述题（待回答）：如果只计算GMV，不筛选或分组支付方式，是否需要关联支付表？

用户回答“不需要”，正确；随后追问“都是同一个订单，之前那个不应该就是300吗”。本次
固定使用两件商品100元、50元的同一示意订单，先澄清150与300各自指什么，不推进新知识。
如果指JOIN后四行上的`SUM(price)`，计算结果确实为300；它重复累加了两件商品，真实
GMV仍为150。商品表记录卖了什么（100+50），支付表记录这150怎样支付（信用卡100、
代金券50）；它们是同一笔交易的两个视角，不能再将商品金额与支付金额相加当作成交额。
关联后的四行是两条商品行各匹配两条支付记录，不表示实际发生了四次商品销售。
本轮只解释这一处疑问，关联放大的理解仍待用户确认。

### 2026-10-22｜Day 2：从错误JOIN到EXISTS筛选

用户已复述关联会重复计算，并请求具体SQL及继续学习。需纠正“本质就是不能关联支付表”
这一概括：支付表可以参与查询，问题是直接裸连多条支付记录后再累加商品行金额。
重复的是`i.price`，不是把支付表的`payment_value`当作商品金额再次相加。
本单元只展示SQL与筛选写法，不改运行时、不启动训练或评测。

会把前述150元示意订单算成300元的写法：

```sql
SELECT SUM(i.price) AS gmv
FROM analytics.fact_orders o
JOIN analytics.fact_order_items i ON i.order_id = o.order_id
JOIN analytics.fact_payments p ON p.order_id = o.order_id
WHERE o.order_status NOT IN ('canceled', 'unavailable');
```

订单表一行匹配两条商品行，得到两行；再匹配两条支付记录，得到四行；SUM累加这四行的
商品金额。只问总体GMV时直接去掉支付表JOIN即可，订单表仍用于检查默认订单状态。

下一小步：如果问题是“使用过信用卡的订单，其商品成交额是多少”，支付记录只用于筛选
订单，可按Catalog的规定使用EXISTS，不将其多条记录拼到商品行上：

```sql
SELECT SUM(i.price) AS gmv
FROM analytics.fact_orders o
JOIN analytics.fact_order_items i ON i.order_id = o.order_id
WHERE o.order_status NOT IN ('canceled', 'unavailable')
  AND EXISTS (
    SELECT 1
    FROM analytics.fact_payments p
    WHERE p.order_id = o.order_id
      AND p.payment_type = 'credit_card'
  );
```

EXISTS只判断该订单有没有符合条件的支付记录。这里求的是筛选后订单的完整商品成交额，
不是信用卡实付金额，也不是按支付方式分摊的GMV。口径来源为
`src/data_analysis_agent/resources/catalog/olist_catalog_v3.yaml:175`；
`metric_context.py:93-97`另有避免事实行放大的生成提示，不构成业务正确性证明。

本轮复述题（待回答）：同一订单若有两条信用卡支付记录，EXISTS写法会让它的一件100元
商品被累加一次还是两次？

用户回答“一次”，正确。EXISTS只提供有无匹配的筛选判断，多条匹配不会展开外层商品行。

### 2026-10-22｜Day 2：指标口径怎样进入QueryPlan

本单元回到实现，只读`src/data_analysis_agent/query_plan.py:194-202`的
`MetricPlan.from_metric()`及483行调用，不改代码、不运行训练或评测。输入是已从服务端
Catalog选中的`MetricDefinition`；输出是本次QueryPlan中的一份`MetricPlan`。

`from_metric()`复制metric_id、grain、time_field、source_tables、default_filters和
allowed_dimensions，不调用LLM，也不生成或执行SQL。`QueryPlan.from_selection()`用
`tuple(MetricPlan.from_metric(metric) for metric in metrics)`把每个选中指标的定义放进
本次计划。这里的指标口径来自Catalog，不因每次用户换种问法就重新让模型定义。

以GMV为例，计划带上商品行粒度、购买时间和排除canceled/unavailable的默认条件。
保存这些要求不等于已经在SQL中执行过滤；实际候选SQL是否遵循要求仍需检查。尤其不能
由“计划中有default_filters”推出后续校验已经完整证明所有默认条件和指标公式正确。

本轮代码复述题（待回答）：

1. `default_filters=metric.default_filters`中的默认条件，来自服务端Catalog还是模型临时生成？
2. 把条件复制进MetricPlan，是否就意味着数据库已经自动排除了取消订单？为什么？

用户回答：第一题来自Catalog，正确；第二题“不代表，这里只是服务端整理要求，最终还
要看模型生成的SQL”，主线正确。补充：候选SQL还会接受服务端计划、安全和结果等检查，
不能把最终是否允许执行只归于模型；现有检查也不能证明全部业务口径正确。

### 2026-10-22｜Day 2：客单价必须按订单平均

本单元只核对一个新指标，不改运行时、不启动评测。先读
`src/data_analysis_agent/resources/catalog/olist_catalog_v3.yaml:255-264`：本项目
`average_order_value`别名为客单价/AOV，定义为有效订单的商品金额之和除以有效订单数，
不含运费、不是支付金额；`grain: order`表示平均的对象是一张张订单。

对照离线Gold的实现`olist_queryspec.py:531-565`，只关注内层`SUM(i.price)`得到
`order_total`、按`o.order_id`分组，再由外层`AVG(order_total)`平均。该renderer用于
结构化Gold，不是在线理解中文或替代模型生成SQL的入口；线上候选也应遵守相同指标口径。
现有`tests/test_olist_queryspec.py:96-102`断言该两层结构，本轮仅阅读，没有重跑测试。

本轮复述题（待回答）：假设两张有效订单，A有两条商品行100元、50元，B有一条商品行50元。

1. 按本项目口径，这两张订单的客单价是多少？
2. 直接对三条商品行执行`AVG(i.price)`，能否得到客单价？它实际平均的是什么？

### 2026-10-22｜学习范围调整：按机制学习，不遍历全部指标

用户指出无需把所有指标逐个学一遍。采用此调整：已有GMV例子用于理解Catalog口径、
统计粒度、关联重复计数、EXISTS筛选及MetricPlan的传递，之后回到实际请求代码主线。
上面的AOV内容保留为参考，未作答，不再作为必答题或Day 2完成前提。

Day 2剩余内容只看候选SQL与计划之间的检查：入口为
`src/data_analysis_agent/trusted_sql_tool.py:127-139`，调用
`plan_sql_guard.py:26`的`sql_plan_mismatch()`。需要掌握调用位置、发现偏差时的处理，
以及有限规则检查不能证明全部业务正确；不再追加一轮轮类似的金额计算题。
本轮只调整学习安排与记录，不改运行时或运行评测。

### 2026-10-22｜Day 2收尾：SQL计划检查的调用与边界

本单元只阅读`TrustedRunSqlTool.execute()`和`sql_plan_mismatch()`的职责，运行已有筛选
回归，不改运行时或新增检查。输入为`args.sql`模型候选和`context.metadata["query_plan"]`
服务端计划；检查函数返回发现偏差的原因字符串，或返回None。

代码入口为`src/data_analysis_agent/trusted_sql_tool.py:128`：发现偏差后记录审计，构造
失败结果，当前候选不送入正常执行路径；未发现偏差时由139行`super().execute()`继续进入
查询链路，后续安全与数据库检查仍然存在。后半段允许有界修复；修复候选在232行再次
检查计划，不因已经修复就直接放行。本单元不展开修复提供方。

`plan_sql_guard.py`检查已确认的筛选及来源路径、部分关联键、Top-N和归因要求。例如只在
注释或未使用CTE写出某筛选，不能算该条件已约束真正参与结果的查询。它不是一般SQL
等价证明器，也没有通用验证所有MetricPlan默认状态条件和聚合公式。返回None表示本检查
未发现它所覆盖的偏差，不能推出SQL业务完全正确；解析错误也由后续SqlPolicy处理。

已有筛选正反例定向回归通过：`tests/test_query_filters.py`中
`test_valid_filter_shapes_are_not_blocked`与
`test_ineffective_or_wrong_filter_cannot_supply_evidence`，共11个参数化用例，11 passed。
这只验证被覆盖的筛选行为，不是线上业务准确率或全链路验收。Day 2待用户完成本轮复述。

本轮复述题（待回答）：SQL保留了SP筛选，但由于重复关联把GMV算大；即使
`sql_plan_mismatch()`返回None，能否说明SQL业务正确？这个None实际表示什么？

用户回答“不能说明业务正确，只表示符合QueryPlan要求”，前半句正确，后半句需缩小：
None只表示**现有规则没有发现其覆盖的偏差**，不是证明所有QueryPlan要求均已满足。
例如当前没有通用证明所有MetricPlan默认状态条件、时间筛选和聚合公式；无法解析的SQL
也交给后续SqlPolicy拒绝，不能把此函数的None单独当执行许可。Day 2核心概念已完成复述，
本次按用户要求检查并修复真实缺陷，不继续遍历其他指标或开启训练。

### 2026-10-22｜Day 2补充：计划检查与一次SQL修复

宏观职责：计划检查找候选丢掉已确认要求的问题；修复让模型在原要求下再给一次SQL。
修复候选仍不可信，不能通过删除筛选、扩张权限来“修好报错”。安全权限仍由后续SqlPolicy
和数据库角色负责，结果形态与数值约束由ResultValidator负责。

当前`plan_sql_guard.py:sql_plan_mismatch()`的主要规则：

| 检查 | 怎么检查 | 没有证明什么 |
| --- | --- | --- |
| 已确认筛选 | AST跟踪实际贡献结果的来源，识别等值/IN、有效AND/OR、部分EXISTS与CTE别名 | 任意复杂条件等价、全部默认状态或时间筛选 |
| 部分关联键 | 对Catalog给出的原始维度表检查已识别的关联键 | 所有事实表关联都不放大金额 |
| 分区Top-N | 使用计划要求分区的窗口排名，并在实际查询路径过滤排名上限 | 排序指标、方向及任意复杂程序全部正确 |
| 指标归因 | 检查支付分摊/多归属要求涉及的真实列引用 | 出现payment_value就等于分摊公式正确 |

在线单层聚合的外层行数上限由SqlPolicy/runner根据服务端计划统一管理，不要求模型先写
字面LIMIT再触发修复。历史离线计划仍保留原协议。

代码阅读只按这一条链往下走：

```text
TrustedRunSqlTool.execute：原候选检查 / 正常执行
  → 可修复的计划偏差或脱敏SQL执行错误
  → build_repair_prompt：原SQL + 错误类别/安全提示 + Catalog + QueryPlan + 必需结果列
  → LlmRepairCandidateProvider：temperature=0，只生成一次SQL，截断响应不用
  → OneShotSqlRepair：候选通过AST策略
  → TrustedRunSqlTool.execute：修复候选再检查原计划
  → SecurePostgresRunner：再次策略检查 + reader只读执行 + ResultValidator
```

语法、分组结构、未知表列及歧义字段属于可修复候选；无权限、超时、连接失败不调用模型
修复，结果合同不满足也不通过反复生成SQL掩盖。最多产生一个修复候选，修复后仍失败即
停止，不开启第二次模型修复。先查AST策略、实际执行时再查一遍，是为了禁止直接执行
未校验候选；这不是第二次模型调用。

本轮先复现失败，再修复：排名条件含`OR TRUE`/`NOT`、未使用CTE或错分区的排名不能再
冒充上限；正确反写`5 >= rn`和CTE别名可通过。分摊字段不再被注释或字符串冒充。
PostgreSQL的GROUP BY错误优先按SQLSTATE识别；修复服务异常、没有完整候选、无权限/
超时等不再全部误报为“候选未通过安全策略”；旧工具调用错误不再触发新SQL修复。

验证为196项相关确定性回归、独立合成PostgreSQL14项集成及受控模型真实浏览器链路。
数据库正例检查了修复后的SP订单数，反例确认删掉SP筛选的修复候选没有执行。
另覆盖提供方失败、取消、修复候选越权和修复后二次执行失败。浏览器检查查询图表、停止、
刷新和CSV恢复；候选提供方受控，不代表已测量在线模型修复成功率，也不是全业务正确性
认证。当前单元收口，下一次按Day 3学习执行安全与结果校验，不再扩展此处规则清单。

### 2026-10-22｜Day 3开始：只读SQL也有访问范围

用户确认Day 2结束并要求进入Day 3。本日依次阅读SqlPolicy的对象访问、reader只读执行、
结果预算及ResultValidator；每次只讲一个单元，用户复述后再继续，TASK-003暂不标记完成。
本轮目标是理解表白名单如何限制只读查询，输入为SQL和服务端role/workspace，输出为允许
或PolicyViolation；不改运行时、不增加规则、不启动模型或训练，验收为阅读和两题复述。

先看`sql_policy.py:113-130`，允许的表、列和敏感字段来自服务端workspace配置；随后看
`evaluate()`的170-189行。它先按role选择允许表，再遍历AST中的真实表引用，检查schema
与表白名单。普通CTE名称单独处理，不能因为写了WITH就把CTE误当成物理业务表。只读查询
也可能访问账户、会话等应用数据，故“没有DELETE”不是完整权限检查。

例子：`SELECT COUNT(*) FROM analytics.fact_orders`可通过本单元的对象权限检查；
`SELECT COUNT(*) FROM app.accounts`是合法只读语句，但schema不在分析范围，拒绝。
admin可访问的表范围比analyst大，仍不是任意SQL权限；134-157行的单语句/只读规则对两种
角色都生效，因此admin的`DELETE FROM analytics.fact_orders`同样拒绝。数据库reader的
职责放到下一单元结合连接代码阅读，本轮不展开。

验证：已有SQL策略定向回归11 passed、16 deselected；另直接调用evaluate复核上述三条
SQL分别为允许、schema拒绝和写操作拒绝。没有向数据库实际执行这些语句，不代表验证了
对应统计口径或数据库权限配置。

本轮复述题（待回答）：

1. `SELECT COUNT(*) FROM app.accounts`没有写操作，为什么仍然必须拒绝？
2. 在当前项目里，admin身份能否通过这条分析查询链执行DELETE？哪类规则仍然限制它？

### 2026-10-22｜Day 3继续：数据库执行的账号与事务

用户要求继续Day 3。前一节对象访问两题尚未作答，本轮沿同一安全执行链阅读实际连接，
不据此标记用户已掌握或完成Day 3。本节只核对现有实现，不改代码、不启动服务或查询。

阅读`postgres_runner.py:387-400`：连接使用服务端settings.reader_user，随后将事务设为
readonly，再设置本次statement_timeout。应用admin并不切换为数据库超级用户。
账号从workspace绑定，见同文件272-276行；不是由模型SQL或浏览器随意指定。

再读`resources/sql/security.sql:12-29`的Olist初始化：reader默认只读，获授analytics的
USAGE和SELECT；writer获授app元数据表的写权限。reader执行分析SQL，writer保存会话/
审计，不能因为服务需要保存聊天而把writer交给模型查询。SqlPolicy负责应用对象/列规则，
数据库授权和只读事务是另一层实际执行限制；reader可以读取的敏感业务标识仍需SqlPolicy
限制输出，不能由“reader只读”推出数据不会泄露。超时与结果预算将在下一小节展开。

本轮复述题（待回答）：

1. SqlPolicy已经限制只读，为什么连接数据库还要用reader账号、设置只读事务？
2. 保存聊天记录需要写数据库，这能否成为让模型SQL使用writer账号的理由？两种操作应
   分别使用哪个账号？

用户回答：第一题是防止SqlPolicy漏检后发生写入改动；第二题不能，分析SQL使用reader，
聊天保存另用writer，主线正确。需纠正“彻底杜绝风险”：权限与只读事务限制越权写入，
不能单独杜绝敏感数据读取、慢查询或业务计算错误，也以账号授权正确为前提。

### 2026-10-22｜Day 3：返回少不等于计算少

本轮只读`postgres_runner.py:398-400`的statement_timeout及139行默认配置，不改运行时
或启动新测试。服务端选择本次结果预算里的statement_timeout_ms，没有该预算时使用连接
设置；默认配置5,000ms，可由环境配置覆盖，不把默认值当作当前在线实例实测值。
`SET LOCAL statement_timeout`把数据库语句的执行时限限定在当前事务内；runner另外有
结果读取deadline。单次数据库语句时限不等于包含模型调用的整轮对话时限。

结果行数与数据库处理量是两回事。例如对百万条商品行求SUM，最终只有一个数字，数据库
仍可能处理百万行。给聚合结果加LIMIT 1不等于只扫描一行。行数/字节预算控制返回结果的
体积，执行超时限制慢查询占用；超时不能将部分计算冒充完整GMV返回。

本轮复述题（待回答）：

1. `SELECT SUM(price) FROM analytics.fact_order_items LIMIT 1`只返回一行，能否据此
   判断数据库也只处理了一行？为什么？
2. 如果查询超过执行时限被取消，能否把“目前已算到的金额”作为完整GMV回答？

用户回答：第一题仍可能需要读取几百万条商品记录求和；第二题不能返回部分金额作为完整
GMV。两题正确。LIMIT约束输出行数，不保证扫描/计算量小；超时也不能把未完成计算当作
完整业务结论。

### 2026-10-22｜Day 3：完整结果、页面预览和模型样本

本轮只阅读现有结果分发代码，不改运行时、不启动测试。以本次计划和资源预算允许、
且已完整通过结果校验的250行统计结果为例：

| 去向 | 数量 | 用途 |
| --- | --- | --- |
| 服务端完整结果与ResultArtifact | 250行 | 保存本次结果，供后续分页、授权下载和结果复用 |
| 网页初始表格预览 | 200行 | 控制首屏传输和渲染量，标明完整结果共250行 |
| 本次查询给模型的结果摘要 | 最多8行样本，摘要最多1,200字符 | 控制上下文体积；包含完整行数等元数据，不是完整结果表 |

`result_budget.py:36-38`定义预览与模型摘要的不同预算。
`src/vanna/tools/run_sql.py:109-111`保留完整records，141行只对页面数据切片；173-174行
分别存放完整results和display_results。`trusted_sql_tool.py:288-318`只将通过验证的
完整results保存为工件，不把200行预览误存成完整结果。

接着阅读`result_validator.py:110-135`：head(max_rows)最多取8行，若超过字符预算则继续
减少样本行；row_count来自完整验证结果。`postgres_runner.py:526-536`把对应预算传给它。
因此8行不是数据库查询上限，200行也不是完整CSV上限；本例完整CSV应有250条数据记录。
执行结果的允许规模由本次计划与资源预算决定，不由这两个展示预算决定。

模型不能仅凭前8行推断未看到的242行，更不能将样本之和当作全部结果总额。需要完整统计
时应使用经过验证的SQL聚合或服务端计算，再把确切统计交给模型解释。

本轮复述题（待回答）：

1. 本次完整且已验证的结果为250行，网页初始预览多少行？下载完整CSV应有多少条数据记录？
2. 模型只看到最多8行样本，能否直接把这些样本的金额之和说成全部250行的总金额？为什么？

用户回答：网页200行、完整CSV250条；不能把8行样本金额之和当作全部金额。两题正确，
第二题补充原因：样本没有覆盖其余242行，不能证明全量统计。用户进一步指出固定8行可能
限制实际分析能力，本轮核对代码并讨论优化，不直接改造运行时或另开完整评测。

### 2026-10-22｜Day 3讨论：结果摘要应服务于本次问题

已确认的实现短板：`build_result_summary()`固定head(max_rows)，不根据Top-N、分组或时间
序列组织证据；例如完整Top-10只能提供最多8条，全年月趋势只能看到前面若干个月。
`chat_runtime.py:995-1027`的结果解释分支只有已有摘要和查询范围，tools=None，当前不会
因为证据不足自动从完整工件取回缺失行。因此摘要不足会限制答案，并不等于数据库没查全。

建议后续按一个结果证据改造任务推进，尚未实现：

1. 小结果在允许列和请求token预算内完整提供；Top-10优先覆盖全部10条，常见州分组/
   月序列也避免机械丢掉末尾。大结果只提供与问题相关的行及全量计算证据，不无限塞表。
2. 全量统计由SQL或受控服务端代码计算，明确基于完整结果还是用户选出的Top-N。
   比率、客单价、去重订单数不能随意对分组结果求和或直接平均，必须遵循指标粒度与公式。
3. 摘要不足的追问允许在已有权限和版本校验下按需读取/计算同一工件；问题确实改变查询
   范围时再查询数据库，不把缺少样本默认变成向用户询问已经知道的业务条件。
4. 统一结果证据在生成、存储、恢复和回答校验中的预算，使用现有请求token预算；目前
   `result_artifact.py:88,110`与`working_memory.py:60,135`仍有1,200字符限制，只调生成端
   会被保存/恢复再次截断。保留允许列和证据约束，不因解释能力扩展而暴露完整任意结果。

实施验收应覆盖Top-10不漏第9/10名、全年末月峰值不遗漏、非可加指标口径、摘要不足时
工件补取、刷新后证据仍完整及跨用户/workspace拒绝。本轮仅记录设计建议，无新增测试或
准确率结论；继续按四周计划学习，不开展训练。

### 2026-10-22｜Day 3改造：服务端计算，模型解释

用户确认上述方向并授权实施。现在的链路是：已验证完整结果 → 服务端组织证据或计算 →
模型解释 → 保存完整工件与有预算的证据。原来固定最多8行、1,200字符的在线限制已取消，
改为默认4K估算tokens；小结果在预算内完整提供，大结果提供全量极值、时间端点、允许的
合计和预算内选定行。页面首屏仍200行，完整CSV仍保留全部已验证结果。

“合计”必须说清范围：前十品类的销售额之和只是这十项的合计，不能说成全店总额。订单
可能跨多个品类，分品类的去重订单数不能累加；客单价、比率也不能对分组数值直接平均或
求和。是否可加由Catalog和当前维度共同决定，模型无权修改这个口径。

建议亲自按下面顺序只看三个小单元，不必一口气读全文件：

1. `result_evidence.py:build_result_evidence()`：输入完整DataFrame及允许列，输出有覆盖
   范围的证据。先看`statistics`和`coverage`；区分全量计算与放得下的`sample_rows`。
2. `chat_runtime.py:_inspect_previous_result()`：接收语义模型提出的`ResultInspection`，
   验证工件归属与版本，再读取完整CSV。只允许选行/筛选已有维度、可加指标求和或两行比较，
   不接受任意代码，不改当前任务条件，也不重跑SQL。
3. `llm_observability.py:_explain_result()`：首次查询成功后，把当前问题、范围及服务端证据
   交给无工具模型解释；数值检查仍在，但它不是完整的业务语义证明器。

存储链的对应修改在`budget.py:set_result_summary()`、`ResultArtifact`和`WorkingMemory`：
使用同一token边界，不再把正确生成的JSON在保存/恢复时按字符切断。受保护历史离线的
8行/1,200字符样本和候选SQL模板不变，不能混同为这次在线结果证据。

已验证：683项相关确定性回归、15项真实PostgreSQL集成；受控模型的真实浏览器七轮中，
只有品类Top-10和全年月序列两轮查询SQL，其余补看、合计、比较与刷新后解释复用工件。
另测停止/断连和跨用户下载拒绝。没有测量公网模型的业务准确率。

下一小节先审阅第一个小单元，确认能说清“完整结果、模型证据、页面预览”三个范围；
Day 3尚未因功能完成自动记为学习完成，本轮没有新增问答题或训练实验。

### 2026-10-23｜Day 3续读：先算全量统计，再选择模型可见行

本节只读`result_evidence.py:223-278`和`tests/test_result_evidence.py:50`。默认概览路径中，
`statistics`先针对本次选定的完整结果计算极值和允许的合计；后面的循环才按token预算添加
`sample_rows`。`coverage=selected_rows`表示证据没有逐行包含全部结果，不表示数据库只返回
样本，也不表示极值只从样本计算。只有明确提供的全量统计才能支持对应的全量结论。

测试构造366天：前365天金额均为1，最后一天9,876；即使模型证据容不下所有行，服务端
统计仍应包含合计10,241、最大值所在的12月31日和末日数值。这里是测试数据，不是业务实测。

复述题（待用户回答，不标记本节掌握）：

1. 若完整结果250行，证据只放得下20行，`sum_of_returned_rows`应基于250行还是20行？
   为什么？假设指标已批准可加，且没有额外选行或筛选。
2. `coverage=selected_rows`时，模型能否根据服务端明确给出的`maximum_row`指出全年峰值？
   能否凭这20行样本就断言全年每个月都在增长？

用户回答：合计基于完整250行；能根据服务端最大值判断峰值，不能从20行样本推出每月增长。
两题正确。补充：不必让模型亲自看到全部行，服务端看完整序列、给出明确计算结论也可以。

用户追问“目前系统怎么判断全年每月都增长”。核对实现：`build_result_evidence()`目前计算
极值、允许的合计和时间端点，没有按月连续增长、增长月份数量或下降反例的服务端计算。
12个月全部进入证据时仍由解释模型阅读判断；证据不完整时提示词禁止从样本外推，但
`followup_adds_numbers()`仅检查新数字，不能识别没有新数字的错误趋势结论。因此当前没有
代码级保证“每月增长”判断正确，本轮只诊断并记录，未改造运行时。

可靠的后续做法是先验证期间、月份完整性与分组，再按时间逐月计算差额；单个全年月序列
有11次相邻月比较，全部严格大于0才能称全年逐月增长。持平不算严格增长；缺月、空值或
不完整期间不得直接得出肯定结论，也不能自行补0。期末高于期初不代表中间每月都增长。

### 2026-10-23｜Day 3讨论：趋势与其他结果分析的共同设计

用户要求系统考虑趋势及其他query的覆盖，而非只修一个问法。本轮读取结果证据、语义
解释、首次结果解释与完整工件追问路径，用合成月序列`100→120→110→150`调用现有函数。
确认当前没有趋势计算字段；错误的“每个月都增长”及“订单数量最高为150”都不会触发
新数字检查，后者把GMV证据错用于订单数。数字存在不等于对象、范围和结论正确。

已形成[分步实施方案](../../plan/feature-result-analysis-operations-v1.md)：服务端按完整结果
计算趋势、比较、排名、占比等；同时核对期间、分组与分母。关键事实结论由服务端呈现，
模型理解问题并解释。首次查询和追问复用计算；工件足够不重查，缺少可查询数据才补查，
缺月只限制相关结论，不把其他能回答的问题一起拦截。方案中区分现有、待实现和不支持
类型，未把设计标为功能完成。本轮不新增问答题，Day 3尚未结束。

### 2026-10-23｜Day 3改造落地：计算总体与模型可见范围分开

新增`result_analysis.py`供首次查询和工件追问共用。模型提出“判断逐月增长、比较两期、
求合计或份额”等受限参数；服务端读取完整已验证结果计算，直接呈现事实结论。
`100→120→110→150`虽然首末增加，仍有一次下降，不能回答“每月都增长”。缺月不补0；
客单价不能加总；Top-N占全店的份额缺分母时才补查，旧分子必须继续绑定旧工件，不能
因为新查询换了排序就改成另一组商品。扩展同比年份也不能顺便改变其他全年合计的范围。

结果token不再固定4K：先用请求总预算减去输出预留、问题、计划和历史，实际发送时再按
系统提示、工具和消息重算。计算始终覆盖完整结果；token只决定模型可见证据，页面仍预览
200行。对应阅读入口为`budget.py:result_evidence_limit()`、`result_analysis.py:analyze_result()`
和`result_evidence.py:render_analysis_facts()`，之后按一个小单元继续学习，不自动进入Day 4。
本轮浏览器验收与工程进度见`PROJECT.md`；没有启动新训练。

### 2026-10-26｜Day 3续读：ResultValidator与关联放大证据

用户确认：缺少指标列时应直接拒绝；结果列齐全且`safe_to_answer=True`也不能证明SQL业务口径一定正确。
本轮沿`SecurePostgresRunner._run_sql_sync()`和`ResultValidator.validate()`补上一个具体缺口：此前
`join_multiplicity`虽然有校验入口，但线上默认一直是`None`，所以错误JOIN把金额放大后可能仍被当作可回答结果。

现在QueryPlan生成的ResultContract从Catalog带出各源表的事实主键和粒度。主查询通过Policy后，Runner在同一个
PostgreSQL `REPEATABLE READ`只读事务中生成受控诊断查询：按实际聚合条件计算输入行数和去重事实行数，只返回两个
标量。输入数多于去重事实数时，说明至少有事实行重复进入聚合，`join_multiplicity`交给ResultValidator，结果被
拒绝而不是生成数字。探针同样经过SqlPolicy；敏感主键只在COUNT(DISTINCT ...)内部使用，不进入模型或前端。

它不是万能SQL等价证明器：加权支付分摊、复杂相关子查询、无法识别事实主键的派生关系会记为`uncovered`，不能据此
证明安全。正常多对一关联、按品类的合法多成员归属和先聚合再关联不应因为“表有一对多关系”就被拦截。证据说明
“检测到重复输入”，不自动说明业务原因，也不把主键送给LLM。对应代码是`join_evidence.py`、
`semantic_catalog.py:row_keys`和`postgres_runner.py`探针接线；下一节继续看执行错误如何进入一次候选修复。

## 2026-10-26｜学习材料归档与两周计划切换

当前学习问答和代码审阅记录继续集中在本文件；项目事实、工程测试和阶段决策分别写入 `PROJECT.md`、`evals/results/` 与飞书项目文档；后训练知识单独维护在 `docs/post-training/learning/` 及飞书后训练学习笔记。本地学习问答没有和项目进度混写。

原四周路线已压缩为 [`process-agent-internship-improvement-v2-2week.md`](../../plan/process-agent-internship-improvement-v2-2week.md)。当前从 Day 3 的 SQL 安全、结果验证和 JOIN 证据收尾，随后进入真实质量归因、部署边界、后训练复盘和面试模拟。每天继续采用“读一小段真实代码 → 用户复述 → 定向测试 → 记录边界”的方式；工程完成不自动视为用户掌握。
