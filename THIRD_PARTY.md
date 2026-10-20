# 代码、数据与模型资产说明

核对日期：2026-10-20。本清单区分仓库代码、演示数据和仓库外研究资产，不重新授权第三方
数据，不表示已完成所有传递依赖的法律审核。Python/前端依赖分别由 `pyproject.toml` 和
`frontends/webcomponent/package.json` 声明；分发安装包时还应保留各依赖自己的许可声明。

## 仓库与演示

| 资产 | 来源 / 许可依据 | 本仓库处理 |
| --- | --- | --- |
| Vanna Agent、工具、服务与 Web Component 底座 | [Vanna](https://github.com/vanna-ai/vanna)，仓库 [MIT LICENSE](LICENSE) | 保留 `Copyright (c) 2024 Vanna.AI`；明确本项目是在其上扩展的 fork |
| 本项目运行时、配置模板与文档 | 本仓库 MIT LICENSE | 可按仓库许可使用；不把第三方数据一并视为 MIT |
| 合成电商演示 | `infra/postgres/synthetic.sql` 自构造样本，元数据标记 CC0-1.0；生成脚本按仓库 MIT | 随代码提供，标为 `synthetic-olist-v1`；不含外部真实交易快照 |
| Python / npm 依赖与 Chromium | 包自己的许可及分发说明 | 通过包管理器安装，不宣称仓库 MIT 覆盖这些依赖 |

## 仓库外数据与模型

| 资产 | 已核实的来源 / 状态 | 分发边界 |
| --- | --- | --- |
| Olist 真实快照 | [发布者 Kaggle 页面](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，公开 API 本轮返回 CC BY-NC-SA 4.0、version 2；已有逐文件记录见 [manifest](data/manifest/datasets.yaml) | 不随 Git、wheel、演示分发；不能按 MIT 代码许可理解真实数据的商业授权 |
| TheLook 当前快照 | [所用 Kaggle 镜像](https://www.kaggle.com/datasets/mustafakeser4/looker-ecommerce-bigquery-dataset)，公开 API 本轮仍为 Unknown、version 1 | 不分发 CSV、dump 或派生数据包；不冒充官方 BigQuery 导出。企业使用应接自己的授权数据 |
| Spider | [官方任务页](https://yale-lily.github.io/spider) 声明数据为 CC BY-SA 4.0 | 原始数据库与发布包在仓库外；test-suite 代码许可不自动适用于另行下载的数据库资产 |
| CSpider | [原作者仓库](https://github.com/taolusi/chisp)；中文问题及所用数据库包的授权需按具体 release 确认 | 本轮未确认一项覆盖全部资产的明确许可，不分发原始包 |
| Qwen2.5-Coder-1.5B | [官方模型卡](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B) 标为 Apache-2.0 | 不随产品下载或 wheel 分发权重；Adapter 及训练数据的发布资格不能仅由基座许可推导 |
| 其他历史模型、Adapter、预测与日志 | 后训练目录保留配置及脱敏汇总，具体权重许可按所用版本核对 | 本仓库未发布这些资产；不以研究结果代替数据/模型发布授权 |

在线模型是运营者自行配置的 API 服务，需要自己的账号与额度；代码 MIT 许可不包含提供方
服务的访问权或数据处理授权。当前无自动 PyPI、模型、Docker 镜像或数据包发布流程。
