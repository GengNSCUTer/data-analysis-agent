# Olist 分析层转换与加载

原始 Olist CSV 永远不进入 Git。本项目把已核验的 Kaggle version 2 解压在仓库外目录，
再由 `scripts/transform_olist.py` 生成 PostgreSQL `COPY` 可直接导入的规范 CSV。

```bash
python scripts/transform_olist.py \
  --raw-dir /disk2/gengnan/data-analysis-agent-data/olist-v2-2026-08-03/raw \
  --output-dir /disk2/gengnan/data-analysis-agent-data/olist-v2-2026-08-03/analytics-v1 \
  --dataset-version-id olist-kaggle-v2-2026-08-03 \
  --manifest data/manifest/datasets.yaml \
  --verify-source
```

转换器校验已选 8 个源 CSV 的 SHA-256、必需列、时间、整数和金额格式；源文件变化或不
能无歧义规范化的值会使运行失败。输出文件和 `rejected_fact_reviews_orphan_orders.csv` 均
留在仓库外目录。当前已核验版本的实际逻辑记录数为：客户 99,441、卖家 3,095、商品
32,951、品类映射 71、订单 99,441、订单项 112,650、支付 103,886、评价 99,224；孤立
评价为 0。评价原始文件含可跨行的评论文本，不能以物理行数当作逻辑记录数。

本地 PostgreSQL 编排位于 `infra/postgres/compose.yaml`。从 `.env.example` 创建一个不进入
Git 的 `infra/postgres/.env` 后，可启动并加载：

```bash
docker compose --env-file infra/postgres/.env -f infra/postgres/compose.yaml up -d
docker compose --env-file infra/postgres/.env -f infra/postgres/compose.yaml exec db pg_isready
./infra/postgres/load_olist.sh
```

容器只监听 `127.0.0.1:5433`，转换数据只读挂载为 `/data/analytics`。加载脚本先建表，
在单个事务内清空旧的开发数据、插入 `analytics.dataset_versions`，再按维表、订单、订单项/
支付/评价顺序执行客户端 `COPY`。`evals/sql/golden_metrics.sql` 保存首次加载后需要固化的
核心指标查询。当前服务器实际使用独立的用户态 PostgreSQL 实例，而不是 Docker：

```bash
./infra/postgres/load_olist_local.sh
/disk2/gengnan/conda_envs/pg_runtime/bin/psql \
  -p 35434 -U postgres -d data_analysis_agent \
  -v ON_ERROR_STOP=1 -f evals/sql/verify_olist_golden.sql
```

2026-08-03 已完成真实导入，8 张表行数与转换报告一致，订单项、商品、卖家、支付和评价
外键违规均为 0。首次导入发现 Olist 有一条承运商交接时间略早于购买时间，因此删除了
不在数据合同中的过严约束；客户实际送达不得早于购买时间的约束继续保留。
