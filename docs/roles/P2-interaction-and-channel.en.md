# P2 — Interaction and channel

[简体中文](P2-交互与通信层.md) · [English](P2-interaction-and-channel.en.md)

P2 owns direct/group communication, human interaction and presentation of recorded
execution. The course requires Agent–Human private messaging, multi-agent/human group
chat and proactive clarification/permission handling. A minimal CLI, bot or text UI is
sufficient; elaborate frontend design earns no extra credit.

These responsibilities are fixed. Transport, persistence and replay presentation are
design choices coordinated through the shared contracts.

## Current boundaries

| Area | Contract or implementation | Status boundary |
| --- | --- | --- |
| Agent/human addressing | `contracts/message.py`: Message, Scope, Channel | Shared API |
| Human questions/approvals/review | `contracts/human.py`: HumanIO | Shared API; decision policy belongs to orchestration |
| Question recovery | Optional ResumableHumanIO | Original scoped question only; no reused tool grants |
| Default Channel | `adapters/bus.py`: InMemoryBus | Reference adapter, not persistent production delivery |
| Formal console/file interaction | `eval/human.py`: ConsoleHuman, FileHuman | Current formal task ports with recorded request/response evidence |
| Replay data | `eval/replay.py` | P3 read-only export; actual P2 consumer acceptance remains pending |

Implementation locations are a design choice; document the actual source paths.
Do not infer a persistent transport or finished replay UI from a reference adapter
or an export file.

## Channel contract

Implement `publish(msg)`, `subscribe(agent_id, callback)` and
`history(scope, limit=50)` from [interfaces](../INTERFACES.en.md).
The human member uses fixed ID `human`; Message carries project/conversation/task
identity, sender, recipients, kind, content and metadata.

- Direct messages reach the addressed members without leaking to unrelated subscribers.
- Group/broadcast delivery remains within the intended project and conversation.
- Persistent implementations save before delivery and retain ascending historical order.
- Subscriber callbacks must not stall the bus; cancellation and errors must be surfaced.
- Scope filtering must include project/conversation and membership, not only a display tab.

The in-memory default is useful for framework behavior but does not prove restart
persistence, complete private-channel isolation or production consumer sign-off.

## HumanIO and recovery

`ask(req) -> HumanResponse` shows the original request and returns its explicit answer.
`notify(msg)` presents a notification. Keep these meanings separate:

| Request | Required interaction |
| --- | --- |
| `question` | Non-empty clarification matching the original request ID/options |
| `approval` | Explicit approve/reject for the displayed action and full arguments |
| `review` | Explicit quality/proposal decision, with its evidence and optional feedback |
| `steer` (Channel) | Human adjustment during execution; not an action approval |

The UI displays and records decisions. It does not decide business requirements,
change Gate policy, accept an output automatically or invent a response. Scripted and
auto-approving development adapters cannot supply formal human authorization.

For durable same-task recovery, preserve project/conversation/task/role scope, original
request ID/body and provenance. An optional `resume(req)` may wait for that original
pending question or recover its already consumed response. It does not create a new
ID, reuse an approval or promote clarification into a quality decision. Recovering a
consumed response does not replay its callback; unconsumed callbacks with external side
effects must be idempotent by request ID.

Cancellation should stop active waits/background work while preserving truthful
pending/consumed evidence. A pending file is not proof that a process is alive; a
staged response is not proof the agent consumed it. Matching records also do not
authenticate manually typed versus assisted authorship.

## Formal workflow and P3 handoff

The formal entry is `python -m moumou` (also `python -m moumou.eval.cli`), using real
console/file interaction. Instructor sources are default; the supplementary bank
requires explicit selection. See [setup](../SETUP.en.md) and
[evaluation](../EVAL.en.md) for pending/response file formats.

Formal team members keep stable role IDs/private directories across sequential tasks,
with project:role long-term memory. Generic runtime instances retain their existing
sharing policy. The UI must preserve the recorded scope and identity.

Consume P3 replay records using scenario+task identity, original span IDs/parents and
source/line hashes. Retain original state, distinguish raw from effective reviewed
quality and show absent relationships/measurements as unknown. Do not infer parent
edges, zero durations, successful quality or runtime liveness from missing data.

A viewer may use a timeline, table or tree. It must keep human questions/permissions,
agent completion, machine acceptance and quality decisions distinguishable. P3 export
availability is not evidence that the actual P2 viewer loaded or accepted that schema.

## Acceptance and delivery

Retain meaningful channel/HumanIO regressions. Actual checks should cover DM privacy,
group delivery, project/conversation filtering, restart history, original-ID question
recovery, explicit rejection, cancellation and consumer schema handling. Record the
environment, command, result and known limits when executed.

Current changed interfaces and recovery code still require regression execution and
real consumer sign-off. A minimal UI, passing framework fixture or saved export alone
does not establish teacher task quality. P2 does not own prompts, tool execution,
approval policy or performance scoring.

Use [delivery requirements](../DELIVERY.en.md), [shared interfaces](../INTERFACES.en.md)
and [P3 evidence responsibility](P3-data-and-evaluation.en.md) as the handoff references.
