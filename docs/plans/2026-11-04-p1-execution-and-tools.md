# moumou-Agent：P1 执行与工具层分阶段实施计划


**Goal:** 在现有框架上完成可靠的文件与代码执行、独立工作区、Git 整合、GitHub 推送及联网搜索能力。

**Architecture:** 保持现有 `Tool` 和 `WorkspaceProvider` 协议，将公共路径校验与进程执行抽到独立模块，通过 `default_tools()` 和 `Engine(workspace=...)` 接入。优先交付 LocalJail 与 Git worktree；Docker 作为基本功能完成后的扩展。

**Tech Stack:** Python >=3.11、asyncio、Pydantic、httpx、Git、pytest、ruff；本机使用独立 Conda 环境。

**Spec:** 用户提供的课程要求、本地 P1/TASKS 分工文档，以及下文列出的仓库协议与代码。本文承接已讨论的 P1 规划；未明确的团队接口事项列在阶段 0，不视为已获团队确认。

**文档日期：** 2026-11-04。所有勾选框表示待完成工作。

## 1. 如何使用这份计划

这份文档回答每个阶段“做什么、交什么、怎样算完成”。先按阶段推进，再在实施某个阶段时细化代码。没有约定最终截止日期，因此使用相对工作日，不绑定具体日历日期。

预估总投入 **15–18 人日**，包含开发、针对性测试、联调和交付说明。人日是实际投入量；等待队友或服务配置的时间另计。基本功能未完成前，不开展 Docker、界面美化或其他扩展。

第一步从阶段 0 开始。每个阶段完成后记录实际结果和提交链接，再进入下一阶段。

## 2. 职责边界

### P1 负责

- 文件读取、列目录、内容搜索、创建、覆盖、追加、删除，以及建议实现的文本补丁。
- shell 和 Python 执行、超时、取消、进程清理及输出限额。
- 每个 Agent 的独立工作区、Git worktree、分支整合与冲突报告。
- Git 本地操作、经过框架审批的 GitHub 推送、联网搜索。
- 自己模块的测试、接口使用说明、隔离边界说明和演示材料。

### 由其他成员负责

| 成员 | 职责 | P1 如何配合 |
| --- | --- | --- |
| P0 | 编排、LLM、角色、任务分解、审批策略与 trace 包装 | 提供稳定工具接口和工作区实现，报告接口缺口 |
| P2 | 私聊群聊、人类交互、审批界面 | 返回结构化结果和可读错误，不在工具内部弹确认框 |
| P3 | 存储、题库、指标与报告 | 提供可复现的执行与整合场景，不自行计算全组指标 |

## 3. Global Constraints / 全局约束

1. 以仓库现有 `src/moumou/contracts/` 为准，不直接照抄旧任务书接口；协议变更先与 P0 对齐。
2. 工具必须提供 `name`、`description`、`params_schema`、`action_class`、`timeout_s` 和异步 `run()`。
3. 普通执行失败返回 `ToolResult(ok=False, error=...)`；取消时先清理资源，再传播 `CancelledError`。
4. 文件工具的目标路径必须在 `ctx.workspace` 内；越界使用稳定错误码 `path_outside_workspace`。
5. stdout/stderr 各保留最多 8192 字节的正文；截断标记另计，置 `truncated=True`，避免切坏 UTF-8 字符。
6. 新增和修改的产物使用相对 workspace 的路径返回。删除产物的传递规则在阶段 0 与 P0 确认。
7. 审批、计时和 trace 包装由框架承担，工具内部不重复实现。`git_push` 必须标为 `git_push`。
8. 保留现有工具名和 `default_tools()` 入口，不删除由编排器拦截的标记工具。
9. 文档产物使用 Markdown/TXT；执行工具仍需支持项目代码文件，不把代码限制为文本文档扩展名。
10. P1 单元测试和主要联调不依赖真实 LLM，不需要为此增加模型调用代码。
11. 代码和运行产物放在 `D:\Projects\moumou-Agent`；环境放在 `D:\Dev\conda-envs\moumou-agent`；Conda 缓存使用 `D:\Dev\conda-pkgs`。
12. 依赖安装到项目环境，尊重已有锁文件；不额外创建 `.venv`。机器路径通过配置传入，不写死到可复用代码。

第 5 条是本计划建议采用的明确口径，实施时同步到测试和说明中。

