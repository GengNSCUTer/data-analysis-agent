# 结果引用与解释输出改造验收（2026-10-27）

> 历史实现：本方案随后因限制自然表达被替代，不再描述当前运行时。
> 当前模型自由回答与按疑点核验见[后续验收](free-result-answer-20261027.md)。

本轮修复学习审阅中实际复现的数字错配：SP=20/RJ=10时，旧数字集合检查会放行
SP=10/RJ=20，也会将证据中的行数2当作可用金额。未新增模型审核轮次、数据库表或hash，
未改变SQL Policy、reader、结果合同或数据权限。

## 实现

1. 模型解释改为选择结果行、maximum/minimum/sum的JSON引用，服务端从同一证据渲染实体、
   指标与数值；禁止模型在引用中提交value、entity、unit或自由文本。无效选择直接输出服务端
   事实，不再请求模型修复。首次解释与结果追问共用`result_explanation.py`；删除旧数字池正则
   和`constrain_followup_answer()`，更新全部调用方及受控测试Provider。
2. 正常选择先展示模型选定的结论；无法使用选择时，已有分析结论先展示，按行/Top-N请求
   保留明细顺序，其余场景先极值和允许相加的合计。极值取对应完整结果行，不受明细样本前缀
   限制；并列数量来自完整统计，明确代表行而非唯一最高。
3. 数值按Catalog的kind/unit格式化，金额与时长保留两位、比例转百分数、计数保留整数。
   金额使用Decimal四舍五入；未知币种不标成人民币或美元；维度键01/NA不当数值转换。
   行展示、极值与合计共用格式化，范围/行数放现有依据卡片；追问也显示卡片，预算缩减后
   使用实际给模型的行数。卡片保留原工件的时间/筛选，不用当前待查范围替换。

真实页面还发现并修复：空`top_n={}`误标成Top-N；TheLook结果别名customer_state缺少中文
展示名。前者归一为空范围限制，后者复用workspace维度别名，不增加数据集专用判断。

## 验证

| 层次 | 结果 | 覆盖 |
| --- | --- | --- |
| 与CI相同的确定性Python清单 | 698 passed，10 deselected | 新引用、旧调用方、SQL/状态/预算/权限等；deselected不计通过 |
| 定向Ruff、diff检查 | 通过 | 本轮直接改动文件 |
| 隔离合成库受控浏览器 | 1 passed，26轮 | 实际Web Component/SSE/reader/工件；含两次恶意解释、趋势/比较/占比、换图、刷新、CSV与关联放大拒绝 |
| Olist + 真实DeepSeek-V3.2浏览器 | 1 passed，4轮 | 各州销售额、概括、换图、刷新后解释；CSV与独立SQL一致；SQL调用[1,0,0,0] |
| TheLook + 真实DeepSeek-V3.2浏览器 | 1 passed，4轮 | 销售额前十客户州、概括、换图、刷新后解释；CSV与独立SQL一致；SQL调用[1,0,0,0] |

真实模型实际返回引用JSON，解释调用不提供SQL工具；最终页面及持久化回答不含引用JSON。
截图人工查看确认：正文先结论，依据卡片显示中文指标/维度、版本、行数与时间范围。
没有前端JS改动，复用现有组件，不需要另起前端。

首次受控浏览器失败来自测试读取Web Component宿主inner_text未进入Shadow DOM，以及旧断言
期待未经格式化的小数；改用真实可见元素定位和同一数值格式化函数后通过。TheLook首次独立
对照误按物理state列读取结果，修正为合同别名customer_state。其后完整216州绘图被现有
200行图表限制正常拒绝；没有放宽限制，另以明确前十名完成绘图验收，不能称216州均可绘图。

仓库外证据根目录：
`/disk2/gengnan/data-analysis-agent-data/evals/result-explanation-20261027/`。

- `controlled-final/observations.json`、`reference-fallback.png`：26轮与恶意解释替代。
- `live-olist-r2/olist-demo/`：Olist四轮、真实Provider引用与截图。
- `live-thelook-r2/thelook-demo/`：216州绘图预算拒绝的原始记录。
- `live-thelook-top10/thelook-demo/`：TheLook前十名场景的四轮、Provider引用与截图。

## 边界

引用有效只证明所展示实体与数字来自对应证据，不证明模型选对了回答重点，也不证明原SQL
业务口径正确。当前结果解释只接受上述事实引用，趋势/同比等继续走已有服务端分析操作，
不是任意自由文字的语义审核系统。此次34轮为开发验收，不是新准确率或泛化分数。
Olist/ TheLook原有认证、存储、执行与图表上限保留，未重跑历史60题/750条离线套件。
