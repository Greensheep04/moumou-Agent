# 接口说明

> 面向需要接入各模块的同学。读完之后应该能回答三个问题：
> 要实现哪些方法、输入输出是什么；实现在什么时机被调用；怎么把自己的实现接进去自测。
>
> 端口定义位于 `src/moumou/contracts/`，目前处于冻结状态。如果有调整需求，建议先在群里同步一下，
> 避免三份实现同时返工。

## 0. 通用约定

### 0.1 异步模型

所有端口方法都在同一个事件循环里执行，多个 Agent 会并发调用同一份实现。因此：

- 耗时操作建议用 `asyncio.create_subprocess_exec` 或 `asyncio.to_thread` 包装
- `time.sleep`、`requests` 这类同步调用会阻塞整个事件循环（所有 Agent 一起停下来）
- 框架外层用 `asyncio.wait_for` 兜底超时；协程被取消时，子进程的清理由实现方负责

### 0.2 错误的表达方式

建议返回结构化的失败结果，而不是抛出异常——异常虽然会被框架捕获，但语义会丢失，
模型拿不到可读的提示。

`ToolResult.error` 会原样进入模型上下文，建议用简短英文描述现象；详细堆栈由框架记录在 trace 里。

### 0.3 怎么把实现接进去

`Engine` 的构造参数就是注入点，四个端口都可以替换：

```python
from pathlib import Path

from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools

registry = ToolRegistry()
registry.register_all(*default_tools(), GitTool(), GitPushTool(), WebSearchTool())

engine = Engine(
    llm,
    tools=registry,          # P1
    workspace=MySandbox(),   # P1
    bus=MyBus(),             # P2
    human=MyHuman(),         # P2 / P3
    settings=Settings(project_id="demo", root=Path("runs/demo")),
)
```

`TraceStore` / `MemoryStore` 目前由 `Recorder`（JSONL）承担，正式替换时机在 P3 接入时确定。

### 0.4 自测方式

每个端口都有一份契约测试，替换实现后跑一遍即可：

```bash
pytest tests/test_tool_contract.py -q      # P1
pytest tests/test_channel_contract.py -q   # P2
make eval                                  # P3
```

单点调试可以直接实例化，不必启动整个引擎：

```python
import asyncio
from pathlib import Path

from moumou.contracts import ToolContext


async def main():
    ctx = ToolContext(agent_id="me@t1", task_id="t1", workspace=Path("."))
    print(await GitTool().run({"branch": "main"}, ctx))


asyncio.run(main())
```

---

## 1. `Tool`（P1）

工具是 Agent 作用于外部世界的唯一手段。审批、计时、日志、超时兜底由框架统一处理，
工具侧只需要专注执行本身。

### 签名

```python
class Tool(Protocol):
    name: str                 # 唯一名字，snake_case，如 "shell_exec"
    description: str          # 一句话，直接给模型看，说明"什么时候该用它"
    params_schema: dict       # JSON Schema，直接喂给 function calling
    action_class: str         # 决定是否需要人工审批，见下表
    timeout_s: float          # 框架的外层兜底超时

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

### 输入

`ctx: ToolContext`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `agent_id` | `str` | 调用方，如 `python_dev@t1.code` |
| `task_id` | `str` | 所属任务，如 `t1.code` |
| `workspace` | `Path` | 该 Agent 专属的可读写目录 |
| `env` | `dict[str, str]` | 附加环境变量，可为空 |

`args: dict` 由模型填写。字段是否齐全、类型是否正确都不保证，实现时建议先做校验。

### 输出

`ToolResult`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | `bool` | 是否成功，必填 |
| `stdout` / `stderr` | `str` | 给模型看的输出，超过 8KB 建议截断并置 `truncated=True` |
| `exit_code` | `int \| None` | 执行类工具填写 |
| `error` | `str \| None` | `ok=False` 时填写，会原样回给模型 |
| `artifacts` | `list[str]` | 产出文件，相对 workspace 的路径，供后续整合与展示 |
| `truncated` | `bool` | 是否发生截断 |

### 调用时机

一次模型返回可能包含多个 `tool_call`，框架会逐个执行并把结果回填给模型。
同一个工具实例可能被多个 Agent 并发调用，实现时注意不要持有全局可变状态。

### `action_class` 对照

| 值 | 含义 | 默认是否审批 |
| --- | --- | --- |
| `fs_read` | 只读 | 否 |
| `fs_write` | 写文件 | 否 |
| `fs_delete` | 删文件 | **是** |
| `shell` | 执行任意命令 | **是** |
| `git_commit` | 提交 | 否 |
| `git_push` | 推远端 | **是** |
| `network` | 联网 | 否 |
| `install` | 装依赖 | **是** |

### 实现约定

| 项 | 约定 |
| --- | --- |
| 参数校验 | 缺失或类型不符时返回 `ok=False`，并在 `error` 里说明期望 |
| 路径安全 | 所有路径 resolve 后应落在 `ctx.workspace` 内，包括 `..`、绝对路径与符号链接 |
| 输出长度 | 超过 8KB 建议截断，避免一次 `cat` 大文件就撑爆上下文 |
| 子进程 | 超时或被取消时需要 kill，避免残留进程 |
| 审批与日志 | 由框架统一处理，工具内无需重复实现 |

### 参考实现

`src/moumou/tools/builtin.py`（8 个工具，可以直接照抄结构）

---

## 2. `WorkspaceProvider`（P1）

每个 Agent 一份独立工作区，互相不可见。

```python
class WorkspaceProvider(Protocol):
    def allocate(self, project_id: str, conversation_id: str, agent_id: str) -> Path: ...
    def release(self, path: Path) -> None: ...
    def integrate(self, src: Path, dst: Path, artifacts: list[str]) -> list[str]: ...
