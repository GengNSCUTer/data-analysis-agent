---
goal: Export an externally stored, non-reversible protected-family summary for Olist seed leakage checks
version: 1.0
date_created: 2026-09-04
last_updated: 2026-09-04
owner: Data Analysis Agent
status: Implemented; no real protected input exported
tags: [feature, post-training, holdout, data-contract]
---

# Olist Protected-Family Summary 受限导出 v1

## 1. 目标

为 Olist QuerySpec materializer 准备可审计的 protected-family 摘要，而不让 materializer 或 seed
构造步骤读取 60 条 holdout 的问题、Gold SQL、结果或 case ID。导出器只转换已经由受限人工流程审查的
`family_id` 清单，并输出不可逆 fingerprint summary 与旁路 evidence。

## 2. 输入、输出与禁止项

| 项目 | 合同 |
| --- | --- |
| 批准输入 | 仓库外 JSON；只允许版本、当前 WorkspacePin、protected-source manifest SHA-256、人工 review reference 和排序去重后的 `family_id`。 |
| 输出 | 仓库外新目录中的 `protected_family_summary.json` 和 `protected_family_summary_evidence.json`；原子写入。 |
| summary 内容 | 仅 `summary_version` 与 SHA-256 `family_fingerprints`，保持 materializer 兼容。 |
| evidence 内容 | 输入/输出 SHA-256、版本、workspace、源 manifest hash、family 数、生成时间和 review reference；没有 family 原文。 |
| 禁止项 | 不接受 case ID、question、prompt、SQL、result、seed、数据库路径或 holdout 原文；不接受仓库内输入或输出；不接受空列表、重复/未排序 family、版本漂移或伪造 hash。 |

## 3. 人工责任边界

导出器不拥有读取 protected holdout 的权限，也不能证明批准输入是否完整。受限人工流程必须在独立环境中：

1. 依据已冻结的 family 规则，把 protected case 映射成 `family_id`；
2. 只把排序、去重后的 family ID 及 source-manifest hash 写入批准输入；
3. 填写可追溯但不包含原文的 review reference；
4. 运行导出器，并保存 summary/evidence 到仓库外受控 release 目录。

Materializer 仍不得读取 protected 原文；后续真正物化时必须把 summary 与其 evidence 一起冻结并在 run
manifest 中记录二者 hash。实际消费端的强制 evidence 参数属于下一次“小批 Gold 准入/物化”任务，不能被
本任务假装已经完成。

## 4. 验收

单元测试用合成 family ID 验证成功导出、输出确定性、仓库内路径拒绝、未知/原文字段拒绝和 workspace 漂移
拒绝。没有使用、读取或导出真实 protected holdout，也没有调用 materializer、renderer、数据库、tokenizer
或 GPU。
