# Project Status

[简体中文](STATUS.md) · [English](STATUS.en.md)

P3 evaluation update: 2026-11-16. Other module milestones retain their recorded scope.

## Overall progress

| Module | Owner | Status | Notes |
| --- | --- | --- | --- |
| Data contracts and ports | P0 | ✅ done | 8 models, 6 ports, frozen |
| Task state machine | P0 | ✅ done | 8 states, legal transitions, atomic state + audit write |
| Scheduler | P0 | ✅ done | event driven, parallel or serial by dependency |
| Autonomous decomposition and role creation | P0 | ✅ done | the model proposes a plan, the framework runs four checks |
| Approval and human involvement | P0 | ✅ done | approval / question / review |
| Machine acceptance | P0 | ✅ done | 6 kinds of check, automatic retry on failure |
| Execution log | P0 | ✅ done | JSONL, 9 span kinds, replayable |
| Context assembly | P0 | ✅ done | four layers, admission rules, opening budget, three-layer compaction |
| Memory and experience evolution | P0 | ✅ done | conclusion board + reflection + scoring / decay / eviction |
| Execution sandbox | P1 | 🚧 implemented; team integration pending | path checks, file/code tools, process cleanup; Windows/Ubuntu CI passed |
| Git / GitHub integration | P1 | 🚧 implemented; team review pending | Worktree/artifact APIs and P1 demo evidence retained; formal task commands are documented in EVAL.en.md |
| Web search | P1 | 🚧 implemented; live validation pending | Tavily protocol/error tests passed; team owns credential-based validation |
| Message bus | P2 | 🚧 reference impl | in-memory version works, needs persistence |
| Command line interface | P2 | ⬜ not started | |
| Approval interaction UI | P2 | ⬜ not started | |
| Persistent storage | P3 | implemented | JSONL plus SQLite projection and recovery |
| Task sources | P3 | unified Markdown protocol | Instructor 3 teams / 6 goals first; retained supplementary 15 tasks explicitly selected |
| Metrics and reports | P3 | implemented; actual quality stays evidence-based | Real human decisions, phase/subtree accounting and offline reconstruction |

Legend: ✅ done　🚧 in progress / partial　⬜ not started

## Per-member tasks

Each member decides how to implement; only the **major features** and the acceptance method are
listed here.

### P0 — orchestration core

Done: contract layer, state machine, scheduling, delegation, approval gate, acceptance, execution
log, reference tools and workspace.

Remaining:

- goal mode and plan mode
- full-pipeline integration regression
- memory loop verified against the real model (needs a task that fails or takes many rounds)

### P1 — execution and tools

- execution sandbox (path checks → optional Docker backend)
- file and code execution tools
- git tools (commit needs no approval, push does)
- web search tool
- one workspace per agent, including branch merging

Acceptance: `pytest tests/test_tool_contract.py`

### P2 — interaction and channel

- persistent message bus (1:1 and group chat)
- command line interface (message stream, task tree, approval cards)
- approval and question interaction
- execution log replay

Acceptance: `pytest tests/test_channel_contract.py`

### P3 — data and evaluation

- persistent trace and memory interfaces, with explicit scopes and recovery limits
- original instructor Markdown as the default; supplementary fifteen tasks retained in the same format
- continuing four-member groups, real HumanIO and separately recorded quality review
- task/subtask completion, delivery/review timing, measured usage and search audit
- offline reports from authoritative task evidence; no claim that all six goals passed

Formal run: `make test` / `make eval` or `python -m moumou.eval.cli --tasks eval/tasks`.
Framework regression: `make unit-test`. Supplementary runs require explicit
`--tasks eval/supplementary` and keep a separate denominator. See [evaluation](EVAL.en.md).

## Milestones

| Date | Milestone | Contents |
| --- | --- | --- |
| 2026-10-21 | Framework skeleton | contracts, orchestration, acceptance, log, evaluation skeleton, docs |
| 2026-10-22 | Context and memory | four-layer context, three-layer compaction, conclusion board, reflection and eviction |
| TBD | Execution layer | P1's sandbox and tools |
| TBD | Interaction layer | P2's bus and interface |
| TBD | Evaluation runs | P3's task set and metrics |
| TBD | Ablations and report | comparison experiments, performance report |
| TBD | Demo | week 13 |
