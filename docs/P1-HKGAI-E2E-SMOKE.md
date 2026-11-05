# HKGAI 真实多 Agent 端到端单次测试

日期：2026-11-05（Asia/Shanghai）。代码版本：a970971dfbdfe5eda12577b9338cd7f1219a1d78。
运行编号：df4391e0d292。执行次数：1；未追加真实模型重跑。

## 本次执行总结

结果：未通过。主 Agent 已调用真实 HKGAI 并提出分工计划，但计划在应用前被校验拒绝，因此子 Agent、依赖交付和最终验收没有启动，不能计为真实多 Agent 链路已跑通。

| 项目 | 实际结果 |
| --- | --- |
| 模型 | t2_hkgai-v3_fp8_1m_e7 |
| 模型请求 | 2 次；先 list_files，再 submit_plan |
| 框架耗时 | 13.25 秒 |
| 服务报告 token | 输入 6150，输出 1199，合计 7349 |
| 已创建任务 | 1 个主任务 |
| 任务状态 | DONE=0、DEAD=1、unfinished=0 |
| 重试 | 0；未追加试跑 |
| 子 Agent / 产物 | 未启动；没有生成要求的三个文件 |
| 来源与作用域 | 真实 API；独立 Git 工作区；脚本化审批；关闭记忆和角色评审 |

这是一次 smoke 的失败记录，不能由一个样本推断整个系统的稳定完成率，也没有计算实际费用或剩余额度。

## 测试任务与运行控制

目标：主 Agent 使用真实模型生成计划，安排脚本与 README 两个独立子任务，再安排一个依赖前两项的验证子任务；最终在父工作区验收 greeting.py、README.md、verification.json，并实际执行脚本。

限制：单次运行，最多 20 次模型请求、120000 累计 token、300 秒；每任务 max_retries=0。仅注册文件工具、run_python 和必要标记工具，未提供 Git 推送、shell、联网或安装工具。密钥只由模型客户端使用，不出现在提示词中，并从测试进程向工具继承的环境中移除。

上述设置是本次小规模 P1 接入测试的运行配置，不是正式评测全集或记忆消融结果。

## 已确认的原因

主 Agent 的提交包含：

```json
{"kind":"command","target":"python3 greeting.py Student","timeout_s":10}
```

Acceptance 的运行时校验要求 command 带 expect，例如：

```json
{"kind":"command","target":"python3 greeting.py Student","expect":"0","timeout_s":10}
```

原始错误位置：subtasks.0.acceptance.1。原始错误：acceptance kind 'command' requires `expect`。

本地检查发现，submit_plan 向模型公开的 acceptance Schema 仅把 kind、target 列为 required，而 expect 默认 null；运行时另通过校验器施加条件要求。这使模型看到的参数约束与运行时要求存在提示缺口。TaskRunner 对 DelegationError 走失败路径，本次禁用重试后主任务直接进入 DEAD，没有将具体计划错误反馈为可修正的工具结果。

此外，模型提出了 code、docs、verify、lead_finalize 四个子任务，偏离本次明确要求的三个子任务。即使先修复 expect，仍需要检查这项结构偏差。

## 本地诊断，不是第二次执行

对保存的计划进行纯本地重放：原计划校验复现失败；仅在诊断副本中补上 expect="0" 后，Plan 模型校验通过，仍保留四个子任务的偏差。没有调用模型、没有应用该副本、没有执行子任务，也没有修改产品代码。原始 smoke 结果仍为失败。

诊断结果表明，当前阻塞点在计划生成约束与 P0 校验/纠错衔接，尚无证据判断 P1 真实模型下的并行产物交付是否通过。

## 整体任务进度分析

| 范围 | 状态 | 下一步 |
| --- | --- | --- |
| P1 核心工具、Git 工作区、正式评测接入 | 已实现；此前 254 项测试与双平台 CI 通过 | 本轮产品代码未变化 |
| HKGAI 普通对话、工具调用和工具回传 | 上一轮真实验证通过 | 保留该基础结论 |
| 真实模型自主计划 | 已产生计划，当前格式与任务结构未通过本次要求 | 与 P0 对齐条件字段及非法计划反馈 |
| 真实多 Agent 并行、依赖、产物整合、最终验收 | 尚未进入执行阶段 | 先补离线回归和修复方案，经检查后再单次试跑 |
| 正式评测、完成率/耗时统计 | 未完成 | 链路稳定后再按团队正式题库重复评测 |
| 真实搜索、真人审批、跨成员签收 | 待对应团队配置/联调 | 分别验收，不能由本次结果替代 |

建议下一轮先与 P0 确认：如何在模型工具 Schema 中准确表达 command/expect 条件；如何将计划校验错误反馈给主 Agent；如何检查明确指定的任务结构。先用已保存的失败计划补无密钥回归，再按确认的方案修复；后续真实重跑另记录，保留本次失败证据。

## 本地证据与审阅状态

- runs/hkgai-e2e/df4391e0d292/summary.json：单次运行结果、任务与工作区状态。
- runs/hkgai-e2e/df4391e0d292/llm-calls.jsonl：真实模型调用与返回计划。
- runs/hkgai-e2e/df4391e0d292/trace.jsonl：框架事件。
- runs/hkgai-e2e/df4391e0d292/diagnosis.json：无额外模型请求的本地诊断。
- runs/hkgai-e2e/run_once.py：本地运行脚本，不含密钥。

本轮未执行源仓库 git add、commit 或 push，未更改 GitHub PR。源仓库 HEAD 保持原值。文档改动待负责人检查；.env 和 runs 继续被 Git 忽略，密钥不进入提交内容。

## 计划恢复修复后的状态补充

本轮修复后另做一次真实运行：非法计划可反馈并纠正，三个子 Agent 完成，产物验收通过；主任务因本地 20 请求上限未正常收尾，严格 E2E 仍未通过。原始失败记录保留，详情见 P1-PLAN-RECOVERY-VALIDATION.md。

## 后续收尾优化结果

运行 8279938ebdc3 已正常完成主任务和三个子任务，父任务最终六项机器验收通过。该结果不改写本文对应旧运行记录。子任务自报验收的限制、最新整体进度和提交建议见 P1-LEAD-FINALIZATION-VALIDATION.md。所有本轮改动仍待本地审阅，未提交或推送。

## 本轮提交状态补充

本文对应试跑时的“未提交/未推送”是当时状态。现已按负责人确认将计划恢复与收尾源码分别形成本地提交 a03c8c9、64066b7；本文作为第三批脱敏文档交付。实际推送和最终 CI 以 [PR #1](https://github.com/Greensheep04/moumou-Agent/pull/1) 的 Checks 为准，main 保持不合并。密钥与原始 runs 仍仅保留本地。最新框架符合性及跨模块签收要求见 P1-FRAMEWORK-COMPATIBILITY.md。