```

| 方法 | 调用时机 | 约定 |
| --- | --- | --- |
| `allocate` | 每个任务开跑前 | 返回已存在的目录；同一 `agent_id` 重复调用应返回同一个 |
| `release` | 任务彻底结束后 | 只清理自己根目录下的内容 |
| `integrate` | 子任务完成时 / 任务开跑前 | 将 `artifacts` 从 `src` 搬到 `dst`，返回 `IntegrateResult` |

`integrate` 是「多个 Agent 各干各的」变成「一个可验证的结果」的唯一通道。
当前参考实现是复制文件；git 版本对应的是把子任务分支合并进父分支。

返回值用结果对象而不是路径列表，因为 **合并冲突是正常结果，不是异常**：

```python
class IntegrateResult(BaseModel):
    ok: bool = True
    copied: list[str] = []       # 成功搬运的路径
    conflicts: list[str] = []    # 冲突的路径，相对 workspace
    error: str | None = None
```

冲突时的约定：子任务本身仍然算完成（它的验收已经通过），冲突会写进该任务的 `error`
字段和执行日志，父任务的上下文里能看到，由父任务决定如何合并。

参考实现：`src/moumou/adapters/workspace.py`

---

## 3. `Channel`（P2）

Agent 之间、人与 Agent 之间的消息通路。

```python
class Channel(Protocol):
    async def publish(self, msg: Message) -> None: ...
    def subscribe(self, agent_id: str, cb: Callable[[Message], Awaitable | None]) -> None: ...
    def history(self, scope: Scope, limit: int = 50) -> list[Message]: ...
```

协议定义在 `src/moumou/contracts/message.py`。

### `Message`

| 字段 | 说明 |
| --- | --- |
| `id` / `ts` | 自动生成 |
| `project_id` / `conversation_id` | 作用域 |
| `task_id` | 可为空 |
| `sender` | Agent id，或固定值 `"human"` |
| `recipients` | 目标列表；**空列表表示广播** |
| `kind` | `chat` / `delegate` / `report` / `ask_human` / `approval` / `steer` |
| `content` / `meta` | 正文与附加数据 |

### `Scope`（`history` 使用）

`kind`（`dm` / `group`）+ `project_id` + `conversation_id` + `members`。

### 一个关键设计：人类也是普通成员

人类在总线上的 id 固定为 `human`，与 Agent 走同一套寻址。因此私聊、群聊、审批三种交互
可以复用同一套机制，界面侧不需要为"人"单独开一条通道。

### 实现约定

| 项 | 约定 |
| --- | --- |
| 持久化 | `publish` 建议先落盘再投递，进程重启后消息仍可查 |
| 回调隔离 | 订阅回调放在独立 task 中执行，慢订阅者不应拖住总线 |
| 顺序 | `history` 按时间升序返回，且只包含同一会话的消息 |
| 私聊 | DM 不应投递给无关成员 |

参考实现：`src/moumou/adapters/bus.py`（内存版，需替换为持久化版本）

---

## 4. `HumanIO`（P2 做交互版，P3 做脚本版）

人类介入的唯一入口，三种形态共用一个接口。

```python
class HumanIO(Protocol):
    async def ask(self, req: HumanRequest) -> HumanResponse: ...
    async def notify(self, msg: Message) -> None: ...
