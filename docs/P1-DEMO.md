# P1 演示与团队验收脚本

> 本页保留 P1 演示参考。当前环境与正式命令以[环境说明](SETUP.md)和[评测说明](EVAL.md)为准。
> 历史观察不代表当前候选版本已验收。

## 环境

环境规格中保留 Python 3.11 配置；原生 Windows P3 开发使用已激活的 Conda nlp/Python 3.12，
具体安装见[环境说明](SETUP.md)。演示产生独立 runs 子目录，保留原有记录。

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

第一条命令会打印 runs/p1-demo/<run-id>/ 下的 p1-summary.json、trace 路径、父工作区路径以及交付 Git 图。
第二条检查底层工作区接线、带初始代码的修复、并行、依赖、冲突、独立运行及 Gate 批准/拒绝。
两者使用脚本化模型或模拟服务，不是老师题库成绩。

## 团队真实联调入口

```powershell
python -m moumou --scenario scenario_1 --out runs/test/NEW_P1_TEAM_SESSION
python -m moumou.demo.p1_live --check search
```

第一条使用当前连续老师团队协议和真实人类交互。第二条是独立联网检查，会消耗 provider 额度，
不替代正式场景，也不进入场景的搜索账本。配置对应课程/搜索密钥，保留独立输出目录。
推送与审批边界见[外部服务检查](P1-LIVE-VALIDATION.md)。

## 取证清单

- 开发历史：`git log --graph --decorate --oneline --all`；捕获真实最终历史及各成员贡献，不依赖已过期的分支名称。
- PR：查看变更文件、讨论和最终审查结论；P0/P3 的接入约定在 PR 中审阅。
- 验证：保存实际本地命令输出、commit/源码身份和平台。拟提交目录不含 GitHub Actions 工作流，旧托管运行仅为历史证据。
- 工作区：展示实际 demo 输出中的 worktree 路径及 delivery graph，不将共享 Git 元数据当作安全隔离。
- 审批：使用 summary 中的 approved/rejected branch 与 trace 核对；scripted 决策标为脚本化，真人界面另与 P2 验证。
- 评测：区分无密钥接线验证与 P3 正式真实模型报告。runs 中的原始证据保留本地，需要共享时只选精简、无敏感信息的材料。

截图应直接来自最终实际运行与团队审查页面，本文件提供取证位置；不使用重绘日志冒充原始截图。最终答辩截图与真人交互由团队在确定版本后补充。

## 可用于报告的 P1 技术说明

P1 将文件读写、代码执行、Git 操作和搜索统一为 Tool 实现，通过 ToolResult 返回成功状态、受限输出和相对产物路径。文件工具使用 LocalJail 校验路径；批准的 shell/Python 属于可信执行，应用层检查不能替代操作系统或容器隔离。Windows 进程先暂停创建，加入 Job 后恢复；POSIX 使用独立进程组，支持超时、取消和后台后代清理。

GitWorkspaces 为各 project/conversation 提供管理仓库、为各 Agent 提供私有 worktree。
文件交付使用临时 index 和三方整合，冲突时保留目标。P1 演示和底层集成通过
`Engine(workspace=provider)` 注入；老师正式 CLI 不再暴露旧
`--workspace-backend` / `--only` / `--repeats` fixture 参数。
当前持续团队目录与协议见[评测说明](EVAL.md)。

审批集中在现有 Gate；推送与普通 Git 操作分开，只有批准路径能够执行推送。搜索返回 title/url/snippet 并限制响应与输出规模。同步 Git 延迟、同一 Provider 实例内的锁，以及应用层隔离边界均需在报告中如实说明。
