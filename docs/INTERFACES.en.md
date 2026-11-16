# Interfaces

[简体中文](INTERFACES.md) · [English](INTERFACES.en.md)

> For anyone plugging a module in. After reading it you should be able to answer three questions:
> which methods to implement and what goes in and out of them; when they are called; and how to
> wire your implementation in and test it.
>
> The ports live in `src/moumou/contracts/` and are currently frozen. If something needs to change,
> please raise it first so three implementations do not have to rework at once.

> The Chinese [INTERFACES.md](INTERFACES.md) is the authoritative version. This file is a translation.

## 0. Shared conventions

### 0.1 The async model

Every port method runs on the same event loop, and multiple agents call the same implementation
concurrently. Therefore:

- wrap slow work in `asyncio.create_subprocess_exec` or `asyncio.to_thread`
- a synchronous call such as `time.sleep` or `requests` blocks the whole event loop (every agent
  stops with it)
- the framework applies an outer `asyncio.wait_for`; when a coroutine is cancelled, cleaning up the
  child process is the implementation's job

### 0.2 How to express failure

Prefer returning a structured failure to raising. Exceptions are caught by the framework, but the
semantics are lost and the model gets no readable hint.

`ToolResult.error` goes straight into the model's context, so a short English description of the
symptom works best; detailed stack traces belong in the trace, where the framework records them.

### 0.3 How to wire an implementation in

The constructor of `Engine` is the injection point; all four ports can be replaced:

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

`TraceStore` / `MemoryStore` are currently served by the SQLite reference implementations; P3
decides when to replace them.

### 0.4 How to test it

Each port has a contract suite; run it after swapping the implementation:

```bash
pytest tests/test_tool_contract.py -q      # P1
pytest tests/test_channel_contract.py -q   # P2
make eval                                  # P3
```

For a single component, instantiate it directly instead of starting the whole engine:

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

## 1. `Tool` (P1)

Tools are the only way an agent acts on the outside world. Approval, timing, tracing and the
timeout guard are handled by the framework; a tool only has to execute.

### Signature

```python
class Tool(Protocol):
    name: str                 # unique, snake_case, e.g. "shell_exec"
    description: str          # one sentence shown to the model: when to use this
    params_schema: dict       # JSON Schema, fed straight into function calling
    action_class: str         # decides whether approval is required, see below
    timeout_s: float          # the framework's outer timeout

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

### Input

`ctx: ToolContext`

| Field | Type | Meaning |
| --- | --- | --- |
| `agent_id` | `str` | the caller, e.g. `python_dev@t1.code` |
| `task_id` | `str` | the owning task, e.g. `t1.code` |
| `workspace` | `Path` | this agent's own read/write directory |
| `env` | `dict[str, str]` | extra environment variables, may be empty |

`args: dict` is filled in by the model. Neither presence nor type is guaranteed, so validating
first is a good idea.

### Output

`ToolResult`

| Field | Type | Notes |
| --- | --- | --- |
| `ok` | `bool` | required |
| `stdout` / `stderr` | `str` | shown to the model; truncate above 8KB and set `truncated=True` |
| `exit_code` | `int \| None` | for execution tools |
| `error` | `str \| None` | required when `ok=False`; goes back to the model verbatim |
| `artifacts` | `list[str]` | produced files, relative to the workspace, used for integration |
| `truncated` | `bool` | whether output was clipped |

### When it is called

One model response may contain several `tool_call`s; the framework executes them one by one and
feeds the results back. The same tool instance can be called concurrently by several agents, so
avoid global mutable state.

### `action_class` reference

| Value | Meaning | Approval by default |
| --- | --- | --- |
| `fs_read` | read only | no |
| `fs_write` | write files | no |
| `fs_delete` | delete files | **yes** |
| `shell` | run arbitrary commands | **yes** |
| `git_commit` | commit | no |
| `git_push` | push to a remote | **yes** |
| `network` | network access | no |
| `install` | install dependencies | **yes** |

### Implementation conventions

| Item | Convention |
| --- | --- |
| Argument validation | missing or mistyped arguments → `ok=False` with the expectation in `error` |
| Path safety | resolved paths should stay inside `ctx.workspace`, including `..`, absolute paths and symlinks |
| Output size | truncate above 8KB so a single `cat` of a large file cannot blow up the context |
| Child processes | kill them on timeout or cancellation so nothing is left behind |
| Approval and logging | handled by the framework; a tool should not duplicate them |

### Reference implementation

`src/moumou/tools/builtin.py` (eight tools; the structure can be copied directly)

---

## 2. `WorkspaceProvider` (P1)

One private workspace per agent, mutually invisible.

```python
class WorkspaceProvider(Protocol):
    def allocate(self, project_id: str, conversation_id: str, agent_id: str) -> Path: ...
    def release(self, path: Path) -> None: ...
    def integrate(self, src: Path, dst: Path, artifacts: list[str]) -> IntegrateResult: ...
