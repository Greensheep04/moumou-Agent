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
automatically shared. Child goals must be self-contained. For missing inputs, use
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

The current recovery, trace and phase-evidence additions have regression source but
await execution. Published e35e820's 905/8 result is a historical baseline, not validation
of the changed implementation. Actual task quality and consumer sign-off require their
own evidence; see [delivery requirements](DELIVERY.en.md).

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
| `IntegrateResult` | `workspace.py` | the outcome of moving artifacts between workspaces |
| `TraceEvent` | `trace.py` | one execution record |
| `MemoryRecord` | `memory.py` | one lesson |
| `HumanRequest` / `HumanResponse` | `human.py` | human involvement |
| `Plan` / `RoleDraft` / `SubtaskSpec` | `plan.py` | the decomposition plan the model submits |

Run `pytest tests/test_contracts.py` and relevant adapter regressions after changing
these fields. A schema-compatible change still needs its actual runtime and consumer
checks; the existence of a test file is not a passing result.
