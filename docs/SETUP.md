# 环境与运行说明

[简体中文](SETUP.md) · [English](SETUP.en.md)

## 运行环境与安装

使用 Python 3.11 及以上、Git，以及课程提供的 DeepSeek 或 HKGAI 服务。支持原生 Windows，无需 WSL。本地开发使用 Python 3.12 的 Conda `nlp` 环境；GNU Make 为可选工具。

在项目根目录的 PowerShell 中执行：

```powershell
conda activate nlp
python -m pip install -e ".[dev]"
```

下文命令使用已激活环境的 `python`。其他系统激活各自的 Python 环境后也使用相同模块命令。框架回归和离线报告重建不需要模型或搜索密钥。

仅通过完整路径启动某个 `python.exe` 不会激活该环境的 `PATH`。工具命令和验收子进程继承启动进程的 `PATH`；因此应先激活环境，或通过 Conda 启动默认老师评测入口：

```powershell
conda run --no-capture-output -n nlp python -m moumou
```

使用额外的 Git 工作树时，环境中的可编辑安装可能仍指向另一个项目副本。在目标工作树根目录中，为当前 PowerShell 会话绑定源码，并在测试前检查实际加载位置：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "src"
python -c "import moumou.eval.cli; print(moumou.eval.cli.__file__)"
python -m moumou.eval.cli --plan
```

打印路径应属于准备测试的工作树。该设置只影响当前进程环境，不重新安装依赖、不修改系统 Python 配置，也不要求在工作树之间复制密钥。

## 本地密钥

仅在 `.env` 尚不存在时复制模板，再编辑本地文件：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 课程模型密钥；真实任务执行必需 |
| `DEEPSEEK_MODEL` | DeepSeek/HKGAI 服务指定的模型名称 |
| `DEEPSEEK_BASE_URL` | 兼容服务地址，包含服务要求的 API 前缀 |
| `DEEPSEEK_TIMEOUT_S` | 单次模型请求超时秒数，默认 180 |
| `TAVILY_API_KEY` | 需要真实联网调查的任务所用的搜索密钥 |

HKGAI 也沿用 `DEEPSEEK_*` 配置名。课程 HKGAI 公共地址为 `https://api1.hkgai.net/v1`，配置模型为 `t2_hkgai-v3_fp8_1m_e7`。真实密钥只填写在本地 `.env` 中。环境变量优先于 `.env`；密钥和运行产物不进入 Git。

## 框架检查

```powershell
python -m pytest -q
python -m ruff check src tests
```

这些是实现检查，与老师任务的质量验收分开。pytest 的 Make 等价命令为 `make unit-test`；`make test` 和 `make eval` 会运行老师评测，需要模型配置和真实人类输入。命令示例不表示已经执行；当前候选版本的实际验证状态见[性能与验证报告](PERFORMANCE.md)。

## 运行老师题库

默认题源为 [eval/tasks](../eval/tasks/README.md)：三组团队，每组保留老师原始四角色提示词和两个顺序任务。以下命令只检查题源，不需要密钥、不创建事件循环、不调用模型，也不创建运行目录：

```powershell
python -m moumou --plan
python -m moumou.eval.cli --tasks eval/tasks --plan
```

自建十五题保留在 [eval/supplementary](../eval/supplementary/README.md)，同样使用 Markdown 团队/任务协议，分为八组，最后一组只有一道题。它们优先级低于老师题库，不会自动选择，也不计入老师成绩。检查或运行时，在同一 CLI 显式指定 `--tasks eval/supplementary`。

以下两个命令使用相同正式协议：

```powershell
python -m moumou
python -m moumou.eval.cli --tasks eval/tasks
```

默认运行全部三组、使用真实控制台交互和真实质量审阅，并在 `runs/test/` 下创建新目录。选择单组或文件输入：

```powershell
python -m moumou --scenario scenario_1 --human files --out runs/test/NEW_SESSION
```

模型响应默认仍使用 buffered 客户端。在同一入口显式添加 `--llm-transport stream` 可选择学校 SSE 客户端；它只接受完整响应，另存尝试元数据，不自动降级到 buffered，默认不重试HTTP失败。受限HTTP重试需另行显式选择，见下文。短请求检查不能证明长任务可靠性或内容事实正确。

学校流式客户端另支持显式推理设置：

```powershell
python -m moumou --llm-transport stream --llm-reasoning-effort none --out runs/test/NEW_REASONING_SESSION
```

省略 `--llm-reasoning-effort` 时值为 `None`，请求不发送该字段；buffered 模式拒绝此选项。运行清单、流式侧记录及尝试诊断保存的是请求设置，不证明提供方实际遵守。在同一学校端点及指定模型的一次固定请求中，`none` 返回了完整的 `read_source` 工具调用但未执行工具；默认及 `thinking.type=disabled` 试验以 `finish_reason=length` 结束。这项有限诊断不等于老师题通过，也不证明 HTTP 504 已全面解决。

