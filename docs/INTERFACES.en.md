# Interfaces

[简体中文](INTERFACES.md) · [English](INTERFACES.en.md)

> For anyone plugging a module in. After reading it you should be able to answer three questions:
> which methods to implement and what goes in and out of them; when they are called; and how to
> wire your implementation in and test it.
>
> The shared ports live in `src/moumou/contracts/`. Coordinate interface changes with
> their consumers, preserve existing call shapes where possible and update the contract tests.


## 0. Shared conventions

### 0.1 The async model

Agent execution and asynchronous tool, Channel and HumanIO calls share an event loop.
Storage ports expose synchronous methods. Multiple agents can call the same adapter, so:

- wrap slow work in `asyncio.create_subprocess_exec` or `asyncio.to_thread`
- a synchronous call such as `time.sleep` or `requests` blocks the whole event loop (every agent
  stops with it)
- tool/model operations have their own timeouts; adapters must clean up subprocesses and
  awaited resources on cancellation. Interactive node limits remain cooperative and do not
  impose a universal hard deadline while a person is answering

### 0.2 How to express failure

Prefer returning a structured failure to raising. Exceptions are caught by the framework, but the
semantics are lost and the model gets no readable hint.

`ToolResult.error` goes straight into the model's context, so a short English description of the
symptom works best; detailed stack traces belong in the trace, where the framework records them.

### 0.3 How to wire an implementation in

The constructor of `Engine` is the injection point for its tool, workspace, Channel,
HumanIO and memory adapters:

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

After replacing a port implementation, run its contract checks and framework regression:

```bash
pytest tests/test_tool_contract.py -q      # P1
pytest tests/test_channel_contract.py -q   # P2
make unit-test                           # framework regression, including P3
```

`make eval` runs the formal instructor tasks with the configured model and real human
interaction; it is not a framework self-test.

For a single component, instantiate it directly instead of starting the whole engine:

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
| `integrate` | after successful child acceptance / before a dependent task starts | move published `artifacts` from `src` to `dst` and report what happened |

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

### Declared result files and publication

`submit_result.files` may declare ordinary files already present in the task workspace,
including unchanged files. Worker validates the entire declaration and rechecks it after
acceptance; only successful acceptance permits publication into `node.artifacts` and
integration. Missing files, directories, absolute/outside-workspace paths, invalid paths
and `.git` entries reject the whole result with correctable `invalid_result_files` tool
feedback. No subset of that declaration is published.

Published declarations are deduplicated with existing tool-tracked artifact paths,
including tracked deletions. Validation of declared files does not discard those paths
or turn the workspace into an automatic whole-directory export.

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

## 4. `HumanIO` (real interaction and deterministic port tests)

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
| `project_id` / `conversation_id` / `role` | explicit scope on recoverable questions; additive fields defaulting to empty text for older callers |

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
| `review` | saved deliverables and quality evidence | explicit decision and optional feedback |
| `steer` (Channel message) | running-task context | a human can redirect work during execution |

### Why one interface covers three shapes

Formal task evaluation uses actual `HumanIO` questions, permissions and post-delivery
quality reviews. `eval.human.ConsoleHuman` and `FileHuman` retain the real requests and
responses. Approval of a tool action does not establish content-quality acceptance.
`ScriptedHuman` in `adapters/human.py` remains a deterministic framework-test utility,
not an answer source for formal instructor or supplementary task sessions.

Question recovery is an optional capability rather than a requirement on every HumanIO:

```python
class ResumableHumanIO(Protocol):
    async def resume(self, req: HumanRequest) -> HumanResponse: ...
```

Only the original scoped `question` can be resumed. Console/File ports verify its
original ID/body, recover a consumed response without repeating its callback, or wait
for the original outstanding response. An unconsumed callback must be idempotent by
request ID if it changes external state. Tool approvals and quality reviews are not
replayed as grants. A runner with pending history and an unsupported port fails explicitly.

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
| `tokens_in` / `tokens_out` | recorded usage fields; coverage and provider evidence determine whether totals are known |
| `payload` | attached data |

Trace consumers, including P2, should recognize `result_rejected` for an invalid final
file declaration and `declared_files_published` after accepted files are published.
These events distinguish a correctable result rejection from successful publication;
neither by itself establishes instructor goal quality.

Recorder supplies active nested-span parents with async context isolation and keeps
top-level agent metadata distinct from payload. Existing null parents are not backfilled.
Span parentage is not TaskNode delegation parentage; replay retains both as recorded.

### `MemoryRecord`

`project_id` + `role` form the scope, `task_id` provides provenance; `situation` / `action` /
`outcome` / `lesson` are the content; `score` and `uses` drive evolution.

Formal instructor agents have stable role IDs and private role workspaces across their
two tasks, so project:role identifies one continuing persona. Different scenario/run
databases isolate teams. Generic Engine role@task instances retain the existing shared
project-role memory policy; universal per-runtime-instance isolation is not claimed.

### Implementation conventions

