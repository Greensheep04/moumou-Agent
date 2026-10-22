# Changelog

[简体中文](CHANGELOG.md) · [English](CHANGELOG.en.md)

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
