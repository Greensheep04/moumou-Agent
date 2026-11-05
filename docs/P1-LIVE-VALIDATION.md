# P1 外部服务验收

## 本轮范围

2026-11-05：按约定完成无密钥开发。没有调用真实搜索或真实模型；这两项由团队配置并验收。搜索 HTTP 协议使用 MockTransport，推送包装器使用真实本地 bare 远端验证。它们不能替代对应的真实服务验收。

## 搜索

在本地 .env 配置 TAVILY_API_KEY，或通过环境变量提供。在项目专用环境执行：

```powershell
python -m moumou.demo.p1_live --check search --query "multi-agent software engineering"
```

入口会加载 .env，通过 Gate 调用现有 web_search；只保存成功与否、结果数量、字段校验和 trace 路径，不把密钥或完整检索内容写入摘要。缺少密钥时返回失败，不发起请求。每次输出到 runs/p1-live/<run-id>/summary.json。

## 推送

必须显式给出测试远端和 --execute-push；默认命令不会推送：

```powershell
python -m moumou.demo.p1_live --check push --execute-push --source . --remote-url https://github.com/Greensheep04/moumou-Agent.git
```

入口克隆可信本地仓库的已提交 HEAD，将克隆的 origin 显式设为给定测试远端。它只创建两个随机命名的 p1-verify/ 验证分支，不修改源仓库分支，不改写 main，不创建额外测试提交。批准分支包含源仓库原有真实历史；拒绝分支仅存在于本地。

通过 Gate 分别执行 scripted approve/reject 后，使用 ls-remote 核对：批准分支等于源 HEAD，拒绝分支不存在。JSON 会标记 approval_mode=scripted；这证明审批逻辑与远端写入关系，不能宣称已经完成 P2 的真人交互演示。测试后保留远端批准分支供核对，不自动删除。

认证沿用系统 Git 凭证。不要在 URL 中放密码或 Token。Git 连接超时返回结构化失败。

## 验收记录

| 检查 | 状态 | 证据 |
| --- | --- | --- |
| 搜索缺少密钥 | 返回配置错误，不调用网络 | tests/test_p1_live_validation.py |
| .env 加载、结构化结果、摘要无密钥 | 模拟 HTTP 验证通过 | 同上；生产协议测试见 test_web_search.py |
| Gate 批准/拒绝与本地真实远端 | 通过，main 与源分支保持原状 | test_push_wrapper_checks_real_refs_and_gate_decisions |
| 超时 | 返回失败 JSON | test_cli_reports_git_timeout_without_traceback |
| Tavily 真实服务 | 暂缓，需团队密钥 | 未计为通过 |
| 工具经 Gate 的 GitHub 真实推送 | 待本轮最终验证 | 后续在本节追加实际提交与分支 |
| 真人审批界面 | 待 P2 联调 | 脚本化测试不计为真人验收 |
