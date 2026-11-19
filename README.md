# Moumou 多智能体协作系统

[简体中文](README.md) · [English](README.en.md)

Moumou 将人类提出的任务交给一组协作智能体执行。智能体可以自主委派工作，在各自私有工作区中调用工具，遇到歧义或需要授权的操作时询问人类，并保留经验用于后续任务。正式评测采用老师提供的连续团队会话。

技术栈：Python 3.11+ · asyncio · SQLite · Pydantic v2 · 学校 DeepSeek / HKGAI API。一般文档使用 Markdown 或纯文本；老师半导体题按原题交付 HTML 幻灯片。

## 从这里开始

| 需要做什么 | 对应说明 |
| --- | --- |
| 安装依赖、配置运行环境 | [环境配置](docs/SETUP.md) |
| 运行老师题目、理解结果 | [评测流程](docs/EVAL.md) |
| 准备最终提交与证据 | [交付清单](docs/DELIVERY.md) |
| 理解路由、记忆与隔离 | [系统架构](docs/ARCHITECTURE.md) |
| 对接其他模块 | [接口说明](docs/INTERFACES.md) · [团队职责](docs/roles/TASKS.md) |
| 查看验证版本与待验收事项 | [性能与验证](docs/PERFORMANCE.md) |

2026-11-19 验证工作树 `79a0dbaa…` 完成1779 passed、8 skipped、Ruff 0 问题。原软件、新闻、基金三场景已分别尝试，三个首题均因正文前HTTP502结束为DEAD，三个第二题未执行，正式交付及新增Tavily均0；内容质量保持未知。测试已按用户请求暂停，502原因尚未确认。执行身份、配置和分阶段证据见[性能报告](docs/PERFORMANCE.md)。

同日较早工作树 `73e1c969…` 完成1728 passed、8 skipped、Ruff 0 问题；其软件场景首题随后因HTTP502自然失败，正式交付0，第二题未执行，原记录已封存，不与后续三组尝试混算。

同日较早验证的工作树 `16981d95…` 完成1668 passed、8 skipped、Ruff 0 问题。新闻首题辅助单轮以 Agent 根任务 DONE、10 份正式文件结束，但仍有内容审阅限定，人工质量保持未知；这不是同源码三场景六题验收。分版本结果见[性能报告](docs/PERFORMANCE.md)。

截至 2026-11-18，原始发布版 `085b12e`、冻结研究复用版 `e59ee40c…` 和 `6a830670…` 的补充修订保留各自证据。6a 的基金1正式交付四文件但仍有内容问题；新闻修订第一题与基金2修订根任务均因 HTTP 504 结束为 DEAD、正式导出为零。新闻第一题的七份子任务候选经独立核对支持核心内容，仍不等于正式交付；该轮新闻第二题未启动。人工质量状态保持未知。

已发布基线源码 `be0d3f57…` 完成原生 Windows Conda `nlp` 全量回归：1613 passed、8 skipped、0 failures/errors、Ruff 0 问题，pytest 180.15s（执行包装层 180.562s），源码与测试前后不变。失败输入恢复仅限父任务自己的子任务，须等全部依赖进入终态，保留唤醒上限并将失败传播到稳定状态，不绕过质量检查。该版新闻首题辅助复测自然结束为 DEAD，耗时 4202.906s、正式导出为零、质量未知。实际观察到失败检查器没有启动下游编辑、父任务得到恢复机会；但恢复计划仍遗漏必要文件交接，四次流式请求持续产生大量普通正文却未在限时内完整结束。候选内容还存在时态过期、因果表述过强和最终编辑审阅未完成等问题，不能用框架回归代替老师题通过，详见[性能报告](docs/PERFORMANCE.md)。

历史检查点 `556d78fc…` 完成 1601 passed、8 skipped、0 failures/errors、Ruff 0 问题，pytest 178.56s（执行包装层 178.938s），源码与测试前后不变。该版包含直接依赖间的有界原生凭证交接、核验写作规则及显式学校流式推理选项。其新闻首题辅助轮因记者耗尽 token 预算重试并暴露 P1 提前释放依赖消费者的问题，由操作员中止。原始 manifest 的 `running` 保持不变，没有最终 Task1 结果、导出快照或流式侧记录；这是中止失败基线，内容质量未知，不能伪装成自然收尾 DEAD 或经过验证的零文件导出，也不与新复测混算。框架回归不等于老师题通过，也不证明 HTTP504 已全面修复；独立停机证据和分版本结果见[性能报告](docs/PERFORMANCE.md)。

