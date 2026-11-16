# 老师任务评测

[简体中文](EVAL.md) · [English](EVAL.en.md)

## 统一协议，显式区分题源

[eval/tasks/README.md](../eval/tasks/README.md) 是默认老师题库，保留各组原始角色初始化和两个任务文本：

| 场景 | 固定成员 | 顺序任务 |
| --- | --- | --- |
| `scenario_1` | Technical Lead、Software Developer、Test Engineer、Release Manager | 会议室预订系统，随后使用率仪表板 |
| `scenario_2` | Editor-in-Chief、Reporter、Fact-Checker、Editor | 校园新闻报道，随后 AI 写作检测器事实核查 |
| `scenario_3` | Fund Manager、Research Analyst、Risk Manager、Marketing Specialist | 模拟投资方案，随后半导体 HTML 演示 |

老师十份 Markdown 全部作为原始输入保留。加载器记录题源哈希和原提示词，不编造验收测试或答案。完整老师题库有六个根目标；Task 2 沿用同组四成员、私有角色工作区、经验、真实 Task 1 结果及对话引用。分工、实现和合适的检查由执行过程与人类协商确定。

正式团队适配器传入完整原角色提示词、当前目标和全部验收条件；先限制可选历史、依赖摘要、结论板和经验，并在私有 `.evaluation/context/<digest-prefix>.json` 中保留精确辅助内容。新文件名使用哈希前 32 个十六进制字符，引用和校验保留完整 SHA-256；不同内容的碰撞明确拒绝，不重命名旧归档。必需指令超过开场预算时，`ContextAdmissionError` 在调用 LLM 前拒绝准入。归因模型遗漏前先检查实际发送的输入；框架截断属于系统失败，未完成目标仍保留在计划分母中。

低优先级[补充题库](../eval/supplementary/README.md)将原自建十五题保留为相同角色/任务约定的 Markdown，共八组，除最后一组单题外每组两题。必须显式 `--tasks eval/supplementary`，不会自动降级运行；使用独立结果和十五目标分母。原 setup 与检查信息保留，但历史脚本答案元数据不授权真实人工作答或审批。

记忆迁移题的原 warmup 已作为题目中的明确准备前提。团队应实际执行并记录；CLI 不运行旧消融计划，也不会自动注入记忆。迁移结论需要真实准备和检索证据。

在项目根目录激活环境后：

```powershell
# 只查看题源，不需要密钥、不调用模型、不创建运行目录。
python -m moumou --plan

# 相同正式入口，使用真实控制台输入及质量审阅。
python -m moumou
python -m moumou.eval.cli --tasks eval/tasks

# 单组、真实文件输入、新输出目录。
python -m moumou --scenario scenario_1 --human files --out runs/test/NEW_SESSION

# 老师任务之后可显式选择自建题，协议相同。
python -m moumou.eval.cli --tasks eval/supplementary --plan
```

`make test` 和 `make eval` 使用该协议。确定性框架回归使用 `make unit-test` 或 `python -m pytest -q`；这些测试提供实现证据，不是老师任务完成率。

`tests/` 中可包含题源加载器和入口检查，但老师原题位于 `eval/tasks/`，不是这个测试目录。
有意义的回归源码建议随代码保留，老师没有单列 `tests/` 为硬性交付物；缓存、临时文件和本地输出无需上传。

## 人类交互与质量

正式评测使用真实问题、工具权限和交付后的质量审阅。控制台模式直接询问；文件模式将请求保存在 `SCENARIO_ROOT/human/pending/`，并从 `human/responses/REQUEST_ID.json` 接受对应的 `HumanResponse`；字段见[运行说明](SETUP.md)。没有预设答案或自动同意。

质量请求发出前，任务结果已保存供检查。明确人类同意记录为 `human_accepted`；拒绝记录为 `human_rejected`，跳过该组后续依赖任务。普通任务阶段错误、拒绝和未完成交付不阻止其他已配置独立组执行；初始化、配置或清理失败属于整场基础设施停止，取消/KeyboardInterrupt 也停止整场。未尝试组在完整分母中保持未知。

