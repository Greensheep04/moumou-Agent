# 计划约束与错误反馈修复：离线回归及真实试跑

日期：2026-11-05（Asia/Shanghai）。基础 HEAD：a970971dfbdfe5eda12577b9338cd7f1219a1d78。
所有代码与文档改动均为本地未提交状态。真实运行编号：7e91f44a0d7c。

## 本次执行总结

修复与离线验证通过。随后按授权只进行了一个真实 HKGAI 测试运行：模型修正一次非法计划，三个子 Agent 均完成，所有产物已交付；主 Agent 最终提交结果前触发本次 20 次模型请求上限，因此严格端到端结果仍记录为未通过，没有追加第 21 次请求或另一次真实运行。

### 已完成的修复

| 文件 | 变化 | 保持的约定 |
| --- | --- | --- |
| src/moumou/contracts/task.py | 公开 Schema 用 anyOf 明确 command、file_contains、json_key 的 expect 必填且非 null；补充字段用途说明 | 数据字段和运行时校验保持兼容，不自动填补遗漏的 expect |
| src/moumou/contracts/plan.py | subtasks 字段说明明确遵守用户任务数/依赖结构，父任务自行恢复并收尾 | 未全局硬编码三个任务，未改变既有任务上限 |
| src/moumou/orchestrator/worker.py | 捕获预期 DelegationError，记录 plan_rejected，并通过原 tool_call_id 回传 invalid_plan 给模型 | 留在同一上下文与预算内；意外基础设施异常仍走原失败路径 |
| tests/test_plan_recovery.py | 增加条件字段、非法计划纠正、无部分派发、步数耗尽回归 | 不调用真实 API，不放宽验收断言 |

非法计划在创建角色和子任务前被拒绝。纠错沿用本次任务尝试的步数、token 和时间检查；既有 scheduler 的失败重试配置保留，本次真实场景 max_retries=0。

### 离线验证

- RED：实现前 5 failed / 3 passed，复现 Schema 与错误反馈缺口。
- GREEN：计划恢复、委派、契约、编排专项 44 passed。
- 全量：262 passed in 129.62s；Ruff 与差异格式检查通过。
- 一次只读独立审查：未发现 Critical、Important 或 Minor 问题。新增回归直接覆盖步数耗尽；token/墙钟检查保留既有实现，未声称新增专项覆盖。
- 离线日志：runs/plan-recovery/red.txt、green.txt、full-suite.txt。

### 真实模型单次运行

| 项目 | 实际结果 |
| --- | --- |
| 模型 | t2_hkgai-v3_fp8_1m_e7 |
| 框架耗时 | 56.75 秒 |
| 实际模型请求 | 20 次 |
| 服务报告 token | 输入 47040，输出 4012，合计 51052 |
| 任务总数 | 4：一个主任务、三个不同角色的子任务 |
| 终态 | DONE=3、DEAD=1、unfinished=0 |
| 计划错误恢复 | 一次 plan_rejected 后成功 plan_applied，无 scheduler 重试 |
| 工作区与调度 | 四个独立 Git worktree；两个独立子任务的实际模型调用时间重叠 |
| 依赖与交付 | 验证 Agent 收到 greeting.py 和 README.md，三个产物均交付到父工作区 |
| 工具与整合错误 | 0；没有冲突或交付失败 |
| 严格端到端结论 | 未通过，主任务未提交最终结果 |

这次模型最初把 subtasks 数组提交成 JSON 字符串。框架返回具体校验错误后，模型在同一上下文中改为正确数组，成功创建 code、docs、verify 三个任务。这直接验证了错误反馈恢复路径。修正后的验收项均含所需 expect；本次没有产生额外 lead_finalize 子任务。

三个产物：greeting.py、README.md、verification.json。独立再次执行脚本输出 Hello, Student!，退出码 0；JSON 的 passed=true、stdout="Hello, Student!"。原定六项独立机器验收全部通过，未修改框架的原任务状态。

主 Agent 第 19 次模型调用执行脚本，第 20 次检查 JSON。随后一次本地调用尝试在发往服务前被本地请求计数器拦截，根任务记录 crash:RuntimeError:single-run request/token ceiling reached。tokens 为 51052，未达到 120000 上限；耗时未达到 300 秒上限，所以本次触发的是 20 请求限制。该限制属于测试控制，不是 HKGAI 配额/认证错误。无法据此声称额外一次请求一定能完成。

这是单个样本，3/4 是该次创建任务的状态比例，不代表整个项目进度或正式题库完成率。记忆与角色评审关闭，审批使用 ScriptedHuman，真人交互仍未验收。

## 整体任务进度分析

| 范围 | 当前状态 | 后续重点 |
| --- | --- | --- |
| 计划字段提示与校验错误反馈 | 本轮已修复；离线与真实纠错路径已验证 | 保留失败与纠正回归，供 P0 审阅 |
| P1 执行、独立工作区、并行、依赖交付 | 本轮真实三子 Agent 场景已执行并验证产物 | 扩展场景前先完成主任务正常收尾 |
| 完整端到端终态 | 仍未通过 | 优化父任务重复检查与及时 submit_result；若保持流程，再确认合理请求预算 |
| 正式评测与统计 | 未完成 | 单次链路正常终结后再按团队题库重复评测 |
| 真实搜索、真人审批、团队签收 | 仍待对应联调 | Tavily 配置、P2 HumanIO 与 P0/P3 审阅 |

建议下一步针对父任务收尾成本做小范围检查，明确何时复用已完成验收、何时提交最终结果，再决定下一次真实试跑的预算。保留本次严格失败结果，不通过手工改 DONE 或追加请求将它改记成功。

## 证据和审阅状态

- runs/hkgai-e2e/7e91f44a0d7c/summary.json：真实运行结果、工作区、事件与检查。
- runs/hkgai-e2e/7e91f44a0d7c/llm-calls.jsonl、trace.jsonl：真实调用与框架证据。
- runs/hkgai-e2e/7e91f44a0d7c/independent-acceptance.json：六项机器验收，未追加 API 调用。
- runs/hkgai-e2e/7e91f44a0d7c/source-diff.patch 与 summary 中的 source_fingerprints：本次未提交代码快照。
- 首次失败 df4391e0d292 的记录保留，未被覆盖。

本轮未执行源仓库 git add、commit 或 push，也未更新远程 PR/CI。源仓库 HEAD 保持不变；本轮 262 测试是本地证据，不能当作远程 CI 结果。密钥仅保留于被忽略的本地 .env，候选改动和运行文本均检查不含密钥。请项目负责人检查本地差异后再决定提交。

## 后续收尾优化结果

运行 8279938ebdc3 已正常完成主任务和三个子任务，父任务最终六项机器验收通过。该结果不改写本文对应旧运行记录。子任务自报验收的限制、最新整体进度和提交建议见 P1-LEAD-FINALIZATION-VALIDATION.md。所有本轮改动仍待本地审阅，未提交或推送。

## 本轮提交状态补充

本文对应试跑时的“未提交/未推送”是当时状态。现已按负责人确认将计划恢复与收尾源码分别形成本地提交 a03c8c9、64066b7；本文作为第三批脱敏文档交付。实际推送和最终 CI 以 [PR #1](https://github.com/Greensheep04/moumou-Agent/pull/1) 的 Checks 为准，main 保持不合并。密钥与原始 runs 仍仅保留本地。最新框架符合性及跨模块签收要求见 P1-FRAMEWORK-COMPATIBILITY.md。