历史检查点源码 `8bc0c2ca…` 保留 1538 passed、8 skipped、0 failed、Ruff 0 问题，pytest 用时 179.14s（执行包装层 179.734s），检查前后源码与测试一致。正式入口注册实际文件引文核对 `verify_artifact_quote`、来源访问审计 `source_access_audit`，并将收据绑定到最终导出文件版本。可选流式传输已接入，默认仍为 `buffered`。学校 SSE 客户端的文本、工具调用组装及工具结果回传三项短请求检查通过，只证明这些小输入的接口能力，不能宣称修复 HTTP504；该历史检查点的首次新闻首题辅助验证因两次本地推理输出超时结束为 DEAD，三个子任务已完成但正式导出仍为零，人工质量保持未知。接入和证据字段见[评测流程](docs/EVAL.md)与[接口说明](docs/INTERFACES.md)。

历史组合源码 `8f7f8d23…` 的 1354 passed、8 skipped、0 failed、Ruff 0 及真实辅助运行分别保留。该版11月18日的基金1 DONE、正式4文件但仍需两点内容纠正；新闻1与基金2均因HTTP504根任务DEAD、正式导出0，该新闻连续Task2未启动。取证内容及基金2候选浏览器功能核对不改变失败状态。独立原新闻2→补充S2T1为DONE、正式3文件，独立审阅支持核心纠正并建议两项编辑澄清；它不是原连续Task2。该轮辅助验证在14:52全部模型后端及Rich界面已停止，原人工质量仍null。详见[性能报告](docs/PERFORMANCE.md)。字面匹配、记录到的来源访问和框架回归不等于事实或质量批准；不同源码和辅助模式不能拼成当前候选“6/6通过”，Final闭集和最终签收仍待完成。

## 安装与配置

在仓库根目录激活 Python 3.11+ 环境。正式目标环境为原生 Windows，使用 Conda `nlp`：

```powershell
conda activate nlp
python -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在本地 `.env` 中填入课程提供的配置：

```ini
DEEPSEEK_API_KEY=your-course-key
DEEPSEEK_MODEL=your-course-model
DEEPSEEK_BASE_URL=your-compatible-course-endpoint
TAVILY_API_KEY=your-search-key
```

模型配置支持课程指定的 DeepSeek / HKGAI 接口；当前联网检索使用 Tavily，已登记公开原文的读取与新搜索分别留证。真实 `.env` 和密钥必须排除在 Git 之外，只提交配置模板。文件式人类回复、环境细节和可选冒烟命令见[环境配置](docs/SETUP.md)。

## 运行老师评测协议

```powershell
python -m moumou --plan
python -m moumou
```

`--plan` 只检查题源身份，不调用模型。第二条命令开始真实执行，并等待真实的人类回答和质量决定。`python -m moumou.eval.cli` 使用同一个正式入口。

默认使用 `--llm-transport buffered`。显式 `--llm-transport stream` 可选择推理设置、输出限额及 `--llm-tool-choice required-when-tools`；后者仅在提供工具时请求完整工具调用，无工具请求仍保持plain格式，失败不降级。`--require-declared-inputs` 默认关闭；独立可选的 `--opening-context-tokens` 为老师开场选择配额，省略仍为8000，不改P1默认或EMA。原有 `--llm-completion-cap`、`--llm-max-content-chars`、输入声明和诊断字段的完整示例及质量边界见[评测流程](docs/EVAL.md)。这些配置不证明事实正确、操作获批或老师任务通过。

仅stream可显式选择 `--llm-transient-http-retries 0|1|2`，默认0次HTTP重试。非零值仅重试正文前的502/503/504，固定等待5秒／15秒并共享原调用截止时间；不会重试部分200响应或绕过输出检查。重试后完整调用用量仍可能未知。用法和计量边界见[评测流程](docs/EVAL.md#受限瞬时http重试)。

[老师原题](eval/tasks/README.md)包含三个团队，每组四个指定角色、两个顺序任务。Task 2 复用同组身份、角色私有工作区、已交付文件和记忆，不同组拥有独立状态。每组两个任务和四个角色共享初始六次 Tavily 新检索额度；缓存命中单独统计，追加额度必须获得明确的人类授权。学校模型调用额度与搜索计量分开。

保留的十五个[自建补充题](eval/supplementary/README.md)使用相同 Markdown 格式，优先级低于老师题库：

```powershell
python -m moumou.eval.cli --tasks eval/supplementary
```

补充题成绩与老师题分开。每次执行使用新的本地 `runs/test/<id>/` 目录，保留完整对话、实际人类交互、任务结果、trace、数据库和产物。CLI 不会自动覆盖或续跑已有输出目录。

## 查看结果与运行框架检查

```powershell
# 从已有证据重建报告，不调用模型，也不重新执行任务。
python -m moumou.eval.report --run runs/test/EXISTING_SESSION

