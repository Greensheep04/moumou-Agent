# P2 interaction and channel: compatibility candidate

[简体中文](P2_DELIVERY.md) · [English](P2_DELIVERY.en.md)

This working patch retains the author's original `6f53165f720675311d1d21ab7a25e8e95f81dbc5`
commit and protects P1/P3 behavior against current main `0e030bd`. Nothing has been
staged, committed or merged. New regressions are written but unexecuted. Measurements
from the original PR's older directory are not results for this candidate.

## Integration boundaries

P2 supplies persistent messages, human request queues, terminal rendering and read-only
replay. P1 keeps tools/workspaces/execution; P3 keeps instructor sources, formal sessions,
approval/response evidence, search allowances and quality metrics. The patch does not
replace Engine's default bus, change existing P1/P3 source or add the old YAML bank.

| Module | Responsibility and limit |
| --- | --- |
| `bus/sqlite.py` | Persist before delivery, exact-pair DM history, optional project/conversation and subscription scope |
| `agents/human.py` | Original-ID futures/queues, explicit response validation and cancellation; not a durable recovery backend itself |
| `ui/cli.py` | `--run` attaches to the formal file port; `--demo` keeps the separate scripted demonstration |
| `ui/session.py` | Read actual requests/state and safely stage real input; never consume, authorize or score on behalf of the backend |
| `ui/replay.py` | TraceEvents/JSONL, task-specific TraceStore, formal run directories and P3 replay inputs |
| `tests/test_p2*.py` | Unexecuted P2 boundary regressions, distinct from instructor scores |

The original root-level test moves to `tests/test_p2.py`. Shared Channel assertions
stay intact; the fixture supplies isolated SQLite databases and closes them.

## Optional installation

From an activated environment:

```powershell
python -m pip install -e ".[dev,p2]"
```

Rich is declared only in the `p2` extra. P1/P3 core/dev dependencies and existing lock
files are unchanged. Real `.env` credentials were not copied into the compatibility
worktree; no dependency was installed during patch authoring. Configure the assigned
course service in the appropriate directory using [setup](../SETUP.en.md).
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

No pytest, lint, compilation, CLI probe, model or network evaluation ran for this patch.
Later checks must cover DM/session isolation, IDs/options, cancellation, atomic response
publication, the real P3 file port, replay identity and native Windows terminal input.
Unchanged source does not replace runtime regression evidence for P1/P3.

The original author commit is not squashed or rebased. Compatibility fixes remain separate;
actual diffs and commit scope require user confirmation. Main's primary checkout remains
untouched. See [interfaces](../INTERFACES.en.md), [evaluation](../EVAL.en.md) and
[delivery requirements](../DELIVERY.en.md).