| Item | Convention |
| --- | --- |
| Tree structure | `query` results must allow the span parent/child relation to be rebuilt |
| Scoping | `search` and `all` filter the requested project-role scope; generic instances of one role share that scope |
| Persistence | writes are durable across a restart |

Current implementations:

- `src/moumou/store/trace.py`: `SqliteTraceStore` preserves complete `TraceEvent` data, filters exact task IDs and orders by UTC start time and `span_id`. Identical imports are idempotent; conflicting records cannot overwrite existing data.
- `src/moumou/trace/recorder.py`: `Recorder` retains `span`, `record`, `query`, `all_events` and `close`, including its original one-argument JSONL use.
- `src/moumou/memory/store.py`: separate SQLite memory storage with project-role
  filtering, scoring, soft deletion and character-bigram recall. Phase captures belong
  to `eval/session_evidence.py`; they do not change the storage identity policy.

### Current trace integration and recovery

The default wiring inside `Engine` requires no change to Agent recording calls:

```python
self.trace = Recorder(root / "trace.jsonl", sqlite_path=root / "trace.db")
```

`Recorder(path)` still uses JSONL only. With `sqlite_path`, startup strictly imports the existing JSONL to fill missing projection events without rewriting the source. Each event is written and flushed to JSONL before SQLite is updated; the files do not share an atomic transaction.

While healthy, `query(task_id)` uses SQLite's time ordering. `all_events()` retains JSONL record order for replay and existing metrics. Nested spans can finish in a different order from their start times; compare complete events by `span_id` when checking the two representations.

A runtime SQLite read or write failure is exposed through `projection_error` and a warning. That instance pauses projection updates and queries the complete JSONL in time order. Reopening can fill missing projection events from a valid journal; invalid/conflicting records, external edits and extra database rows are not automatically repaired. SQLite failures do not replace the span's original business exception or cancellation.

JSONL and SQLite must use different files. `Recorder.close()` closes both the file and projection connection; `Engine.close()` closes its default Recorder and memory store. The caller closes any injected external memory instance. A SQLite instance belongs to its creating thread; another thread needs its own connection.

Automatic nested/async parent capture is implemented in Recorder. Existing supplied
relationships are preserved, missing historical parents are not invented, and task
dependency edges are not the span call tree. A Worker `execution` span represents one
actual attempt, including materialization; it is not assignment-to-delivery wall time.

### Task sources and continuing teams

`eval.cases.load_cases(path)` loads Markdown scenario folders in their README order,
retaining original role prompts, ordered task texts and source hashes. The default
`eval/tasks` source is the instructor's three four-member groups and six root goals.
The optional lower-priority `eval/supplementary` source retains the fifteen self-authored
tasks in the same format: eight groups, with one task in the last group. It is selected
explicitly and never automatically mixed into instructor results.

`python -m moumou` and `python -m moumou.eval.cli` invoke the same formal protocol;
`--tasks` selects the source. There is no separate task-format-specific launch path.
The team adapter in `eval.team` configures exactly the supplied identities, private
continuing role workspaces and same-role execution locks. Task 2 receives actual Task 1
results, delivered files and a readable original dialogue archive, rather than fabricated
history or a fresh independent attempt. A source's actual task count defines its goal plan.

The team context states the file/delegation contract: only direct dependencies with
explicit `artifacts` transfer files; dependency chains and parent directories are not
automatically shared. Child goals state their own work and inputs; exact ancestor goals
are separately retained as scoped references, without parent-private answers or files.
For missing inputs, use
`submit_plan` to ask the owning role for an explicit delivery, then declare the dependency,
or use `ask_human`; do not bypass private workspaces or repeat denied cross-role searches.

`TeamContextBuilder` preserves the complete original role prompt, current goal, every
`Acceptance` object and same-task recorded answered questions in the opening messages
without mutating their source objects.
Optional prior history, dependency reports, board content and lessons are bounded first;
their exact auxiliary payload remains recoverable in the role's private
`.evaluation/context/<digest-prefix>.json`. New filenames use the first 32 digest hex
characters; references and validation metadata retain the full SHA-256. A same-name
archive with different content is rejected, and existing archives are not renamed.
The team adapter calls the optional provider hook
`register_private_evidence(workspace, relative_file)` after saving validated context
archives or `.evaluation/prior-dialogue.txt`. GitWorkspaces adds only these exact
file paths to its managed repository's `info/exclude`; it does not ignore the
directory, edit project/global ignore settings, or change the dirty-destination
check. Ordinary files, unregistered `.evaluation/` files and staged/tracked changes
still block delivery. Tracked evidence requires explicit repair rather than automatic
untracking; existing dialogue copies are checked before their bytes are replaced.
Registered evidence remains readable and cannot be declared or integrated
as an artifact. Releasing a worktree containing private evidence is refused to retain
the otherwise ignored bytes. Providers without this optional hook retain their existing
behavior, and the default workspace/bus selection is unchanged.
If mandatory instructions cannot fit, it raises
`ContextAdmissionError` before the LLM call instead of slicing the goal. Attribute missing
instructions to the framework's transmitted context, not to model noncompliance.

### Real human and model evidence

