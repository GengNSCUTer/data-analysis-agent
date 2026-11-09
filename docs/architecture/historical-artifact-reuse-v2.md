# 历史可信结果自动复用（2026-11-05）

本轮实现单Artifact复用。用户可以直接问旧分析中的数值，不必手动指定第几张表；模型提出
结构化需求并可选择候选，代码决定旧结果是否足够。语义相似、历史摘要和Artifact ID均不是
读取或复用授权。不自动JOIN多份结果，不扩展后训练或多Worker请求协调。

## 一条请求如何处理

1. 服务端按当前用户、workspace、会话、context_epoch、版本、角色和有效期查询历史索引。
   语义解释器只看到最近最多8张元数据卡片，不看到CSV、SQL或旧摘要中的数值。
2. `SemanticIntent.requested_answer`描述实际需要的指标、分组、筛选、时间、粒度、Top-N和
   完整性；`history_reference`是可选的来源提示。普通数据问题也会尝试历史检索，按指标筛选
   最多64份候选；每份由覆盖证明器检查，不用相似度授权。
3. `CoverageProver`比较查询范围，检查指标、完整性、时间桶、过滤和粒度；汇总只使用Catalog
   允许相加的指标。随后复验CSV及索引中的已有SHA/行数，核对真实分组唯一性，再本地计算。
4. 命中时复用现有`build_result_evidence`、`analyze_result`、图表和无工具回答核验链。回答绑定
   来源、原始范围、目标范围、快照创建时间和计算操作。表格预览、完整CSV、模型证据各自有预算。
5. 不能证明时继续原有Catalog → QueryPlan → SQL候选 → PlanSqlGuard → SqlPolicy → reader
   → ResultValidator链路。显式引用歧义或已失效时澄清，不猜实体或将缺失当零。

| 决策 | 当前行为 |
| --- | --- |
| DIRECT_REUSE | 同口径结果展示、解释或换图，无业务SQL |
| LOCAL_COMPUTE | 完整结果等值筛选、合法降粒度/时间汇总、已返回排名内比较等，无业务SQL |
| DERIVED_QUERY | 用户明确基于指定旧分析修改并重查；从原范围应用补丁，再走完整SQL链 |
| SQL_REQUIRED | 缺指标、细粒度不足、Top-N不覆盖总体、不可加、需刷新或无法验证 |
| CLARIFY | 历史指代不唯一、来源不可用或条件确有歧义 |

元数据候选匹配后的“proven”只证明本轮支持的覆盖关系。它依赖原查询已经通过既有执行治理，
不是任意SQL业务语义的等价证明。底层数据可变而版本未变时，旧快照也不等于实时数据；
回答明确标注快照时间，要求刷新或新查询当前库存值时重新查询。明确读取已展示库存结果、
解释或换图仍可复用原快照，不能因为指标标记current_snapshot就强制再次查询；这条读取
权限不扩展到普通新任务，也不能覆盖显式refresh、过期、权限或版本失效。

## 状态与存储

`app.result_artifacts`仅保存作用域、来源消息/Run、序号、版本、角色、描述符、父来源和固定
到期时间，完整CSV/Plotly仍在外部工件目录。新成功查询的消息、AnalysisState可信结果指针与
READY索引在`PostgresConversationStore`同一事务中提交，并复用会话行锁和`state_version`。
提交之前不向SSE放出该查询的数据/正文；失败时不发布READY或数值正文，未注册文件在宽限期后清理。
不新增查询指纹或权限hash；文件完整性继续使用项目已有的SHA。

只读历史回答只追加消息和provenance，不改变当前AnalysisState（包括最近成功结果指针）。
最近展示的历史视图从已保存回答metadata投影给解释器，与当前任务分开；用户回到当前任务时
仍沿用原任务。新任务失败也保留上一份成功结果引用，不能把旧数值冒充新查询。
明确derive_query才应用旧范围与补丁，执行期记录待查询状态，成功后更新可信结果。

普通新建/修改分析与只读回看不同：即使命中缓存，也更新本轮已验证的任务范围，不能回答了
订单量却仍把销售额当作当前任务。范围完全一致时指向原Artifact；范围经过筛选或汇总时，将
本地计算结果保存为单来源的独立CSV并原子登记父来源与新状态，分页和下载对应本轮结果。
派生结果沿用原快照时间与到期日，不借本地计算延长数据保留期。模型空的可选任务字段不会
覆盖所选历史来源的范围；改变历史回答范围需明确提出requested_answer。

