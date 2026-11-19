# 共享接口

[简体中文](INTERFACES.md) · [English](INTERFACES.en.md)

本文面向接入模块的开发者，说明需要实现的方法、输入输出、调用时机和验证方式。共享端口位于 `src/moumou/contracts/`。接口变更应与消费者对齐，尽量保留既有调用形式，并更新契约测试。

## 0. 通用约定

### 异步与失败

Agent 执行、异步工具、Channel 和 HumanIO 共用事件循环，存储端口则提供同步方法。同一适配器可能被多个 Agent 调用：

- 慢操作使用 `asyncio.create_subprocess_exec` 或 `asyncio.to_thread`。
- `time.sleep`、同步 `requests` 等会阻塞整个事件循环。
- 工具/模型调用有各自超时；取消时适配器负责清理子进程和待等待资源。交互式节点限制是协作式的，不对人类等待提供统一强制截止时间。

优先返回结构化失败，让模型得到可理解的错误。`ToolResult.error` 会进入模型上下文，建议用简短准确的英文描述现象；完整异常留在 trace。异常仍可能由框架捕获，但不能代替适当的业务失败结果。

### 注入实现

`Engine` 构造函数是工具、工作区、Channel、HumanIO 和记忆适配器的注入点：

```python
from pathlib import Path

from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools

registry = ToolRegistry()
registry.register_all(*default_tools())  # 已含 Git/搜索工具，避免重复注册

engine = Engine(
    llm,
    tools=registry,          # P1
    workspace=MySandbox(),   # P1
    bus=MyBus(),             # P2
    human=MyHuman(),         # P2 / P3
    settings=Settings(project_id="demo", root=Path("runs/demo")),
)
```

Trace 通过 `Recorder` 收集。默认 Engine 同时写 `trace.jsonl` 和 SQLite `trace.db`。长期记忆使用独立 `memory.db` 中的 `SqliteMemoryStore`，可通过 `Engine(memory=...)` 替换；Recorder 不提供长期记忆。

### 验证入口

替换实现后，应运行相应端口契约和框架回归：

```bash
pytest tests/test_tool_contract.py -q      # P1
pytest tests/test_channel_contract.py -q   # P2
make unit-test                           # 框架回归，含 P3
```

`make eval` 使用配置模型和真实人类交互运行老师任务，不是框架自测。单组件只读检查可以直接实例化，不必启动整个 Engine：

```python
import asyncio
from pathlib import Path

from moumou.contracts import ToolContext
from moumou.tools.git import GitTool


async def main():
    ctx = ToolContext(agent_id="me@t1", task_id="t1", workspace=Path("."))
    print(await GitTool().run({"operation": "status"}, ctx))


asyncio.run(main())
```

## 1. `Tool`（P1）

工具负责执行具体操作，审批、计时、trace 和外层超时由框架负责。

```python
class Tool(Protocol):
    name: str                 # 唯一 snake_case 名称，如 "shell_exec"
    description: str          # 告诉模型何时使用
    params_schema: dict       # function calling 的 JSON Schema
    action_class: str         # 决定审批类别
    timeout_s: float          # 框架外层超时

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

### 输入

`ToolContext`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `agent_id` | `str` | 调用者，如 `python_dev@t1.code` |
| `task_id` | `str` | 所属任务，如 `t1.code` |
| `workspace` | `Path` | 该 Agent 自己的读写目录 |
| `env` | `dict[str, str]` | 附加环境变量，可为空 |

`args` 由模型提供，不能假定字段齐全、类型正确，应先校验。

### 输出

| `ToolResult` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | `bool` | 必填 |
| `stdout` / `stderr` | `str` | 给模型的输出；过长时截断并标记 |
| `exit_code` | `int \| None` | 执行类工具退出码 |
| `error` | `str \| None` | `ok=False` 时给出原因，原样返回模型 |
| `artifacts` | `list[str]` | 产物路径，相对工作区，用于整合 |
| `truncated` | `bool` | 是否截断 |

同一模型响应中的多个 tool call 顺序执行，再回传结果。不同 Agent 仍可能并发调用同一工具实例，应避免不受保护的全局可变状态。

### 操作类别

| `action_class` | 含义 | 默认审批 |
| --- | --- | --- |
| `fs_read` | 只读 | 否 |
| `fs_write` | 写文件 | 否 |
| `fs_delete` | 删除文件 | 是 |
| `shell` | 任意命令 | 是 |
| `git_commit` | 提交 | 否 |
| `git_push` | 推送远端 | 是 |
| `network` | 网络访问 | 否 |
| `install` | 安装依赖 | 是 |

这是通用默认策略；正式任务会话另外启用 `git_commit` 审批，覆盖暂存/提交操作。角色选择工具子集不能自行修改审批策略。

### 实现约定

| 项目 | 约定 |
| --- | --- |
| 参数 | 缺失或错误类型返回 `ok=False`，在 `error` 说明期望 |
| 路径 | 解析后保持在 `ctx.workspace` 内，处理 `..`、绝对路径和符号链接 |
| 输出 | 超过约 8KB 时截断，防止单次读取挤满上下文 |
| 子进程 | 超时/取消时清理进程及其子进程 |
| 审批和日志 | 由框架统一处理，工具不重复申请 |

基础实现见 `src/moumou/tools/builtin.py`。路径检查属于应用边界，不会把任意执行代码变成操作系统沙箱。

## 2. `WorkspaceProvider`（P1）

每个 Agent 使用自己的目录，通过显式产物整合交换文件。

```python
class WorkspaceProvider(Protocol):
    def allocate(self, project_id: str, conversation_id: str, agent_id: str) -> Path: ...
    def release(self, path: Path) -> None: ...
    def integrate(self, src: Path, dst: Path, artifacts: list[str]) -> IntegrateResult: ...
