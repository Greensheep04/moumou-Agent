# P1 本地验收记录

日期：2026-11-04。工作分支：feat/p1-sandbox。基线：e3b9e7a。

## 环境

- Windows；Python 3.11.18；Git 2.55.0.windows.3。
- 专用 Conda 环境：D:\Dev\conda-envs\moumou-agent。
- 代码：D:\Projects\moumou-Agent。
- 核心和开发依赖版本见 requirements-win-py311.lock。

## 最终验证

| 命令 | 实际结果 |
| --- | --- |
| python -m pytest -q | 193 passed in 72.75s |
| python -m ruff check src tests | All checks passed |
| python -m moumou.demo.p1 | done=3、dead=0、脚本输出 hello, moumou、冲突列表 same.txt、原目标保留 |

运行日志位于 runs/p1-verification/final-suite.txt、final-lint.txt、final-demo.txt。runs 为本地证据目录，未纳入 Git。最终演示明细为 runs/p1-demo/42167ecabb2a/p1-summary.json。

最初基线为 138 passed / 4 failed。以下原有 Windows 失败已在最终回归中解决：

- test_acceptance_kinds.py::test_pytest_kind
- test_orchestration.py::test_dependent_task_receives_upstream_artifacts
- test_tool_contract.py::test_shell_timeout_actually_kills_the_process
- test_tool_contract.py::test_cancelling_a_tool_kills_its_child

现有契约字段、审批策略和编排器保持不变。工具契约测试仅修改跨平台命令构造和 PID 存活检查，没有删减断言。

## 独立审查

一次只读全分支审查发现 4 项 Important 问题，均已增加失败回归并修复：

1. 依赖文件的版本来源：B 基于 A 的产物修改/删除再交付，误冲突或漏删除。
2. Git 环境变量：GIT_DIR/GIT_WORK_TREE 等能把操作重定向到外部仓库。
3. 后台子进程继承管道：父进程成功退出后仍等待 EOF，导致误超时。
4. Windows 加入 Job 前已开始运行：快速后代可能逃逸清理范围。

修复后专项测试 30 passed；再运行全量得到上述 193 passed。另补 Git merge 修改文件的 artifacts 回归，先失败后通过。没有遗留 Minor 审查项。没有发起第二轮审查。

审查未验证 Linux/云端 CI、真实 GitHub/Tavily；恶意代码 OS 隔离不在 LocalJail 基本版保证内。文档在审查后由实现者核对。

## 尚未完成的外部验收

- TAVILY_API_KEY 当前未配置，真实搜索调用未运行；HTTP 协议与错误路径使用 MockTransport 验证。
- Git 推送成功/拒绝审批使用真实本地 bare 仓库验证；项目分支尚未推送 GitHub。
- Windows/Linux CI 文件已添加，尚未在 GitHub Actions 执行，Linux 不计为已验证。
- P0 对同步接口/隔离边界的确认、P3 正式题库、真实 DeepSeek/HKGAI 联调由团队继续。
- 最终报告需要的提交历史截图待最终团队分支确定后补充。

这些事项不应被本地测试通过替代。

## 2026-11-05 远程验证补充

feat/p1-sandbox 已推送，提交 2ac5a2b；[Windows/Ubuntu CI](https://github.com/Greensheep04/moumou-Agent/actions/runs/41978444389) 的依赖安装、pytest、ruff 和 P1 demo 均通过。此记录更新上文当时尚未推送/运行 CI 的状态。开发者直接推送不等于 git_push 工具经 Gate 的真实远端验收。

本轮按约定先完成无密钥开发；Tavily 真实调用和真实模型联调由团队后续完成。P0 接入边界、P3 评测入口改动和 P2 人工审批仍需要对应负责人审阅，不视为已确认。
