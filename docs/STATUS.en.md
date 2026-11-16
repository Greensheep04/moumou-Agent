# Project Status

[简体中文](STATUS.md) · [English](STATUS.en.md)

Implementation inventory updated for the 2026-11-16 delivery preparation. Source
presence, executed regression evidence and instructor-task quality are separate.
This page does not certify another member's sign-off or an unperformed test.

## Implementation inventory

| Area | Owner / integration boundary | Current source | Acceptance boundary |
| --- | --- | --- | --- |
| Contracts, task states and delegation | P0 and port consumers | Models, audited state transitions, DAG validation, retry and parent wake-up | Shared-field changes need consumer coordination |
| Scheduler and execution | P0 / P1 | Concurrent task slots, per-node budgets, cooperative cancellation cleanup | Formal HumanIO waiting is not a whole-process hard deadline |
| Context and human clarification | P0 / P2 / P3 | Full required instructions, scoped original-question ledger, pinned answered facts, pending-ID recovery | New recovery regression code remains unexecuted |
| Tool approval and acceptance | P0 / P1 / P2 | Gate, explicit HumanIO decisions and machine checks | Formal runner requires approval for Git staging/commit; generic defaults differ |
| Memory and experience | P0 / P3 | Role-scoped retrieval, reflection, scoring, decay, eviction and observations | Stored lessons or prompt exposure do not establish causal improvement |
| Workspace and tools | P1 | Local/Git workspaces, explicit artifact integration, file/code/Git/search tools and native execution support | Live operations and platform claims need their actual recorded checks |
| Channel and interactive surface | P2 | Reference in-memory channel and HumanIO adapters; formal console/file human ports | Production channel/UI and replay-consumer acceptance require P2 confirmation |
| Trace persistence | P3 | JSONL journal, SQLite projection/recovery, task query, automatic nested span/agent metadata | New hierarchy/concurrency checks remain unexecuted |
| Formal evaluation | P3 | Instructor-first Markdown protocol; continuing four-role teams; isolated scenario state | Six instructor goals need actual execution and quality evidence |
| Saved evidence and offline tools | P3 | Delivery snapshots, phase memory/failure captures, diagnostics, review/report/analysis/replay | New implementations remain untested; historical missing captures stay unknown |

## P3 delivery state

The remaining P3 implementation and relevant documentation are written. The newest
question-recovery, trace, evidence, independent-group continuation and offline tools
have authored regressions but no new execution result. Published `e35e820` has its
own historical regression evidence; that evidence does not validate the current
development tree or prove teacher-task success.

| Outstanding acceptance | Required evidence |
| --- | --- |
| Current code verification | Native Windows / activated Conda regression and relevant static checks, with exact source identity |
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
