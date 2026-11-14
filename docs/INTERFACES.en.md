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
registry.register_all(*default_tools())  # includes Git/search tools; avoid duplicates

engine = Engine(
    llm,
    tools=registry,          # P1
    workspace=MySandbox(),   # P1
    bus=MyBus(),             # P2
    human=MyHuman(),         # P2 / P3
    settings=Settings(project_id="demo", root=Path("runs/demo")),
)
```

Trace collection uses the `Recorder` facade. `Engine` writes both `trace.jsonl` and SQLite
`trace.db` by default. Memory uses a separate `SqliteMemoryStore` in `memory.db`, replaceable
through `Engine(memory=...)`; it is not provided by `Recorder`.

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

The current memory scope combines project and role, so instances of the same role share experience. How this maps to the course's independent Agent identity requirement still needs alignment; scope filtering alone does not prove isolation between every Agent instance.

### Implementation conventions

| Item | Convention |
| --- | --- |
| Tree structure | `query` results must allow the span parent/child relation to be rebuilt |
| Scoping | `search` is isolated by scope; one agent's memories should not be retrievable by another |
| Persistence | writes are durable across a restart |

Current implementations:

- `src/moumou/store/trace.py`: `SqliteTraceStore` preserves complete `TraceEvent` data, filters exact task IDs and orders by UTC start time and `span_id`. Identical imports are idempotent; conflicting records cannot overwrite existing data.
- `src/moumou/trace/recorder.py`: `Recorder` retains `span`, `record`, `query`, `all_events` and `close`, including its original one-argument JSONL use.
- `src/moumou/memory/store.py`: separate SQLite memory storage; full contract and identity-isolation integration checks remain unfinished.

### Current trace integration and recovery

The default wiring inside `Engine` requires no change to Agent recording calls:

```python
self.trace = Recorder(root / "trace.jsonl", sqlite_path=root / "trace.db")
```

`Recorder(path)` still uses JSONL only. With `sqlite_path`, startup strictly imports the existing JSONL to fill missing projection events without rewriting the source. Each event is written and flushed to JSONL before SQLite is updated; the files do not share an atomic transaction.

While healthy, `query(task_id)` uses SQLite's time ordering. `all_events()` retains JSONL record order for replay and existing metrics. Nested spans can finish in a different order from their start times; compare complete events by `span_id` when checking the two representations.

A runtime SQLite read or write failure is exposed through `projection_error` and a warning. That instance pauses projection updates and queries the complete JSONL in time order. Reopening can fill missing projection events from a valid journal; invalid/conflicting records, external edits and extra database rows are not automatically repaired. SQLite failures do not replace the span's original business exception or cancellation.

JSONL and SQLite must use different files. `Recorder.close()` closes both the file and projection connection; `Engine.close()` closes its default Recorder and memory store. The caller closes any injected external memory instance. A SQLite instance belongs to its creating thread; another thread needs its own connection.

Only supplied `parent_span` values are currently preserved. Automatic parent capture and concurrent call-tree validation remain unfinished; the task dependency tree is not the span call tree.

### Evaluation artifacts for integration and replay

`run_all(fixtures, llm, root, ..., workspace_backend="local", eval_id=None)` treats `root` as
one fresh batch directory. The `git` workspace backend remains available for P1 delivery
checks. Direct `run_once` keeps the existing `<root>/<fixture_id>/r<repeat>/` layout; reused
run directories are rejected. Each run retains its own databases, trace and workspace tree.

The batch `manifest.json` records the evaluation identity, code and fixture hashes, public
model/configuration metadata, and planned runs. Per-run `result.json` contains status, task
and acceptance observations, metrics and evidence paths; unavailable measurements are null.
`results.jsonl` is appended after each attempt, while `report.md` provides a readable view.
P2 consumers should use these saved paths instead of assuming a shared trace/database.

`moumou.eval.artifacts.load_results(root)` reads authoritative per-run result files using the
manifest plan, including a result saved before its batch-journal append was interrupted.
It does not resume execution. Results and the journal are separate durable writes; there is
no atomic transaction spanning both files. Machine acceptance is distinguished from empty
acceptance/self-report, and Engine-reported metrics are not a claim of complete token coverage.

### Supervised evaluation attempts

`run_all(..., hard_timeout_s=None, worker_factory=None)` retains the in-process object API
when the deadline is absent. A positive deadline selects a separate supervised Python
worker per attempt. This mode currently requires native Windows and uses suspended launch,
a parent-owned Job Object and bounded cleanup, including descendants launched by P1 tools.
The deadline covers startup, execution and client shutdown; OS process creation itself is
not preemptible by the Python supervisor.

Worker requests and diagnostic/candidate files live below the batch's private worker
artifact directory. The parent validates the candidate's schema, nonce, batch/fixture/repeat
identity, path and field types after process exit and cleanup, then publishes the normal
per-run `result.json`. A success candidate written by a worker that later hangs or crashes
is not success. Timeouts, startup failures, crashes and invalid results remain inspectable;
an unverified cleanup cannot be presented as successful containment.

Model clients are rebuilt and closed inside the worker, not pickled or shared. DeepSeek /
HKGAI configuration is public metadata; its key travels only in the private child environment.
Queued `FakeLLM` responses are portable. Callable/closure clients require an explicit trusted
`module:function` factory, supplied through the Python API rather than task-fixture content.
Factories receive the `TaskFixture` and synchronously return a client; this is intended for
deterministic offline integration checks. The public CLI exposes `--hard-timeout SECONDS`
and does not expose arbitrary factory imports.

### Evaluation controls, metrics and behavior

`run_once` and `run_all` accept optional `options=EvaluationOptions(...)`. Omitting options
preserves Engine defaults. Explicit controls include long-term `memory_enabled`, `max_slots`,
`role_review_enabled`, project/conversation identities and a portable MemoryRecord snapshot.
The same validated controls pass through supervised requests; actual settings and the
initial memory hash are recorded in the manifest and attempt `evaluation` metadata.

Fixtures may declare evaluation-only `requirements` and a disjoint `warmup`. Artifact checks
still use the shared Acceptance interface. `behavior_checks` independently record approval,
successful search, actual lesson injection, delegation and scheduling evidence. `root_success`
requires artifact and behavioral acceptance; a root TaskNode can remain DONE while the
evaluation outcome is failed for missing behavior. P2 should display both verdicts.

Attempt results add nullable `submitted_at`, `terminal_at`, `task_elapsed_s`, `maintenance_s`,
complete/unknown wrapper usage, `metrics`, `memory_observations`, and node dependencies and
machine verdicts. Each batch saves `metrics.json`; call `eval.metrics.summarize` with the
manifest and authoritative results to keep interrupted planned attempts in the denominator.
The [metric contract](metrics.md) defines coverage, timing and repeated-trial statistics.

`eval.ablation.plan_ablation` creates a call-free plan; `run_ablation` records four fixed groups
in `ablation.json` and gives each held-out attempt its own normal batch. Only MemoryRecord
values cross the warmup boundary. Warmup is cooperative in-process execution, separately
accounted; a held-out `hard_timeout_s` is not a claim of hard containment for warmup.

### Offline reports and experiment snapshots

`eval.report.build_report(root)` reads a normal batch or an ablation's saved plan and
authoritative nested attempt files. It recomputes statistics, including planned missing
attempts, rather than trusting `metrics.json`, JSONL tails or the ablation job-result cache.
The model has `kind`, `purpose`, `model`, `manifest`, `groups`, `warmups` and evidence paths.
Each group exposes the normal `metrics.summarize` output plus observed records and missing
plan rows. P2/report consumers must retain group identities and measured sample counts.
Mixed source versions, models, purposes, fixture definitions or fixed controls are rejected.

`generate_report(root, output=None)` writes only a derived `.md`/`.txt` report, defaulting to
`root/report.md`. It does not load credentials, call a model, create an event loop or re-run
tasks. Output cannot overwrite attempt or warmup evidence. Old unlabeled datasets stay
unclassified; regenerating a report does not turn a self-built pilot into a closed-set score.

`eval.freeze.prepare_experiment` saves full effective fixtures, explicit held-out/warmup
budget overrides, public model settings, runtime/lock-file copies, environment versions and
the complete four-group schedule. Its `freeze.json`, sidecar hash and `snapshot/` are local
evidence. Credentials, `.env`, Git internals and personal notes are not copied. Dirty runtime
snapshots are explicitly recorded; creating one never creates a Git commit.

`verify_experiment(root)` checks saved contents and the current runtime/environment;
`check_current=False` validates a historical archive only. `check_start(..., scope="full")`
returns readiness without executing anything. Missing declared search capability keeps its
rows `blocked_not_started` in the full plan. The explicit `non-network` scope returns a
selection with deferred counts; it neither executes that shard nor implements resume.

`run_ablation(..., before_attempt=guard)` optionally invokes a synchronous/async guard before
each warmup or held-out client is created. Its context contains `phase`, the actual canonical
`fixture`, original `planned` row, backend and deadline. `freeze.check_attempt` verifies those
against the snapshot; public model settings are checked only if supplied, not inferred from
an opaque factory. Guard rejection stops the batch and preserves earlier results. Checks
occur at attempt boundaries; the runtime must remain unchanged during execution. An archived
copy is not automatically the executed Python runtime.

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
