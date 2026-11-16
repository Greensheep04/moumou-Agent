# Moumou Coding Agent

[简体中文](README.md) · [English](README.en.md)

> Moumou is a coding agent. Hand it a task and it decomposes the work itself, assigns
> subtasks to sub-agents, runs them in parallel, and only delivers the final result once
> every subtask has passed acceptance.

Stack: Python 3.11+ · asyncio · SQLite · Pydantic v2 · DeepSeek / HKGAI API

---

## Project status

| Area | Owner | Status |
| --- | --- | --- |
| Data contracts · task state machine · scheduling · autonomous delegation | P0 | ✅ |
| Human-in-the-loop (approval / question / review) | P0 | ✅ |
| Machine acceptance (command · pytest · file · JSON) | P0 | ✅ |
| Execution log (JSONL, replayable) | P0 | ✅ |
| Context assembly | P0 | 🚧 baseline |
| Memory and experience evolution | P0 | ⬜ |
| Execution sandbox · Git / GitHub · web search | P1 | ⬜ |
| Message bus · CLI | P2 | 🚧 reference impl |
| Persistent storage · instructor task sessions · reviewed metrics | P3 | Protocol implemented; actual task quality recorded separately |

Legend: ✅ done　🚧 in progress　⬜ not started

Per-member tasks, acceptance commands and milestones live in
**[docs/STATUS.md](docs/STATUS.md)**.

## Capabilities

- **Autonomous decomposition** — not a fixed pipeline; the model decides whether to split,
  how many subtasks to create, and who owns each one
- **Instructor teams** — use the four supplied identities per scenario and continue from
  Task 1 to Task 2 with the same group; generated roles remain a lower-level Engine capability
- **Parallel agents** — independent subtasks run concurrently to cut wall-clock time
- **Private workspaces** — each team member keeps a private working directory; declared
  deliverables cross workspace boundaries through the integration interface
- **Human-in-the-loop** — risky actions (deleting files, pushing code, installing packages)
  stop and wait for approval
- **Proactive clarification** — the agent asks when the requirement is ambiguous instead of
  guessing
- **Mid-run steering** — a human can steer or redirect a running task through conversation

## Quick start

The canonical [instructor tasks](eval/tasks/README.md) define three four-member groups,
each receiving Task 1 and then Task 2 with continuing state. From an activated environment:

```powershell
python -m moumou --plan
python -m moumou
python -m moumou.eval.cli --tasks eval/tasks
```

The two launch commands use the same formal protocol. Real human questions and quality
reviews are retained; each group's two tasks share an initial six-request Tavily allowance.
The school's DeepSeek/HKGAI model access is separate from that search allowance. Full dialogue,
private outputs and `test.json` stay under a fresh local `runs/test/` directory. Agent `DONE`
and implementation tests do not establish reviewed task quality. See [setup](docs/SETUP.en.md)
and [evaluation](docs/EVAL.en.md) for real file-based replies and report reconstruction.

The [supplementary bank](eval/supplementary/README.md) retains the fifteen self-authored
tasks in the same Markdown protocol. It has lower priority and runs only when explicitly
selected with `python -m moumou.eval.cli --tasks eval/supplementary`; its results are kept
separate from the instructor's six goals. Both sources use the same launch path and real
human interaction.

```bash
git clone https://github.com/Greensheep04/moumou-Agent.git
cd moumou-Agent

# 1. Activate your Python 3.11+ environment first.
python -m pip install -e ".[dev]"

# 2. No API key needed
make unit-test     # framework regression tests
make demo          # full pipeline against a scripted model, finishes in seconds
make demo-memory   # memory loop: a task fails → reflection → the next one reuses it

# 3. Needs a real model (configure the key first, see below)
make smoke    # verify the key and model name
make test     # instructor tasks, real human interaction and quality review
make eval     # the same formal evaluation entry
```

## Configure your API key (one per person)

Please avoid committing any key. `.env` is gitignored; the repository only ships a template.

```bash
cp .env.example .env
# edit .env and put your key after DEEPSEEK_API_KEY=
```

Use the course-provided DeepSeek or HKGAI configuration:

```ini
DEEPSEEK_API_KEY=your-key
DEEPSEEK_MODEL=your-course-model
DEEPSEEK_BASE_URL=your-compatible-course-endpoint
```

Verify it:

```bash
make smoke
```

It performs three checks: a plain chat round-trip, a request with function calling, and a
listing of the tools the agent advertises. All three passing means the key and model id are
correct. Expected output:

```
[1] plain chat  1.57s   content: 'pong'    tokens: 38 in / 14 out
[2] tool call   1.13s   tool: echo  args={'text': 'hi'}
[3] agent advertises 8 tools: ['ask_human', 'delete_file', ...]
```

## How to plug in

You only need to implement one port; nothing else changes. Full signatures, input/output
fields and implementation conventions are in **[docs/INTERFACES.en.md](docs/INTERFACES.en.md)**.

