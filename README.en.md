# Moumou Multi-Agent Collaboration System

[简体中文](README.md) · [English](README.en.md)

Moumou turns a human request into a group of cooperating agents. Agents delegate
work, use private workspaces, ask people for clarification or approval, and retain
experience for later tasks. Formal evaluation uses the instructor's continuing
team conversations.

Stack: Python 3.11+ · asyncio · SQLite · Pydantic v2 · course DeepSeek / HKGAI API.
Document deliverables use Markdown or plain text.

## Start here

| Need | Guide |
| --- | --- |
| Install and configure the runtime | [Setup](docs/SETUP.en.md) |
| Run instructor tasks and interpret outcomes | [Evaluation](docs/EVAL.en.md) |
| Prepare the final submission and evidence | [Delivery checklist](docs/DELIVERY.en.md) |
| Understand routing, memory and isolation | [Architecture](docs/ARCHITECTURE.en.md) |
| Integrate another module | [Interfaces](docs/INTERFACES.en.md) · [Team responsibilities](docs/roles/TASKS.en.md) |
| Check implementation and outstanding acceptance | [Project status](docs/STATUS.en.md) |

The question-history, trace, evidence and offline review additions are implemented
in the current development tree; their new regression tests have not been executed.
Agent `DONE`, framework tests and reviewed instructor-task quality are distinct.
No claim is made that all six instructor goals have passed.

## Install and configure

From the repository root, activate a Python 3.11+ environment. The target native
Windows setup uses Conda `nlp`:

```powershell
conda activate nlp
python -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit local `.env` with the course-provided values:

```ini
DEEPSEEK_API_KEY=your-course-key
DEEPSEEK_MODEL=your-course-model
DEEPSEEK_BASE_URL=your-compatible-course-endpoint
TAVILY_API_KEY=your-search-key
```

The model configuration supports the course DeepSeek/HKGAI backbone. Live search
currently uses Tavily. Keep `.env` and actual credentials out of Git; commit only
the template. Follow [setup](docs/SETUP.en.md) for file-based human replies,
environment details and optional smoke commands.

## Run the instructor protocol

```powershell
python -m moumou --plan
python -m moumou
```

`--plan` inspects source identities without model calls. The second command starts
real execution and waits for real human answers and quality decisions.
`python -m moumou.eval.cli` is the same formal entry.

The original [instructor bank](eval/tasks/README.md) has three groups, each with
four supplied roles and two sequential tasks. Task 2 reuses its group's identities,
private role workspaces, delivered files and memory. Groups have separate state.
Each group initially shares six fresh Tavily requests across both tasks and all
roles; cache hits are separate, and extra allowance requires an explicit human
grant. The school's model allowance is separate from search metering.

The fifteen self-authored [supplementary tasks](eval/supplementary/README.md) remain
available in the same Markdown format, at lower priority:

```powershell
python -m moumou.eval.cli --tasks eval/supplementary
```

Their scores remain separate from instructor results. Every execution uses a fresh
local `runs/test/<id>/` directory, retaining dialogue, actual human exchanges, task
results, trace, databases and outputs. The CLI does not overwrite or resume an
existing output directory automatically.

## Inspect results and run framework checks

```powershell
# Rebuild a report from existing evidence; no model call or task rerun.
python -m moumou.eval.report --run runs/test/EXISTING_SESSION

# Framework regression, separate from real task evaluation.
python -m pytest -q
```

These are reproduction commands, not new test results. With Make available,
`make unit-test` runs pytest; `make test` and `make eval` run the instructor protocol.
Native Windows does not require Make. Offline review, analysis and replay export
are documented in [setup](docs/SETUP.en.md) and [evaluation](docs/EVAL.en.md).

`tests/` contains code-function, interface and regression checks, including checks
of the task loader and evaluation framework. It is separate from the instructor
bank in `eval/tasks/` and the lower-priority bank in `eval/supplementary/`.
All framework tests passing does not prove that the six instructor goals passed.
Keep meaningful regression sources with their code; generated test output stays local.

## P2 interaction UI (unverified compatibility candidate)

Install the optional UI dependency, then attach to the existing formal file port:

```powershell
python -m pip install -e ".[dev,p2]"
# Terminal 1: keep the formal instructor entry and use a fresh output directory.
python -m moumou --human files --out runs/test/P2_SESSION
# Terminal 2: attach to that same saved run and answer its actual pending requests.
python -m moumou.ui.cli --run runs/test/P2_SESSION
```

P2 renders and stages actual human replies. P3 still owns approvals, consumption,
question recovery, search grants and quality evidence. P1 tools and the formal task
bank remain unchanged. `--demo` is a separate scripted demonstration, not instructor
acceptance. See [P2 integration](docs/delivery/P2_DELIVERY.en.md). These commands and
the new regressions have not been executed for this candidate.

## Integration boundaries

| Area | Responsibilities | Evidence needed for acceptance |
| --- | --- | --- |
| P0 orchestration | Task states, routing, delegation, context, approval and memory evolution | Contract/regression results and actual integration behavior |
| P1 execution | Tools, workspace boundaries, explicit artifact handoff, native process handling and search | Tool contracts, environment and live-operation evidence |
| P2 interaction | Human/private/group channels, interactive presentation and replay consumer | Channel contracts and an actual consumer/interface check |
| P3 data and evaluation | Trace/memory persistence, task sources, real HumanIO evaluation, metrics and reports | Framework results plus saved deliveries and quality decisions |

Shared-contract changes require consumer coordination. Keep APIs and bilingual
documentation consistent, and retain failures and unknowns in the report. Passing
one contract suite establishes that suite's scope; it does not establish complete
system acceptance. Formal Git staging/commit operations require approval under the
formal runner policy, while lower-level Engine defaults are documented separately.

## Repository layout

```text
src/moumou/
  contracts/      shared data models and port protocols
  orchestrator/   states, scheduling, delegation, approval and acceptance
  context/        context admission, compaction and conclusion board
  memory/         experience storage, retrieval, reflection and evolution
  trace/          JSONL journal and SQLite projection
  tools/          file, Python, shell, Git and search tools
  sandbox/        workspace and native execution boundaries
  adapters/       reference model, HumanIO, channel and workspace implementations
  eval/           formal sessions, saved evidence, reports, review and replay
  demo/           deterministic framework demonstrations

eval/tasks/       original instructor Markdown sources
eval/supplementary/ lower-priority sources using the same protocol
tests/            meaningful framework and integration regressions
docs/             architecture, interfaces, operation and delivery guides
runs/             local runtime evidence; excluded from Git
```

Chinese and English runtime and P3 interface guides are maintained for this delivery.
Older changelog entries retain their original milestone scope; use the current
guides for the formal protocol. Detailed module briefs are under [docs/roles](docs/roles/),
context details in
[Context and memory](docs/CONTEXT-MEMORY.en.md), and troubleshooting in the
[FAQ](docs/FAQ.en.md). The [Chinese delivery checklist](docs/DELIVERY.md) and
[English delivery checklist](docs/DELIVERY.en.md) describe the final submission.