TaskRepo records question exchanges by project/conversation/task/role, with original
request ID/body, response and timestamps. `record_human_request`, `record_human_response`
and `human_exchanges` support same-task re-entry without restoring unmatched tool calls.
Only recorded scoped questions are resumed with their original ID/body; damaged or
missing records fail explicitly. Approval is requested afresh, never reused as a grant.
Gate rejection text is returned in the current ToolResult stderr; wrong IDs or absent
decisions are invalid approvals. A human-port record alone does not authenticate manual
versus explicitly assisted/scripted authorship.

`eval.human.RecordingLLM` records complete redacted request/response events, phase,
call identity and observed usage. `ConsoleHuman` and `FileHuman` implement `HumanIO`.
The formal CLI requires explicit human confirmation before the `git_commit` operation
stages and commits files. This is harness approval policy; the generic default
approval policy and P1 tool contract remain unchanged.
File mode retains original requests in `human/requests/` and pending requests in
`human/pending/`. A real response is placed in `human/responses/REQUEST_ID.json` and
retained after validation under `human/consumed/`. Echo the actual request ID; questions
require text, while permissions and reviews require an explicit approve/reject decision.

Default post-delivery human review waits after the task result is saved and before the
next task is posted. A rejection preserves the agent's actual status, records rejected
quality and skips later dependent tasks in that group; independent groups can continue.
Deferred review remains unknown. P2 must distinguish
agent `DONE`, machine acceptance and accepted/rejected/pending content quality. Reusing
an explicit standing tool permission requires provenance; it is not a new manual answer
and cannot be promoted to quality approval.

### Suite files for replay and reporting

Each launch reserves a fresh `runs/test/<id>/` directory, or a new explicit `--out`.
The suite `test.json` has `kind: instructor_test`, source hashes, public model/runtime
configuration, `dataset_kind` (`instructor`/`supplementary`), planned root goals and scenario
`source_kind`. `active.json` identifies the current
scenario, task phase and human-input directory. A scenario retains:

- `task_1-result.json` and, when present, `task_2-result.json`;
- `dialogue.jsonl` containing complete redacted model and real human events;
- `trace.jsonl`, `trace.db`, task and memory databases;
- four private role workspaces, durable human exchanges and search audit files.
- immutable `task_N-artifacts/manifest.json` plus captured file blobs, and inline task
  artifact/memory/failure capture references; old runs without captures remain unknown;
- `state-snapshot.json`, when saved, is scenario-close diagnostic state including
  unfinished nodes; it is not a delivery, a new score or a larger goal plan.

A task record exposes its own `root_task_id`, subtree, `root_status`,
`root_machine_criteria_present`, nullable `root_machine_accepted`, `quality_status`,
nullable `verified_goal_success`, delivery/review timing and phase summaries. Scenario
summaries are cumulative. The raw second `Engine.run()` report includes earlier state;
do not present it as Task 2-only metrics. See [metrics](metrics.md) for exact boundaries.

Manifest, task-result and dialogue writes do not form a cross-file transaction. A forced
stop may leave raw status `running`; retain that evidence and an explicit stop/process
record instead of fabricating a graceful finish. The public CLI does not resume an old
output directory or answer pending requests automatically.

### Search metering

Additional search grants require an explicit `approval_id`. Repeating the same ID/amount
is idempotent; conflicting reuse is rejected. Resumed HumanIO callbacks must use that
identity and cannot promise exactly-once side effects for arbitrary external callbacks.

`search_budget.SearchBudgetTool` wraps only the registered P1 `web_search` implementation,
preserving its Tool contract. The initial six fresh Tavily HTTP attempts are shared by
all four roles and both tasks of one scenario, with separate counters for tool calls,
actual provider attempts, cache hits, deduplication and denials. Cached evidence retains
its original retrieval time. Additional allowance requires a real human grant; a denied
search result does not prove a human was asked or approved anything. The school's model
allowance is separate from this search counter.

### Offline report consumer API

Recovery, trace and phase evidence are validated against their actual source versions;
see the [performance report](PERFORMANCE.en.md). Historical regression does not validate
later changes. Actual task quality and consumer sign-off require their own evidence;
see [delivery requirements](DELIVERY.en.md).

`eval.report.build_report(root)` validates one saved suite and its authoritative task
records. `generate_report(root, output=None)` writes a derived `.md`/`.txt` report without
credentials, model calls, test execution or continuation. Pending/missing quality is not
turned into success. Runtime artifacts must remain accessible for replay and output review.
An unindexed task result or inconsistent task identity fails archive validation; the
reader does not silently include or ignore it when calculating scores.
The reader also recognizes historical `midtest.json` archives; that filename is read-only
compatibility, not another application entry.

`eval.review` records an append-only actual Console/File HumanIO review under
`reviews/<UUID32>/`, bound to the saved run/code/task/artifact hashes. Re-review requires
the current tip via `--supersedes`. Raw results remain unchanged; report records expose
`raw_quality`, effective quality, `quality_source` and review history. Matching I/O and
hashes establish record consistency, not cryptographic human authentication. Old absent
artifact captures stay `unsupported_historical_artifact_snapshot`.

