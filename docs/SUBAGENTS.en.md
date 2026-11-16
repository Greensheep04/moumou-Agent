# Sub-agents and Continuing Teams

[简体中文](SUBAGENTS.md) · [English](SUBAGENTS.en.md)

A running agent combines an assigned task, its own context and a private workspace.
The generic engine creates roles as needed; formal instructor sessions instead
reuse the four original members of each supplied team. The mechanisms below are
implemented in source. New recovery, hierarchy and cleanup regressions have not
been executed in the current development version.

## Delegation lifecycle

```mermaid
sequenceDiagram
    participant L as Parent agent
    participant D as Delegation
    participant R as Task repository
    participant W as Child agent
    L->>D: submit_plan with structured subtasks
    D->>D: validate graph, limits, roles and tools
    D->>R: create children and set parent PENDING
    R->>W: dependencies ready; dispatch
    W->>W: build scoped context and execute tools
    W->>R: submit_result and acceptance evidence
    R->>L: children finish or parent wakes on failure
    L->>L: integrate declared artifacts and continue
```

The plan contains reasoning, role drafts and subtasks. Each subtask specifies a key,
self-contained goal, role, sibling dependencies and machine acceptance criteria.
Validation rejects duplicate keys, unknown/cyclic dependencies, excessive depth or
subtask count, unavailable roles, unknown tools and generic delegation of the lead
role. Optional strict mode also requires non-empty subtask acceptance. Formal team
validation additionally rejects identities outside the instructor's four members.
Validation errors return to the model for correction.

Children use IDs derived from their parent and key. A parent waiting on children
becomes `PENDING`; when dependencies finish it becomes eligible again. Failed
children can wake the parent within the configured wake-up bound. Re-dispatch
uses a round namespace so IDs do not collide.

## Identity, workspace and memory

| Mode | Runtime identity and private workspace | Long-term memory |
| --- | --- | --- |
| Generic Engine | `role@task_id`; a workspace allocated for that instance | Existing project-role namespace, shared by instances of that role |
| Formal instructor team | Stable original role ID; one distinct private directory per team member across Task 1/2 | Project-role namespace for that same continuing persona |

Formal scenarios and fresh run directories have separate state. Project-role
sharing is deliberate; it is not a claim of independent long-term storage for
every generic runtime instance. Workspaces and explicit file integration are
application boundaries, not a universal operating-system security sandbox.

Only declared artifacts cross workspace boundaries. A child does not receive the
parent's private conversation or sibling transcripts automatically. The parent
must provide a self-contained task and explicit dependencies. The formal team
adapter also records prior task context and bounded history, with complete
auxiliary archives in the member's private `.evaluation/` directory.

## Context and answered questions

Context combines the assigned role and framework rules, the full current goal and
acceptance objects, upstream delivery reports, scoped experience/board notes and
the agent's dialogue. Required role/goal/acceptance and recorded same-task answers
are admitted before optional material. Dependency previews, lessons, notes and
formal prior history yield first. If mandatory content cannot fit,
`ContextAdmissionError` is raised before a model call; the current goal is not
silently reduced to 200 characters.

The generic `ContextPolicy` opening default is 8,000 estimated tokens, bounded by
the configured prompt ceiling. Formal team admission uses its actual configured
policy and retains source-linked auxiliary archives. These limits are runtime
configuration, not a provider capability claim.

Original clarification questions and answers are persisted by
project/conversation/task/role. On parent re-entry, retry or process recovery,
answered records return to pinned opening context. Newly answered facts refresh
that prefix during the same loop. Pending scoped questions retain the original
request ID; consumed responses can be recovered without issuing a new question or
re-running their callback. An unconsumed callback must itself be idempotent by
request ID. Unsupported, conflicting or incomplete recovery fails explicitly.

These records are facts from the configured HumanIO port. They do not assert a new
manually typed answer, grant reusable permission or establish quality acceptance.
Tool approvals are evaluated for the current action; rejection feedback is returned
to the model. The formal runner requires approval for Git staging/commit as well
as its other configured critical actions. Generic Engine defaults remain separate.

Runtime compaction preserves the pinned prefix and complete assistant/tool turns.
The generic defaults trigger at 600,000 estimated tokens and target 256,000;
deterministic shrinking precedes dropping old whole turns, and optional model
summarization is disabled by default. Configure these values for the actual course
model; see [context and memory](CONTEXT-MEMORY.en.md).

## Handoff and acceptance

`submit_result` produces a structured `TaskResult`:

```python
summary: str
files: list[str]
notes: str
blockers: list[str]
```

The worker validates explicitly declared result files before publication and
rechecks them after acceptance. Invalid declarations receive correctable feedback;
unchanged existing files can be valid deliverables. Tracked artifact changes and
explicit result files remain distinct sources of publication. Consumers receive
files through the workspace integration interface, including conflict diagnostics.
Generic parent previews are bounded; full records remain in saved evidence.

Machine acceptance failure moves an attempt through retry/final failure according
to its budget. Empty acceptance establishes self-report, so `DONE` alone is not
verified task quality. Formal evaluation saves machine evidence and real quality
review separately. A rejected or incomplete task skips later dependent work in
that group; other groups can continue. Initialization/configuration, cleanup
failure or explicit cancellation can stop the entire suite.

## Experience and evolution evidence

| Record | Scope and purpose |
| --- | --- |
| Working dialogue | One running task's own messages and tool history |
| Conclusion board | Project/conversation/role handoff notes for later instances |
| Task/event/trace history | Durable execution provenance for analysis and recovery |
| Experience memory | Project-role lessons, retrieval, scoring, decay and soft deletion |

Reflection and evolution are controlled by the configured policy. Role revision
is a separate engine capability requiring an explicit HumanIO review; the formal
evaluation runner fixes role review off to preserve the instructor identities.

New observation events distinguish memory records returned by retrieval, selected
by context admission and matched in the actual outbound prompt. Ambiguous,
truncated or multiline matches remain unknown. Selected IDs or prompt appearance
do not prove that the model used a lesson or that memory caused improvement.
Phase snapshots retain saved active metadata and observed events; later readers
must not reconstruct an earlier phase from the live memory database.

## Trace and offline consumers

Recorder supplies nested span parents with async context isolation and top-level
agent metadata. The worker records an actual execution attempt, including its
handled failure/cancellation status. Attempt durations are not the full
assignment-to-delivery wall time. Scheduler shutdown awaits cooperative runner
cleanup before the caller closes storage; this is not a hard process deadline.

Formal deliveries can save immutable selected file bytes and phase memory/failure
captures. A scenario-close state snapshot may include unfinished nodes as diagnostics;
it does not create a delivery or alter the goal plan. Offline reports, analysis,
reviews and P2 replay consume saved evidence, preserving missing historical fields
and unknown quality. A P2 export is a handoff contract, not consumer sign-off.

See [interfaces](INTERFACES.en.md), [metrics](metrics.md),
[evaluation](EVAL.en.md) and the [delivery checklist](DELIVERY.en.md).
