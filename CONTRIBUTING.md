# 参与开发与独立试用

先按 [部署说明](docs/deployment.md) 在自己的空数据库中启动合成数据演示；接业务库时使用
[填写模板](docs/templates/workspace/README.md)。不需要 GPU，不安装后训练依赖也能使用在线产品。
当前为 Vanna 的项目 fork，发行包仍叫 `vanna`；安装本仓库构建的 wheel，不用 PyPI 同名包代替。

## 开发路径

使用 Python 3.12、Node.js 22。现有环境已经安装依赖时可从构建开始：

```bash
python -m pip install -e '.[product,test,browser-test]'
cd frontends/webcomponent
npm install --package-lock=false
npm run build
cd ../..
python -m pytest -q -m 'not integration and not postgres' \
  tests/test_semantic_intent.py tests/test_analysis_state.py \
  tests/test_analysis_context.py tests/test_execution_memory.py \
  tests/test_sql_policy.py tests/test_result_validator.py tests/test_local_auth.py
```

完整 CI 文件清单与各层验证方法见 [测试说明](docs/testing.md)。服务入口是
`data-analysis-agent` / `python -m data_analysis_agent.server`，Web Component 由服务同源提供，
无需再起前端开发服务器。修改前端后重新构建；安装后的 wheel 需要重新构建安装才会更新。
已有业务库不要执行 `--synthetic`，元数据初始化单独使用管理员，HTTP 服务只保留受限凭据。

## 提交改动

先说明具体问题、用户可观察的行为和验证方法，再做必要修改。修复应包含能复现该问题的测试，
更新接口调用方和当前文档。正常追问应继续可用；不要用新增关键词闸门解决模型理解错误。
涉及 SQL、身份或结果隔离时，保留 AST、reader role、归属校验和审计。

提交前运行相关测试、前端构建（有前端变更时）和 `git diff --check`。PR 说明写清问题、结果、
测试及尚未覆盖的边界。使用 Conventional Commit，如 `fix: preserve filters on drilldown`。
PR 和 push 会触发质量检查，CI 不使用真实模型 key，也不训练模型。

不要提交 `.env`、私钥、口令、真实用户日志、原始数据、数据库 dump、模型或 Adapter。
仓库内只保留代码、配置、许可来源、小型合成 fixture 和脱敏汇总。数据/模型与代码许可分别
处理，见 [第三方资产说明](THIRD_PARTY.md)；上游 Vanna 的 MIT 版权声明必须保留。

## 独立部署反馈

目前尚无外部开发者独立部署记录。若愿意试用，可在自己的环境按部署文档启动后，提交只包含
以下信息的 Issue：系统及 Python/Node/PostgreSQL 版本、使用 Compose 还是 wheel、文档哪一步
失败、脱敏错误、能否登录并完成一次合成查询、重启后会话和结果是否恢复。

模型相关问题请另列原问题（使用合成数据）、预期指标口径、实际行为和是否连续追问；不要把
“能执行 SQL”当作“答案正确”。安全漏洞按 [安全说明](SECURITY.md) 处理，不公开利用细节。
正式 tag、GitHub Release、PyPI 或容器仓库发布需要另行决定，本仓库不自动发布上游包名。
