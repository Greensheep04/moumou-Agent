# P3 - Data and Evaluation

[简体中文](P3-数据与评测层.md) · [English](P3-data-and-evaluation.en.md)

P3 makes execution and quality evidence traceable for the team. Keep the course's
required completion rate and execution time, actual human decisions and original
inputs separate from framework implementation tests.

## Owned interfaces and files

| Area | Current responsibility |
| --- | --- |
| `src/moumou/store/trace.py` and Recorder integration | Durable trace storage, exact-task queries and JSONL recovery |
| `eval/tasks/` | Original instructor Markdown: three four-member teams, six sequential goals; default and first priority |
| `eval/supplementary/` | Fifteen self-authored tasks in the same Markdown protocol; lower priority, explicit selection |
| `src/moumou/eval/cases.py` | Source loading and original role/task hashes |
| `src/moumou/eval/team.py` | Fixed-team adapter, continuing private workspaces and real handoff/history |
| `src/moumou/eval/human.py` | Complete redacted model dialogue and real console/file HumanIO |
| `src/moumou/eval/cli.py` | Shared formal entry and per-task/scenario accounting |
| `src/moumou/eval/search_budget.py` | Shared scenario search allowance and audit |
| `src/moumou/eval/source_evidence.py`, `src/moumou/tools/source.py`, `src/moumou/tools/source_quote.py` | Shared public evidence, immutable page versions and local lexical quote checks |
| `src/moumou/tools/artifact_quote.py`, `src/moumou/tools/source_audit.py` and `src/moumou/eval/quote_delivery.py` | Actual-file quotations, recorded source access and final-export evidence binding |
| `src/moumou/eval/quote_handoff.py` | Bounded native receipts for explicitly delivered direct-dependency files at the recipient's opening context |
| `src/moumou/eval/school_stream.py` | Explicitly selected streaming client and separate attempt diagnostics, preserving the LLM response contract |
| `src/moumou/eval/report.py` | Offline reconstruction for current and historical saved evidence |
| `src/moumou/eval/analysis.py` and `replay.py` | Saved instructor failure/memory observations and a read-only source-linked P2 export |
| `src/moumou/eval/review.py` and `session_evidence.py` | Append-only explicit review and immutable phase/diagnostic evidence |

Core storage ports remain `TraceStore.append/query` and the `MemoryStore`
write/search/all/scoring/deletion methods in `contracts/store.py`. Preserve the
exact event and memory scopes. Project-plus-role storage filtering alone does
not establish isolation between every possible Engine agent instance.

The instructor adapter has stable role agent IDs and private role directories; within
that protocol project:role is the continuing persona, while scenario/run databases
separate teams. Generic Engine per-task instances retain the existing sharing policy.

## Current formal protocol

```powershell
python -m moumou --plan
python -m moumou.eval.cli --tasks eval/tasks
python -m moumou.eval.cli --tasks eval/supplementary --plan
python -m moumou.eval.report --run runs/test/EXISTING_SESSION
```

`make test` and `make eval` call the same protocol; `make unit-test` runs framework
pytest. Instructor material is first priority. Supplementary material is retained,
not automatically selected or included in instructor scores. The original tasks
are not rewritten as a predetermined workflow or a script of human answers.

Task 2 keeps its team's four identities, separate continuing directories, memory,
actual prior outputs and readable dialogue archive. Formal execution uses real
questions, permissions and default post-delivery quality review. `ScriptedHuman`
remains a deterministic unit-test utility. It cannot provide formal human approval.

The P3 teacher context adapter and generic builder both retain full ancestor-goal
references. Worker validates the current repository's `parent_id` chain and supplies
exact text, identities and hashes, pinned in root-to-parent order. Dependencies,
historical task IDs and role names do not imply ancestry. Parent-private answers,
approvals, memories and files remain isolated. Insufficient mandatory budget must fail
before the model; visible references do not establish content quality. P0 continues to
own the orchestration semantics consumed by this adapter.

The trusted operator binds `verify_source_quote` once to actual team workspace paths
returned by either LocalDirs or Git providers. It checks retained text for one
`metadata_sha256` in the scenario store, without HTTP, new search, latest-page fallback
or role-supplied documents/paths. Lexical match, non-match and unknown are `true`,
`false` and `null`; whitespace-only normalization still preserves numbers, dates, case
and punctuation. This supports quote location, not factual, risk, scope or human-quality
judgments.

The formal entry also registers `verify_artifact_quote`, producing version-bound
receipts from actual UTF-8 file slices, and `source_access_audit`, reporting recorded
source versions, access successes/failures and served windows for the bound team.
Access records do not establish full reading or understanding. After export,
`artifact_quote_evidence` references separate evidence binding tool-owned receipts
to final file hashes; edits need new checks. This infers neither whole-article
coverage nor quality acceptance. See [interfaces](../INTERFACES.en.md) for consumer
fields and scope.

The opening context can also summarize native receipts for same-scope `DONE`
direct dependencies recorded in the trusted repository, including an explicitly
listed previous root. Only `result.files` also accepted in `artifacts` qualify,
and the helper checks the actual same-byte files in the recipient's own workspace.
Its full, compact and empty variants fit the context budget, with an upper bound
of 2000 characters and 8000 UTF-8 bytes. Missing or omitted history means unknown,
not that a producer did no checks. Private files, human answers and full role
history remain isolated; selected checks do not prove full-file quality.

