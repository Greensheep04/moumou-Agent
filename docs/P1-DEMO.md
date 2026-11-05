# P1 演示与团队验收脚本

## 环境

在项目目录使用专用 Python 3.11 环境。环境由 environment.yml 和 requirements-win-py311.lock 描述。演示产生独立 runs 子目录，保留原有记录。

## 约 4 分钟的无密钥演示

| 时段 | 展示 | 核对点 |
| --- | --- | --- |
| 0:00–0:40 | P1 负责 sandbox、workspace、tools；展示 default_tools 和 Engine(workspace=...) | 工具遵守共享接口，审批由 Gate 执行 |
| 0:40–1:40 | 运行 python -m moumou.demo.p1 | 3 done、0 dead，脚本输出 hello, moumou |
| 1:40–2:30 | 查看输出中的工作区路径和 Git graph | Agent worktree 独立，只有声明产物交付到父任务 |
| 2:30–3:10 | 查看 conflicts 和 conflict_rolled_back | same.txt 冲突返回路径，目标原内容保留 |
| 3:10–4:00 | 展示正式评测接入与审批证据 | git 后端、setup 基线、重复运行隔离；拒绝时远端无新分支 |

```powershell
python -m moumou.demo.p1
python -m pytest tests/test_eval_workspace.py tests/test_p1_live_validation.py -q
```

第一条命令会打印 runs/p1-demo/<run-id>/ 下的 p1-summary.json、trace 路径、父工作区路径以及交付 Git 图。第二条命令覆盖正式 CLI 接线、带初始代码的修复、并行、依赖、冲突、独立 repeat 和 Gate 批准/拒绝。两者都使用脚本化模型或模拟服务，不是实际模型完成率。

## 团队真实联调入口

```powershell
python -m moumou.eval.cli --workspace-backend git --only s2_bugfix --repeats 3 --out runs/eval/p1-git-team-001
python -m moumou.demo.p1_live --check search
```

这两条命令需要团队配置有效模型/搜索密钥，本轮没有执行。每批更换输出目录，记录提交、模型、后端、运行编号，并保留失败。推送入口及人工审批边界见 P1-LIVE-VALIDATION.md。

## 取证清单

- 开发历史：git log --graph --oneline origin/main..feat/p1-sandbox；提交作者与真实开发过程保持一致。
- PR：查看变更文件、讨论和最终审查结论；P0/P3 的接入约定在 PR 中审阅。
- CI：保存最终提交对应的 Windows/Ubuntu 两个 Job 结果，而非只截图流程配置文件。
- 工作区：展示实际 demo 输出中的 worktree 路径及 delivery graph，不将共享 Git 元数据当作安全隔离。
- 审批：使用 summary 中的 approved/rejected branch 与 trace 核对；scripted 决策标为脚本化，真人界面另与 P2 验证。
- 评测：区分无密钥接线验证与 P3 正式真实模型报告。runs 中的原始证据保留本地，需要共享时只选精简、无敏感信息的材料。

截图应直接来自最终实际运行与团队审查页面，本文件提供取证位置；不使用重绘日志冒充原始截图。最终答辩截图与真人交互由团队在确定版本后补充。

## 可用于报告的 P1 技术说明

P1 将文件读写、代码执行、Git 操作和搜索统一为 Tool 实现，通过 ToolResult 返回成功状态、受限输出和相对产物路径。文件工具使用 LocalJail 校验路径；批准的 shell/Python 属于可信执行，应用层检查不能替代操作系统或容器隔离。Windows 进程先暂停创建，加入 Job 后恢复；POSIX 使用独立进程组，支持超时、取消和后台后代清理。

每个项目/会话使用独立管理仓库，每个 Agent 分配独立 worktree。产物整合按文件维护输入与交付版本，在临时 index 上执行三方补丁，避免未声明文件进入父工作区；冲突返回路径，保留目标。正式评测可通过 --workspace-backend git 使用此实现，并把 fixture.setup 作为各 Agent 共同的已提交 seed。

审批集中在现有 Gate；推送与普通 Git 操作分开，只有批准路径能够执行推送。搜索返回 title/url/snippet 并限制响应与输出规模。同步 Git 延迟、同一 Provider 实例内的锁，以及应用层隔离边界均需在报告中如实说明。
