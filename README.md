# Moumou 多智能体协作系统

[简体中文](README.md) · [English](README.en.md)

Moumou 将人类提出的任务交给一组协作智能体执行。智能体可以自主委派工作，在各自私有工作区中调用工具，遇到歧义或需要授权的操作时询问人类，并保留经验用于后续任务。正式评测采用老师提供的连续团队会话。

技术栈：Python 3.11+ · asyncio · SQLite · Pydantic v2 · 学校 DeepSeek / HKGAI API。文档交付使用 Markdown 或纯文本。

## 从这里开始

| 需要做什么 | 对应说明 |
| --- | --- |
| 安装依赖、配置运行环境 | [环境配置](docs/SETUP.md) |
| 运行老师题目、理解结果 | [评测流程](docs/EVAL.md) |
| 准备最终提交与证据 | [交付清单](docs/DELIVERY.md) |
| 理解路由、记忆与隔离 | [系统架构](docs/ARCHITECTURE.md) |
| 对接其他模块 | [接口说明](docs/INTERFACES.md) · [团队职责](docs/roles/TASKS.md) |
| 查看已实现内容与待验收事项 | [项目状态](docs/STATUS.md) |

当前开发版本已编写问答恢复、执行记录、证据保存和离线复核等新增实现，其新增回归测试尚未执行。智能体 `DONE`、框架测试通过和老师题目质量验收是不同结果，不能据此宣称六个老师目标全部通过。

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

模型配置支持课程指定的 DeepSeek / HKGAI 接口；当前联网检索使用 Tavily。真实 `.env` 和密钥必须排除在 Git 之外，只提交配置模板。文件式人类回复、环境细节和可选冒烟命令见[环境配置](docs/SETUP.md)。

## 运行老师评测协议

```powershell
python -m moumou --plan
python -m moumou
```

`--plan` 只检查题源身份，不调用模型。第二条命令开始真实执行，并等待真实的人类回答和质量决定。`python -m moumou.eval.cli` 使用同一个正式入口。

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
