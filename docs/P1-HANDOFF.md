# P1 给 P0 / P3 的接入与验收清单

## P0 接入

- 工具保持 default_tools() 入口，现有工具名与标记工具保留。
- 工作区使用 moumou.adapters.GitWorkspaces，通过 Engine(workspace=provider) 注入。
- contracts、Gate、TaskRunner 未修改；同步 WorkspaceProvider 延续现状。
- 本地工具执行请经过 Gate，尤其 git_push，不能直接调工具来代替审批。
- 建议确认：应用层隔离是否符合本次课程范围；同步 Git 延迟是否可接受；父工作区有修改时先提交的行为是否符合编排预期。

## P3 可直接采用的验收场景

| 输入 | 机器可判验收 |
| --- | --- |
| 写脚本并执行 | 文件存在、stdout 精确匹配、exit_code=0 |
| 两 Agent 写不同文件 | 父工作区同时存在两文件、内容匹配 |
| B 依赖 A 的脚本 | B 工作区可执行脚本、依赖交付事件存在 |
| 两源修改同一位置 | integrate.ok=false、conflicts 包含路径、目标原内容保留 |
| Python 无限循环 | 指定时间内失败、PID 及后代不存在 |
| 推送审批拒绝 | error=rejected_by_human、远端分支未创建 |
| 搜索服务返回 429 | ok=false、可读错误、框架仍可接受下一任务 |

场景实现见 tests/test_p1_integration.py、tests/test_git_workspace.py、tests/test_process_runner.py、tests/test_git_push.py。

## 正式提交前的外部验证

- [ ] 团队确认隔离与同步接口边界。
- [ ] 使用约定的 GitHub 分支验证远端认证与推送。
- [ ] 配置有效 TAVILY_API_KEY，执行一次真实联网检索。
- [ ] 查看 Windows/Linux CI 结果，修复平台差异。
- [ ] 由团队使用真实 DeepSeek/HKGAI 和正式测试集运行评测。
- [ ] 补最终提交历史截图，并核对每位成员的真实贡献。

P1 本地测试不代替真实模型任务完成率或正式课程评测。

## 2026-11-05 远程验证补充

feat/p1-sandbox 已推送，提交 2ac5a2b；[Windows/Ubuntu CI](https://github.com/Greensheep04/moumou-Agent/actions/runs/41978444389) 的依赖安装、pytest、ruff 和 P1 demo 均通过。此记录更新上文当时尚未推送/运行 CI 的状态。开发者直接推送不等于 git_push 工具经 Gate 的真实远端验收。

本轮按约定先完成无密钥开发；Tavily 真实调用和真实模型联调由团队后续完成。P0 接入边界、P3 评测入口改动和 P2 人工审批仍需要对应负责人审阅，不视为已确认。
