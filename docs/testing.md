# 测试分层与复核

测试实现链路是否按约定工作，与模型是否正确理解任意业务问题是两件事。所有用例和完整日志
不要混算一个“准确率”。产品测试不使用离线 protected final，也不回流训练。

| 层次 | 依赖 | 验证内容 / 入口 |
| --- | --- | --- |
| 确定性回归 | Python 产品/test 依赖，无 key、DB、GPU | 语义对象、状态继承、摘要及并发、执行记忆、SQL 策略、结果与工件；完整清单在 [CI](../.github/workflows/tests.yml) 的 `Run deterministic Python tests` |
| PostgreSQL 集成 | 独立合成业务库、reader/writer、产品依赖 | 角色、实际取消/超时、会话事务、认证、审计、工件路由；CI 的数据库 job 独立运行 |
| 受控模型浏览器 | 上一层 + Playwright Chromium | 实际页面、SSE、停止/断连、图表、刷新/CSV、归属和网络失败不重发；不消耗在线模型额度 |
| 真实在线模型 | 已运行实例、私有账号/API key | 自然语言理解、连续追问和独立 SQL 数值；显式手动运行，记录首次失败和定向复测 |

## 无外部凭据的开发回归

```bash
python -m pip install -e '.[product,test]'
DATA_ANALYSIS_ENV_FILE=/dev/null python -m pytest -q \
  -m 'not integration and not postgres' \
  tests/test_semantic_intent.py tests/test_analysis_state.py \
  tests/test_analysis_context.py tests/test_execution_memory.py \
  tests/test_sql_policy.py tests/test_result_validator.py \
  tests/test_runtime_control.py tests/test_local_auth.py
```

这是开发子集，完整清单以 CI 为准。`postgres` 标注需要数据库的测试；`integration` 包括浏览器
测试。没有开启相应环境开关的 skip 必须记录，不能计为通过。CI 同时构建前端和 wheel，并在
源码目录之外安装检查资源；不把开发机有环境、缓存或模型权重当作用户已经安装成功。

`test_demo_scenarios.py` 中的真实Olist快照golden核对也标为 `postgres`，但不纳入合成库CI；
其固定数据参考值与合成样本不同。CI只运行该文件的无数据库清单/按钮测试。

## 数据库和浏览器

先按 [部署说明](deployment.md) 只在空的独立数据库中初始化合成数据，导出其中的受限连接、
状态目录与 `PGPASSFILE`。以下测试会写应用元数据，不能指向正式用户库。路由测试需要构造
模型客户端，但不调用公网，可把 `SILICONFLOW_API_KEY` 设为 `test-no-network`。

```bash
RUN_PROJECT_DB=1 python -m pytest -q -m postgres \
  tests/test_postgres_runner.py tests/test_postgres_conversation_store.py \
  tests/test_postgres_run_recorder.py tests/test_trusted_routes.py \
  tests/test_local_auth.py tests/test_copilot_contract.py tests/test_result_artifact_routes.py

python -m pip install -e '.[browser-test]'
python -m playwright install chromium
python scripts/verify_runtime_capacity.py --browser-only --output /private/new-browser-run
```

受控脚本启动自己的临时 HTTP 进程、创建唯一测试账号和会话；结束后停止该进程，不触碰现有
用户或业务表。`--output` 使用新的仓库外目录，生成的凭据与原始截图不提交。容量验证不加
`--browser-only`，即 1/5/10 并发各20次。CI 数据库容器是临时的，不下载 Olist/TheLook 原始数据。

浏览器工具需匹配宿主系统。当前服务器Ubuntu20.04使用Playwright1.62.0和对应Chromium
完成验证；1.64.0在该系统安装Chromium时报告不支持。此处可先安装 `playwright==1.62.0`，
再安装浏览器。CI使用 `ubuntu-latest`，不因此锁低所有部署者的Playwright版本。

## 在线业务与安装恢复

真实 TheLook 60题用 `scripts/verify_workspace_onboarding.py`，见 [接入说明](workspace-onboarding.md)。
全新 wheel、独立 PostgreSQL、真实模型和重启恢复用 `scripts/verify_isolated_deployment.py --help`；
需要 PostgreSQL 16+ 二进制和新虚拟环境运行，证据生成在仓库外。两者都需要私有模型额度，
不由 push/PR 自动触发。已有的首次报告、失败与定向复测分别记录，不把混合证据写成全量新跑。