Agent `DONE`、子任务测试全绿和启动器可运行是不同证据。空根验收清单属于自报，不能称机器验证。`--quality-review deferred` 保持质量未知，不因 Agent 已停止就产生 verified success。独立质量评估可使用完整保存对话和输出，但必须记录实际评估者和决定；默认 CLI 等待真实人类审阅，不伪造独立模型分数。

同任务委派恢复会从 scoped 问答账本恢复已答内容，保留原 ID 和来源；原待答问题按原身份续接，不编造答复。这些事实进入固定开场上下文，工具审批授权不会复用。人类端口记录可包含明确授权的操作者协助，仍须与新手工答复或质量批准区分。

后续质量决定使用[运行说明](SETUP.md)中的追加式 `moumou.eval.review`。原质量与有效质量覆盖分别可见；覆盖不会恢复执行或改写原结果。

## 密钥、搜索和上限

使用课程 DeepSeek/HKGAI 配置，密钥保存在本地 `.env`，见[环境说明](SETUP.md)。真实联网搜索需要 `TAVILY_API_KEY`。初始 `--search-requests 6` 覆盖一组两题和全部四角色，缓存命中、去重调用及真实 provider 尝试分别记录。增加配额需要明确人类授权；它不限制学校模型调用额度。

每节点执行上限是协作式循环保护。应记录实际等待、失败调用及中断；场景墙钟时间不等于并发 span 累计时间之和，也不是整进程硬截止保证。

## 结果与中断

默认每次在 `runs/test/<run_id>/` 下新建目录，也可通过 `--out` 指定新路径。`test.json` 保存计划和场景状态，`active.json` 标识当前阶段。每组任务结果、完整 `dialogue.jsonl`、数据库、搜索审计和私有角色输出均保留在该目录；运行产物和密钥不提交。

失败和中断证据必须保留。强制停止可能使 `test.json` 仍记录 `running`，不能改写为正常完成；归档应附实际停止/清理证据和未答请求。CLI 不自动恢复已有输出目录。之后续接或新试验需要明确且有记录的状态转移方案，保留原记录和未知质量。

新交付保存带哈希的不可变文件副本及阶段记忆/失败快照；场景关闭时的状态快照可保留未完成节点作诊断，不算交付任务。旧运行没有快照时保持未知，后续工作区文件或实时 `memory.db` 不能冒充历史状态。

```powershell
python -m moumou.eval.report --run runs/test/EXISTING_SESSION
```

离线重建只使用保存的任务证据，不调用模型、不重新验收、不回答未决问题。旧归档可用于历史分析，但不构成另一个正式题库或入口。未被索引的任务结果或不一致身份属于完整性错误，应明确修复归档后再评分，不能静默纳入或丢弃。

报告的 `suite_wall_time_s` 仅使用保存且带时区的 `started_at` 与 `finished_at`。任一端点缺失则未知；格式无效、无时区或顺序颠倒则拒绝。它包含该区间内等待和未完成阶段；`delivery_time_s` 只覆盖保存交付的样本，`n=0, sum=0` 不代表整场执行零秒。`report_builder.source_sha256` 与原 manifest 的 `code` 启动快照分开，重建不会改变运行来源。

离线 `moumou.eval.analysis` 和 `moumou.eval.replay` 提供保存的失败/记忆观测及供 P2 使用的只读 JSON，保留原事件、行哈希及 scenario+task 身份，分别呈现已答、待答和已写入但未消费的答复。当前候选实际验证状态见[性能与验证报告](PERFORMANCE.md)。命令示例、导出结构或历史回归不能证明当前质量或 P2 已签收。

## 报告清单

- 写明题源哈希、选择场景、模型、环境及实际输出目录。
- 保留全部已选根目标分母，包括未发布、未完成和未审阅目标。
- 分开报告 Agent 完成、机器验收和已审阅质量。
- 区分本题子树/阶段调用与场景累计观测。
- 报告交付时间、审阅等待、usage 缺失覆盖及搜索尝试。
- 链接实际对话、产物和检查，说明失败与剩余不确定性。

[指标口径](metrics.zh-CN.md)定义具体字段。框架回归通过或自建实验完成不代表老师六目标通过；只有实际执行和有记录的质量证据才能支持该结论。[交付清单](DELIVERY.md)将这些记录对应到架构、性能报告、贡献截图和最终演示要求。