| You | Implement | Put it in | Acceptance |
| --- | --- | --- | --- |
| **P1** | `Tool`, `WorkspaceProvider` | `src/moumou/tools/`, `src/moumou/sandbox/` | `pytest tests/test_tool_contract.py` |
| **P2** | `Channel`, `HumanIO` (interactive) | `src/moumou/bus/`, `src/moumou/ui/` | `pytest tests/test_channel_contract.py` |
| **P3** | Storage, real-human instructor evaluation and metrics | `src/moumou/store/`, `src/moumou/eval/` | Framework tests plus recorded task/review evidence |

Three conventions:

1. `src/moumou/contracts/` is frozen for now. If it needs to change, please raise it first so
   three implementations do not have to rework at once.
2. Keep each module's implementation in its own directory; `orchestrator/` stays with the
   orchestration layer.
3. Passing contract tests is the definition of done.

Suggested first day:

- **P1**: extract the path check in `tools/builtin.py` into a standalone `LocalJail` that keeps
  the three `../etc/passwd` contract cases green, then add `git`, `git_push`, `web_search`.
- **P2**: implement a persistent `Channel`, point `make_bus` in the contract test at it, then
  build the CLI.
- **P3**: retain trace data, original instructor sources, continuing team state and actual
  quality decisions; distinguish framework regression tests from task evaluation.

## Repository layout

```
src/moumou/
  contracts/     data contracts and port protocols (frozen; see the contract tests first)
  orchestrator/  core: state machine · scheduling · delegation · approval gate · acceptance
  trace/         execution log (JSONL)
  context/       context assembly
  tools/         tool implementations             ← extended by P1
  adapters/      reference implementations        ← replaced by P1/P2/P3
  eval/          task loading · evaluation runner  ← extended by P3
  demo/          runnable demo
eval/tasks/      original instructor Markdown teams and sequential tasks
eval/supplementary/ lower-priority self-authored tasks in the same Markdown protocol
docs/            documentation (see below)
tests/           tests, including three interface contract suites
runs/            runtime artifacts (not committed)
```

## Documentation

Every document is available in both Chinese and English.

| Document | Audience | Contents |
| --- | --- | --- |
| [docs/SETUP.en.md](docs/SETUP.en.md) · [中文](docs/SETUP.md) | **new joiners, read first** | Dependencies, `.env`, verification, commands |
| [docs/INTERFACES.en.md](docs/INTERFACES.en.md) · [中文](docs/INTERFACES.md) | **anyone implementing a port** | Port signatures, input/output formats, conventions |
| [docs/STATUS.en.md](docs/STATUS.en.md) · [中文](docs/STATUS.md) | everyone | Module progress, per-member tasks, milestones |
| [docs/ARCHITECTURE.en.md](docs/ARCHITECTURE.en.md) · [中文](docs/ARCHITECTURE.md) | anyone curious about the design | Mechanism rationale, diagrams, known limits |
| [docs/SUBAGENTS.en.md](docs/SUBAGENTS.en.md) · [中文](docs/SUBAGENTS.md) | anyone asking how sub-agents work | Creation, context, memory, communication |
| [docs/EVAL.en.md](docs/EVAL.en.md) · [中文](docs/EVAL.md) | P3 / report consumers | Instructor protocol, real interaction, metrics and reports |
| [docs/FAQ.en.md](docs/FAQ.en.md) · [中文](docs/FAQ.md) | everyone | Environment, key, git and test pitfalls |
| [docs/roles/TASKS.en.md](docs/roles/TASKS.en.md) · [中文](docs/roles/TASKS.md) | **teammates, start here** | One page: where to start, what changed |
| [docs/roles/](docs/roles/) | P1 / P2 / P3 | One detailed brief per teammate, bilingual |
| [CHANGELOG.en.md](CHANGELOG.en.md) · [中文](CHANGELOG.md) | everyone | What each milestone delivered |

## Architecture in one picture

```mermaid
flowchart LR
    CLI["CLI / Gradio<br/>P2"] --> BUS["Channel<br/>P2"]
    CLI --> HUMAN["HumanIO<br/>P2 · P3"]
    ENG["Engine<br/>P0"] --> SCH["Scheduler<br/>P0"]
    SCH --> RUN["TaskRunner<br/>P0"]
    RUN --> GATE["Gate<br/>approval · trace · timeout"]
    GATE --> TOOLS["Tool<br/>P1"]
    TOOLS --> WS["Workspace<br/>P1"]
    GATE --> HUMAN
    RUN --> REPO[("TaskRepo<br/>SQLite")]
    RUN --> TRACE[("TraceStore<br/>P3")]
```

**Rectangles are the orchestration core; rounded boxes are external implementations.** The core
only reaches them through six ports — swap any implementation and nobody else edits a line.

---

> Environment and configuration: [docs/SETUP.md](docs/SETUP.md) ·
> [docs/SETUP.en.md](docs/SETUP.en.md)
