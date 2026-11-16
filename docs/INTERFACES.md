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

只有直接依赖及显式 `artifacts` 传递文件；不会自动分享父目录和传递依赖链。子目标必须自包含。缺失输入应通过 `submit_plan` 请拥有者明确交付并声明依赖，或调用 `ask_human`；不能搜索父级、同级或其他运行目录绕过隔离。

`TeamContextBuilder` 固定保留完整原角色、当前目标、全部 `Acceptance` 和同任务已答信息，不修改源对象。可选历史、依赖、结论和经验先受限，完整辅助内容保存在私有 `.evaluation/context/<digest-prefix>.json`，新文件名为 32 位前缀、引用为完整 SHA-256，同名不同内容拒绝，不重命名旧归档。必需内容超限时抛 `ContextAdmissionError`，不切断目标；框架遗漏指令不能直接归为模型不服从。

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

当前恢复、trace 和阶段证据功能有回归源码，但尚待执行。已发布 e35e820 的 905/8 是旧基线，不验证修改版本；实际任务质量和消费者签收仍需自己的证据。交付清单见[交付要求](DELIVERY.md)。

`eval.report.build_report(root)` 校验保存会话和权威单题结果；`generate_report(root, output=None)` 输出派生 `.md` / `.txt`，不需要模型密钥、不调用模型、不重跑测试或恢复任务。未决质量不转成成功。未被索引的任务结果或身份不一致会报完整性错误，不会悄悄计入或删除。历史 `midtest.json` 仅只读兼容，不是另一入口。

`eval.review` 通过真实 Console/File HumanIO 在 `reviews/<UUID32>/` 追加复核，绑定原运行、代码、题源、任务结果和产物哈希。重复复核须 `--supersedes` 指定当前末端。原始结果保持不变，报告另给 `raw_quality`、有效质量、`quality_source` 和复核历史。匹配 I/O 和哈希说明记录一致，不是密码学身份认证。旧产物快照缺失保持 `unsupported_historical_artifact_snapshot`。

最终复核发布使用 `reviews/.publish.lock`。锁已存在时须明确检查并处理并发/中断写入者，不自动删除旧锁或绕过当前末端校验；人工等待期间不占发布锁。

`eval.analysis.build_analysis/generate_analysis` 支持老师格式和历史 groups。`eval.replay.build_replay/generate_replay` 输出全新派生 JSON，提供场景+任务身份、原节点/TraceEvent、来源/行哈希和人类事件索引，区分已答、待答、暂存答复。它们消费已存阶段证据，不读取 live memory.db 或可变工作区冒充旧快照；导出不代替 P2 签收。缺失父关系、状态、质量保持缺失/未知，外部停止记录不替换原 manifest。

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
| `IntegrateResult` | `workspace.py` | 文件整合结果 |
| `TraceEvent` | `trace.py` | 执行事件 |
| `MemoryRecord` | `memory.py` | 长期经验 |
| `HumanRequest` / `HumanResponse` | `human.py` | 人类交互 |
| `Plan` / `RoleDraft` / `SubtaskSpec` | `plan.py` | 模型提出的拆分计划 |

字段变化后运行 `pytest tests/test_contracts.py` 和相关适配器回归。即使 schema 兼容，也仍需实际运行及消费者检查；测试文件存在不等于已经通过。