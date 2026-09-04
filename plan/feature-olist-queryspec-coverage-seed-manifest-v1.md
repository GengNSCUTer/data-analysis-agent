---
goal: Design and freeze a small static Olist QuerySpec coverage seed manifest
version: 1.0
date_created: 2026-09-04
last_updated: 2026-09-04
owner: Data Analysis Agent
status: Implemented; not materialized
tags: [feature, post-training, data-contract, coverage]
---

# Olist QuerySpec 静态 Coverage Seed 清单 v1

## 1. 目标与非目标

本任务提交一个小型、可逐条审阅的结构化 seed 清单，作为受控物化器的未来输入范例。它验证十项
指标、四种允许结果形状和三类 split 可以在不掺入自然语言或 SQL 的前提下被清楚地声明。

非目标：不调用物化器，不生成 QuerySpec、Gold SQL、Prompt 或训练 JSONL；不执行数据库或结果合同；
不读取 protected holdout；不做 token 审计、模型加载或训练。

## 2. 输入、输出与不变量

| 项目 | 本轮约束 |
| --- | --- |
| 输入 | 冻结的 Olist v2 指标合同、coverage matrix、QuerySpec 验证规则。 |
| 输出 | `data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl`、审阅说明与结构测试。 |
| 禁止字段 | `question`、`prompt`、`sql`、`result`、`limit`、排序、自由过滤、语言变体、真实行数据。 |
| 版本 | 所有行隐式使用 `WorkspacePin.current()` 的 Olist v2/PostgreSQL 快照；不允许 seed 私自覆盖版本。 |
| split | 显式声明 `train`、`validation`、`in_domain_test`；同一 `join_program_id` 不得跨 split。 |
| protected 边界 | 仅允许未来传入不可逆 family fingerprint summary；本轮不创建空摘要，也不读取/派生 protected holdout 内容。 |

## 3. 验收证据

测试仅检查清单 JSON 结构、字段白名单、QuerySpec 验证、family 不重复、program 不跨 split，以及预期
覆盖分布。它不调用 `render_gold_sql()`、不写仓库外目录、也不访问数据库。通过这些测试不代表 Gold SQL
业务正确，也不代表最终训练/验证/测试划分具有模型统计代表性。

## 4. 下一关

下一项必须单独设计 restricted protected-family summary 的生成责任人、输入权限、hash 证据和人工复核
流程；只有该边界获批后，才可将本清单传给受控 materializer 做小批外部物化。