```

### `HumanRequest`（输入）

| 字段 | 说明 |
| --- | --- |
| `id` | 请求 id，回答时需要带回 |
| `task_id` / `agent_id` | 提问方 |
| `kind` | `approval`（请求授权）/ `question`（请求澄清）/ `review`（请求审阅） |
| `summary` | 一句话，作为卡片标题显示 |
| `action` / `action_class` | 审批场景下为工具名与动作类别 |
| `payload` | 完整参数，供"展开详情"使用 |
| `options` | 可选答案；空列表表示自由文本作答 |
| `context` | 附加说明，例如提问原因 |

### `HumanResponse`（输出）

| 字段 | 说明 |
| --- | --- |
| `request_id` | 原样带回 |
| `decision` | `approve` / `reject`；自由提问时为 `None` |
| `text` | 补充说明或自由文本答案 |

### 界面侧需要支持的三种卡片

| 场景 | 显示 | 交互 |
| --- | --- | --- |
| `approval` | 工具名 + 参数摘要 | 同意 / 拒绝按钮 |
| `question` | 问题文本 | 输入框 |
| `steer` | —— | 执行过程中随时可发，用于调整方向 |

### 一个接口三种形态的原因

演示场景下接交互式实现，由真人回答；批量评测时接 `ScriptedHuman`，按 YAML 预置答案自动作答。
这样带人工确认的任务也能进入自动化测试，完成率指标才有意义。

参考实现：`src/moumou/adapters/human.py`

---

## 5. `TraceStore` / `MemoryStore`（P3）

```python
class TraceStore(Protocol):
    def append(self, e: TraceEvent) -> None: ...
    def query(self, task_id: str) -> list[TraceEvent]: ...   # 按时间升序

class MemoryStore(Protocol):
    def write(self, rec: MemoryRecord) -> str: ...           # 返回 id
    def search(self, scope: str, query: str, k: int = 5) -> list[MemoryRecord]: ...
    def all(self, scope: str) -> list[MemoryRecord]: ...
    def update_score(self, rec_id: str, delta: float, used: bool = True) -> None: ...
```

### `TraceEvent`

| 字段 | 说明 |
| --- | --- |
| `span_id` / `parent_span` | 用于还原调用树 |
| `task_id` / `agent_id` | 归属 |
| `kind` | `task` / `llm` / `tool` / `approval` / `question` / `steer` / `delegate` / `role` / `acceptance` / `memory` |
| `name` | 具体名字，如 `tool:write_file` |
| `t0` / `t1` | 起止时间；`duration_ms` 为计算属性 |
| `status` | `ok` / `error` / `cancelled` |
| `tokens_in` / `tokens_out` | token 计数，指标计算需要 |
| `payload` | 附加数据 |

### `MemoryRecord`

`project_id` + `role` 构成作用域，`task_id` 用于溯源；
`situation` / `action` / `outcome` / `lesson` 是内容；`score` 与 `uses` 用于淘汰。

### 实现约定

| 项 | 约定 |
| --- | --- |
| 树结构 | `query` 的结果应能还原 span 父子关系 |
| 作用域 | `search` 按 scope 隔离，A Agent 的记忆不应被 B 检索到 |
| 持久化 | 写入落盘，进程重启后仍可读取 |

参考实现：`src/moumou/trace/recorder.py`（JSONL，仅覆盖 trace）

---

## 6. `TaskRepo`（编排层自用，供参考）

编写评测或分析工具时，读这个接口即可：

```python
def get(task_id) -> TaskNode | None
def children(task_id) -> list[TaskNode]
def by_status(status) -> list[TaskNode]
def ready(limit) -> list[TaskNode]
def transition(task_id, to, reason) -> None      # 校验 + 落库 + 记事件，原子
def event_log(task_id) -> list[dict]             # 状态变更历史
def set_result(task_id, result) -> None
def add_artifacts(task_id, artifacts) -> None
```

状态变更建议统一走 `transition`：它会在同一事务里同时更新任务表和事件表，
绕过它直接改状态会让事件日志与实际状态不一致，报告里的时间线也会对不上。

---

## 7. 数据模型速查

全部位于 `src/moumou/contracts/`，均可 `from moumou.contracts import X`。

| 模型 | 文件 | 用途 |
| --- | --- | --- |
| `TaskNode` | `task.py` | 任务节点（依赖、验收、预算、状态） |
| `Acceptance` | `task.py` | 一条机器可判的验收项 |
| `Budget` | `task.py` | 步数 / token / 墙钟 / 重试上限 |
| `RoleSpec` | `agent.py` | 角色（名字、职责、可用工具、创建理由） |
| `AgentInstance` | `agent.py` | 运行实例，带两个作用域属性 |
| `Message` / `Scope` | `message.py` | 消息与寻址 |
| `Tool` / `ToolContext` / `ToolResult` | `tool.py` | 工具三件套 |
| `TraceEvent` | `trace.py` | 一条执行记录 |
| `MemoryRecord` | `memory.py` | 一条经验 |
| `HumanRequest` / `HumanResponse` | `human.py` | 人类介入 |
| `Plan` / `RoleDraft` / `SubtaskSpec` | `plan.py` | 模型提交的拆解计划 |

这些字段如有变动，`pytest tests/test_contracts.py` 会立即失败——它是防止接口漂移的闸门。