Offline review publication uses `reviews/.publish.lock`. An existing lock requires
explicit inspection/reconciliation of the interrupted or concurrent writer; readers
and writers do not automatically delete a stale lock or bypass current-tip validation.

`eval.analysis.build_analysis/generate_analysis` support the instructor format as well
as legacy groups. `eval.replay.build_replay/generate_replay` produce a fresh derived JSON
for P2: scenario+task identity, original node/TraceEvent objects, source/line hashes and
human-event indexes, with answered/pending/staged observations separate. They consume
saved phase captures, never live memory.db or mutable workspaces as earlier snapshots.
An export is not actual P2 consumer sign-off. All readers preserve missing parent/state
and unknown quality; sidecar stopping evidence does not replace the raw manifest.

Replay checks the report's semantic `result_content_sha256` before combining a reread
task with effective review data, while retaining the separate original byte hash.
Original required TraceEvent identity/kind/time/status fields are validated; optional
historical fields remain absent as recorded. Fresh replay output uses exclusive file
creation so a concurrent writer's existing evidence is not replaced.

Lower-level `run_once`/`run_all`, worker, telemetry, metrics, snapshot and analysis APIs
remain for framework checks and archived independent experiments. Their per-attempt
`manifest.json`/`result.json` semantics are historical and must not be pooled with the
current continuing-team goal protocol. They do not define a second formal task bank or CLI.

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
def record_human_request(node, request) -> None
def record_human_response(node, response) -> None
def human_exchanges(node, *, require_answered=True) -> list[dict]
```

State changes should go through `transition`: it updates the task table and inserts the audit event
in one transaction. Bypassing it leaves the event log inconsistent with the actual state, and the
timeline in the report stops adding up.

Human exchanges are scoped by project/conversation/task/role and retain the original
agent ID as provenance. Requests are committed before waiting; a response must match
its request ID and cannot overwrite a conflicting recorded answer. The default
`SqliteTaskRepo` additionally exposes `all_tasks()` for Engine traversal; trace storage
does not need a separate task-enumeration method.

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
| `HumanApprovalFeedback` | `tool.py` | scoped note from one actual approved request |
| `IntegrateResult` | `workspace.py` | the outcome of moving artifacts between workspaces |
| `TraceEvent` | `trace.py` | one execution record |
| `MemoryRecord` | `memory.py` | one lesson |
| `HumanRequest` / `HumanResponse` | `human.py` | human involvement |
| `Plan` / `RoleDraft` / `SubtaskSpec` | `plan.py` | the decomposition plan the model submits |

Run `pytest tests/test_contracts.py` and relevant adapter regressions after changing
these fields. A schema-compatible change still needs its actual runtime and consumer
checks; the existence of a test file is not a passing result.

## Quality-facing optional interfaces

- Model-facing `submit_plan` calls require an explicit `deps` array on every
  subtask: `[]` means independent work; producer keys create the file handoff.
  Omission receives correctable `invalid_plan` feedback before creating children
  or roles. Direct Python `Plan`/`SubtaskSpec` callers retain default-empty deps.
  A filename in prose or a historical imported task ID creates no dependency;
  explicit empty arrays can still be logically wrong and need plan review.
- `ToolResult.human_approval` optionally carries `HumanApprovalFeedback` from an
  actual approved request, bound to its request/tool-call/task/role/scope. The gate
  discards tool-supplied attestations and the context renderer shows genuine notes
  separately, without changing `stdout`, `stderr`, errors or pagination JSON.
  This is per-call feedback, not a reusable grant, search-budget increase,
  clarification-ledger entry or human quality approval. Rejection behavior remains.
- `read_file` retains its original bounded preview when called with only `path`.
  Explicit `offset`/`limit` enables UTF-8 byte pagination (`limit`1..8192). `stdout`
  contains only content; `stderr` JSON exposes `offset`, `next_offset`, `file_size`,
  `bytes_returned`, `limit` and `eof`. Follow the returned cursor, not `offset+limit`,
  because pages end on UTF-8 boundaries. Invalid cursors and too-small pages fail.
- `search_files` preserves its bounded JSON match-list `stdout`. `stderr` JSON
  reports `files_started`, `max_file_bytes`, observed large-file skips and a bounded
  relative-path sample, `walk_exhausted` and `output_truncated`. Counts describe
  visited entries; exhausted iteration does not prove unreadable/filtered entries
  were covered. Empty matches do not establish absence in omitted content.
- The formal evaluation registry adds `source_history` (`fs_read`) and `read_source`
  (`network`). Their scenario-owned `SourceEvidenceStore` shares public evidence only;
  it does not grant access to private workspaces. A source/page hash proves captured
  bytes, not that a claim is true. Original retrieval and current serving times differ.
  Source-page offsets/limits count extracted-text characters, unlike file byte
  pagination; follow that reader's `next_offset` and retained metadata identity.
- New source captures retain immutable raw/text blobs and canonical retrieval
  metadata, identified by full SHA256, separately from the latest-URL cache pointer.
  `SourceEvidenceStore.raw_blob`, `text_blob`, `document_version` and
  `retention_status` expose exact stored content/version provenance. Unknown hashes
  return no content; corrupt records fail validation. A new GET has a new retrieval
  identity even when bytes match; cache hits and pagination retain it. Legacy
  backfill verifies only the currently stored row and labels its origin; it cannot
  reconstruct already overwritten history. Captured versions do not certify claims.
- `SearchBudgetTool(..., source_store=...)` optionally records public search evidence;
  omitting it preserves existing behavior and all request charging/grant rules.
- `TaskRunner` calls an optional context `validate_delivery(node, workspace, result)`
  for root submissions before acceptance and again before publication. The teacher
  context checks explicit document-reference closure; ordinary contexts without the
  hook preserve their contract. Rejections use the existing correctable result-file
  feedback. Neither files nor quality approvals are synthesized.
- `FileSession` retries only eligible read-only disappearance/permission errors and
  an exact Windows extended-prefix resolve race, with full path checks on each of
  three attempts. Persistent read failure reports operation/path/native error fields;
  genuine aliases/escapes and unrelated validation errors still reject immediately.

### Scoped ancestor-goal interface

`context.lineage.collect_assignment_references(repo, node)` uses existing `repo.get`
to follow only the current task's `parent_id` chain. It returns an immutable tuple of
`AssignmentReference` in root-to-parent order. Records contain only task/project/
conversation/parent identities, depth, role and the complete original goal; serialization
adds provenance and a UTF-8 `goal_sha256`. Missing, cross-scope, cyclic, mismatched or
depth-inconsistent ancestry rejects explicitly. Dependencies, earlier phases, private
answers and files do not repair the chain. Roots return an empty tuple.

`ContextBuilder.build(..., assignment_references=None)` and `TeamContextBuilder.build`
accept the optional keyword for compatibility with old direct Python calls. Actual
Worker collects and supplies the full delegated chain. A custom builder that cannot
accept it, or omits the exact references from its pinned opening, fails before the
model. The child's current goal, role and acceptance remain unchanged. Full references
are admitted as mandatory before optional content; they cannot be sliced or replaced
by an archive link. Teacher evidence retains exact records, including lineage-only
context. Refresh, re-entry and compaction retain the pinned opening. Trace records
reference IDs/hashes as admission evidence, not observed model use.

Ancestor goals are not the parent's full context. Parent-private answers, approvals,
results, memories, notes and files are not inherited, and no search/action authority
is added. A previous phase in Task 2's dependencies is not an ancestor of that root.
Conflicts remain visible rather than being silently rewritten as a new requirement
or human decision.

### Local lexical quote-check interface

`tools.source_quote.VerifySourceQuoteTool(store, *, workspaces=None)` registers
`verify_source_quote` with action class `fs_read`. The trusted operator can supply an
actual path collection at construction, or register the tool, configure the team and
call `bind_workspaces(team.workspaces.values())` once before task submission. Exact
canonical unaliased role paths are accepted, without guessing directory prefixes;
this accommodates LocalDirs and managed Git worktrees. Unbound operation, foreign
paths and rebinding reject. Model arguments cannot change bindings or select a store.

Arguments are only a full lowercase `metadata_sha256`, a nonempty `quote` and optional
`mode` (`exact` by default, or `collapse_whitespace`). Quotes are limited to 4096 Unicode
code points and 16384 UTF-8 bytes. Model-supplied document bodies, paths, namespaces and
other extra arguments reject. The tool uses the bound store's `document_version` to
validate metadata, index and raw/text hashes, and requires a registered source. It does
not read the latest-cache pointer, make HTTP/search calls, append a read event or spend
a Tavily request.

| `status` | `ToolResult.ok` | `lexical_match` | Meaning |
| --- | --- | --- | --- |
| `matched` | `true` | `true` | Literal present in this exact retained version |
| `not_found` | `true` | `false` | Version checked, literal absent; the claim is not thereby false |
| `unknown` | `false` | `null` | Missing/corrupt/unregistered evidence or unsafe identity; no version fallback |
| `rejected` | `false` | `null` | Invalid arguments or workspace binding, not a factual check result |

`exact` preserves every code point. `collapse_whitespace` collapses Unicode whitespace
runs to one ordinary space and trims outer resulting spaces; numbers, dates, case,
punctuation and Unicode composition remain unchanged. `coordinates` reports the first
zero-based half-open `[start,end)` span in code points and identifies retained extractor
text or normalized derived text, never HTML byte offsets or an occurrence count. Output
retains source/version hashes, original retrieval time and current `checked_at`, plus
comparison-text/quote hashes. The comparison-text excerpt is at most 360 characters and states
`contains_entire_match`; total JSON is bounded to 16 KiB.

Lexical `true`, tool success, factual correctness and human acceptance are separate
judgments. Source text remains untrusted. Presence does not establish attribution,
date/metric scope, completeness or delivery quality. Missing evidence remains unknown;
comparing two model-pasted literals is not source verification.

### Actual-file quotes and delivery binding

`tools.artifact_quote.VerifyArtifactQuoteTool(store, *, workspaces=None)` registers
`verify_artifact_quote` (`fs_read`). The operator binds `team.workspaces.values()`
once after `configure_team`; model arguments cannot change workspaces or stores.
Arguments are `metadata_sha256`, relative `path`, Unicode code-point `start`/`end`
(zero-based half-open), optional `mode` and `expected_artifact_sha256`. A pasted
`quote` or forged receipt is rejected. It reads a regular UTF-8 file in the caller's
own workspace, rejecting directories, private credential paths, aliases/reparse
points and hard links. Files are bounded to 1 MiB; slices to 4096 code points and
16384 UTF-8 bytes.

Output includes whole-file SHA-256, byte/character counts, actual slice hash,
code-point and UTF-8 byte coordinates, exact source identity, comparison hashes
and lexical three-state result. The file must remain stable across the checks;
later edits invalidate the old whole-file identity. JSON is bounded to 16 KiB and
contains no artifact/source prose. `audit_records()` returns deep copies of at most 256 tool-owned
records plus `dropped_records`; model-generated text is not this journal.
`not_found` is a valid lexical negative, not a factual-false judgment.

`eval.quote_delivery.capture_artifact_quote_evidence(scenario_dir, snapshot_ref,
task, journal, *, task_ids, secrets=())` uses the trusted current task tree and that
journal to validate the immutable snapshot, final whole-file/slice identity and
source-receipt structure. Write-once `task_N-artifact-quote-evidence.json` refuses
overwrite. `task_result.artifact_quote_evidence` records its path, SHA-256, status
and counts before human quality review. `matched`/`not_found` apply only to valid
records whose final bytes still match. Prior-phase or changed whole files are
`unapplied`; missing/corrupt evidence is `unknown` or unavailable. A byte-identical
child copy retains the original actor/task/path and is labelled content identity
only, never a manager read. Dropped journal records make evidence incomplete;
without a valid journal, counts remain null. `quotation_coverage`,
`factually_correct` and `quality_pass` always remain null.

### Dependency receipt context

The trusted CLI calls
`TeamContextBuilder.bind_artifact_receipts(repo, artifact_verifier.audit_records)`
once after binding actual team workspaces. Models cannot replace the repository
or journal getter. `eval.quote_handoff.summarize_dependency_checks` uses the
current repository's same-project, same-conversation `DONE` direct dependencies.
Only structured `result.files` also present in accepted `artifacts` qualify;
an explicit direct dependency may be a previous root task. Historical task names
or a transitive relationship alone do not grant access.

The helper reads only declared, recipient-owned integrated UTF-8 files and checks
their current whole-file and slice identities against native tool-owned receipts.
It does not open the producer's private files or share private human answers,
approvals, memory, tool history, source prose or raw quotations. File identity is
not evidence that the receiving role read the file. Later edits invalidate receipt
applicability, and selected-slice matches do not establish full quotation coverage,
factual truth or human approval.

The opening-time summary includes bounded file identities, check counts and sample
receipt IDs. It is limited to 2000 characters and 8000 UTF-8 bytes. Full, compact
and empty alternatives are admitted through the context budget, without truncating
mandatory goals, lineage references or same-task human answers. Missing, dropped,
malformed or omitted history remains unknown; it never establishes that the
producer performed no checks. This summary is separate from final-export evidence.

Published baseline source `be0d3f57…`, including this handoff and the reasoning option below,
completed full native Windows Conda `nlp` regression: 1613 passed, 8 skipped,
no failures/errors and zero Ruff diagnostics, with stable source/tests. Parent
failure recovery covers only its own children after all dependencies are terminal,
preserves `MAX_WAKEUPS` and propagates failures to a fixed point. Acceptance and
quality checks remain required. The actual NewsTask1 retest naturally ended DEAD
with zero formal exports and null quality. The failed checker's downstream editor
was not launched and the parent recovered; the subsequent plan still omitted
review-file dependencies. All 33 receipts remain unapplied to the formal snapshot.
Source-integrity and replay checks do not certify the private candidate content.

The earlier `556d78fc…` (1601 passed, 8 skipped) and `8bc0c2ca…` regressions remain
separate historical checkpoints.
The assisted NewsTask1 trial on `556d78fc…` was operator-stopped after reporter token-budget
exhaustion/retry exposed a failed-dependency scheduling bug. The raw `running`
record and absent final result/export/transport sidecar remain unchanged; content
quality is unknown. This interrupted baseline is separate from the current retest;
interface regressions do not certify teacher quality.

### Source access audit interface

`tools.source_audit.SourceAccessAuditTool(store, *, workspaces=None)` registers
`source_access_audit` (`fs_read`). One-time `bind_workspaces(team.workspaces)` binds
actual `agent_id -> workspace` pairs and checks both caller ID and canonical path,
using actual LocalDirs or managed Git paths. Optional arguments are `agent_id`,
`task_id`, `all_tasks`, `limit` (default 10, maximum 50) and `before_event_id`.
The default is current actor/task. Explicit `agent_id="*"` selects the bound group;
`all_tasks=true` includes its current and previous tasks and cannot accompany `task_id`. This
does not expand private-answer, approval, memory or workspace permissions.

`eval.source_audit_store.SourceAuditStore` uses `mode=ro`, `query_only` and short,
deadline-bounded queries against an existing source store. It does not construct
or migrate the original store or fall back to writable getters. Actor/task filters
precede LIMIT; stable event IDs support continuation. SQL does not write database
content, but SQLite may use existing WAL/SHM coordination: this is not a claim of
filesystem immutability. Missing/corrupt versions and unattributable legacy events
remain unknown.

Reports contain window-scoped successes/failures, fresh/cache counts, distinct
versions, known first/last recorded times and served-interval unions per exact
retained extractor-text version. `counts_for_this_page` and coverage do not mean
complete history or model understanding; follow `pagination.next_before_event_id`.
JSON is bounded to 32 KiB and explicitly marks clipping. An oversized single
event retains identity and a continuation cursor, returning unknown rather than
silently losing it. `factual_grade` remains null. Search origin is not provider
HTTP billing; the search-budget journal remains authoritative for attempts.

### Optional school SSE transport

Formal CLI `--llm-transport buffered|stream` defaults to `buffered`. Explicit
`stream` uses `eval.school_stream.SchoolStreamClient` with the existing async
`chat(messages, tools)` protocol. Its legacy client policy retains `max_attempts=1`
and `retry_transport=False`, with default `completion_cap=None` (explicit limits
below). It sends no `stream_options`, never downgrades to buffered and defaults to
no completion-token cap. Separately selected HTTP retries can increase total HTTP
request limits, as described below; byte/event guards and cumulative node budgets
remain. Legacy Python transport retry requires both a second attempt and
`retry_transport=True`; the CLI does not expose that legacy policy. It, HTTP
retry and Worker retry are separate layers.

`--llm-reasoning-effort none` is accepted only with `--llm-transport stream`.
The Python client accepts `reasoning_effort=None` or `"none"`; the default `None`
omits the wire field, while explicit `"none"` sends a top-level
`reasoning_effort` field. Buffered mode rejects the CLI option before reading
configuration or creating output. `test.json` records `model.reasoning_effort`,
the stream sidecar records `reasoning_effort`, and attempts record
`requested_reasoning_effort`. None of these fields proves provider compliance.

One fixed-request diagnostic on the configured school endpoint and model returned
a complete `read_source` tool call with `none`, without executing it. Default and
`thinking.type=disabled` trials ended with `finish_reason=length`. This limited
observation establishes neither an instructor-task pass nor a general HTTP 504 fix.

The client validates complete SSE, UTF-8, tool arguments and finish state, discards
incomplete output and executes no partial tools. Each scenario's
`stream-transport.json` records independent `call_id`, attempt, status, timings,
finish reason and `usage_raw`/`usage_present`, excluding prompts, content, tool
arguments, authorization and full headers. Its IDs are not `RecordingLLM` dialogue
IDs; do not infer phases from order. Missing/failed usage is not measured zero or
a complete billing total. Original HTTP 504 failures and unfinished instructor
tasks retain their saved status. This transport option is not a reliability or
content-acceptance claim.

### Explicit file inputs

`configure_team(..., require_declared_inputs=True)` installs an Engine-private plan schema and `eval.input_handoff.DeclaredInputs`; omission preserves existing behavior. Each child declares `inputs: [{producer, path}]` or `[]`. Producers must be sibling keys in direct `deps`; each case-insensitive destination path has exactly one producer. Historical task IDs cannot substitute for sibling keys. Input metadata in native DB `meta` binds actual project/conversation/task/parent/role/goal identity.

The optional worker hook checks scoped DONE producer paths in `result.files ∩ artifacts` before native copying; first admission then verifies actual received bytes before a model call. Snapshot/path/alias checks retain the 1MiB file limit. A version declared by a different new producer task may replace bytes still matching a prior native delivery; unknown or locally edited destination bytes cause a conflict before copying. Existing all-artifact/child-to-parent transfer semantics are unchanged.

The `declared_inputs_received` trace/meta receipt contains `contract_sha256` and observed `source_id`, `path`, `sha256`, `bytes`; `final_file_review` and `human_quality` remain null. It proves opening-time delivery only, not immutable historical producer files or final review. The bounded task/file index remains optional context. This mechanism is separate from `quote_handoff`'s recipient-only receipt summary; neither can infer omitted inputs or certify semantic quality.

Stream-only positive-integer options `--llm-completion-cap` and `--llm-max-content-chars` populate `model.completion_cap` / `model.max_content_chars`, the stream sidecar and attempt diagnostics. Defaults are None /2097152. Attempts record `requested_completion_cap`, `max_content_chars`, `max_argument_chars`, `content_chars_received`, `content_bound_exceeded`, `limit_triggered` and `partial_discarded`; character limits count Unicode code points, not bytes. Overflow is checked before appending a fragment; received and accepted counts may differ. `provider_output_limit_compliance` remains null. `input_handoff_policy` separately records explicit input selection.

### Opening quota and tool-request policy

`configure_team(..., opening_budget_tokens=None)` preserves the original policy
when omitted. `teacher_opening_policy` validates explicit integers from 500
to 2147483647, rejecting bools, wrong types and values above the actual policy's
`prompt_ceiling`. It copies `ContextPolicy`, changing `opening_budget_tokens` for
the teacher `TeamContextBuilder` only, without modifying `engine.policy`, the
compactor or shared TokenBudget/EMA. CLI `--opening-context-tokens` does not require
stream mode and retains an effective default 8000. Mandatory content is not
truncated; unfit openings still fail admission.

Plans and `test.json` record `opening_context_policy` with `requested_tokens`
(null when omitted), `effective_tokens`, `prompt_ceiling_tokens`, `scope` and
`token_estimator_calibration="preserved"`. These describe selection, not exact
tokenizer counts or proof that the model used the content correctly.

CLI `--llm-tool-choice` accepts `auto` or `required-when-tools` only in stream mode.
Omission passes no new client option, preserving auto; the Python client defaults
to `tool_choice_policy="auto"`. The `required-when-tools` policy sends required with nonempty
offered tool schemas; tool-free calls omit tools/tool_choice and retain plain
format. This branch is selected before the request, with no failure downgrade.
Required assembly needs complete calls with valid JSON-object arguments, names
from that request's offered set, `tool_calls` finish and DONE. Otherwise it rejects
the response and dispatches no partial calls. Argument JSON Schema, business
correctness, execution and Gate approval remain separate checks.

Plan `llm_tool_choice` preserves the original CLI selection (null when omitted).
Stream `test.json.model` and `stream-transport.json` record `tool_choice_policy`,
`tool_choice_explicit` and `provider_tool_choice_compliance`. Attempts separately
record `configured_tool_choice_policy`, actual `requested_tool_choice`,
`offered_tool_count` and `required_tool_choice_satisfied`. Tool-free actual choice
is null; unevaluated satisfaction remains null. Provider compliance remains null.
The local satisfaction predicate alone is not success: combine `status`, `done`,
`finish_reason` and `partial_discarded`, because response-close failure can still
fail an otherwise assembled response.

### Bounded HTTP retries and usage

`SchoolStreamClient(..., transient_http_retries=0)` retains the legacy path by
default. It accepts exact integers 0/1/2, rejecting bools and wrong types. Nonzero
values cannot combine with legacy `max_attempts=2` or `retry_transport=True`.
CLI `--llm-transient-http-retries` omission is None and passes no new constructor
option. Explicit 0/1/2 require stream and are validated before keys/output creation.

Only 502/503/504 header errors with `decoded_bytes=0`, `first_body_byte_s=null`,
`event_count=0` and `tool_count=0` can retry, at most
`1+transient_http_retries` requests with fixed 5s/15s waits. The whole chat call
shares configured cooperative `timeout_s` (180s in this round); no request renews
the allowance. Insufficient time, expiry or cancellation stops further requests.
Other statuses, transport errors, partial 200/SSE, protocol, length or output-guard
failures never trigger HTTP retry or downgrade, and no partial tools execute.

Plans record `llm_transient_http_retries`, `llm_max_http_attempts_per_call` and
`llm_http_retry_backoff_s`. Stream `test.json.model` and the sidecar preserve
`transient_http_retries`, `transient_http_retries_explicit`, `http_retry_statuses`,
`http_retry_backoff_s` and `http_retry_shared_deadline_s`. `legacy_max_attempts=1`
identifies the old constructor value; model `max_attempts` and sidecar
`max_attempts_per_call` describe the total HTTP limit. `retry_transport=False`
does not mean the independent HTTP policy is disabled.

Attempts record `configured_transient_http_retries`, `legacy_max_attempts`,
`legacy_retry_transport`, `max_http_attempts_per_call`, `shared_call_deadline_s`,
`http_request_started`, `http_retry_eligible`, `http_retry_decision`,
`http_retry_backoff_planned_s`, `http_retry_backoff_elapsed_s`,
`http_retry_backoff_completed`, `http_retry_backoff_error_class` and
`call_deadline_expired`. The enabled path adds `call_elapsed_s`; sidecar
`http_request_attempt_count` counts only `http_request_started=true`, not provider
receipt or billing. Backoff interruption is recorded separately from attempt
status; a single attempt does not establish task completion.

Attempt `usage_present`/`usage_raw` retain that response's observed usage.
`returned_call_usage_complete` describes coverage of the whole returned call.
If only request two or three succeeds, returned LLMResponse uses
`usage_present=False`, `usage_raw={}` and that attempt's
`returned_call_usage_complete` is false, while known successful-attempt usage
remains in the sidecar. Retained numeric token fields or the final success row are
not a complete total; preceding unknown failure usage is not zero. Request/dialogue
IDs, Worker retries and HTTP attempts remain distinct, without order-based identity
or billing reconstruction.
