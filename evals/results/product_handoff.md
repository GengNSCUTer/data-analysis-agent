# 第六项：CI、文档与开源交付验收

日期：2026-10-20。对应[六项工程计划](../../plan/process-agent-product-readiness-v1.md)第六项。
本轮整理自动回归、可安装产物、贡献/安全/许可与面试事实，不改产品默认模型，不启动训练，
不重新运行protected final，也不发布tag、release或PyPI包。

## 自动化与隔离验证

`.github/workflows/tests.yml` 在push/PR/手动触发时分开执行：

1. 无数据库、无模型凭据的确定性回归，前端构建、用例清单和wheel资源检查。
2. 临时PostgreSQL16合成库：初始化受限角色与应用元数据，执行实际数据库/认证/工件路由测试。
3. 受控provider配合真实PostgreSQL和Chromium：实际分析SSE、图表、停止/断连、刷新/CSV、
   跨用户拒绝和网络错误不重发。不依赖在线模型行为，不消耗模型额度。

真实模型、独立安装重启和业务数值对照保留显式手动入口，见[测试说明](../../docs/testing.md)。
移除上游遗留的 `python-publish.yaml`，防止fork在创建release时自动向PyPI发布上游同名包。
保留上游版权，安装包名仍为 `vanna`，项目链接改指向本仓库。

| 本地验证 | 结果 | 条件及边界 |
| --- | --- | --- |
| 与CI相同的纯测试清单 | **529 passed、10 deselected** | 新venv安装wheel，不装Torch；清除模型key、禁用.env、DB指向不可连接端口。10项为明确排除的DB/浏览器测试 |
| PostgreSQL/账号/工件路由 | **37 passed、13 deselected** | 同一份CI命令，独立PG18.3合成库，初始化两次通过；13项为该文件集合中的非postgres测试 |
| 真实Chromium完整链路 | **passed** | 受控provider，停止/断连后active/waiting/inflight均为0；正常查询有表和图；刷新同工件/CSV，跨用户404；故障SSE未触发polling重发 |
| 前端与包 | **passed** | TypeScript/Vite构建；wheel离源码目录导入，两套工作区资源可读；无Torch；`pip check`通过 |
| 确定性评测清单 | **passed** | 60题清单、26项策略断言和3个固定Demo清单；不是60次在线模型回答 |
| 静态检查 | **passed** | 本轮改动Python文件的Ruff、compileall及diff检查 |

本机为Ubuntu20.04，浏览器验证使用Playwright1.62.0；1.64.0在该宿主安装Chromium时明确
不支持。CI为ubuntu-latest，使用其支持的版本。本轮本地重放成功不等于GitHub托管job或
PG16容器已经执行成功；远端结果需查看Actions。本机Docker实运行和外部开发者独立部署
仍未验证。新依赖下TestClient产生一个httpx接口弃用警告，未导致测试失败。

## 本轮实际发现与处理

早期纯测试会进入未分类的数据库bootstrap/认证路由；已将这些函数标为 `postgres`，保留
token/口令等纯测试。真实Olist快照golden检查同样归为postgres，但不接入合成库CI。
图表复用测试改为读取实际runtime workspace配置，避免静态旧profile漏可相加指标属性。

数据库测试原先假设一定有5个州、真实Olist的SP最高，以及客户数必定超过1,000；这些不适合
合成库。现按独立reader聚合结果核对州计数/排序，截断测试显式设两行资源上限，保持对
不完整结果的degraded断言。没有放宽SQL策略、越权保护或业务结果校验。

失败日志保留；本轮没有为“全部通过”删掉测试或把数据库失败计为跳过。

## 说明与尚未完成项

README、PROJECT、请求架构与文档导航已对齐当前在线入口、容量和后台摘要。新增/整理
`CONTRIBUTING.md`、`SECURITY.md`、`THIRD_PARTY.md`、`docs/testing.md`和
[面试项目事实](../../docs/interview/project-release-facts-v1.md)。旧作品集定位页改为入口，
不维护另一份过时模型/认证/分数说明；历史实验和用户原稿保留。

交付可复核的安装与接库路径，尚不等于企业生产验收。没有真实参与者，因此没有编造
独立部署反馈；正式发布、企业SSO/行权限、多实例控制和备份恢复仍不在已实现范围。
用户逐段代码复述仍按四周学习计划推进，本轮整理事实不表示已经掌握或需要立即继续训练。

原始证据位于仓库外
`/disk2/gengnan/data-analysis-agent-data/evals/product-readiness-stage6-WB0gppgI/`：
`pure-fresh-wheel.log`、`postgres-tests.log`、`browser/report.json`、`report.json`、
安装/构建和清单日志。首次失败的数据库报告与日志另存 `*-first-attempt.*`；原始账号和连接
口令不随报告提交。更早的业务/容量报告保留原分母，不因本轮CI结果改写。