Model transport still defaults to `buffered`; only explicit `--llm-transport stream`
selects the P3 stream client, without automatic downgrade. Post-cleanup
`stream-transport.json` holds safe attempt metadata and preserves unknown usage.
Its call IDs are independent of dialogue IDs; consumers must not infer a pairing
from list order. See [evaluation](../EVAL.en.md) for operation and limitations.

Explicit `--llm-transport stream --llm-reasoning-effort none` sends the optional
school reasoning setting; omitting it leaves `None` and sends no field, and
buffered mode rejects it. Manifest, stream and attempt records describe the
requested setting, without guaranteeing provider behavior. One fixed-request
school diagnostic returned a complete `read_source` tool call with `none` but
did not execute it; default and `thinking.type=disabled` trials ended with
`finish_reason=length`. This is not an instructor-task pass or a general HTTP 504
fix.

## Metric and replay contract

- Use the actual selected root-goal plan: six for all instructor tasks, fifteen for
  all supplementary tasks; keep unposted and pending quality visible.
- Report agent `DONE`, machine acceptance and actual human-reviewed quality separately.
  Empty root criteria are self-report, not machine verification.
- Keep each task's own subtree/phase calls distinct from cumulative scenario totals.
- Record delivery time, human review wait, total through review and measured usage;
  overlapping spans are not additive wall time and unknown values are not zero.
- Preserve `runs/test/<id>/test.json`, saved task results, dialogue, trace, workspaces
  and search evidence for P2 replay and reviewer access. Credentials stay excluded.
- Do not claim memory benefit or significance from one successful pair or a stored lesson.

The detailed [metric contract](../metrics.md) and [interface contract](../INTERFACES.en.md)
are the shared definitions. Offline report generation does not resume an old session
or answer a pending review. Preserve forced-stop evidence even if the raw manifest
still says running.

Current source `be0d3f57…` passed full native Windows Conda `nlp` regression:
1613 passed, 8 skipped, 0 failures/errors and zero Ruff diagnostics; pytest 180.15s
(wrapper 180.562s), with stable source/tests. The failed-dependency repair only
allows a parent to review its own children after all dependencies are terminal,
keeps `MAX_WAKEUPS` and propagates failures to a fixed point, without waiving
quality checks. Its NewsTask1 retest used unchanged inputs, original roles/goal and
operator-feedback bytes, explicit stream/none, six reused public records and no
new Tavily allowance. It naturally ended DEAD with zero formal exports and null
quality after 4202.906s. Scheduling recovery was directly observed, but the recovery
plan still omitted review-file dependencies. Four large ordinary-content streams
missed complete finish at their deadlines; private candidates still need content
corrections and actual editorial review. Integrity checks are not content acceptance.

Historical checkpoint `556d78fc…` passed full native Windows Conda `nlp` regression:
1601 passed, 8 skipped, 0 failures/errors and zero Ruff diagnostics; pytest
178.56s (wrapper 178.938s), with stable source/tests. The combined checks include
the reasoning setting and receipt handoff. Its assisted NewsTask1 trial used
explicit stream/none, six reused public records and no new Tavily allowance;
reporter token-budget exhaustion/retry exposed a P1 failed-dependency scheduling
bug, so the operator stopped it. Raw `running` and missing final Task1 result,
export snapshot and transport sidecar remain preserved, without fabricated DEAD
closure or verified zero-file export. Content quality is unknown. This interrupted
baseline is not the current retest.

Historical checkpoint `8bc0c2ca…` retains 1538 passed and 8 skipped. The
first assisted NewsTask1 run on that historical checkpoint ended DEAD after two local
reasoning-only stream timeouts; three
subtasks completed, but formal exports remain zero and human quality is unknown.
Three small school-interface probes do not establish an HTTP504 fix. Regression and assisted runs on `8f7f8d23…` are
historical evidence. See the [performance report](../PERFORMANCE.en.md) for source
identity, integration
checks and unresolved content defects. Regression success cannot validate six accepted
goals on another source version or provide P0/P1/P2 sign-off. Corrective supplementary
sessions remain separate; forensic checks of private candidate files from a failed
session do not create formal root artifacts. Unrun phases and unknown quality remain visible.

## P3 delivery package

| Deliverable | Required content |
| --- | --- |
| Code and sources | Reproducible configuration template, the original instructor task hashes, retained supplementary bank and meaningful framework regressions |
| Architecture and interfaces | Routing/delegation, human recovery, identity/memory policy, sandbox limits and saved-evidence consumer fields |
| Performance report | Actual selected plan, verified goals, execution times, observed failures, memory changes and limitations, linked to original saved evidence |
| Reproduction | Native Windows / activated Conda instructions, commands, model identity and non-secret configuration |
| Contribution evidence | Repository URL and genuine Git history/screenshots showing individual development contributions |

[Delivery requirements](../DELIVERY.en.md) define the packaging and acceptance
checklist. [Performance evidence](../PERFORMANCE.en.md) keeps measured results separate
from pending evaluations. Final closed-set measurements must be added after the actual
set is available and run; no placeholder value is a submitted score.

## Design freedom and boundaries

Storage layout, retrieval implementation and report presentation remain design choices.
Instructor prompts, original task text, actual human replies and evidence integrity are
inputs to preserve. Setup/check information in supplementary tasks is retained, while old
scripted-answer metadata does not authorize a real response.

P0 owns scheduling, memory policy and orchestration semantics. P1 owns execution/tools;
P2 owns the interaction/replay interface. P3 adapters consume those contracts and document
cross-module effects. Validation commands are run explicitly; no bundled GitHub Actions
workflow is required for the delivery. Low-level archived experiment APIs support
historical analysis, rather than competing formal task entries.
