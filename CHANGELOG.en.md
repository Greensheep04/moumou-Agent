# Changelog

[简体中文](CHANGELOG.md) · [English](CHANGELOG.en.md)

## 2026-10-23 · Role review

A model may now propose a change to a role, but nothing takes effect without a human
approving it.

### Added

**Trigger**: every three finished tasks, once a run has ended, the model is asked whether
any role description would have changed an outcome. Running at the end rather than
between tasks keeps it from blocking work in flight.

**Structured proposal**: the model submits `role` / `proposed_description` / `reason` /
`evidence` through `submit_revision`; an empty role means "nothing worth changing".

**Human approval**: through `HumanIO.ask(kind="review")`, with the current and proposed
descriptions side by side. Approve bumps the version; reject with a note feeds the next
round, up to three; reject with no note ends the cycle.

**Record**: every proposal and decision lands in the `role_revisions` table and the trace.

**Documentation**: a new "Role evolution" section in the sub-agent document.

### Verified

- 151 automated tests pass (9 new: approve, reject, iteration with a note, empty
  proposal, unknown role, length cap, engine-level trigger)

## 2026-10-23 · Sub-agent hand-off and failure recovery

The hand-off from a sub-agent changed from prose to structured fields, and a parent is now woken
when a child fails so it can re-dispatch.

### Added

**Structured hand-off**: a sub-agent calls `submit_result` with `summary` / `files` / `notes` /
`blockers`; the parent renders those fields, and the cap rose from 800 to 3000 characters. A plain
text reply still works and is wrapped as `summary`.

**Failure recovery**: a `DEAD` child no longer takes its parent down. The parent is woken (twice at
most) and may re-dispatch; the replacement children carry a round prefix `t1.r1.a` so ids cannot
collide with the first attempt.

**Lead prompt**: a dedicated lead prompt spelling out the criteria for "do it yourself or delegate",
that sub-task goals must be self-contained, that acceptance criteria must be one of six forms, and
that a failure should not be forwarded to the human unchanged. The framework rules are appended
after the role text, so they win on conflict.

**Documentation**: a new "Sub-agents" document in Chinese and English.

### Verified

- 142 automated tests pass (new: hand-off rendering, `submit_result` end to end, wake-up and round
  prefix)
- `s2_bugfix` against the live API: the model did call `submit_result`, and the fields landed in the
  database


## 2026-10-22 · Context and memory system

This milestone delivers the context and long-term memory systems: four-layer context assembly,
three-layer compaction triggered by budget, the conclusion board, and an experience memory that
retires itself.

### Added

**Context**: thresholds centralised in `ContextPolicy` (1M window / 600K trigger / 256K target);
token estimation on the vendor's ratios with automatic calibration; three-layer compaction
(deterministic extraction → whole-turn dropping → model summary, the summary is off by default);
an opening-message budget and admission rules.

**Memory**: SQLite storage with character-bigram retrieval; reflection runs only for tasks that
failed, needed retries, or took more than eight steps, and produces structured lessons; scoring,
decay, soft delete and a permanent purge after thirty days; citation write-back so the scoring
actually takes effect.

**Conclusion board**: hand-off notes between instances of one role, scoped precisely to
project:conversation:role.

### Verified

- 127 automated tests pass
- three evaluation tasks run against the live API with a 1.0 completion rate, ¥0.057 in total
- memory loop demo: task A fails and writes a lesson, task B retrieves it and finishes

### Changed

- Chinese retrieval moved from FTS5 trigram to character bigrams: measurement showed trigram is
  sensitive to word order and cannot match two-character queries
- `Budget.max_tokens` default raised from 200K to 12M; it is a runaway guard, not a bill

## 2026-10-21 · Initial framework skeleton

This upload completed the initial framework skeleton. The whole path — task submission, automatic
decomposition, assignment to sub-agents, execution, acceptance and delivery — is connected, and the
remaining modules can be built on top of it.

### Added

**Data contracts**: 8 core models, 6 port protocols, and the task state machine (8 states with a
table of legal transitions).

**Orchestration core**: an event-driven scheduler (parallel or serial by dependency), autonomous
decomposition (the model submits a structured plan; the framework checks for cycles, validates
roles and enforces count and depth limits), roles created at runtime (look before you create, a new
role must justify itself), an approval gate (risky actions pause for a human), machine acceptance
(6 kinds of check with automatic retry), and crash recovery.

**Execution and tools**: 8 built-in tools; one isolated workspace per agent with path-escape checks
everywhere.

**Evaluation**: task YAML format, loader, batch runner, 3 sample tasks, and a completion-rate and
timing report.

**Documentation**: bilingual README, interface spec, architecture note, project status, evaluation
guide, FAQ and three role briefs.

### Verified

- 65 automated tests pass, three of which are interface contract suites
- the live DeepSeek API works (model `deepseek-flash`): plain chat, function calling and tool
  registration all behave
- the end-to-end demo runs, all 3 tasks complete, completion rate 1.0

### Pending integration

These modules already have ports reserved and are waiting for their owners:

- execution sandbox (currently application-level path checks; a Docker backend is pending)
- Git / GitHub integration
- web search
- persistent message bus
- command line interface and approval interaction
- persistent storage and metrics reporting
- the evaluation task set grown to 15
- ablations (memory on/off, parallel on/off)

Owners, acceptance methods and progress per module are tracked in [docs/STATUS.md](docs/STATUS.md).