```

| Method | Called when | Convention |
| --- | --- | --- |
| `allocate` | before every task starts | returns an existing directory; the same `agent_id` should get the same one |
| `release` | once a task is fully finished | only clean up inside the provider's own root |
| `integrate` | when a child finishes / before a task starts | move `artifacts` from `src` to `dst` and report what happened |

`integrate` is the only channel through which "several agents worked separately" becomes "one
verifiable result". The reference implementation copies files; the git version merges a child's
branch into the parent's.

It returns a result object rather than a list of paths because **a merge conflict is a normal
outcome, not an exception**:

```python
class IntegrateResult(BaseModel):
    ok: bool = True
    copied: list[str] = []       # paths that landed
    conflicts: list[str] = []    # paths that could not be merged, relative to the workspace
    error: str | None = None
```

Convention on conflict: the child still counts as done (its own acceptance passed), the conflict is
written into the child's `error` field and the trace, and the parent sees it in the dependency
report and decides how to reconcile.

Reference implementation: `src/moumou/adapters/workspace.py`

---

## 3. `Channel` (P2)

The message path between agents, and between humans and agents.

```python
class Channel(Protocol):
    async def publish(self, msg: Message) -> None: ...
    def subscribe(self, agent_id: str, cb: Callable[[Message], Awaitable | None]) -> None: ...
    def history(self, scope: Scope, limit: int = 50) -> list[Message]: ...
```

The protocol is defined in `src/moumou/contracts/message.py`.

### `Message`

| Field | Notes |
| --- | --- |
| `id` / `ts` | generated |
| `project_id` / `conversation_id` | scope |
| `task_id` | may be empty |
| `sender` | an agent id, or the fixed value `"human"` |
| `recipients` | target list; **an empty list means broadcast** |
| `kind` | `chat` / `delegate` / `report` / `ask_human` / `approval` / `steer` |
| `content` / `meta` | body and attached data |

### `Scope` (used by `history`)

`kind` (`dm` / `group`) + `project_id` + `conversation_id` + `members`.

### One key design decision: humans are ordinary members

A human's id on the bus is the fixed value `human`, addressed exactly like an agent. Direct
messages, group chat and approvals therefore reuse one mechanism, and the UI does not need a
separate channel for "the person".

### Implementation conventions

| Item | Convention |
| --- | --- |
| Persistence | `publish` should persist before delivering, so messages survive a restart |
| Callback isolation | run subscriber callbacks in their own task; a slow subscriber must not stall the bus |
| Ordering | `history` returns ascending by time, and only messages from the same conversation |
| Direct messages | a DM should not be delivered to unrelated members |

Reference implementation: `src/moumou/adapters/bus.py` (in-memory; needs a persistent replacement)

---

## 4. `HumanIO` (P2 interactive, P3 scripted)

The single entry point for human involvement, three shapes behind one interface.

```python
class HumanIO(Protocol):
    async def ask(self, req: HumanRequest) -> HumanResponse: ...
    async def notify(self, msg: Message) -> None: ...
```

### `HumanRequest` (input)

| Field | Notes |
| --- | --- |
| `id` | request id, echoed back in the response |
| `task_id` / `agent_id` | who is asking |
| `kind` | `approval` (permission) / `question` (clarification) / `review` (review) |
| `summary` | one line, shown as the card title |
| `action` / `action_class` | tool name and action class, for approvals |
| `payload` | full arguments, for an "expand" view |
| `options` | allowed answers; an empty list means free text |
| `context` | extra explanation, e.g. why the question is being asked |

### `HumanResponse` (output)

| Field | Notes |
| --- | --- |
| `request_id` | echoed back |
| `decision` | `approve` / `reject`; `None` for a free-text answer |
| `text` | the note, or the free-text answer |

### The three cards the UI needs

| Shape | Shows | Interaction |
| --- | --- | --- |
| `approval` | tool name + argument summary | approve / reject buttons |
| `question` | the question text | a text field |
| `steer` | — | can be sent at any time during a run, to redirect the work |

### Why one interface covers three shapes

During a demo the interactive implementation asks a real person; during batch evaluation
`ScriptedHuman` answers from YAML. That is what keeps tasks requiring human confirmation inside
automated testing, without which the completion-rate metric would be half meaningless.

Reference implementation: `src/moumou/adapters/human.py`

---

## 5. `TraceStore` / `MemoryStore` (P3)

```python
class TraceStore(Protocol):
    def append(self, e: TraceEvent) -> None: ...
    def query(self, task_id: str) -> list[TraceEvent]: ...   # ascending by time

