# Project Status

[简体中文](STATUS.md) · [English](STATUS.en.md)

Status updated on 2026-11-18; runtime stop cutoff is 14:52. Implementation, source-bound framework regression
and instructor content acceptance are separate. This page does not certify another
member's sign-off. The [performance and verification report](PERFORMANCE.en.md) is
the authority for published baselines, frozen live runs and subsequent fixes.
At this cutoff the three 8f context runs and separate original-News2→supplementary-S2T1
are closed. All validation model backends/Rich UIs stopped; standalone News2 is DONE
with three files. The subsequent independent review supports the core corrections with
two editorial clarifications; raw human quality remains null.

## Implementation inventory

| Area | Owner / integration boundary | Current source | Acceptance boundary |
| --- | --- | --- | --- |
| Contracts, task states and delegation | P0 and port consumers | Models, audited state transitions, DAG validation, retry and parent wake-up | Shared-field changes need consumer coordination |
| Scheduler and execution | P0 / P1 | Concurrent task slots, per-node budgets, cooperative cancellation cleanup | Formal HumanIO waiting is not a whole-process hard deadline |
| Context and human clarification | P0 / P2 / P3 | Full required instructions, scoped original-question ledger, pinned answered facts, pending-ID recovery | Recovery regressions are recorded against executed source; they are not human quality acceptance |
| Tool approval and acceptance | P0 / P1 / P2 | Gate, explicit HumanIO decisions and machine checks | Formal runner requires approval for Git staging/commit; generic defaults differ |
| Memory and experience | P0 / P3 | Role-scoped retrieval, reflection, scoring, decay, eviction and observations | Stored lessons or prompt exposure do not establish causal improvement |
| Workspace and tools | P1 | Local/Git workspaces, explicit artifact integration, file/code/Git/search tools and native execution support | Live operations and platform claims need their actual recorded checks |
| Channel and interactive surface | P2 | Reference in-memory channel and HumanIO adapters; formal console/file human ports | Production channel/UI and replay-consumer acceptance require P2 confirmation |
| Trace persistence | P3 | JSONL journal, SQLite projection/recovery, task query, automatic nested span/agent metadata | Hierarchy/concurrency checks and live-session evidence are separated by version |
| Formal evaluation | P3 | Instructor-first Markdown protocol; continuing four-role teams; isolated scenario state | Six instructor goals need actual execution and quality evidence |
| Saved evidence and offline tools | P3 | Delivery snapshots, phase memory/failure captures, diagnostics, review/report/analysis/replay | Verification is source-bound; historical missing captures stay unknown |
| Ancestor-goal references | P0 / P3 context interface | Exact goals/hashes from the current repository's valid parent chain, pinned opening and teacher private archive | No parent-private answers, approvals, memories or files; visibility is not model use |
| Lexical quote checking | P3 / P1 tool interface / P2 display | `verify_source_quote`, bound once to actual role paths, reads an exact source version | No HTTP or new search; match/non-match/unknown is not factual or human-quality judgment |

## P3 delivery state

Question recovery, trace, saved evidence and offline tools have versioned regression
and real instructor-session records. Later gate, explicit-dependency, source-retention
and search-coverage fixes are validated separately from the earlier frozen source.
The [performance and verification report](PERFORMANCE.en.md) records execution
identity, latest integration checks and unresolved content defects. Historical
framework results establish neither six accepted goals on one final version nor
P0/P1/P2 consumer sign-off.

The `6a830670` 1307/8 and lineage-only `4c99184f` 1325/8 results retain their own scope.
Latest combined `8f7f8d23` completed 1354 passed, 8 skipped, 0 failed and clean Ruff,
with stable source/tests. Framework success is not content acceptance.

Current 8f assisted records: Fund1 root DONE with four formal files and two remaining
content corrections. Its numeric backend exit was not recovered and remains unknown;
UI exited 0 and native observation confirmed no longer running. News1 and Fund2 roots
ended DEAD after HTTP504, with zero formal exports, backend exit1/UI exit0; that news
Task2 did not run. News forensic core content is supported with NC01 overclaimed quote-
verification scope. Fund2 has four content corrections; its nine-slide candidate's
19 passing browser-function observations remain forensic only. Closed source audits
confirm zero new Tavily/added grants for all four. Standalone News2 is DONE with three
formal files and backend/UI exit0; independent review supports core corrections with
N2C-01/02 qualifications. Human quality stays null; archival requires final records
and scope review, not automatically ready when
backends stop.

The old `6a830670` news/semiconductor reviewed supplementary sessions ended `DEAD`
after repeated HTTP 504 failures, with zero formal root exports. News Task 2 did not
run. Local forensic checks of recovered candidates do not change those formal results
or fill quality fields with success. Later supplementary sessions retain separate
accounting from the canonical six goals.

| Outstanding acceptance | Required evidence |
| --- | --- |
| Final submitted-code verification | Native Windows / activated Conda regression and relevant static checks, with exact source identity matched to the performance report |
| Instructor task completion | Actual saved deliveries and real quality decisions for the selected full goal plan |
| Generated application correctness | Independent checks of the saved CLI/web/database outputs and known failure boundaries |
| Cross-module integration | Actual P0/P1/P2 consumption and confirmation of shared scope/record semantics |
| Memory evolution report | Saved phase observations and an honest comparison; no causal inference from lesson counts alone |
| Final Test Set | Released original data, a separate real run and its formal report |
| Submission package | Repository link, architecture, performance report and genuine collaborative commit-history screenshots |

No pending item is replaced with a fabricated result. Refer to the
[delivery checklist](DELIVERY.en.md) for the final package and evidence requirements.

## Reproduction entry points

```powershell
# Source inspection only.
python -m moumou --plan

# Real instructor conversations, model calls and actual human interaction.
python -m moumou

# Framework regression; distinct from teacher-task quality.
python -m pytest -q

# Read existing results without rerunning the task.
python -m moumou.eval.report --run runs/test/EXISTING_SESSION
```

`make test` / `make eval` use the formal task protocol; `make unit-test` uses pytest.
The default bank is `eval/tasks`. The retained fifteen-task supplementary bank
requires explicit `--tasks eval/supplementary` and has a separate denominator.

See [setup](SETUP.en.md), [evaluation](EVAL.en.md), [metrics](metrics.md),
[interfaces](INTERFACES.en.md) and [team responsibilities](roles/TASKS.en.md).