# 框架回归，与真实任务评测分开。
python -m pytest -q
```

这些是复现命令，不代表新增测试结果。安装了 Make 时，`make unit-test` 运行 pytest；`make test` 和 `make eval` 运行老师协议。原生 Windows 无需安装 Make。离线复核、分析和回放导出见[环境配置](docs/SETUP.md)与[评测流程](docs/EVAL.md)。

`tests/` 包含代码功能、接口和回归检查，也检查题库加载器与评测框架本身；它不同于 `eval/tasks/` 的老师题库和 `eval/supplementary/` 的低优先级补充题。框架测试全绿不能证明老师六目标通过。有意义的回归源码与功能代码一起保留，生成的测试输出只保存在本地。

## P2 文件会话与回放

安装可选界面依赖后，可以连接正式评测的文件交互端口：

```powershell
python -m pip install -e ".[dev,p2]"
# 终端一：沿用正式老师入口，输出目录必须是新目录。
python -m moumou --human files --out runs/test/P2_SESSION
# 终端二：连接同一运行目录，展示并回答实际待答请求。
python -m moumou.ui.cli --run runs/test/P2_SESSION
```

P2 只展示和暂存实际人类答复；审批、答复消费、问答恢复、搜索额度及质量记录仍由 P3 处理。
它不会另建题库或替换现有 P1 工具。`--demo` 是独立脚本演示，不等于老师任务验收。
详见 [P2 接入说明](docs/delivery/P2_DELIVERY.md)。原生 Windows Rich 已用于真实老师会话；`e59ee40c…` 两组后台和界面正常退出，后来 6a 补充轮也实际消费了人类答复，但两次根任务的 HTTP 504 失败仍保留。此前界面读取异常与重附加不能由新运行覆盖；最终候选的真实流程、内容和 P2 真人交互签收仍需单独确认。

## 模块对接边界

| 模块 | 负责内容 | 验收需要的证据 |
| --- | --- | --- |
| P0 编排 | 任务状态、路由、委派、上下文、审批与记忆演化 | 契约/回归结果和真实集成行为 |
| P1 执行 | 工具、工作区边界、明确产物交接、原生进程处理与搜索 | 工具契约、环境与真实操作证据 |
| P2 交互 | 人类/私聊/群聊通道、交互展示与回放消费端 | 通道契约和实际消费端/界面检查 |
| P3 数据与评测 | trace/记忆持久化、题源、真实 HumanIO 评测、指标与报告 | 框架结果、已保存交付及质量决定 |

共享契约变更需要与消费模块协调，API 与中英文文档保持一致，报告中保留失败和未知结果。通过某个契约测试只说明该测试覆盖的范围，不能作为系统全面验收。正式 runner 的 Git 暂存/提交操作需要审批；底层通用 Engine 的默认策略单独说明。

## 仓库目录

```text
src/moumou/
  contracts/      共享数据模型与接口协议
  orchestrator/   状态、调度、委派、审批与验收
  context/        上下文准入、压缩与结论板
  memory/         经验存储、检索、反思与演化
  trace/          JSONL 日志及 SQLite 投影
  tools/          文件、Python、shell、Git 与搜索工具
  sandbox/        工作区与原生执行边界
  adapters/       参考模型、HumanIO、通道与工作区实现
  eval/           正式会话、证据保存、报告、复核与回放
  demo/           确定性的框架演示

eval/tasks/       老师原始 Markdown 题源
eval/supplementary/ 同协议的低优先级补充题
tests/            有意义的框架与集成回归
docs/             架构、接口、运行与交付说明
runs/             本地运行证据，排除在 Git 之外
```

本次交付维护中英文运行与 P3 接口说明。旧变更记录保留当时里程碑，当前正式协议以现行说明为准。模块详细说明见 [docs/roles](docs/roles/)，上下文机制见[上下文与记忆](docs/CONTEXT-MEMORY.md)，故障排查见[常见问题](docs/FAQ.md)。[中文交付清单](docs/DELIVERY.md)与[英文交付清单](docs/DELIVERY.en.md)列出最终提交要求。
