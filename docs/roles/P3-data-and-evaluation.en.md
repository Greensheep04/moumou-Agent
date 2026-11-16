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
| `src/moumou/eval/report.py` | Offline reconstruction for current and historical saved evidence |

Core storage ports remain `TraceStore.append/query` and the `MemoryStore`
write/search/all/scoring/deletion methods in `contracts/store.py`. Preserve the
exact event and memory scopes. Project-plus-role storage filtering alone does
not establish isolation between every possible Engine agent instance.

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

## Design freedom and boundaries

Storage layout, retrieval implementation and report presentation remain design choices.
Instructor prompts, original task text, actual human replies and evidence integrity are
inputs to preserve. Setup/check information in supplementary tasks is retained, while old
scripted-answer metadata does not authorize a real response.

P0 owns scheduling, memory policy and orchestration semantics. P1 owns execution/tools;
P2 owns the interaction/replay interface. P3 adapters consume those contracts and document
cross-module effects. Existing workflows are unchanged. Low-level archived experiment
APIs are historical analysis infrastructure, not competing formal task entries.