```

| 方法 | 用途 | 约定 |
| --- | --- | --- |
| `allocate` | 任务运行前分配工作区 | 返回存在目录，同一 `agent_id` 应得到同一路径 |
| `release` | 由宿主在合适时机结束工作区生命周期 | 只清理自身根目录；需保留产物证据时不可直接删除 |
| `integrate` | 子任务验收后或依赖任务执行前 | 将显式 `artifacts` 从 `src` 整合到 `dst` 并报告结果 |

参考实现复制文件，Git 版本按对应分支整合。冲突是正常结果，不应用异常代替：

```python
class IntegrateResult(BaseModel):
    ok: bool = True
    copied: list[str] = []
    conflicts: list[str] = []
    error: str | None = None
```

子任务自身验收通过后，即使向父任务整合产生冲突，它仍可保持完成；冲突写入其 `error` 与 trace，父任务从依赖报告读取并处理。

### 声明结果文件与发布

`submit_result.files` 可声明任务工作区中已存在的普通文件，包括本次未修改的文件。Worker 整批校验声明，并在验收后复检；通过验收后才加入 `node.artifacts` 并整合。文件缺失、目录、绝对/越界路径、非法路径及 `.git` 项会让整个结果以可纠正的 `invalid_result_files` 工具反馈被拒绝，不发布其中一部分。

声明产物与原工具跟踪路径去重合并，包括原先记录的删除路径。声明校验不会丢掉这些路径，也不会自动导出整个工作区。参考实现见 `src/moumou/adapters/workspace.py`。

## 3. `Channel`（P2）

Channel 承载 Agent 间以及人类与 Agent 间的消息。

```python
class Channel(Protocol):
    async def publish(self, msg: Message) -> None: ...
    def subscribe(self, agent_id: str, cb: Callable[[Message], Awaitable | None]) -> None: ...
    def history(self, scope: Scope, limit: int = 50) -> list[Message]: ...
```

协议见 `src/moumou/contracts/message.py`。

| `Message` 字段 | 说明 |
| --- | --- |
| `id` / `ts` | 自动生成 |
| `project_id` / `conversation_id` | 范围 |
| `task_id` | 可为空 |
| `sender` | Agent ID，或固定值 `"human"` |
| `recipients` | 接收者列表；空列表表示广播 |
| `kind` | `chat` / `delegate` / `report` / `ask_human` / `approval` / `steer` |
| `content` / `meta` | 正文与附加数据 |

`Scope` 由 `kind`（`dm` / `group`）、`project_id`、`conversation_id` 和 `members` 组成。人类使用固定 ID `human`，和 Agent 一样寻址，私聊与群聊可共用消息机制。

| 项目 | 约定 |
| --- | --- |
| 持久化 | `publish` 先持久化再投递，以便重启后保留 |
| 回调隔离 | 慢订阅回调不能阻塞消息总线 |
| 顺序 | `history` 按时间升序，只返回对应会话消息 |
| 私聊 | 不投递给无关成员 |

`src/moumou/adapters/bus.py` 是内存参考实现，不能据此宣称 P2 持久消费已经完成。

## 4. `HumanIO`

澄清、权限审批和质量/角色复核共用一个入口：

```python
class HumanIO(Protocol):
    async def ask(self, req: HumanRequest) -> HumanResponse: ...
    async def notify(self, msg: Message) -> None: ...
```

### 输入与输出

| `HumanRequest` 字段 | 说明 |
| --- | --- |
| `id` | 请求 ID，答复必须回填 |
| `task_id` / `agent_id` | 提问者与所属任务 |
| `kind` | `approval` / `question` / `review` |
| `summary` | 单行标题 |
| `action` / `action_class` | 审批的工具名与操作类别 |
| `payload` | 完整参数，供查看 |
| `options` | 允许值；空列表表示自由文本 |
| `context` | 背景和原因 |
| `project_id` / `conversation_id` / `role` | 可恢复问题的显式范围；新增字段默认空字符串，兼容旧构造方式 |

| `HumanResponse` 字段 | 说明 |
| --- | --- |
| `request_id` | 回填原请求 ID |
| `decision` | `approve` / `reject`；自由回答可为 `None` |
| `text` | 答案或附加说明 |

| 形态 | 展示内容 | 交互 |
| --- | --- | --- |
| `approval` | 工具和完整参数 | 显式批准/拒绝 |
| `question` | 原问题与背景 | 非空回答或允许选项 |
| `review` | 已保存交付和质量证据 | 显式判断及可选反馈 |
| `steer`（Channel 消息） | 运行中上下文 | 调整当前方向 |

正式评测通过 `eval.human.ConsoleHuman` / `FileHuman` 保存真实请求和答复。批准工具不等于接受产物质量。`adapters/human.py` 的 `ScriptedHuman` 仅用于确定性框架测试，不能作为正式老师题或补充题的答案来源。

### 可选恢复能力

```python
class ResumableHumanIO(Protocol):
    async def resume(self, req: HumanRequest) -> HumanResponse: ...
```

只恢复原范围内的 `question`。控制台/文件接口核对原 ID 和正文，取回已消费答复而不重复回调，或继续等待原答复。尚未消费答复的回调如会改变外部状态，须按 request ID 幂等。工具审批和质量复核不能被回放成授权。执行器存在待恢复记录但适配器不支持时明确失败。

## 5. `TraceStore` / `MemoryStore`（P3）

```python
class TraceStore(Protocol):
    def append(self, e: TraceEvent) -> None: ...
    def query(self, task_id: str) -> list[TraceEvent]: ...   # 按时间升序

class MemoryStore(Protocol):
    def write(self, rec: MemoryRecord) -> str: ...
    def search(self, scope: str, query: str, k: int = 5) -> list[MemoryRecord]: ...
    def all(self, scope: str) -> list[MemoryRecord]: ...
    def update_score(self, rec_id: str, delta: float, used: bool = True) -> None: ...
    def evict_candidates(self, scope: str, threshold: float = 0.3) -> list[str]: ...
    def mark_deleted(self, rec_ids: list[str]) -> None: ...