## 4. Review Focus / 重点风险

| 风险 | 所属阶段 | 必须验证的行为 |
| --- | --- | --- |
| Windows 路径、符号链接/junction、特殊 Agent ID | 1、3 | 不越界、不发生工作区身份碰撞、不误删管理目录 |
| 超时与取消只杀掉 shell，后代继续执行 | 2 | 超时返回；取消向上传播；后代进程均退出 |
| 输出先无限缓存，再截断 | 2 | 大量双路输出可以排空，保留缓冲有界，结果截断 |
| merge 带入未声明文件，或冲突破坏父工作区 | 3 | 产物范围明确；冲突后可恢复；重复整合可预测 |
| 新工具注册成功但缺少真实功能验证 | 4、5 | Git/搜索有独立功能测试；推送拒绝时远端不变 |

## 5. 阶段总览

| 阶段 | 工作量 | 完成后你拥有的成果 | 建议交付批次 |
| --- | --- | --- | --- |
| 0：建立基线与对齐接口 | 1 人日 | 能跑项目、知道现有问题、验证 worktree 可行性 | 基线记录 |
| 1：路径与文件工具 | 2–3 人日 | 统一 LocalJail 和完整文件操作 | PR 1 |
| 2：进程与 Python 执行 | 3 人日 | 超时、取消、大输出都可控的执行器 | PR 2 |
| 3：Git 与 worktree 整合 | 4–5 人日 | Agent 分支独立开发并整合，冲突可报告 | PR 3 |
| 4：推送与联网搜索 | 2 人日 | GitHub 推送及结构化搜索 | PR 4 |
| 5：全链路联调 | 2 人日 | 框架实际使用 P1 实现，场景回归通过 | PR 5 |
| 6：文档与演示 | 1–2 人日 | 可交付、可复现、能答辩的 P1 部分 | 交付提交 |

PR 是建议拆分方式，可以按团队工作流调整。开发过程中持续提交，不在期末集中上传。

## 6. 阶段 0：建立基线与对齐接口

**目的：** 确认项目当前能否运行，并尽早发现会影响后续实现的接口问题。

### 要做的事

- [ ] 在约定目录准备代码 checkout，记录分支和提交 SHA；保留现有未提交修改。
- [ ] 建立独立 Conda 环境，按 `pyproject.toml` 安装项目和开发依赖，记录 Python/Git 版本。
- [ ] 在项目目录运行 `python -m pytest -q`、`python -m moumou.demo.skeleton`，记录真实基线，不照抄文档里的测试数量。
- [ ] 阅读 `contracts/tool.py`、`contracts/workspace.py`、`tools/builtin.py`、`adapters/workspace.py` 和两份相关测试。
- [ ] 在临时仓库做小实验：创建两个 worktree、分别提交不同文件、合并、列出图状日志。实验产物不作为正式实现。
- [ ] 与 P0 对齐下面四项，并把结论记录进文档。

### 需要对齐的四项

1. **平台：** 现有测试包含 `python3` 和 POSIX 引号写法。区分 Windows 测试适配与工具真实错误；约定 Linux 回归环境，不把语法失败当作超时测试通过。
2. **提交与产物：** 是否由 Agent 显式提交；如何整合未提交修改、删除文件；是否允许未声明文件进入父工作区。
3. **隔离边界：** LocalJail 不限制任意代码的系统访问；worktree 共享 Git 元数据。明确基本版是否接受这一边界。
4. **同步工作区协议：** `allocate/integrate/release` 当前是同步方法。约定 Git 操作时限及其对事件循环的影响；需要异步调整时由 P0 协调，P1 不私改协议。

### 交付与完成标准

- 一份基线记录：环境版本、提交 SHA、测试结果、已知问题、四项对齐结论。
- worktree 小实验验证成功，或已定位明确障碍并形成可执行处理方案。
- 能在独立环境运行项目；若原有测试失败，留下可复现记录，后续修改与基线比较。

## 7. 阶段 1：路径与文件工具

**目的：** 让所有文件工具共享同一套路径规则，并补齐日常文件操作。

**建议文件：** 新建 `src/moumou/sandbox/local.py`、`tools/filesystem.py`；调整 `tools/builtin.py`；新建 `tests/test_local_jail.py`、`tests/test_filesystem_tools.py`。实际拆分沿用仓库命名习惯。

