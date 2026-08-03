# 第一轮评测

评测分为两类，不能混淆：确定性评测验证版本化用例、SQL AST 策略和 Olist golden SQL；
在线模型评测验证真实 SSE 的工具调用、表格、图表和中文结论。前者可以进入 CI，后者需要
本地模型密钥，不能在 GitHub Actions 中运行。

运行确定性评测：

```bash
python scripts/run_project_evaluation.py --database \
  --output /tmp/first-round-deterministic.yaml
```

`evals/cases/v1.yaml` 包含 60 条用例：指标 12、多表关联 8、趋势 6、口径解释 4、歧义 4、
安全/边界 26。`evals/results/first-round-deterministic.yaml` 记录本轮：26/26 安全策略预期
通过，且本地 PostgreSQL golden SQL 通过。该结果不是 LLM 准确率。

真实模型的已验证最小集合为：中文州订单查询（表格与结论）以及同一聚合问题的 Plotly 柱状
图。它们通过 `examples/trusted_olist_web_demo.py` 的 SSE 在本地运行，因依赖 SiliconFlow
密钥和非确定性模型输出，不作为 CI 的数值基准。
