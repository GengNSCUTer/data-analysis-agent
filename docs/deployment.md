# 安装、初始化与重启恢复

本项目提供一个 FastAPI 服务，内含原生 Web Component 页面。当前自托管入口是 `data-analysis-agent`；无需另起前端开发服务器，也不需要 GPU 或模型训练环境。在线推理使用配置的 SiliconFlow API。已有 PostgreSQL 业务库接入见 [工作区配置说明](workspace-onboarding.md)；不向已有业务库运行合成数据初始化。

首次演示使用仓库自构造的合成电商数据：1,200 笔订单、300 个客户城市、3 个品类，表结构沿用 Olist。版本标记为 `synthetic-olist-v1`，不是真实 Olist 数据，不代表业务准确率或新 Schema 泛化能力。真实业务库接入另行配置，不运行合成数据初始化。

## Docker Compose 启动

需要 Docker Engine 与 Compose v2，账号能够连接 Docker daemon，构建时能够下载 Python/Node 依赖。以下命令从仓库根目录执行。

```bash
git clone https://github.com/GengNSCUTer/data-analysis-agent.git
cd data-analysis-agent
cp .env.example .env
```

编辑 `.env`，填写 API key 和三个不同的随机数据库密码。建议用 `openssl rand -hex 24` 分别生成三个密码，避免密码中的 URL 保留字符。默认端口 32013；端口占用时改 `DATA_ANALYSIS_HTTP_PORT`。

```bash
docker compose --env-file .env -f infra/postgres/compose.yaml up -d --build
docker compose --env-file .env -f infra/postgres/compose.yaml ps -a
docker compose --env-file .env -f infra/postgres/compose.yaml logs init app
curl -f http://127.0.0.1:32013/health
```

启动顺序是 PostgreSQL 健康 → 一次性 `init` 创建 Schema、只读角色和合成数据 → `app`。`init` 正常退出状态应为 0；再次执行不会重复插入订单，发现其他数据版本会拒绝填入合成数据。该流程不接触本机已有 PostgreSQL，也不将数据库端口发布到宿主机。

打开 `http://127.0.0.1:32013/embedded-copilot`，默认用户名 `analyst`。首次密码由服务端生成，保存在应用数据卷中；在自己的终端读取后登录：

```bash
docker compose --env-file .env -f infra/postgres/compose.yaml exec app python -c 'from pathlib import Path; print(Path("/var/lib/data-analysis-agent/bootstrap-password.txt").read_text())'
```

先试“按客户州统计2017年商品成交额，并画柱状图”，再试“改为按客户城市统计，返回完整城市列表，不用画图”。第二个查询可通过“更多 → 浏览结果 / 下载完整结果”检查超过 200 行的分页与完整 CSV。

远程服务器可以只绑定本机，通过 SSH 转发访问：

```bash
ssh -N -L 32013:127.0.0.1:32013 your-user@your-server
```

## 不使用 Docker 的 wheel 安装

需要 Python 3.12、Node.js 22 和独立的 PostgreSQL 16+。在 PostgreSQL 中准备名为 `data_analysis_agent` 的空数据库及用于初始化的管理员；管理员凭据只交给初始化命令。HTTP 应用只使用两个受限角色的 passfile。

Linux 上若已有 PostgreSQL 二进制、尚无独立实例，可以创建自己的数据目录（将示例绝对路径换成自己可写的仓库外目录）。`initdb` 会提示设置管理员密码，随后填入私有环境文件的 DSN；不要指向正在使用的数据库目录。

```bash
initdb -D /absolute/private/copilot-postgres -U app_admin -A scram-sha-256 --pwprompt
pg_ctl -D /absolute/private/copilot-postgres -l /absolute/private/copilot-postgres.log -o '-h 127.0.0.1 -p 5433 -k /tmp' -w start
createdb -h 127.0.0.1 -p 5433 -U app_admin -W data_analysis_agent
```

