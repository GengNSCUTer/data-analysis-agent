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
核心指标查询。当前服务器用户没有 Docker daemon 权限，所以编排和加载脚本只完成静态
校验，尚未启动 PostgreSQL 或执行真实导入。
