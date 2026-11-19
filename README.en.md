# Moumou Multi-Agent Collaboration System

[简体中文](README.md) · [English](README.en.md)

Moumou turns a human request into a group of cooperating agents. Agents delegate
work, use private workspaces, ask people for clarification or approval, and retain
experience for later tasks. Formal evaluation uses the instructor's continuing
team conversations.

Stack: Python 3.11+ · asyncio · SQLite · Pydantic v2 · course DeepSeek / HKGAI API.
General documents use Markdown or plain text; the instructor's semiconductor task
explicitly requires HTML slides.

## Start here

| Need | Guide |
| --- | --- |
| Install and configure the runtime | [Setup](docs/SETUP.en.md) |
| Run instructor tasks and interpret outcomes | [Evaluation](docs/EVAL.en.md) |
| Prepare the final submission and evidence | [Delivery checklist](docs/DELIVERY.en.md) |
| Understand routing, memory and isolation | [Architecture](docs/ARCHITECTURE.en.md) |
| Integrate another module | [Interfaces](docs/INTERFACES.en.md) · [Team responsibilities](docs/roles/TASKS.en.md) |
| Check verified versions and outstanding acceptance | [Performance and verification](docs/PERFORMANCE.en.md) |

As of 2026-11-18, original published `085b12e`, research-reuse source `e59ee40c…`
and assisted revisions on `6a830670…` retain separate evidence. Fund1 on 6a formally
delivered four files but still needs content corrections. The revised news Task1
and Fund2 roots ended DEAD after HTTP 504 errors, with zero formal exports. The
seven news Task1 child candidates support the core content on independent review;
they are still not a formal delivery. That news run never started Task2.
Human-quality decisions remain unknown.

Current source `be0d3f57…` completed full native Windows Conda `nlp` regression:
1613 passed, 8 skipped, 0 failures/errors and zero Ruff diagnostics. Pytest took
180.15s (wrapper 180.562s), with unchanged source/tests. Failed-input recovery is
limited to a parent's own children after all dependencies finish, retaining the
wakeup limit and propagating failures to a fixed point. It does not bypass quality
checks. Its assisted NewsTask1 retest naturally ended DEAD after 4202.906s with
zero formal exports and unknown quality. The repaired scheduler prevented the
failed checker's downstream editor from running and allowed the parent to recover;
the recovery plan still omitted required file handoffs. Four streamed requests
returned large amounts of ordinary content but no complete finish before their
deadlines. Candidate review also found stale time framing, an unsupported causal
claim and incomplete final editorial review. These failures remain separate from
framework regression; see [performance](docs/PERFORMANCE.en.md).

Historical checkpoint `556d78fc…` completed the native Windows Conda `nlp` full framework
regression: 1601 passed, 8 skipped, 0 failures/errors and zero Ruff diagnostics.
Pytest took 178.56s (wrapper 178.938s); source and tests stayed unchanged across
checks. This includes the bounded native-receipt handoff between direct
dependencies, verification guidance and explicit school-stream reasoning option.
Its assisted NewsTask1 trial, using explicit stream/none, six reused public records
and no new Tavily allowance, was stopped by the operator after reporter token-budget
exhaustion/retry exposed a P1 bug that released dependency consumers too early.
The unchanged raw manifest still says `running`; no final Task1 result, export
snapshot or stream sidecar was produced. This is an interrupted failure baseline,
with unknown content quality, not a naturally closed DEAD result or verified
zero-file export. That interrupted baseline remains separate from the later
naturally failed retest. Framework regression is not a teacher-task
pass or proof of a general HTTP504 fix. See [performance](docs/PERFORMANCE.en.md)
for the separate stop evidence and versioned outcomes.

Historical checkpoint `8bc0c2ca…` recorded 1538 passed, 8 skipped, 0 failed
and zero Ruff diagnostics; pytest took 179.14s (wrapper 179.734s), with stable source/tests across
checks. The formal entry now registers `verify_artifact_quote` for actual file
slices and `source_access_audit` for recorded access, and binds receipts to final
exported file versions. Streaming is optional; the default remains `buffered`.
Three short school SSE checks succeeded: text, tool assembly and a tool-result
round trip. They establish those small-input capabilities, not an HTTP504 fix. The first assisted NewsTask1 run on that historical checkpoint ended DEAD after two local
reasoning-only stream timeouts; its three subtasks completed but formal exports
remain zero. Human quality remains unknown. See [evaluation](docs/EVAL.en.md) and
[interfaces](docs/INTERFACES.en.md) for integration and evidence fields.

Historical combined source `8f7f8d23…` retains its separate 1354 passed, 8 skipped,
0 failed, zero-Ruff record and actual assisted-run evidence. Its three November 18
context-assisted runs ended: Fund1 DONE with four
formal files but two remaining content corrections; News1 and Fund2 roots DEAD after
HTTP504 with zero formal exports, leaving that news Task2 unstarted. Forensic content
and candidate-browser checks do not change those failures; human quality remains unknown.
A separate original-News2→supplementary-S2T1 run ended DONE with three formal files,
with independent review supporting the core corrections and recommending two editorial
clarifications; it is not continuous original Task2. At the
November 18 14:52 cutoff all model backends and Rich UIs of that assisted round had stopped.
Human quality remains null.
See [performance](docs/PERFORMANCE.en.md). Lexical matches, recorded source access
and framework regression do not establish factual or human-quality approval.
Earlier software tasks, source versions
and assistance modes cannot be pooled into “6/6 passed”; the unseen final closed
set and final delivery sign-offs remain outstanding.

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
currently uses Tavily; reading registered public originals is recorded separately
from new searches. Keep `.env` and actual credentials out of Git; commit only
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

The default is `--llm-transport buffered`. Explicitly select `--llm-transport stream`
for a separate streaming validation; it never automatically downgrades to buffered.
An optional `--llm-reasoning-effort none` requires stream mode. Omitting it keeps
the provider's default; its effect must be verified for the selected endpoint/model.
After cleanup, stream mode writes metadata-only `stream-transport.json`; missing
usage stays unknown. See [evaluation](docs/EVAL.en.md) for configuration and limits.

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

## P2 file sessions and replay

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
acceptance. See [P2 integration](docs/delivery/P2_DELIVERY.en.md). Native Windows Rich
has been used in actual instructor sessions; the e59 backends/UIs exited normally,
and the later 6a supplementary runs consumed real human replies. Two root HTTP 504
failures remain preserved, as do earlier UI read failures and reattachments. Actual
flows, content and a person-operated P2 sign-off still require final-candidate review.

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
