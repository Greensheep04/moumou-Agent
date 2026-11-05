# P1 子任务机器验收与 CI 稳定性验证

日期：2026-11-05。基线：4e680a038d45ee88d4f09e1be9d8693d59f18f49，分支 feat/p1-sandbox。本轮仅本地修改，未提交、推送或合并；未新增真实 API 请求。

## 本次执行总结

### CI 失败核对与修复

截图对应首次 push workflow 的 Ubuntu 失败。已定位至测试辅助函数 alive：Linux 进程在检查 /proc/<pid>/stat 存在后退出，随后读取触发 FileNotFoundError。此记录表明测试探针存在竞态，不能据此判定产品进程清理失败。

同一提交后续重跑的 [push workflow](https://github.com/Greensheep04/moumou-Agent/actions/runs/42014292938) 及 [PR workflow](https://github.com/Greensheep04/moumou-Agent/actions/runs/42014301001) 已重新核对，Windows/Ubuntu 四个 job 均 success。初次失败记录保留，重跑通过不能替代修复竞态。

本地修复只让探针在读取期间进程文件消失时返回已退出；僵尸判断、无 procfs 的 POSIX 回退、Windows 原路径不变。新增六项确定性探针回归，在 Windows 上也能复现 Linux 竞态，权限错误仍抛出。

**以上远程绿色结果属于基线提交，不覆盖本轮未推送代码。** 下一次推送后必须检查新 SHA 的实际 CI。

### 严格子任务机器验收

新增可选配置，使用编排层的 Settings：

```python
from moumou.orchestrator import Settings

settings = Settings(require_subtask_acceptance=True)
```

默认值 False，保留旧计划允许空 acceptance 并记录 self_report 的行为。开启后，任一子任务 acceptance 为空均退回计划，错误点名对应子任务；验证发生在创建任何角色、子任务及修改父依赖之前。TaskRunner 沿用 invalid_plan 工具反馈，让主 Agent 在原步骤预算内修正，不增加自动重试或请求上限。

Plan 字段说明与 lead 提示明确要求代码/文件任务填写机器验收，优先执行或内容检查，并说明检查在子任务工作区中进行、依赖产物已交付。没有按目标文字猜测检查，也没有自动添加宽松的检查来伪造通过。plan_applied 事件补充 require_subtask_acceptance 标记，便于确认运行策略。

严格模式只保证非空且符合既有 Acceptance 结构，不保证检查足以覆盖需求。file_exists 不能证明代码正确；正式评测仍应独立预设最终验收。单层开关适用于该 Engine 的所有委派层级，不强制根任务验收，也不改变无需委派的任务。

### 离线验证与覆盖边界

| 验证 | 结果 |
| --- | --- |
| 竞态修复前 | 1 failed、5 passed；失败准确为 stat 读取时文件消失 |
| 竞态修复后 | 6 passed |
| 严格验收实现前 | 4 failed、17 passed，涉及新配置/校验/Schema 描述 |
| 相关专项 | 48 passed in 16.76s |
| 最终全量 | 293 passed in 147.32s |
| Ruff、git diff --check | 通过 |
| P1 demo | 通过，纯离线运行 |
| 密钥、运行文件与无关标识检查 | 通过，暂存区为空 |
| 独立只读代码审查 | 未发现阻塞项 |

覆盖：严格模式逐项拒绝及无写入副作用、默认兼容、非法计划反馈纠正、持续非法计划耗尽原预算、子任务检查失败不能 DONE、严格及默认模式下父任务独立验收、实际 Git 工作区交付与收尾提示。

上一轮真实运行 8279938ebdc3 的原始计划已离线回放：3 个子任务均为空验收，默认接受、严格拒绝，新增 API 请求为 0。本回放只验证计划准入，不是新的真实多 Agent E2E。严格模式下真实模型是否能首次生成或反馈后修正完整检查、条件式收尾能否真实启用，尚待下一次实调。

本机为 Windows；本轮未提交，所以尚无新代码的 Ubuntu Actions 结果。确定性探针回归覆盖该 Linux 文件消失条件，仍需推送后双平台验证。

## 整体进度与框架符合性

P1 工具、Git 独立工作区、并行/依赖、交付和父任务最终验收已有单次真实 smoke 证据。本轮补强了子任务验收的可控准入；既有父子最终 AcceptanceChecker、审批 Gate、Scheduler、WorkspaceProvider、P2/P3 ports 和评测指标口径未改。

Settings 增加一个默认关闭配置；TaskNode、Acceptance、Plan 的数据字段/类型不变，仅 Plan 字段描述更新。没有数据库迁移，没有修改默认评测 runner/CLI 行为，严格模式需在 Engine 设置中显式开启。

Settings/Delegation/提示词仍涉及 P0 职责，需要 P0 审阅。本轮不构成 P2 真人沟通/审批、P3 正式题库/重复统计/记忆演化验收；真实 Tavily 仍待配置。当前未合并 main，对团队未公开提交的改动无法判断冲突。

## 提交建议与后续 CI 规则

负责人检查后，建议分两批：

1. 测试稳定性：test_process_runner.py 的竞态修复与 test_process_probe.py。
2. 子任务验收：配置、准入、Schema/提示、对应回归与脱敏文档；由 P0 审阅。

本轮没有执行这两批提交。下一轮经负责人确认推送后，对每个推送 SHA 核对 push 和 PR workflow 的状态，等待 Windows/Ubuntu 全部完成；失败时读取失败 job 日志、定位原因并报告。仅本地 commit 尚未触发 Actions 时，不以旧 run 代替。不要只看通知截图或分支历史上的绿色结果，也不要靠反复重跑掩盖未修复问题。

CI 通过后再安排一次开启严格模式的真实 HKGAI E2E，保留请求/token/时间上限及失败记录；通过后由 P3 进行至少三次重复和正式题库验证。main 保持不合并，直至团队完成审阅。

本地证据保留在被忽略的 runs/acceptance-hardening/：probe-red.txt、probe-green.txt、acceptance-red.txt、targeted.txt、full-suite.txt、replay-summary.json。密钥、原始 runs、数据库、工作区与无关标识均不进入提交。

## 两批提交状态补充

上文“本轮未提交/未推送”描述离线开发轮次的状态。负责人随后已明确批准分两批提交推送，要求逐 SHA 核对 push/PR 双平台 CI，通过后再进行一次严格模式真实 E2E。第一批竞态修复为 e52295f；严格验收实现与本文作为第二批交付，最新状态以对应提交的 Actions 为准。真实 E2E 结果产生后单独提供脱敏总结，原始日志仍留在本地 runs，不并入这两批源码提交，main 不合并。

第二批提交前重新运行全量离线回归：293 passed in 140.98s，Ruff 通过；第一批相关专项 30 passed in 4.75s。后续真实测试将绑定最终提交 SHA，并额外校验子任务机器验收和条件式收尾提示，不能用空验收自报替代。