`requested_answer`一旦提交，指标列表就是必填的非空结构；缺字段/空数组走解释器已有的一次
结构纠错重试，未知或越权指标不重试。当前任务尚无指标时由原准入流程澄清，不构造空需求
进入覆盖证明。历史复用与最近结果检查共用审计构造：记录原工件行数、实际选中行数、进入
解释证据的行数与选择操作；筛选/汇总后的工件行数另见artifact_reuse.computed_row_count。
需要补查基期或总体时同步记录analysis_acquisition，不能只记录最终SQL而丢失补查原因。

- 清空分析：递增`context_epoch`，旧结果不再自动参与新上下文，仍可从历史列表显式查看。
- 编辑回退：同一事务将被撤回轮次及后续结果标为REVOKED，恢复先前分析状态。
- 删除会话：外键级联删除索引，立即失去读取权限；文件随后清理。
- 角色变化：旧角色下的结果保守失效；每次读取重新检查当前Catalog的指标、维度和表权限。
- 版本/文件/有效期变化：拒绝复用；摘要或聊天文本不能恢复已失效的数据授权。

新manifest的`created_at/expires_at`固定，换图不延长TTL。读写及GC使用共享文件系统上的POSIX
读写锁，避免不同Store/进程在读取中清理。它不代表完整产品已经支持多Worker；请求互斥、取消
和容量仍是单进程。已打开并校验的CSV下载可以在目录清理后读完。

新工件标记为需要索引；未提交、已撤销或删除会话留下的文件超过宽限期后清理。旧的、不具备
固定期限或来源证明的结果不会自动补成READY；需要重新查询。不会删除历史实验或训练资产。

已有部署升级时，先使用相应workspace的管理员初始化配置执行
`data-analysis-agent-init --metadata-only`，再移除管理员配置并重启应用；它只更新app元数据和
受限writer授权，不改业务表或reader授权。本次已在Olist和TheLook业务库应用并验证。

## 代码与接口

| 文件 | 职责 |
| --- | --- |
| `artifact_history.py` | 作用域/版本/权限过滤，历史卡片，事务内索引提交及到期状态 |
| `artifact_coverage.py` | 覆盖证明、拒绝理由、受限本地计算 |
| `artifact_reuse_planner.py` | 元数据候选、来源复验、选择单份结果、SQL回退 |
| `semantic_intent.py` | 请求需求与历史引用提案，日期与状态范围规范化 |
| `chat_runtime.py` | 查询前复用，状态隔离、来源卡片、提交后展示及无工具回答 |
| `conversation_store.py` | 来源消息、READY和状态原子保存；编辑撤销 |
| `result_artifact_store.py` | 不可变CSV、固定TTL、文件锁、过期/孤儿清理 |

`GET /api/project/conversations/{id}/artifacts`返回授权目录；已有`artifacts/{artifact_id}/manifest`
和`rows`现在允许读取同会话历史结果。`result.csv?artifact_id=...`显式下载原始完整结果，
省略参数仍下载当前任务结果。新入口在“更多 → 历史可信结果”。历史查询不改当前任务，
下载也不把当前任务切换到旧结果。CSV仍采用一次完整性扫描和分块响应。

## 验收与边界

自动化入口：`tests/test_artifact_coverage.py`、`tests/test_artifact_history.py`、
`tests/test_result_artifact_store.py`和`tests/e2e/test_artifact_history_browser.py`。
浏览器受控模型测试使用真实产品、PostgreSQL、SSE、CSV、图表与独立SQL，覆盖两套业务库，
不把预先给定的意图当作真实模型理解准确率。实际运行数量与真实模型结果追加在PROJECT.md。

当前不支持自动跨会话召回、多Artifact JOIN、任意公式、比率/均值/distinct的无充分统计量
roll-up、跨月周桶重组或分区Top-N本地重算。不支持的覆盖回到受控SQL，并不拒绝合法用户问题。
元数据只检索有界候选，不保证找出无限历史中的所有可复用结果；企业行级权限尚未实现，
未来接入时需将其纳入当前授权检查。未完成80～150条真实模型多轮质量评测或生产并发SLA。
