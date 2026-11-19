# P2 interaction and channel: compatibility integration record

[简体中文](P2_DELIVERY.md) · [English](P2_DELIVERY.en.md)

This page preserves design and handoff notes from the initial P2 compatibility stage.
The author's original `6f53165f720675311d1d21ab7a25e8e95f81dbc5` commit is retained;
`0e030bd` was that stage's P1/P3 baseline, not the current source identity. Versioned
framework checks, formal Rich sessions and replay evidence have since been recorded.
The [performance and verification report](../PERFORMANCE.en.md) is the authority for
latest integration checks, original failures and remaining content defects. Numbers
from the original PR's older directory do not establish later results or member sign-off.

## Integration boundaries

P2 supplies persistent messages, human request queues, terminal rendering and read-only
replay. P1 keeps tools/workspaces/execution; P3 keeps instructor sources, formal sessions,
approval/response evidence, search allowances and quality metrics. The initial patch
retained Engine's default bus and the then-current P1/P3 baseline without restoring
the old YAML bank. Subsequent integration fixes have their own source-bound evidence.

New handoff information does not expand P2's consumer boundary. Worker creates ancestor
goal references from a valid parent chain; P2 retains source IDs/hashes separately from
the current goal, without rendering them as new human answers or permissions.
Parent-private answers, approvals, memories, results and files are not shared through
references. Public-source tools share only the group store. The operator binds
`verify_source_quote` once to actual role paths; P2 does not replace bindings, stores
or authorization on a role's behalf.

| Module | Responsibility and limit |
| --- | --- |
| `bus/sqlite.py` | Persist before delivery, exact-pair DM history, optional project/conversation and subscription scope |
| `agents/human.py` | Original-ID futures/queues, explicit response validation and cancellation; not a durable recovery backend itself |
| `ui/cli.py` | `--run` attaches to the formal file port; `--demo` keeps the separate scripted demonstration |
| `ui/session.py` | Read actual requests/state and safely stage real input; never consume, authorize or score on behalf of the backend |
| `ui/replay.py` | TraceEvents/JSONL, task-specific TraceStore, formal run directories and P3 replay inputs |
| `tests/test_p2*.py` | P2 boundary regressions; executed versions and scope are recorded separately from instructor scores |

The original root-level test moves to `tests/test_p2.py`. Shared Channel assertions
stay intact; the fixture supplies isolated SQLite databases and closes them.

## Optional installation

From an activated environment:

```powershell
python -m pip install -e ".[dev,p2]"
```

Rich is declared only in the `p2` extra. P1/P3 core/dev dependencies and existing lock
files were retained by the initial patch. Real `.env` credentials were not copied into
the compatibility worktree; no dependency was installed during that initial authoring
stage. Later validation environments and results are recorded in the performance report.
Configure the assigned course service in the appropriate directory using [setup](../SETUP.en.md).
UI-specific regressions skip when Rich is absent; a skip is not a passing result.
Install the corresponding extra before full verification.

## Attach to an instructor session

Both terminals must refer to the same run. Start a fresh session with the unchanged entry:

```powershell
# Terminal 1: P3 owns the original instructor protocol.
python -m moumou --human files --out runs/test/P2_SESSION

# Terminal 2: attach to that existing session, without starting a model.
python -m moumou.ui.cli --run runs/test/P2_SESSION
```

The frontend follows the actual scenario/phase in `active.json`, reads original
`human/requests` and `human/pending`, and renders request options. Validated human
input is published under the original ID in `human/responses` without overwriting
another controller. Quitting creates neither default permission nor invented answers.

Use the original ID displayed by the UI; parallel questions never rely on an implicit
current request:

```text
/pending
/answer REQUEST_ID actual answer or original option
/approve REQUEST_ID optional note
/reject REQUEST_ID rejection note
/history
/quit
```

Approve/reject apply only to permission or review requests; questions use `/answer`.
Empty text, mismatched IDs or answers outside the original options are rejected while
the request remains available for correction.

- A pending file is saved request evidence, not proof of a live backend.
- Staged input is not consumption; only backend consumed records/events establish that observation.
- P3 still validates, redacts, restores its ledger, applies `/grant-search N` and records quality.
- Closing the UI does not silently terminate the independent backend or answer its pending questions.
- The file port has no steering-command transport; attachment does not invent a `/steer` queue.

History retains original records and source labels. Model/tool events are not fabricated
Agent group-chat messages.

The quote tool's ordinary ToolResult/trace must preserve three lexical values: `true`
means a match in the selected retained version, `false` means the literal was absent
there, and `null` means unknown evidence or a rejected request. `ToolResult.ok=true`
can describe valid `not_found`; it must not display as factual verification passed.
The tool makes no HTTP/search call or latest-page fallback. Coordinates identify
retained extractor text or whitespace-normalized derived text, and original retrieval
time differs from current check time. Display/replay/quality records must not promote
a literal match into human acceptance or extra search allowance.

## Demonstration and replay

```powershell
python -m moumou.ui.cli --demo
python -m moumou.ui.replay runs/test/P2_SESSION --scenario scenario_1 --task t1 --tree
```

The scripted demonstration needs no model and is not instructor quality acceptance.
Replay preserves scenario-plus-task identity, original span/parent fields and raw versus
effective quality. Missing evidence stays unknown; ambiguous task IDs need a scenario.
No zero duration, missing parent or accepted outcome is manufactured.

## Channel scope and callbacks

Production consumers should bind project/conversation or subscribe with explicit scope.
The unbound two-argument subscribe remains protocol-compatible, not authenticated tenant
isolation. Group history with empty members is an explicit operator conversation view;
it is not a private viewer's authorization filter. DM history contains only direct
messages between the selected pair, excluding broadcasts and third parties.

Async callbacks are awaited with a bound. Default synchronous callbacks retain event-loop
thread semantics and must return promptly. Blocking callbacks that are safe to run in
threads may explicitly opt into offloading. Dropping a timed-out subscription does not
kill an OS thread or provide hard preemption for arbitrary callback code.

## Verification and provenance

The initial unexecuted pytest/lint/CLI/live-session state belongs to patch authoring.
Native Windows regressions, P3 file-port integration, Rich sessions and replay records
have since been saved; see the performance report for exact source and scope. Framework
checks, live flow, content quality and member sign-off remain separate. Later successful
checks do not erase earlier failures.

At the November 18 14:52 cutoff, combined 8f regression is 1354/8/0 with clean Ruff and
all validation model backends/Rich UIs stopped. Separate original-News2→supplementary-S2T1
is DONE with three files and backend/UI exit0. The independent review saved afterward
does not change raw null human quality. Fund1's
numeric backend exit remains unknown; UI exit0 does not imply backend exit0. Pending
files also do not establish a live process. Display actual failed roots, zero formal
exports, unrun phases, forensic candidates and null quality unchanged. Browser
functionality and lexical matches do not become human acceptance. P2 teammate sign-off,
Final Test Set and contribution screenshots still need their own evidence; see the
performance report for actual identity and content findings.

The original author commit and later compatibility history are retained. New diffs,
commit messages and scope require user confirmation. See [interfaces](../INTERFACES.en.md), [evaluation](../EVAL.en.md) and
[delivery requirements](../DELIVERY.en.md).
