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