class MemoryStore(Protocol):
    def write(self, rec: MemoryRecord) -> str: ...           # returns the id
    def search(self, scope: str, query: str, k: int = 5) -> list[MemoryRecord]: ...
    def all(self, scope: str) -> list[MemoryRecord]: ...
    def update_score(self, rec_id: str, delta: float, used: bool = True) -> None: ...
    def evict_candidates(self, scope: str, threshold: float = 0.3) -> list[str]: ...
    def mark_deleted(self, rec_ids: list[str]) -> None: ...
```

### `TraceEvent`

| Field | Notes |
| --- | --- |
| `span_id` / `parent_span` | reconstruct the call tree |
| `task_id` / `agent_id` | ownership |
| `kind` | `task` / `llm` / `tool` / `approval` / `question` / `steer` / `delegate` / `role` / `acceptance` / `memory` |
| `name` | the concrete name, e.g. `tool:write_file` |
| `t0` / `t1` | start and end; `duration_ms` is derived |
| `status` | `ok` / `error` / `cancelled` |
| `tokens_in` / `tokens_out` | needed for the cost metrics |
| `payload` | attached data |

### `MemoryRecord`

`project_id` + `role` form the scope, `task_id` provides provenance; `situation` / `action` /
`outcome` / `lesson` are the content; `score` and `uses` drive evolution.

### Implementation conventions

| Item | Convention |
| --- | --- |
| Tree structure | `query` results must allow the span parent/child relation to be rebuilt |
| Scoping | `search` is isolated by scope; one agent's memories should not be retrievable by another |
| Persistence | writes are durable across a restart |

Reference implementations: `src/moumou/trace/recorder.py` (JSONL, trace only) and
`src/moumou/memory/store.py` (SQLite, memory)

---

## 6. `TaskRepo` (used by the orchestration layer; reference only)

When writing evaluation or analysis tooling, reading this interface is enough:

```python
def get(task_id) -> TaskNode | None
def children(task_id) -> list[TaskNode]
def by_status(status) -> list[TaskNode]
def ready(limit) -> list[TaskNode]
def transition(task_id, to, reason) -> None      # validate + persist + audit, atomically
def event_log(task_id) -> list[dict]             # state-change history
def set_result(task_id, result) -> None
def add_artifacts(task_id, artifacts) -> None
```

State changes should go through `transition`: it updates the task table and inserts the audit event
in one transaction. Bypassing it leaves the event log inconsistent with the actual state, and the
timeline in the report stops adding up.

---

## 7. Data model reference

All of it lives in `src/moumou/contracts/`; import with `from moumou.contracts import X`.

| Model | File | Purpose |
| --- | --- | --- |
| `TaskNode` | `task.py` | a task node (deps, acceptance, budget, status) |
| `Acceptance` | `task.py` | one machine-checkable criterion |
| `Budget` | `task.py` | steps / tokens / wall clock / retries |
| `ContextPolicy` | `context.py` | context and memory thresholds |
| `RoleSpec` | `agent.py` | a role (name, description, tools, justification) |
| `AgentInstance` | `agent.py` | a running instance with its two scope keys |
| `Message` / `Scope` | `message.py` | messaging and addressing |
| `Tool` / `ToolContext` / `ToolResult` | `tool.py` | the tool trio |
| `IntegrateResult` | `workspace.py` | the outcome of moving artifacts between workspaces |
| `TraceEvent` | `trace.py` | one execution record |
| `MemoryRecord` | `memory.py` | one lesson |
| `HumanRequest` / `HumanResponse` | `human.py` | human involvement |
| `Plan` / `RoleDraft` / `SubtaskSpec` | `plan.py` | the decomposition plan the model submits |

If any of these fields changes, `pytest tests/test_contracts.py` fails immediately — that is the
gate against interface drift.