流式模式还可显式选择 `--llm-completion-cap`（请求提供方 `max_tokens`）与 `--llm-max-content-chars`（本地普通内容码点上限）。两者省略时保留原默认值；buffered 模式拒绝显式输出限额。`--require-declared-inputs` 默认关闭，可独立于传输模式开启文件输入声明及首接收检查。可先只核对计划，不调用模型：

```powershell
python -m moumou --plan --llm-transport stream --llm-completion-cap 16384 --llm-max-content-chars 32768 --require-declared-inputs --opening-context-tokens 32768 --llm-tool-choice required-when-tools --llm-transient-http-retries 2
```

其中 `--opening-context-tokens` 独立于传输模式；显式值须为500至2147483647的整数且不超过prompt ceiling，省略仍为8000，只复制老师开场策略，保留P1默认与EMA。`--llm-tool-choice auto|required-when-tools` 仅stream可选；后者有工具时请求required，无工具时保留plain格式，失败不降级。无效配置在读密钥或创建输出前拒绝。这些选项不证明提供方遵守、文件被理解或内容合格；实际执行仍需新输出目录和真实 HumanIO。完整字段及边界见[评测流程](EVAL.md)。

`--llm-transient-http-retries 0|1|2` 同样只允许stream，省略保持0次；设为2表示最多3次HTTP请求。仅正文前的502/503/504可在共享调用截止时间内等待5秒、15秒后重试；其他状态、transport错误、部分200响应、协议/长度/输出保护失败及取消不重试，不降级。多次请求与退避共享配置的 `timeout_s`（本轮180秒），不为每次请求重新获得180秒。重试后整调用用量保持未知，成功attempt的已知usage另留于sidecar，不把失败费用当作零。细节见[评测流程](EVAL.md#受限瞬时http重试)。

11月19日验证源码 `79a0dbaa…` 已完成1779 passed、8 skipped、Ruff0的原生回归；新三场景实际尝试的三个首题均在正文前HTTP502后DEAD，第二题均未执行，正式交付及新Tavily均0。当前按用户请求暂停测试，以上命令只是复现说明，不会恢复已消费claim或旧失败会话。较早 `16981d95…` 单题与 `73e1c969…` 软件失败仍保留各自结果；配置、适配器及未知质量见[性能报告](PERFORMANCE.md)。

已发布基线源码 `be0d3f57…` 完成原生 Windows Conda `nlp` 全量回归：1613 passed、8 skipped、0 failures/errors、Ruff 0 问题，源码与测试前后不变。该版新闻首题辅助复测沿用相同输入、使用 stream/none、复用六条公开记录且新 Tavily 配额为零，自然结束为 DEAD、正式导出为零、质量未知。实际观察到调度恢复，但审阅文件交接缺失、流式响应未完整结束和内容纠正问题仍在，框架检查不代表老师验收通过。

历史检查点 `556d78fc…` 的 1601 passed、8 skipped 另行保留。其新闻首题辅助轮因记者耗尽 token 预算重试并暴露失败依赖调度问题，由操作员中止；原始 `running` 及缺失最终结果、导出和流式侧记录的事实均保留，内容质量未知，不与新复测混同。分版本结果及停机证据见[性能报告](PERFORMANCE.md)。

`--out` 指定的目录必须尚不存在。同一场景保留一个 Engine、四个持续使用的私有角色工作区、记忆，以及 Task 1 的真实结果和对话，供 Task 2 使用。具体分工、实现和检查在执行中协商；评测器不会用固定问题或预设答案代替原任务。

每道题交付落盘后，默认质量审阅等待真实的 `approve` 或 `reject`，可附反馈。拒绝会停止该组后续依赖任务，其他独立组仍可继续。`--quality-review deferred` 明确保留未知质量；该模式运行结束不代表老师任务通过。

## 真实人类输入

控制台模式展示请求并等待回答。审批或审阅输入 `approve` 或 `reject`，后面可附反馈文字。

文件模式将原请求保存在：

```text
SCENARIO_ROOT/human/requests/REQUEST_ID.json
SCENARIO_ROOT/human/pending/REQUEST_ID.json
```

外部人类或控制器将实际 `HumanResponse` 写入：

```text
SCENARIO_ROOT/human/responses/REQUEST_ID.json
```

答复必须对应原 `request_id`。问题需要实际 `text`；审批和审阅需要明确的 `decision`（`approve` 或 `reject`）。接受的答复保存在 `human/consumed/` 和对话中。不能写入预设答复，也不能从工具执行许可推定质量同意。控制器合法复用用户已有授权时，应记录来源，并将该策略判断与新输入的人工作答区分。

## 搜索配额与执行边界

`--search-requests 6` 为每组提供初始六次新的 Tavily HTTP 尝试，由四角色和两道任务共享；完整三组共初始十八次。缓存命中与并发去重分别记录。对问题作出明确人类答复 `/grant-search N` 可以增加配额，模型不能自行授权。

搜索配额与学校模型调用额度分开。每节点步数、token 和协作式时间上限用于避免失控执行，不代表金额预算或整组硬截止时间。真实人类等待和交付后的质量审阅等待按各自边界记录。

## 运行产物与报告

| 位置 | 内容 |
| --- | --- |
| `RUN/test.json` | 整场计划、题源哈希、公共配置和保存状态 |
| `RUN/active.json` | 当前场景、阶段和人类输入目录 |
| `RUN/report.md` | 派生的可读报告 |
| `RUN/scenario_N/task_1-result.json` | Task 1 交付、子树和质量观测 |
| `RUN/scenario_N/task_2-result.json` | Task 2 发布并交付后的观测 |
| `RUN/scenario_N/dialogue.jsonl` | 完整脱敏模型请求/响应及真实人类交换 |
| `RUN/scenario_N/workspaces/` | 四个持续使用的私有角色目录及实际输出 |
| `RUN/scenario_N/human/` | 持久化人类请求与答复 |
| `RUN/scenario_N/search/` | 搜索配额、缓存和请求审计 |
| `RUN/scenario_N/task_N-artifacts/` | 已记录时的不可变交付清单及按哈希保存的文件 |
| `RUN/scenario_N/state-snapshot.json` | 场景关闭时的节点诊断，不是新增交付 |
| `RUN/reviews/<UUID32>/` | 追加式离线审阅意图、原 HumanIO 证据和决定 |

无需模型调用或密钥即可重建已保存的报告：

```powershell
python -m moumou.eval.report --run runs/test/EXISTING_SESSION
```

重建不会执行测试、恢复会话或批准任务。没有自动续跑选项。进程被强制停止时，原 manifest 可能仍为 `running`；应保留该记录，并以明确的停止和清理证据描述真实状态，再规划后续执行。

### 已保存的分析、回放及后续质量复核

下列命令处理已有会话。示例不表示检查已经执行；当前状态见[性能与验证报告](PERFORMANCE.md)。

```powershell
python -m moumou.eval.analysis --run runs/test/EXISTING_SESSION
python -m moumou.eval.replay --run runs/test/EXISTING_SESSION --out runs/test/EXISTING_SESSION/replay-v1.json
python -m moumou.eval.review --run runs/test/EXISTING_SESSION --scenario scenario_1 --phase task_1 --human console
```

分析和回放不调用模型、不重新验收，也不恢复会话。回放 JSON 必须写入新文件，不替换原记录或现有证据。旧运行缺失的快照保持未知，不能读取当前工作区或 `memory.db` 冒充早期状态。同任务原问题的 scoped 恢复能力，与在已有输出目录重新启动 CLI 整场运行是两回事。

离线复核通过真实 Console/File HumanIO 请求明确的 approve/reject，没有直接指定决定的参数或自动同意。文件模式在人类复核的新目录中保存待答队列。再次审阅必须指定 `--supersedes CURRENT_TIP_UUID32`，原审阅全部保留。报告展示有效质量覆盖，同时保留原结果及题源/产物绑定；记录匹配不等于密码学意义上的人类身份认证。

审阅发布使用 `reviews/.publish.lock`。已有锁需要操作者明确检查和协调，不自动删除所谓过期锁。搜索配额回调使用原批准请求身份，避免恢复回调时重复增加同一份额度。

## Make 等价命令

| 命令 | 用途 |
| --- | --- |
| `make test` / `make eval` | 相同的老师任务协议 |
| `make unit-test` | 不调用模型的框架 pytest 检查 |
| `make report REPORT_RUN=runs/test/EXISTING_SESSION` | 离线重建已保存报告 |

需要时用 `EVAL_PY` 指定解释器完整路径。没有 Make 的原生 Windows 用户可使用上面的 Python 等价命令。框架命令中的 `python3` 兼容别名选择当前解释器，不会把 POSIX shell 语法翻译成 Windows 语法；手工终端使用原生命令和 `python`。

拟交付目录不附带 GitHub Actions 工作流。检查应在选定环境中明确执行并保留真实输出。本地删除工作流不会删除历史托管运行；只有删除被提交并推送到相应远端分支后，才会影响该分支。

相关说明：[评测流程](EVAL.md)、[接口](INTERFACES.md)、[指标口径](metrics.zh-CN.md)、[交付清单](DELIVERY.md)。