### 要做的事

- [ ] 将现有 `resolve_in()` 抽成 `LocalJail`，保留必要兼容入口。
- [ ] 对目标路径和 workspace 做解析与范围检查，覆盖 `..`、外部绝对路径、符号链接/junction。
- [ ] 保留 `read_file/write_file/delete_file`，扩展列目录、内容搜索、追加、目录删除；为新增参数提供清晰 JSON Schema。
- [ ] 建议增加文本补丁：old 文本不存在或匹配多处时明确报错，避免误替换。
- [ ] 拒绝删除 workspace 根目录及受保护的 Git 管理入口；权限错误、非法路径等普通异常返回结构化失败。
- [ ] 将新增/修改产物统一成规范相对路径；只读工具不报告写入产物。
- [ ] 为读取、搜索和目录列表设置输出限制；大文件不先无界读取再裁剪。

### 验收

- [ ] 正常创建、覆盖、追加、读取、搜索、删除均返回预期结果。
- [ ] Agent A 的文件工具不能读取或修改 Agent B 的目录。
- [ ] 越界返回 `ok=False`，包含 `path_outside_workspace`。
- [ ] 缺文件、参数错误、权限失败不产生未捕获普通异常。
- [ ] 原有 `tests/test_tool_contract.py` 中文件相关行为保持兼容；新增测试通过。

**交付：** PR 1，包含 LocalJail、文件工具及针对性测试；此时 P0 已可使用完善后的文件工具。

## 8. 阶段 2：进程与 Python 执行

**目的：** 命令失败、死循环或取消都不会拖垮整个平台。

**建议文件：** 新建 `src/moumou/sandbox/process.py`、`tools/execution.py`、`tests/test_process_runner.py`；调整现有 shell/Python 工具接线。

### 要做的事

- [ ] 建立公共执行器，统一处理 cwd、环境、超时、stdout/stderr 和退出码。
- [ ] 对需要参数数组的命令使用 subprocess exec；只有明确的 shell 工具接受 shell 文本。
- [ ] 同时排空 stdout 和 stderr；各保留 8192 字节正文，超出部分丢弃，设置截断标记。
- [ ] 超时清理整棵进程树并返回可读错误；外部取消时清理后重新抛出 `CancelledError`。
- [ ] Python 使用当前项目解释器，支持文件执行；代码片段通过 workspace 内临时文件执行并清理。
- [ ] 显式应用 `ctx.env`，校验 timeout 为有限正数；内部超时和框架外层超时留出清理余量。
- [ ] 按阶段 0 的约定跟踪执行产生的文件，不能将所有旧文件一律算作本次产物。
- [ ] 实现目标平台的进程树终止方式；未支持的平台明确报错或在文档声明，不静默降低保证。

### 验收

- [ ] 正常命令成功；非零退出返回 `ok=False`、退出码及有用错误输出。
- [ ] 无限循环达到超时后结束，PID 检查确认父子进程均不存在。
- [ ] 外层取消后能收到 `CancelledError`，进程不残留；测试结束主动清理测试进程。
- [ ] 大量 stdout/stderr 不死锁，输出被截断，保留缓冲有界。
- [ ] 中文输出不损坏；带空格路径、环境变量覆盖和项目解释器路径均正确。

**交付：** PR 2。工具侧代码执行稳定，可以支持后续 Git 和端到端场景。

## 9. 阶段 3：Git 与 worktree 整合

**目的：** 让多个 Agent 独立修改同一个项目，再把成果整合给父任务。

**建议文件：** 新建 `src/moumou/workspace/git.py`、`tools/git.py`、`tests/test_git_tools.py`、`tests/test_git_workspace.py`；保留 `LocalDirs` 作为参考实现。

### 要做的事

- [ ] 实现本地 Git 工具：status、diff、add、commit、log、branch、merge。
- [ ] 使用明确子命令和结构化参数；不要把免审批 Git 工具做成任意 shell 入口，推送单独处理。
- [ ] 实现 `WorkspaceProvider.allocate(project_id, conversation_id, agent_id) -> Path`：相同身份稳定复用，不同身份分配独立 worktree/分支。
- [ ] 为身份到目录/分支的映射增加唯一性处理，避免简单字符替换导致不同 Agent 共用路径。
- [ ] 实现 `integrate(src, dst, artifacts) -> IntegrateResult`，覆盖依赖产物向下传递和子任务成果向父任务整合。
- [ ] 按阶段 0 的约定处理未提交修改、删除和未声明产物；必要时先生成仅包含交付内容的提交，再整合。
- [ ] 对同一目标工作区的并发整合串行处理；共享仓库管理操作增加必要的同步措施。
- [ ] 合并冲突返回 `conflicts` 路径，并采用已约定的恢复方式；不自动以某一方覆盖另一方。
- [ ] 实现安全释放：只操作 Provider 管理的工作区，不删除外部目录；按团队要求保留证据。