从源码构建一次安装包：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
cd frontends/webcomponent
npm install --package-lock=false
npm run build
cd ../..
python -m pip wheel --no-deps --wheel-dir dist .
python -m pip install 'dist/vanna-2.0.2-py3-none-any.whl[product]'
```

`vanna` 是当前 fork 保留的发行包名，必须安装这里构建的 wheel；不要用 PyPI 的同名上游包代替。安装包同时包含 `vanna`、`data_analysis_agent`、Catalog、初始化 SQL、HTML 和已构建 JS。`[product]` 不安装 Torch/Transformers；测试和后训练分别使用额外依赖，见 `pyproject.toml`。

将下面变量填入一个仓库外、权限为 600 的私有环境文件。`STATE_DIR` 使用自己的绝对目录；`PGPASSFILE` 必须指向该目录中的 `pgpass`。不要复用现有真实业务库运行 `--synthetic`。

```dotenv
SILICONFLOW_API_KEY=your-api-key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V3.2
DATA_ANALYSIS_ADMIN_DSN=postgresql://app_admin:your-hex-admin-password@127.0.0.1:5433/data_analysis_agent
DATA_ANALYSIS_READER_PASSWORD=your-hex-reader-password
DATA_ANALYSIS_WRITER_PASSWORD=your-hex-writer-password
DATA_ANALYSIS_POSTGRES_HOST=127.0.0.1
DATA_ANALYSIS_POSTGRES_PORT=5433
DATA_ANALYSIS_STATE_DIR=/absolute/private/copilot-state
PGPASSFILE=/absolute/private/copilot-state/pgpass
DATA_ANALYSIS_CATALOG_PATH=/absolute/private/copilot-state/synthetic-catalog.yaml
DATA_ANALYSIS_DATASET_VERSION=synthetic-olist-v1
DATA_ANALYSIS_DEMO_PORT=32013
```

在已激活的虚拟环境中导出配置，再初始化，随后移除管理员权限再启动服务。这里的 `source` 只用于自己维护的环境文件。

```bash
set -a
. /absolute/private/deployment.env
set +a
data-analysis-agent-init --synthetic
unset DATA_ANALYSIS_ADMIN_DSN DATA_ANALYSIS_READER_PASSWORD DATA_ANALYSIS_WRITER_PASSWORD
cd /tmp
data-analysis-agent
```

运行不需要 `PYTHONPATH=src`，也不依赖仓库当前目录。首次密码默认位于 `$DATA_ANALYSIS_STATE_DIR/bootstrap-password.txt`。需要后台常驻时用自己的 systemd/screen 启动上述服务，不让进程依赖终端连接。

## 持久化与升级

| 内容 | 保存位置 | 重启后的用途 |
| --- | --- | --- |
| 账号、会话、分析状态、执行记忆、审计 | PostgreSQL / `postgres_data` 卷 | 继续原会话与受控追问 |
| 完整 CSV、图表和工件元数据 | `STATE_DIR/result-artifacts` / `app_data` 卷 | 分页、下载、图表回放 |
| 登录签名密钥 | `STATE_DIR/session-secret`，权限 600 | 重启后在有效期内继续使用登录 cookie |
| 首次密码、受限数据库凭据 | `STATE_DIR/bootstrap-password.txt`、`pgpass`，权限 600 | 登录及 reader/writer 连接 |
| 合成 Catalog | `STATE_DIR/synthetic-catalog.yaml` | 与合成数据版本对应 |

可以显式设置 `DATA_ANALYSIS_DEMO_SESSION_SECRET`；未设置时服务生成并持久化密钥。首次账号密码只在创建账号时生效，修改环境变量不会给已有账号重置密码。Cookie 本身仍有有效期，不是永久登录。

```bash
# 同时重启应用和数据库，保留命名卷
docker compose --env-file .env -f infra/postgres/compose.yaml restart db app
```

数据备份需要同时包含 PostgreSQL 与应用数据目录。不要使用 `down -v`，它会移除持久化卷。升级时先备份，再重新构建、运行 `init` 更新已有 Schema；初始化使用现有的 `IF NOT EXISTS` / `ALTER` SQL，不另起一套迁移框架。新版本与旧版本的指标或数据版本不同时，旧结果引用仍会按已有规则失效。

## 常见问题

| 现象 | 检查项 |
| --- | --- |
| Docker socket permission denied | 使用有 Docker 权限的账号；这是 daemon 权限问题，不是应用启动失败 |
| wheel 构建提示 Missing product asset | 先完成 Web Component 的 `npm run build`；构建器不自动下载前端依赖 |
| no password supplied | `PGPASSFILE` 路径、文件权限 600、host/port/db 与初始化时一致；不要使用管理员账号启动应用 |
| API key missing / 认证失败 | 环境中的 key、base URL；可用 `DATA_ANALYSIS_ENV_FILE` 指定私有配置位置 |
| 重启后工件 404 | 是否保留同一个状态目录和 PostgreSQL；是否仍是同一用户、workspace 和版本 |
| 重启后要求重新登录 | 是否换了签名密钥、丢失状态目录，或 cookie 已超过有效期 |
| 安装浏览器提示系统不支持 | 浏览器测试需匹配宿主系统；本次 Ubuntu 20.04 使用 Playwright 1.62 与对应 Chromium |

`/health` 是进程存活检查；PostgreSQL 由单独的 DB healthcheck 检查。对外部署还需要 HTTPS、来源配置、备份和后续运行保障。当前没有企业 SSO、组织行权限、多实例协调或生产 SLA。

## 本次隔离验证入口

`scripts/verify_isolated_deployment.py` 接受 `--pg-bin` 和 `--output-parent`。必须用全新环境中安装 wheel 后的 Python 运行，脚本检查模块来自 `site-packages`，不依赖源码导入。它创建自己的数据库目录、随机本地端口和应用目录；检查初始化幂等、reader/writer 隔离、真实浏览器登录、模型 SQL、图表、独立参考值、分页、下载、应用与数据库重启及连续追问。结束时停止自己的进程，保留仓库外证据。

已经构建 wheel 后，可从仓库根目录执行以下命令复核。先在环境中配置自己的模型API key；测试会调用真实模型。`pg-bin` 指向包含 `initdb`、`pg_ctl`、`postgres` 的目录。运行账号不能是root。

```bash
python3.12 -m venv /absolute/private/verify-venv
/absolute/private/verify-venv/bin/pip install 'dist/vanna-2.0.2-py3-none-any.whl[product,browser-test]'
/absolute/private/verify-venv/bin/python -m playwright install chromium
/absolute/private/verify-venv/bin/python scripts/verify_isolated_deployment.py --pg-bin /absolute/path/to/postgres/bin --output-parent /absolute/private/verification
```

旧版Linux需要匹配浏览器运行时；本次Ubuntu 20.04实际使用 `playwright==1.62.0` 及其Chromium。浏览器测试依赖仅用于验收，不是产品服务启动所需。

实测结果与 Docker 验证边界记录在 [项目进度](../PROJECT.md) 的 2026-10-20 条目；容器配置校验与容器实际运行分开报告。
