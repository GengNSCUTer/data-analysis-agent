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

`infra/postgres/analytics.sql` 定义目标表、约束和索引，但本阶段尚未启动 PostgreSQL 或
加载这些文件。进入 Phase 3 后，加载作业必须先插入 `analytics.dataset_versions`，再按
维表、订单、订单项/支付/评价的顺序使用显式列清单执行 `COPY`。