### 验收

- [ ] 两个 Agent 修改不同文件，合并后父工作区同时拥有两份成果。
- [ ] 两个 Agent 修改同一位置，报告冲突，之后仍能继续使用目标工作区。
- [ ] 重复 allocate/integrate 不造成目录碰撞或重复破坏。
- [ ] 新增、修改、删除按约定进入整合结果，未声明产物不被意外带入。
- [ ] 不同基础提交、脏工作区、无提交历史等情况返回清晰结果。
- [ ] worktree 新增测试通过；参考实现的既有测试继续保留并通过。

**交付：** PR 3，以及“两个工作区 → 两次提交 → 父工作区整合”的可复现演示。

## 10. 阶段 4：推送与联网搜索

**目的：** 补齐课程要求的外部工具能力。

**建议文件：** 完善 `tools/git.py`；新建 `tools/search.py`、`tests/test_git_push.py`、`tests/test_web_search.py`。

### 要做的事

- [ ] 实现独立 `git_push` 工具，`action_class="git_push"`，由 P0 Gate 在执行前审批。
- [ ] 明确 remote、branch、超时和认证失败的结果；基本版不提供 force push，不把认证信息放入输出。
- [ ] 默认使用本地临时 bare 仓库测试推送，真实 GitHub 验证使用约定的测试分支。
- [ ] 选择一种搜索服务；选择标准是团队可用凭证、结果稳定性、超时和成本，不为比较服务搭建多套后端。
- [ ] 搜索结果包含 `title/url/snippet`；通过现有 `ToolResult.stdout` 返回 JSON 文本，不修改共享结果模型。
- [ ] 配置结果条数、字段长度和请求超时，确保返回 JSON 可解析，不对 JSON 做任意字符串硬切。
- [ ] 缺密钥、限流、网络失败、无结果均返回约定结果；密钥通过配置注入。
- [ ] 将新增工具接入 `default_tools()`，避免调用方重复注册。

### 验收

- [ ] 推送成功、拒绝更新、认证失败都具有可读结果。
- [ ] 经 Gate 执行时，审批拒绝不会触发推送；审批同意才调用工具。
- [ ] 搜索正常返回、空结果、超时、429 和缺密钥均有测试。
- [ ] 常规测试使用模拟网络响应，不消耗真实搜索额度。

**交付：** PR 4，搜索配置示例和真实服务验证记录。

## 11. 阶段 5：全链路联调

**目的：** 证明框架真实使用 P1 实现，而非只证明工具可以单独调用。

**建议文件：** 新增 P1 集成测试和最小示例；使用现有 Engine 注入点。涉及编排行为的改动由 P0 协调。

### 要做的事

- [ ] 通过 `default_tools()` 或 `ToolRegistry` 注入工具，通过 `Engine(workspace=...)` 注入工作区 Provider。
- [ ] 保留 `ask_human`、`submit_plan`、`submit_result`、`submit_revision` 等既有标记工具。
- [ ] 与 P0 用假模型跑通下列场景，先排除执行层问题，再使用真实模型联调。
- [ ] 向 P3 提供测试输入、可判定验收项、预期文件和预期错误；题库与计分归 P3。
- [ ] 运行 P1 测试、全量回归和 ruff；记录环境、命令和实际结果。

### 必须跑通的场景

| 场景 | 预期结果 |
| --- | --- |
| 写代码 → 执行 → 读取输出 | 文件、退出码、输出、artifacts 正确 |
| 两个子任务修改不同文件 → 整合 | 父任务可读取成果并通过验收 |
| 子任务 B 依赖 A 的文件 | B 启动前获得约定产物 |
| 同一位置修改冲突 | 冲突进入返回结果和现有 trace 流程，后续可处理 |
| shell 超时或用户取消 | 框架继续工作，进程不残留 |
| 推送审批被拒绝 | 没有远端写入，任务得到明确反馈 |
| 联网失败 | 返回结构化失败，编排循环可继续 |