```

### TraceEvent

| 字段 | 说明 |
| --- | --- |
| `span_id` / `parent_span` | 重建调用树 |
| `task_id` / `agent_id` | 所属任务与执行身份 |
| `kind` | `task` / `llm` / `tool` / `approval` / `question` / `steer` / `delegate` / `role` / `acceptance` / `memory` |
| `name` | 具体名称，如 `tool:write_file` |
| `t0` / `t1` | 开始/结束；`duration_ms` 为派生值 |
| `status` | `ok` / `error` / `cancelled` |
| `tokens_in` / `tokens_out` | 用量字段；总量是否完整取决于覆盖与真实提供方证据 |
| `payload` | 附加数据 |

P2 等消费者应识别 `result_rejected`（最终文件声明无效）和 `declared_files_published`（通过验收的声明文件已发布）。两者区分可纠正反馈与发布事实，但都不单独证明老师目标质量。

Recorder 自动采集嵌套 span 父关系，异步上下文相互隔离，Agent 身份写在顶层字段。不会补造旧记录的空父关系。`parent_span` 是调用层级，不是 TaskNode 的委派父关系。Worker 的 `execution` span 包含一次真实尝试及依赖物化，不是从任务分配到交付的总墙钟。

### MemoryRecord 与范围

`project_id` + `role` 决定范围，`task_id` 保存来源；`situation` / `action` / `outcome` / `lesson` 保存内容，`score` 和 `uses` 用于演化。

正式团队的稳定 role ID、私有目录在两题间延续，因此 project:role 对应同一逻辑 Agent。场景和运行数据库隔离团队。通用 Engine 的 `role@task` 实例仍共享 project-role 记忆，不能宣称所有运行实例都独立。

`query` 必须保留可重建父子关系的事件，`search` / `all` 只读取请求的 project-role 范围，写入应跨重启持久化。当前实现：

- `src/moumou/store/trace.py` 的 `SqliteTraceStore` 保存完整 TraceEvent，精确过滤 task ID，按 UTC 开始时间和 `span_id` 排序。同内容导入幂等，不允许冲突记录覆盖。
- `src/moumou/trace/recorder.py` 保留 `span`、`record`、`query`、`all_events` 和 `close`，兼容原单参数 JSONL 用法。
- `src/moumou/memory/store.py` 提供 SQLite 范围过滤、评分、软删除和字符 bigram 召回。阶段快照由 `eval/session_evidence.py` 保存，不改变长期身份策略。

### Trace 投影与恢复

默认 Engine 接入：

```python
self.trace = Recorder(root / "trace.jsonl", sqlite_path=root / "trace.db")
```

单独使用 `Recorder(path)` 仍只写 JSONL；指定 `sqlite_path` 时，启动严格导入既有 JSONL，补全投影中缺失事件，不重写源文件。每次先写入并刷新 JSONL，再更新 SQLite，两者不共享原子事务。

健康状态下 `query(task_id)` 使用 SQLite 时间顺序，`all_events()` 保留 JSONL 记录顺序。嵌套 span 可能按不同于开始时间的顺序结束，应按 `span_id` 对比完整事件。

运行中 SQLite 读写失败通过 `projection_error` 和警告公开，该实例暂停投影并从完整 JSONL 按时间查询。重开可从有效日志补导入；无效/冲突记录、外部编辑或数据库额外行不会自动修复。投影失败不会取代 span 原来的业务异常或取消。

JSONL 与 SQLite 必须使用不同文件。`Recorder.close()` 关闭日志和投影连接，`Engine.close()` 关闭默认 Recorder 和记忆存储；外部注入的记忆实例由调用者关闭。SQLite 实例属于创建线程，其他线程须使用自己的连接。

### 题源与连续团队

`eval.cases.load_cases(path)` 按 README 顺序加载 Markdown 场景，保留原角色、任务顺序和来源哈希。默认 `eval/tasks` 为老师三个四人组、六根目标；`eval/supplementary` 保留同格式十五自建题，八组，最后一组只有一题。补充题须显式选择，不能自动混算。

`python -m moumou` 与 `python -m moumou.eval.cli` 是同一正式协议，`--tasks` 选择题源，不再有专属题库格式入口。`eval.team` 固定身份、连续私有角色工作区和同角色执行锁。Task 2 接收真实 Task 1 结果、交付文件和原对话归档，不虚构历史，也不是全新独立尝试。实际题数定义计划分母。

只有直接依赖及显式 `artifacts` 传递文件；不会自动分享父目录和传递依赖链。子目标应说明自身工作和输入，原祖先目标另作为范围化引用保留；这不共享父任务私有人答或文件。缺失输入应通过 `submit_plan` 请拥有者明确交付并声明依赖，或调用 `ask_human`；不能搜索父级、同级或其他运行目录绕过隔离。

`TeamContextBuilder` 固定保留完整原角色、当前目标、全部 `Acceptance` 和同任务已答信息，不修改源对象。可选历史、依赖、结论和经验先受限，完整辅助内容保存在私有 `.evaluation/context/<digest-prefix>.json`，新文件名为 32 位前缀、引用为完整 SHA-256，同名不同内容拒绝，不重命名旧归档。必需内容超限时抛 `ContextAdmissionError`，不切断目标；框架遗漏指令不能直接归为模型不服从。

团队适配器在保存并校验上下文归档或 `.evaluation/prior-dialogue.txt` 后，调用提供方的可选
`register_private_evidence(workspace, relative_file)`。GitWorkspaces 只把这些具体文件路径加入
受管仓库的 `info/exclude`，不忽略整个目录、不改项目或全局忽略设置，原脏目标检查保持不变。
普通文件、未登记的 `.evaluation/` 文件以及已暂存/已跟踪的修改仍阻止交付；已跟踪证据要求显式修复，
不会自动取消跟踪，已有对话副本在覆盖前先检查。证据仍可读取，但不能声明或集成为交付产物；
包含私有证据的工作树拒绝 release，
避免清理时删除已经被 Git 排除的证据。不实现此可选钩子的提供方保持原行为，默认工作区和总线不变。

### 人类与模型证据

TaskRepo 按 project/conversation/task/role 记录原 request ID/正文、response 和时间戳。`record_human_request`、`record_human_response`、`human_exchanges` 支持同任务重新进入，不恢复缺少配对的 tool call。缺失或损坏记录明确失败，工具审批重新进行。Gate 拒绝说明进入当次 ToolResult 的 `stderr`；ID 错误或没有显式决定均不是有效审批。记录本身不能鉴别人类手工输入或已授权辅助操作的身份来源。

`RecordingLLM` 保留请求、响应/失败、阶段、调用 ID 与可测用量；`ConsoleHuman` 和 `FileHuman` 实现真实 HumanIO。新问题保存原始请求和待答队列；文件答复须匹配 request ID，审批/复核必须明确 approve/reject，问题必须非空并满足选项约束。消费后保留原答复证据。

复用真实旧业务答复或明确常设工具权限时，必须保留原来源和授权范围；这不是新的人类手工作答，也不能升级为质量同意。详见[安装说明](SETUP.md)的文件答复格式。真实凭据及常见密钥形式应脱敏，不提交运行记录。

### 场景文件和指标范围

每场全新正式会话的 `test.json` 保存总体计划、题源、代码、环境、配置和场景索引，`active.json` 指向当前阶段。每个场景保留：

- `task_1-result.json`、实际存在时的 `task_2-result.json`；
- `dialogue.jsonl` 和 `human/` 中的原请求、待答、已消费答复与事件；
- `trace.jsonl`、`trace.db`、任务和记忆数据库；
- 搜索配额、请求、缓存来源与用量；
- 各成员私有工作区；
- 新交付的 `artifact_snapshot`、`memory_snapshot`、`failure_snapshot`；
- 收尾时的 `state_snapshot`，可含未完成节点，仅为诊断，不增加交付、成绩或计划目标。

单题结果只计该根目标的子树和阶段调用；场景累计值包含此前题目，不能当作 Task 2 独立用量。定义见[指标说明](metrics.zh-CN.md)。

manifest、任务结果和对话不构成跨文件事务。强制终止可能遗留原始 `running`，应保留并附停止/进程证据，不伪造正常收尾。正式 CLI 不复用已有输出目录，也不自动回答 pending 问题；问题续接能力与整场 CLI 续跑不是同一件事。

### 搜索计量

`SearchBudgetTool` 只包装注册的 P1 `web_search`，保留 Tool 契约。每场景初始六次 Tavily HTTP 新请求由四个角色、两题共享，另计工具调用、提供方尝试、缓存、去重和拒绝；缓存保留原检索时间。额外配额须有实际人类批准，按 `approval_id` 重放相同额度幂等，冲突复用拒绝。通用外部回调仍须自行按 request ID 幂等，不能宣称任何回调都恰好执行一次。

搜索拒绝本身不证明向人提问或获得批准。学校模型额度与搜索计数分别处理。

### 离线报告、复核和回放

恢复、trace 和阶段证据按实际源码版本分别验证，结果见[性能与验证报告](PERFORMANCE.md)。历史回归不能替后续版本背书，实际任务质量和消费者签收仍需自己的证据。交付清单见[交付要求](DELIVERY.md)。

`eval.report.build_report(root)` 校验保存会话和权威单题结果；`generate_report(root, output=None)` 输出派生 `.md` / `.txt`，不需要模型密钥、不调用模型、不重跑测试或恢复任务。未决质量不转成成功。未被索引的任务结果或身份不一致会报完整性错误，不会悄悄计入或删除。历史 `midtest.json` 仅只读兼容，不是另一入口。

`eval.review` 通过真实 Console/File HumanIO 在 `reviews/<UUID32>/` 追加复核，绑定原运行、代码、题源、任务结果和产物哈希。重复复核须 `--supersedes` 指定当前末端。原始结果保持不变，报告另给 `raw_quality`、有效质量、`quality_source` 和复核历史。匹配 I/O 和哈希说明记录一致，不是密码学身份认证。旧产物快照缺失保持 `unsupported_historical_artifact_snapshot`。

最终复核发布使用 `reviews/.publish.lock`。锁已存在时须明确检查并处理并发/中断写入者，不自动删除旧锁或绕过当前末端校验；人工等待期间不占发布锁。

`eval.analysis.build_analysis/generate_analysis` 支持老师格式和历史 groups。`eval.replay.build_replay/generate_replay` 输出全新派生 JSON，提供场景+任务身份、原节点/TraceEvent、来源/行哈希和人类事件索引，区分已答、待答、暂存答复。它们消费已存阶段证据，不读取 live memory.db 或可变工作区冒充旧快照；导出不代替 P2 签收。缺失父关系、状态、质量保持缺失/未知，外部停止记录不替换原 manifest。

## 质量相关可选接口

- `read_file`仅传`path`保持旧有8KB预览。显式`offset/limit`开启UTF8字节分页，`limit`为1..8192；`stdout`仅含正文，`stderr` JSON返回`offset/next_offset/file_size/bytes_returned/limit/eof`。必须跟随返回游标，不能自行按limit累加；不合法游标及无法容纳下一个完整字符的页会失败。
- 正式评测注册`source_history`（`fs_read`）与`read_source`（`network`）。场景级`SourceEvidenceStore`只共享公开证据，不开放角色私有目录。来源/原文哈希只能证明捕获字节，不能证明事实；原检索时间与本次读取/返回时间分别保留。
- `SearchBudgetTool(..., source_store=...)`可选记录公开检索，省略时保持既有行为，计次、缓存与人工追加规则不变。
- `TaskRunner`在根提交验收前及发布前调用可选上下文hook `validate_delivery(node, workspace, result)`。老师上下文检查显式文档引用闭包；无hook的普通上下文保持旧契约。拒绝沿用可纠正文件反馈，不合成文件或质量批准。
- `FileSession`只对读取时的消失/权限错误及精确Windows扩展前缀解析竞态做三次有界重试，每次重查路径。持续失败报告操作、路径及可获得的原生错误字段；真实alias/越界和未知校验错误仍立即拒绝。

Replay 在把重新读取的任务和有效复核合并前检查报告中的规范 JSON `result_content_sha256`，另保留原始字节哈希；校验 TraceEvent 必需身份、类型、时间和状态，历史可选字段按原样保留。派生回放文件使用独占创建，不覆盖并发写入者已有证据。

底层 `run_once` / `run_all`、worker、telemetry、metrics、snapshot 等 API 保留用于框架检查和历史独立实验，其 `manifest.json` / `result.json` 语义不能混入当前连续团队得分，也不定义第二个正式题源或 CLI。

## 6. `TaskRepo`

评测读取这些参考方法：

```python
def get(task_id) -> TaskNode | None
def children(task_id) -> list[TaskNode]
def by_status(status) -> list[TaskNode]
def ready(limit) -> list[TaskNode]
def transition(task_id, to, reason) -> None
def event_log(task_id) -> list[dict]
def set_result(task_id, result) -> None
def add_artifacts(task_id, artifacts) -> None
def record_human_request(node, request) -> None
def record_human_response(node, response) -> None
def human_exchanges(node, *, require_answered=True) -> list[dict]
```

状态更新使用 `transition`，在同一事务更新任务表和审计事件，不能绕过后破坏报告时间线。

问答按 project/conversation/task/role 限定范围，原 Agent ID 是来源。请求先于等待提交，答复必须匹配 request ID，不能覆盖冲突答案。默认 `SqliteTaskRepo` 另外提供 `all_tasks()` 供 Engine 遍历；TraceStore 无须新增任务枚举方法。

## 7. 数据模型速查

从 `moumou.contracts` 导入：

| 模型 | 文件 | 用途 |
| --- | --- | --- |
| `TaskNode` | `task.py` | 依赖、验收、预算和状态 |
| `Acceptance` | `task.py` | 机器可检查条件 |
| `Budget` | `task.py` | 步数、Token、墙钟和重试 |
| `ContextPolicy` | `context.py` | 上下文/记忆阈值 |
| `RoleSpec` | `agent.py` | 角色、说明、工具和理由 |
| `AgentInstance` | `agent.py` | 运行身份及范围 |
| `Message` / `Scope` | `message.py` | 消息与寻址 |
| `Tool` / `ToolContext` / `ToolResult` | `tool.py` | 工具协议及输入输出 |
| `HumanApprovalFeedback` | `tool.py` | 一次真实批准请求的范围化备注 |
| `IntegrateResult` | `workspace.py` | 文件整合结果 |
| `TraceEvent` | `trace.py` | 执行事件 |
| `MemoryRecord` | `memory.py` | 长期经验 |
| `HumanRequest` / `HumanResponse` | `human.py` | 人类交互 |
| `Plan` / `RoleDraft` / `SubtaskSpec` | `plan.py` | 模型提出的拆分计划 |

字段变化后运行 `pytest tests/test_contracts.py` 和相关适配器回归。即使 schema 兼容，也仍需实际运行及消费者检查；测试文件存在不等于已经通过。

## 显式交接、审批反馈与来源版本

模型的 `submit_plan` 调用须在每个子任务中显式填写 `deps`：独立工作用 `[]`，需要文件的消费者列出生产者 key。遗漏在创建子任务/角色前收到可纠正的 `invalid_plan`；直接 Python `Plan`/`SubtaskSpec` 调用仍保留空数组默认值。文字里的文件名、导入来源的历史 task ID 不创建交接边；模型显式填写错误的空数组仍需要计划复核。

`ToolResult.human_approval` 可选携带 `HumanApprovalFeedback`，绑定真实批准请求、工具调用、任务、角色与会话范围。Gate 清除工具伪造的批准元数据，上下文单独呈现真实 NOTE，保持原 stdout/stderr、错误和分页 JSON 不变。备注不是常设权限、新搜索额度、问题答复历史或人工质量批准；拒绝路径保持原语义。

`search_files` 的 stdout 仍为有界 JSON 匹配列表。stderr JSON 提供 `files_started`、单文件上限、已观测的大文件跳过数及有界相对路径样本、`walk_exhausted` 和 `output_truncated`。计数仅覆盖已访问条目；迭代结束不证明不可读或过滤条目被覆盖，空匹配不证明被省略内容不存在。

新 `SourceEvidenceStore` 将实际抓取的 raw/text 和规范化检索元数据按完整 SHA256 不可变保存，另用最新 URL 指针缓存。`raw_blob`、`text_blob`、`document_version`、`retention_status` 读取精确历史版本；未知 hash 不返回其它内容，损坏记录报错。新 GET 即使字节相同也有新的抓取身份，缓存/分页保留原身份与时间。旧库仅核验并回填当下还存在的一行，明确旧来源标记，不能恢复早已覆盖的历史。存下原文不等于事实已经核验。

`read_source` 的 offset/limit 按提取文本字符计算，与 `read_file` 的字节分页不同；应分别跟随各自的 `next_offset`，核对返回的原文版本身份。

### 范围化祖先目标接口

`context.lineage.collect_assignment_references(repo, node)` 只用既有 `repo.get` 沿当前任务的 `parent_id` 链读取，返回从根到直接父任务的不可变 `AssignmentReference` 元组。记录只含 task/project/conversation/parent 身份、depth、role 和完整原 goal；序列化附来源标记及 UTF-8 `goal_sha256`。缺失父节点、跨范围、环、身份或深度不一致明确拒绝，不读取依赖、上一阶段、私有问答或文件来补链。根任务返回空元组。

`ContextBuilder.build(..., assignment_references=None)` 和 `TeamContextBuilder.build` 支持该可选关键字，旧直接 Python 调用保持兼容。实际 Worker 为委派任务收集完整链并传入；自定义 builder 不支持该参数，或返回的固定开场未完整保留原引用时，在模型调用前失败。当前子目标、角色及验收不被改写。完整引用先计入必需预算，不能被可选内容裁剪或仅替换成归档链接；老师归档保存精确记录，只有引用时也生成归档。刷新、重入和压缩保留固定开场，trace 单列引用 ID 与哈希，表示准入而非模型实际采用。

祖先原目标不是父任务完整上下文。父任务私有人答、审批、结果、记忆、结论和文件不会继承，也不增加搜索或操作权限。上一阶段即使在 Task 2 的 `deps` 中，也不是该根任务的祖先。目标之间的冲突保持可见，不自动改成新要求或人工决定。

### 本地字面引文核对接口

`tools.source_quote.VerifySourceQuoteTool(store, *, workspaces=None)` 注册为 `verify_source_quote`，操作类别为 `fs_read`。可信操作员可在构造时提供实际路径集合，或注册工具、完成 `configure_team` 后调用一次 `bind_workspaces(team.workspaces.values())`，然后才提交任务。绑定只接受精确、规范且无 alias 的角色路径；不猜目录前缀，适用于 LocalDirs 与受管 Git 工作树。未绑定、其它路径和重复绑定拒绝。模型参数不能修改绑定或选择来源库。

参数仅为小写完整 SHA-256 `metadata_sha256`、非空 `quote` 和可选 `mode`（默认 `exact`，或 `collapse_whitespace`）。引文最多 4096 个 Unicode 码点且 UTF-8 不超过 16384 字节；自贴原文、path、namespace 等额外参数拒绝。工具调用当前库的 `document_version` 校验元数据、索引及 raw/text 哈希，要求来源已登记；不读取最新缓存指针，不发 HTTP、不搜索、不记录新的读取事件，也不消耗 Tavily 次数。

| `status` | `ToolResult.ok` | `lexical_match` | 含义 |
| --- | --- | --- | --- |
| `matched` | `true` | `true` | 该精确版本存在字面匹配 |
| `not_found` | `true` | `false` | 已检查该版本，未找到该字面串；不证明主张为假 |
| `unknown` | `false` | `null` | 版本缺失、损坏、未登记或身份无法安全返回，不回退其它版本 |
| `rejected` | `false` | `null` | 参数或工作区绑定不合法，未作为事实核对结果 |

`exact` 保留每个码点；`collapse_whitespace` 仅把连续 Unicode 空白折叠为一个普通空格并去掉两端空格，不改数字、日期、大小写、标点或 Unicode 组合。`coordinates` 给出首次出现的零基半开 `[start,end)` 码点坐标，并明确是保存的提取文本还是空白归一后的派生文本，不是 HTML 字节偏移或出现次数。输出保留原来源/版本哈希、原抓取时间与本次 `checked_at`，含比较文本和引文哈希；相应比较文本摘录最多 360 字符，`contains_entire_match` 明示是否含完整匹配，总 JSON 不超过 16 KiB。

字面 `true`、工具成功、事实正确和人工接受分别判断。来源文本仍不可信；引文存在不证明归属、日期/指标口径、完整性或交付质量。缺失证据保持未知，不能比较模型自贴的两段文字就声称核实来源。

### 实际文件引文与交付绑定

`tools.artifact_quote.VerifyArtifactQuoteTool(store, *, workspaces=None)` 注册 `verify_artifact_quote`（`fs_read`）。可信调用方在 `configure_team` 后一次性绑定 `team.workspaces.values()`；模型不能更换工作区或来源库。参数为 `metadata_sha256`、相对 `path`、Unicode 码点 `start`/`end`（零基半开区间），可选 `mode` 和 `expected_artifact_sha256`。不接受 `quote` 或伪造 receipt。只读当前角色自己的普通 UTF-8 文件，拒绝目录、私有凭证路径、alias/reparse point 和硬链接；文件最多 1 MiB，切片最多 4096 码点且 UTF-8 不超过 16384 字节。

输出包含整文件 SHA-256、字节/字符数、实际切片哈希、码点及 UTF-8 字节坐标，并继承精确来源版本、比较哈希和字面三态。检查前后文件必须稳定；之后编辑会使旧整文件身份失效。JSON 最多 16 KiB，不返回正文。`audit_records()` 返回工具自有、深拷贝且最多 256 条的记录及 `dropped_records`；模型返回的文字不是该账本。未找到引文是有效的字面否定，不等于事实为假。

`eval.quote_delivery.capture_artifact_quote_evidence(scenario_dir, snapshot_ref, task, journal, *, task_ids, secrets=())` 使用受信任的当前任务树和上述账本，校验不可变快照及最终整文件、切片和来源 receipt 结构。阶段文件 `task_N-artifact-quote-evidence.json` 一次写入，拒绝覆盖；`task_result.artifact_quote_evidence` 保存路径、SHA-256、状态和计数，先于人工质量请求。`matched`/`not_found` 仅用于仍对应最终字节的有效记录；前阶段或已变整文件列为 `unapplied`，缺失/损坏证据列为 `unknown` 或 unavailable。字节相同的子任务副本保留原 actor/task/path，标明仅内容身份，不推断主管读取。账本丢失使证据 incomplete；没有合法账本时计数为 null。`quotation_coverage`、`factually_correct` 和 `quality_pass` 始终 null。

### 依赖凭证上下文

可信 CLI 在绑定实际团队工作区后调用一次 `TeamContextBuilder.bind_artifact_receipts(repo, artifact_verifier.audit_records)`，模型不能替换仓库或账本 getter。`eval.quote_handoff.summarize_dependency_checks` 只使用当前仓库中同项目、同会话、状态为 `DONE` 的直接依赖。文件必须同时列入结构化 `result.files` 和已接收 `artifacts`；显式直接依赖可以是上一根任务，仅凭历史任务名称或传递关系不产生访问权。

辅助模块只读取接收者自己工作区中已声明、已整合的 UTF-8 文件，将当前整文件及切片身份与工具自有凭证核对。它不打开生产者私有文件，不分享私有人答、审批、记忆、工具历史、来源正文或原始引文。文件身份相同不证明接收者阅读过；后续编辑使旧凭证不再适用，部分切片匹配不证明完整引文覆盖、事实正确或人工批准。

开场摘要包含有限的文件身份、核验计数及示例凭证 ID，最多 2000 字符和 8000 UTF-8 字节。完整、简略、空摘要依次通过上下文预算准入，不截断必需目标、祖先引用或同任务已答信息。历史缺失、被丢弃、损坏或省略时保持未知，不能据此认定生产者没有核验；此摘要与最终导出证据分别保存和解释。

已发布基线源码 `be0d3f57…` 包含此交接及下述推理选项，已完成原生 Windows Conda `nlp` 全量回归：1613 passed、8 skipped、0 failures/errors、Ruff 0 问题，源码与测试前后不变。父任务失败恢复仅限自己的子任务，须等全部依赖进入终态，保留 `MAX_WAKEUPS` 并将失败传播到稳定状态，验收和质量检查继续有效。实际新闻首题复测自然结束为 DEAD、正式导出为零、质量 null；失败检查器的下游编辑未启动，父任务得到恢复机会，但后续计划仍遗漏审阅文件依赖。33 条凭证均未应用于正式快照，来源完整性及回放通过不证明私有候选内容通过。

较早 `556d78fc…`（1601 passed、8 skipped）及 `8bc0c2ca…` 的回归分别作为历史检查点保留。556d 新闻首题辅助轮因记者耗尽 token 预算重试并暴露失败依赖调度问题，由操作员中止；原始 `running` 与缺失最终结果、导出和流式侧记录的事实保持不变，内容质量未知，不与当前复测混同。接口回归不证明老师质量。

### 来源访问审计接口

`tools.source_audit.SourceAccessAuditTool(store, *, workspaces=None)` 注册 `source_access_audit`（`fs_read`）。一次性 `bind_workspaces(team.workspaces)` 绑定实际 `agent_id -> workspace`，同时校验调用者 ID 与规范路径，适用于实际 LocalDirs 或受管 Git 路径。参数仅为可选 `agent_id`、`task_id`、`all_tasks`、`limit`（默认 10，最大 50）、`before_event_id`。默认当前 actor/task；`agent_id="*"` 显式选择同组已绑定角色，`all_tasks=true` 包括其当前及历史任务，不能同时指定 `task_id`。它不扩大私有人答、审批、记忆或工作区权限。

`eval.source_audit_store.SourceAuditStore` 只对已存在的来源库使用 `mode=ro`、`query_only` 和有截止时间的短查询；不构造/迁移原 store，不通过可写 getter 回退。角色/任务过滤先于 LIMIT，事件 ID 游标可继续分页。SQL 不写数据库，但 SQLite 仍可能使用现有 WAL/SHM 协调，不能声称文件系统完全不变。精确版本缺失、损坏或无法归属的旧事件保持未知。

报告包含当前窗口的成功/失败、fresh/cache、不同版本、已知首末记录时间和按精确提取文本版本合并的已服务区间。`counts_for_this_page` 和覆盖字段不代表完整历史或模型理解；使用 `pagination.next_before_event_id` 继续。输出上限 32 KiB，裁剪明确标记，单事件详情过大时保留事件身份和可续游标并返回 unknown，不静默丢弃。`factual_grade` 为 null；搜索 origin 不是 provider HTTP 计费，后者仍以预算账本为准。

### 可选学校 SSE 传输

正式 CLI 的 `--llm-transport buffered|stream` 默认 `buffered`。显式 `stream` 使用 `eval.school_stream.SchoolStreamClient`，遵循既有异步 `chat(messages, tools)` 协议；客户端的旧策略保持 `max_attempts=1`、`retry_transport=False`，默认 `completion_cap=None`（显式CLI限额见下文），不发送 `stream_options`、不自动转回 buffered，默认不设单次 completion-token 请求上限。另行显式选择HTTP重试时，总HTTP请求上限可增加，见下文。流字节/事件保护与累计节点预算保留。Python旧transport重试仅在同时显式启用第二次尝试和 `retry_transport=True` 时可用，CLI未开放这项旧策略；它和新HTTP策略、Worker重试分别计量。

`--llm-reasoning-effort none` 只允许与 `--llm-transport stream` 一起使用。Python 客户端接受 `reasoning_effort=None` 或 `"none"`；默认 `None` 不发送该字段，显式 `"none"` 才发送顶层 `reasoning_effort`。buffered 模式在读取配置或创建输出前拒绝该 CLI 选项。`test.json` 的 `model.reasoning_effort`、流式侧记录的 `reasoning_effort` 及尝试记录的 `requested_reasoning_effort` 保存请求设置，均不证明提供方遵守。

在同一学校端点及指定模型的一次固定请求诊断中，`none` 返回完整 `read_source` 工具调用但未执行工具；默认及 `thinking.type=disabled` 试验以 `finish_reason=length` 结束。这项有限观察不等于老师题通过，也不证明 HTTP 504 已全面解决。

流式客户端校验完整 SSE/UTF-8/工具参数和结束状态，丢弃不完整输出，不执行部分工具。每场景 `stream-transport.json` 保存独立 `call_id`、attempt、状态、时间、结束原因及 `usage_raw`/`usage_present`；不含提示词、正文、工具参数、授权或完整 headers。其 ID 不等于 `RecordingLLM` 对话 ID，不根据顺序拼阶段。usage 缺失/失败不计为实测零，不补完整计费总量。原始 HTTP 504 和未完成老师任务保持原记录；新增传输选项不是可靠性或内容通过声明。

### 显式文件输入

`configure_team(..., require_declared_inputs=True)` 安装当前Engine专用的计划schema和 `eval.input_handoff.DeclaredInputs`；省略时保留原行为。每个子任务须声明 `inputs: [{producer, path}]` 或 `[]`。producer必须是直接deps中的同计划任务键，同一不区分大小写的目标路径只能有一个生产者，不能以历史任务ID代替同计划键。原生DB的meta将契约绑定真实project/conversation/task/parent/role/goal。

可选worker钩子在原生复制前，检查同范围DONE生产者 `result.files ∩ artifacts` 中的输入；首接收时在模型调用前核对实际字节。路径、别名与稳定快照检查保留1MiB单文件上限。显式来自另一个新生产者任务的版本可替换仍匹配先前原生接收记录的字节；未知或接收者已编辑的文件在复制前报冲突。原生全部产物复制和子到父整合语义不变。

`declared_inputs_received` 的trace/meta收据保存 `contract_sha256` 及观察到的source_id/path/sha256/bytes，`final_file_review`、`human_quality`保持null。它只证明开场交付，不证明历史生产者文件永久不可变或最终审阅；有界任务/文件索引仍为可选上下文。该机制与quote_handoff仅读接收者文件的凭证摘要分开，两者均不推断遗漏输入或自动评判内容质量。

仅stream可选的正整数参数 `--llm-completion-cap`、`--llm-max-content-chars` 贯通model.completion_cap/model.max_content_chars、流式侧记录和每次尝试，默认None/2097152。尝试包含requested_completion_cap、max_content_chars、max_argument_chars、content_chars_received、content_bound_exceeded、limit_triggered、partial_discarded；字符限额按Unicode码点计，不是字节。溢出片段在拼接前检查，因此收到和接纳数可不同；provider_output_limit_compliance保持null。input_handoff_policy单独记录显式输入选择。

### 开场配额与工具请求策略

`configure_team(..., opening_budget_tokens=None)` 省略时保持原策略；显式值由 `teacher_opening_policy` 校验为500至2147483647的整数，拒绝bool、错误类型和超过实际策略 `prompt_ceiling` 的值。它复制 `ContextPolicy` 并仅改变老师 `TeamContextBuilder` 的 `opening_budget_tokens`，不修改 `engine.policy`、压缩器或共享TokenBudget/EMA。CLI的 `--opening-context-tokens` 不要求stream，默认有效值仍为8000；必需内容不裁剪，超过准入限额仍失败。

计划与 `test.json` 的 `opening_context_policy` 包含 `requested_tokens`（省略为null）、`effective_tokens`、`prompt_ceiling_tokens`、`scope` 和 `token_estimator_calibration="preserved"`。它们记录选择，不证明估算等于实际token数或模型正确使用了内容。

CLI的 `--llm-tool-choice` 只允许stream中的 `auto` 或 `required-when-tools`，省略不向客户端传新增配置，保留auto。Python客户端接受 `tool_choice_policy="auto"` 默认值。`required-when-tools` 在非空offered工具schema请求中发送required；无工具时不发送tools/tool_choice，保留plain调用。该分支在请求前确定，失败不降级。required组装结果须有有效JSON对象参数、本次offered名称中的完整工具调用、`tool_calls` finish和DONE；不满足时拒绝且不派发部分调用。参数JSON Schema、业务正确性、工具执行和Gate审批仍是独立检查。

计划的 `llm_tool_choice` 保存原CLI选择（省略为null）；stream的 `test.json.model` 与 `stream-transport.json` 保存 `tool_choice_policy`、`tool_choice_explicit`、`provider_tool_choice_compliance`。尝试另存 `configured_tool_choice_policy`、实际 `requested_tool_choice`、`offered_tool_count` 及 `required_tool_choice_satisfied`；无工具时实际choice为null，未评估的满足性保持null。提供方compliance仍为null，局部满足性也不能单独判成功，须结合 `status`、`done`、`finish_reason` 与 `partial_discarded`；结束后关闭失败仍可能使响应失败。

### 受限HTTP重试与用量

`SchoolStreamClient(..., transient_http_retries=0)` 默认保留旧路径；只接受精确整数0/1/2，拒绝bool和错误类型。非零值不能与旧 `max_attempts=2` 或 `retry_transport=True` 混用。CLI的 `--llm-transient-http-retries` 省略为None，不传新构造参数；显式0/1/2均只允许stream，在读密钥或创建输出前校验。

启用后，仅502/503/504响应头错误，且 `decoded_bytes=0`、`first_body_byte_s=null`、`event_count=0`、`tool_count=0` 时可重试，最多 `1+transient_http_retries` 次请求，退避5秒／15秒。整个chat调用共用配置的 `timeout_s` 协作式deadline（本轮180秒），不会为每次HTTP请求重置计时；余量不足、截止或取消不会继续发请求。其他状态、transport错误、部分200/SSE、协议、length或输出保护失败均不触发HTTP重试，不降级，不执行部分工具。

计划保存 `llm_transient_http_retries`、`llm_max_http_attempts_per_call`、`llm_http_retry_backoff_s`。stream的 `test.json.model` 与sidecar保存 `transient_http_retries`、`transient_http_retries_explicit`、`http_retry_statuses`、`http_retry_backoff_s`、`http_retry_shared_deadline_s`；旧构造值由 `legacy_max_attempts=1` 标明，而model的 `max_attempts`／sidecar的 `max_attempts_per_call` 表示总HTTP上限。`retry_transport=False` 不等于未启用独立的HTTP策略。

每次尝试记录 `configured_transient_http_retries`、`legacy_max_attempts`、`legacy_retry_transport`、`max_http_attempts_per_call`、`shared_call_deadline_s`、`http_request_started`、`http_retry_eligible`、`http_retry_decision`，以及 `http_retry_backoff_planned_s`、`http_retry_backoff_elapsed_s`、`http_retry_backoff_completed`、`http_retry_backoff_error_class`、`call_deadline_expired`。启用路径另存 `call_elapsed_s`；sidecar的 `http_request_attempt_count` 只数 `http_request_started=true`，不代表提供方已收到或计费。退避中断与每次尝试状态分别保存，不能从一条尝试推断整个任务已完成。

attempt的 `usage_present`／`usage_raw` 保留该响应的观测值；`returned_call_usage_complete` 描述是否足以覆盖返回的整调用。若第二或第三次请求才成功，返回的LLMResponse改为 `usage_present=False`、`usage_raw={}`，该attempt的 `returned_call_usage_complete` 为false，成功响应的已知usage仍留在sidecar。响应保留的数字token字段或最后成功行不是完整总量；前面失败请求的未知用量不写为0。原请求/对话ID、Worker重试和HTTP尝试仍是不同层，不凭数组顺序拼接身份或费用。
