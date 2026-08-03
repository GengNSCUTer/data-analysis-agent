# 本地 PostgreSQL 开发实例

本项目有一个独立于其他项目的用户态 PostgreSQL 开发实例：

| 项目 | 值 |
| --- | --- |
| 数据库 | `data_analysis_agent` |
| PostgreSQL 版本 | 12.20 |
| TCP 地址 | `127.0.0.1:35434` |
| 数据目录 | `/disk2/gengnan/data-analysis-agent-runtime/postgres/` |
| 日志 | `/disk2/gengnan/data-analysis-agent-runtime/postgres.log` |

它不复用 `rag_agent` 的 35432 端口或 `ai_web_studio` 的 35433 端口。当前服务只绑定
loopback；TCP 使用 SCRAM 认证，当前用户可通过本机 Unix Socket 进行管理。数据库目录、
日志、密码和未来生成的数据均在仓库外，绝不能加入 Git。

状态检查和停止命令：

```bash
/disk2/gengnan/conda_envs/pg_runtime/bin/pg_isready -h 127.0.0.1 -p 35434 -U postgres
/disk2/gengnan/conda_envs/pg_runtime/bin/pg_ctl \
  -D /disk2/gengnan/data-analysis-agent-runtime/postgres stop
```

服务由用户态 `pg_ctl` 启动，不依赖 Docker 或 systemd。Olist analytics 数据已通过
`load_olist_local.sh` 真实加载；应用角色配置仍作为后续独立迭代执行。