**交付：** PR 5，联调记录和供 P3 使用的场景说明。

## 12. 阶段 6：文档与演示

**目的：** 让队友能够接入，让评审能理解你的贡献和实现边界。

### 要做的事

- [ ] 编写 `docs/P1-EXECUTION.md`：模块结构、工具参数、返回值、配置、运行方式。
- [ ] 写清 LocalJail 的应用层边界、任意代码的访问能力、worktree 的共享 Git 元数据。
- [ ] 记录 Windows/Linux 支持范围、进程清理机制、合并与冲突策略。
- [ ] 提交可复现的环境规格和团队约定的依赖锁文件；不提交本地密钥和运行产物。
- [ ] 准备约 3–5 分钟演示：文件工具 → 运行代码 → 两 Agent 分支整合 → 审批/失败处理。
- [ ] 保留开发提交与 PR 证据，准备课程要求的 Git 历史截图。

### 最终交付清单

- [ ] P1 实现代码与配置示例。
- [ ] 单元测试、契约回归、工作区整合测试、Gate 推送审批集成测试。
- [ ] 测试记录：提交 SHA、平台、版本、命令、结果及剩余限制。
- [ ] P1 架构与使用说明、可复现演示步骤。
- [ ] 已合入或可供队友合入的提交/PR，以及贡献记录。

## 13. 可选扩展和压缩工期原则

**Docker 扩展：** 基本版完成后，再考虑执行进程的容器隔离。仍需处理挂载范围、网络、凭证及资源限额，不能仅启动容器就宣称完全隔离。如果阶段 0 确认强隔离是验收硬要求，应提前调整范围和工期。

**工期不足时的顺序：** 先保住路径检查、进程可靠性、Git 整合、推送、搜索和联调；减少补丁工具的扩展选项、搜索后端数量及额外平台支持。课程必需功能不因工期压缩而悄悄删除。

**不要重复开发：** P0 已有审批、trace、编排和记忆；P1 使用这些接口，发现缺口时提供复现和建议，不另起一套。

## 14. 推荐验证命令

在仓库根目录、已激活的专用 Conda 环境中执行。新增测试文件创建后再运行对应文件名。

```powershell
python -m pytest tests/test_tool_contract.py tests/test_workspace.py -q
python -m pytest -q
python -m ruff check src tests
python -m moumou.demo.skeleton
```

现有 `test_workspace.py` 主要验证 `LocalDirs`，新增 worktree 实现必须另有专门测试。现有工具契约测试会检查新增工具的协议字段，但不能替代 Git 和搜索的功能测试。

## 15. 依据与版本

本计划基于只读检查，尚未执行环境安装、测试或产品代码修改。

- 仓库版本：[e3b9e7a](https://github.com/Greensheep04/moumou-Agent/commit/e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9)。
- [Tool 协议与默认审批](https://github.com/Greensheep04/moumou-Agent/blob/e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9/src/moumou/contracts/tool.py)。
- [WorkspaceProvider 与 IntegrateResult](https://github.com/Greensheep04/moumou-Agent/blob/e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9/src/moumou/contracts/workspace.py)。
- [工具参考实现](https://github.com/Greensheep04/moumou-Agent/blob/e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9/src/moumou/tools/builtin.py)。
- [工作区参考实现](https://github.com/Greensheep04/moumou-Agent/blob/e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9/src/moumou/adapters/workspace.py)。
- [工具契约测试](https://github.com/Greensheep04/moumou-Agent/blob/e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9/tests/test_tool_contract.py)。
- 本地材料：用户提供目录中 `md/P1-执行与工具层(1).md`、`md/TASKS(1).md`，以及课程要求粘贴文本。

## 16. 实施进度记录

| 阶段 | 状态 | 实际投入 | 提交/PR | 验证结果或阻碍 |
| --- | --- | --- | --- | --- |
| 0 | 未开始 | — | — | — |
| 1 | 未开始 | — | — | — |
| 2 | 未开始 | — | — | — |
| 3 | 未开始 | — | — | — |
| 4 | 未开始 | — | — | — |
| 5 | 未开始 | — | — | — |
| 6 | 未开始 | — | — | — |
