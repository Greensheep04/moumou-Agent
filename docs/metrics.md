# Evaluation metric contract

This contract is for orchestration instrumentation, human interfaces and report
consumers. Formal task evaluation uses the instructor Markdown under `eval/tasks/`
through `python -m moumou` or `python -m moumou.eval.cli`. The authoritative suite
manifest is `RUN/test.json` (`kind: instructor_test` identifies the shared protocol), with saved
`task_1-result.json` and, when present, `task_2-result.json`. Do not reconstruct scores from a
console message or substitute framework pytest counts for task quality.

`dataset_kind` identifies `instructor` or `supplementary`; scenario `source_kind`
and source hashes retain that provenance. The common protocol kind does not turn
supplementary work into instructor evidence. A historical missing label stays unknown.

## Root goals and quality

`planned_root_goals` is the actual number of task texts in the selected source and
scenarios: six for all instructor scenarios, two for one instructor scenario, and
fifteen for the explicitly selected supplementary bank. Its last group has one task.
Supplementary results do not enter the instructor denominator. Unposted,
interrupted and unreviewed goals stay in that denominator. They do not prove an
agent executed or that a person rejected the output.

| Observation | Meaning |
| --- | --- |
| `agent_declared_complete` / `root_status == DONE` | The framework reached its terminal completion state |
| `root_machine_criteria_present` | The root has actual executable criteria |
| `root_machine_accepted` | Saved machine verdict; null when no machine criteria establish one |
| `quality_status` | Pending, explicitly human-accepted/rejected, or otherwise unreviewed quality |
| `verified_goal_success` | True only with recorded quality acceptance and no contradictory root machine failure; null while review is unknown |

Empty acceptance produces self-report. A task may be `DONE` with unknown machine
acceptance and unknown quality. Human approval can establish a separately
attributed quality decision; it does not create a machine check. Rejection keeps
the actual agent state and output, sets the quality outcome to rejected and stops
later tasks. `--quality-review deferred` leaves quality unknown.

Suite `goal_metrics` reports the planned denominator, persisted deliveries,
agent-declared completions, reviewed goals, verified successes and verified goal
coverage. Only true `verified_goal_success` values enter the success numerator.
Pending, missing and unposted observations are separately described rather than
invented as model failures or human rejections.

Failure attribution must inspect the actual transmitted instructions. The formal adapter
keeps the full role prompt, current goal and all acceptance criteria; optional history,
dependencies, board content and lessons yield first, with exact auxiliary content saved
in private `.evaluation/context/<digest-prefix>.json`. Only new filenames use the first
32 digest hex characters; references and validation metadata retain full SHA-256,
different-content collisions are rejected, and existing archives are not renamed.
An unfit mandatory payload raises
`ContextAdmissionError` before an LLM call. Requirements omitted from the framework's
transmitted context are system/context failures, not evidence of model noncompliance; retain those unfinished
goals in their original planned denominator and keep later repaired runs separate.

## Subtasks and continuing state

Each task record contains only that root's subtree. `observed_subtasks` excludes
the root; `observed_subtasks_done` counts its observed DONE children. A node's
machine-criteria count is not itself a machine verdict. Use the saved acceptance
trace when evaluating whether individual checks actually ran.

Task 1 and Task 2 intentionally share their scenario's four-member team, private
role directories, memory and real handoff/history. They are sequential dependent
requests, not independent statistical repeats. Scenarios use separate project
state. The second raw `Engine.run()` report includes earlier tasks and tokens;
use P3's per-root subtree and phase accounting for Task 2-only measurements.

## Time boundaries

| Field | Boundary |
| --- | --- |
| `elapsed_s` / `delivery_elapsed_s` | Task posting through returned agent delivery and framework maintenance, before quality review |
| `quality_review_wait_s` | Waiting for the actual post-delivery human review |
| `total_to_review_s` | Task posting through completion of the configured review stage |
| `suite_wall_time_s` | Difference between saved, timezone-aware suite `started_at` and `finished_at`; unknown if either endpoint is missing |
| `delivery_time_s` | Summary over saved delivered-task samples only; not the suite's total execution time |

Delivery time includes actual tool execution and any human questions or tool
permission waits during that task. Review duration is additional. Pending or
forcibly interrupted review has no fabricated zero duration. Task durations use a
monotonic clock. Offline suite wall time uses only the saved timestamp endpoints;
invalid, timezone-naive or reversed timestamps fail validation. It includes waiting
and time spent in unfinished stages within that interval. A missing endpoint stays
unknown; it is not inferred from the current clock or the sum of task deliveries.
For `delivery_time_s`, `n=0` and `sum=0` mean there are no saved delivery samples,
not that the suite took zero seconds.

Human summaries contain completed request waits, pending IDs and phase totals.
Parallel human or LLM spans can overlap: cumulative wait/call time must not be
added or subtracted blindly to infer end-to-end wall time. Cooperative per-node
loop budgets are not hard process or whole-scenario deadlines.

## Model calls and token coverage

`human.RecordingLLM` writes complete redacted requests/responses to the scenario's
`dialogue.jsonl`, including phase, call identity and timing. Human events retain
the actual request, response, request ID and wait. API credentials are removed.
This evidence is local and can include user/task data; it is not committed source.

Task `llm` and `human` summaries describe their own phase. Scenario summaries are
cumulative across both tasks. Known token subtotals remain available; complete
totals are null if a recorded call lacks provider usage. Scripted test counters
are not provider-billed cost. Internal HTTP retries, billing for failed calls,
unreported cache usage and monetary price must not be inferred from zero/default
fields. The school model allowance is separate from the search allowance.

## Search audit

`search_delta` reports per-task changes in tool calls, provider attempts, cache
hits, deduplication and denied requests. `search_scenario_cumulative` is a snapshot
of the shared scenario counter, not Task 2-only usage. Scenario search summaries
retain request allowance, used/remaining quota and audit paths.

The initial six fresh Tavily HTTP attempts are shared across all four members
and both tasks of one scenario. Cache hits retain their original retrieval time;
concurrent matching callers do not automatically create duplicate provider
requests. A denied tool result is not an actual human question or grant. Extra
allowance requires a real, explicitly recorded human authorization.

## Saved evidence and offline reconstruction

`python -m moumou.eval.report --run DIRECTORY` rebuilds a derived Markdown report
without credentials, network calls or task execution. For the instructor format,
use the suite plan and authoritative saved task records, retaining missing and
pending outcomes. Rebuilding cannot turn an unanswered quality request into a
success or resume a stopped group. Manifest/task/journal writes are separate
operations and do not form a cross-file transaction.

A forcibly stopped process may leave a stale `running` manifest. Preserve the
raw file and report the actual external stop/process evidence; do not fabricate
a graceful finish, review rejection or accepted goal. Public source/model hashes
and dirty-version facts stay attached to the execution that produced them.

`report_builder.source_sha256` identifies the code used for offline reconstruction.
It is separate from the original manifest's `code` startup snapshot, which identifies
the code that executed the tasks. Rebuilding a report does not relabel the run with
the report builder's version.

Legacy result, telemetry and analysis APIs remain available for historical
archives and internal framework checks. Their independent attempts, optional
memory comparisons and sample statistics are not the current instructor task
protocol. Do not pool those observations with sequential instructor goals or
relabel archived scores as six-goal quality results. Repeated runs need independent
session identities, actual n and compatible settings; one observation has no
sample standard deviation and timing variation alone is not a causal memory gain.
