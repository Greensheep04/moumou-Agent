# P1 评测接入验证记录

日期：2026-11-05。增量起点：2ac5a2b。分支：feat/p1-sandbox。

## 已执行

| 验证 | 实际结果 | 本地证据 |
| --- | --- | --- |
| 正式评测接入实现前 | 19 failed / 1 passed，缺少新后端接线 | runs/p1-integration/eval-red.txt |
| 评测后端、CLI、setup 与隔离专项 | 29 passed | runs/p1-integration/eval-green.txt |
| 阶段 2 全量回归 | 216 passed in 120.26s | runs/p1-integration/stage2-suite.txt |
| 搜索/推送包装器及既有协议测试 | 17 passed | runs/p1-integration/live-green.txt |
| 连接超时新增回归 | 先失败，捕获 TimeoutExpired 后通过 | runs/p1-integration/timeout-red.txt |
| 无密钥正式评测 API | 两个独立 repeat，均 3 done / 0 dead | runs/p1-integration/offline-eval/1d8a28bc9a11/summary.json |
| 备用演示 | 3 done / 0 dead，hello, moumou；same.txt 冲突，原目标保留 | runs/p1-demo/f49a65053197/p1-summary.json |
| 代码检查 | All checks passed | python -m ruff check src tests |

正式评测 API 证据包含源提交、FakeLLM 标识、git 后端和每次 run_dir，另保存 report.md 与 delivery-graph.txt。它只证明接线与产物行为，不作为实际模型完成率。

## 外部状态

原有版本的 Windows/Ubuntu CI 已通过，见 https://github.com/Greensheep04/moumou-Agent/actions/runs/41978444389 。本轮最终提交的远程 CI 在推送后补充。

真实搜索和真实模型调用按约定暂缓。P0/P3 跨成员确认、P2 人工审批以及最终答辩截图仍由团队完成。脚本化 Gate 验证不能代替真人交互验收。

## 接入决策

- 复用专用 checkout 和 P1 分支，保留 Engine 默认 local，避免改变队友原有行为。
- 评测明确选择 git 时，setup 形成每次运行独立的已提交 seed；拒绝复用已有 repeat 目录，重新评测必须提供新输出路径。
- 对两个后端都校验 setup 路径与 fixture ID，避免评测初始化绕过工作区约束。
- 只增加 P3 runner/CLI 的最小 Provider 接线，保留原有指标定义、共享契约及审批策略；最终由 P3 审阅。

## 独立审查与一次修复

审查发现 1 项严重路径隔离问题和 5 项重要问题，均有明确失败回归后修复：

1. Windows 尾随点/空格可把 .git. 解释为 .git，影响初始化仓库配置：LocalJail 在解析和写入前拒绝此类组件，覆盖新建和已有管理目录。
2. 推送可能遵循配置携带 annotated tags：git_push 显式设置 push.followTags=false。
3. URL 密码、查询参数或片段可能保留在克隆配置：建立克隆前拒绝这些凭据形式。
4. setup 同名别名和文件/父目录冲突：写入前统一检查，不保留部分初始化文件。
5. 同一批输出目录下换题运行可能覆盖已有 report：CLI 构造模型前拒绝非空目录，run_all 使用独占 .batch 标记预留新批次。
6. 推送后的远端校验超时可能丢失分支信息：summary 保存阶段、预期 SHA、分支、trace 和每个 ref 是否已验证；未知状态明确标记，不当作不存在。

修复专项 72 passed in 53.97s。回归失败证据：review-path-red.txt、review-remaining-red.txt、review-evidence-red.txt；通过记录：review-green.txt，均位于 runs/p1-integration。

审查另记录一项低优先级限制：本地测试远端应使用绝对路径；相对远端路径目前按验证克隆目录解释。本轮未修改这一行为。

## 同步接口性能交接

独立审查在 Windows 的两个单文件临时场景中测得：初始化并分配两个工作区约 0.86/0.98 秒，单次整合约 1.53/1.58 秒。这是小样本交接数据，不是跨平台基准；同步操作会占用事件循环，需 P0 根据实际任务规模确认可接受性。本轮不变更共享同步接口。

审查未判断的真实 Tavily/模型行为、实际成本和任务完成率按约定留给团队；真人交互、最终 CI/推送和团队签收需各自证据。一般 OS 级隔离、恶意并发文件系统操作、多进程锁与大仓库优化不属于本轮保证。

## 修复后最终本地验证

python -m pytest -q：254 passed in 120.52s。python -m ruff check src tests：All checks passed。全分支 git diff --check 通过。共享 contracts 与 orchestrator 相对本轮起点没有变化。全量日志：runs/p1-integration/final-suite.txt。

## GitHub 工具推送验证

最终代码 f9032008934b622d29ea01c85b3cfd66953b021b 经独立克隆与 Gate 验证通过：批准远端分支 p1-verify/approved-eea29443d2c6 指向源 HEAD，拒绝分支 p1-verify/rejected-eea29443d2c6 未创建。审批为 scripted；不是 P2 真人界面验收。原始 JSON/trace 位于 runs/p1-integration/github-live/bb7307fe5e07/。
