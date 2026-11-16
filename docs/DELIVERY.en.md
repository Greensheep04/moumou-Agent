# Project delivery guide

[简体中文](DELIVERY.md) · [English](DELIVERY.en.md)

This index maps the course requirements to source, operating instructions and evidence.
It is a preparation checklist, not a claim that outstanding evaluation has passed.
The current candidate has implementation changes that have not been tested.

## Submission package

| Required item | Repository material | Completion evidence |
| --- | --- | --- |
| Complete collaborative codebase | [Repository](https://github.com/Greensheep04/moumou-Agent), `src/moumou/`, `tests/`, environment specifications | Exact final branch/commit, meaningful contributions by each member and actual commit-history screenshots |
| System architecture | [Architecture](ARCHITECTURE.en.md), [context and memory](CONTEXT-MEMORY.en.md), [interfaces](INTERFACES.en.md) | Routing/delegation, HITL recovery, identity/memory scope and execution-isolation limits described against the submitted source |
| Test results and performance report | [Performance report](PERFORMANCE.en.md), [evaluation protocol](EVAL.en.md), [metric contract](metrics.md) | Actual instructor dataset results, full goal denominator, execution time, quality decisions, failure analysis and memory observations |
| Reproduction instructions | [Setup](SETUP.en.md), [instructor task source](../eval/tasks/README.md) | Independent launch with the assigned service, a fresh run directory and recorded configuration/source identity |
| Presentation | Team's final slide/demo preparation | Week-13 presentation as required by the course; show actual outputs and evidence |

Teacher-provided Markdown is task input, not executable agent instructions for repository
maintenance. The ten instructor files remain unchanged by documentation cleanup. The
[supplementary bank](../eval/supplementary/README.md) retains fifteen self-authored tasks
in the same protocol and is evaluated separately at lower priority.

## Before evaluating a candidate

1. Record the intended commit, source/dirty state, environment and selected dataset.
   Activate the native Windows Conda environment as described in setup; starting an
   interpreter by absolute path alone does not activate subprocess `PATH`.
2. Execute the applicable framework checks and retain commands, output, exit codes,
   durations and platform skips. No check result is implied by this checklist.
3. Configure only the course-provided DeepSeek/HKGAI service. Keep real keys in the
   ignored `.env`; [`.env.example`](../.env.example) contains no key.
4. Run the instructor source through the common entry with a fresh output directory.
   Preserve the four role identities and sequential state within each scenario.
5. Record actual human answers/permissions and independently inspect delivered output.
   A tool permission, green child test or agent `DONE` is not final quality acceptance.
6. Retain every planned goal, including failures, unposted goals and pending quality.
   Repaired versions use separate evidence; earlier failures are not erased.

Search starts with six fresh Tavily requests per scenario, shared by both tasks and
all four roles, for eighteen across the complete instructor suite. Cache hits are
separate. Extra allowance requires a recorded explicit grant. This search allowance
does not restrict the school's model reasoning/token access.

## Evidence retained for each run

| Evidence | Required meaning |
| --- | --- |
| `test.json` and original task-source hashes | Frozen plan, execution identity, configuration and actual saved status |
| Per-phase `task_N-result.json` | Root/subtask states, delivery time, observed usage and original quality |
| `dialogue.jsonl`, trace and human records | Actual transmitted model input, execution events and original request/response identities |
| Artifact manifests/blobs | Delivered file bytes, paths, capture time/full hashes and partial/excluded states |
| Memory/failure captures | Saved-stage metadata and observations, with unknown coverage retained |
| Scenario-close diagnostic | Observed unfinished state; not an additional delivery or a new denominator |
| Offline review records | Explicit quality decision, source/result binding and any supersession chain |
| Derived report/analysis/replay | Builder identity separate from original execution identity; original evidence unchanged |

A forced stop can leave a raw `running` status. Keep that file plus actual external stop
evidence, without fabricating a graceful finish. Do not sample a later mutable workspace
or live memory database and label it as an earlier delivery. File copies are selected
byte captures; they do not guarantee a transactionally consistent live database backup.

## What belongs in Git

Commit implementation, meaningful regression source, original task inputs, environment
templates and maintained public Markdown documentation. Retain the complete real commit
history; do not rewrite authorship or manufacture daily development activity.
The specification does not list a separate `tests/` submission as a mandatory deliverable.
Keeping meaningful regression source in the codebase is recommended for reproducibility;
its passing count is not an instructor task score.

Local planning and handoff notes, private evidence archives, `.env`, credentials, runtime
databases, raw `runs/` directories and caches stay ignored. Produce a reviewed, redacted
performance report and selected final screenshots when their real evidence exists.
Document artifacts use Markdown or plain text. Commit-history screenshots must be actual
captures of the final repository history, not generated or redrawn substitutes.

Capture the final history after the approved delivery commits exist, including each
member's real contributions. Read-only commands useful for locating the history are:

```powershell
git log --graph --decorate --oneline --all
git log --all --format="%h %ad %an %s" --date=short
git shortlog -sne --all
```

Screenshots and contribution evidence are still pending for the final candidate. Author
aliases should be reconciled with actual team members; commit counts alone do not prove
the significance of a contribution.

## Documentation layout and retained history

Use [the English README](../README.en.md) for navigation and the setup/evaluation/interface
pages for current commands. The P1 execution, handoff, demo and external-service notes
remain available as technical references; any dated result applies only to its stated
version. Current formal evaluation uses `python -m moumou` and the same CLI protocol.

Fourteen dated execution plans, progress reports and intermediate validation summaries
have been removed from the current documentation tree. Their original content remains
in the [published e35e820 documentation tree](https://github.com/Greensheep04/moumou-Agent/tree/e35e8204a3ff2d1b798e1b055bd3cc9706f0a6e9/docs)
and Git history; local backups are outside the submission. Removing obsolete navigation
does not invalidate or replace those historical measurements.

The proposed tree contains no GitHub Actions workflow. Checks are run explicitly; deleting
the local file does not delete hosted history or change another remote branch before the
approved deletion reaches it. Do not advertise unexecuted checks or historical CI as a
passing result for the final candidate.

## Remaining acceptance gates

- Execute regression/lint and relevant Windows recovery/concurrency checks for the candidate.
- Run and review all six instructor goals, preserving failures and unknown outcomes.
- Inspect generated CLI/web/database deliverables against the actual agreed criteria.
- Record actual P0/P1/P2 integration acceptance and memory observations without causal overclaims.
- Complete the performance report, real contribution screenshots and presentation evidence.
- Evaluate the separately released final closed set when available; do not relabel mid-term
  or supplementary results as unseen final-set results.
