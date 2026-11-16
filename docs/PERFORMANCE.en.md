# Performance and verification report

[简体中文](PERFORMANCE.md) · [English](PERFORMANCE.en.md)

Status as of 2026-11-16: **candidate implementation prepared; current verification pending**.
This report deliberately leaves unmeasured quantities unknown. It is not a completed
instructor performance result or a claim that the candidate meets final acceptance.

## Evaluation configuration

| Field | Current record |
| --- | --- |
| Canonical input | Original [instructor Markdown](../eval/tasks/README.md), three four-member teams, two sequential requests each |
| Root-goal denominator | Six when all instructor scenarios are selected |
| Lower-priority input | [Supplementary Markdown](../eval/supplementary/README.md), fifteen goals; separate reports |
| Model backbone | Course-assigned DeepSeek or HKGAI; actual run model/endpoint recorded in the manifest |
| Primary development platform | Native Windows, activated Conda `nlp`; actual Python/platform recorded per run |
| Search allowance | Initially six fresh Tavily HTTP attempts per scenario; cache hits separate; explicit grants recorded |
| Candidate commit/source hash | Pending approved commit and run-start capture; do not substitute the report builder's version |
| Candidate test execution | Not run; regression source has been written |
| Candidate instructor evaluation | Not run; no new score or timing result asserted |

Task 2 intentionally reuses the same group's team, private workspaces, experience and
real Task 1 history. These are dependent requests, not repeated independent experiments.

## Implementation verification

One historical baseline is available: published commit
[`e35e820`](https://github.com/Greensheep04/moumou-Agent/commit/e35e8204a3ff2d1b798e1b055bd3cc9706f0a6e9)
recorded 905 passed and 8 skipped in 209.43 seconds on native Windows with activated
Conda `nlp`, plus a passing Ruff check. These were framework checks for that version,
not instructor task quality. They do not validate the later uncommitted implementation.

| Current candidate check | Outcome | Required evidence |
| --- | --- | --- |
| Framework regression | Not run | Exact command, source identity, output/JUnit, exit code, duration and skips |
| Static lint | Not run | Exact command/output and exit code for the candidate |
| Question recovery after delegation/retry/reopen | Code and regression written; not executed | Original request/answer and actual later model-input evidence |
| Trace concurrency/cancellation and cleanup | Code and regression written; not executed | Actual span/actor hierarchy and completed cleanup observations |
| Artifact/review/report/replay integrity | Code and regression written; not executed | Immutable snapshots, strict read failures and original/effective quality provenance |
| Cross-module consumer verification | Pending | Actual P0/P1/P2 acknowledgment and consumer results |

## Instructor completion and time

The current candidate has no measured instructor session. The following rows are
required report slots, not fabricated failed executions or accepted goals.

| Scenario | Goal | Agent state | Machine evidence | Reviewed quality | Verified success | Delivery time |
| --- | --- | --- | --- | --- | --- | --- |
| Software | Booking application | Unknown | Unknown | Unknown | Unknown | Unknown |
| Software | Usage dashboard | Unknown | Unknown | Unknown | Unknown | Unknown |
| Newsroom | Campus news report | Unknown | Unknown | Unknown | Unknown | Unknown |
| Newsroom | AI-detector fact-check | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown |
| Fund | Simulated investment proposal | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown |
| Fund | Semiconductor presentation | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown |

The simulated fund tasks produce research outputs, not authorization to place real
trades. Each task is judged against its original request and actual clarified criteria.

| Required metric | Candidate value | Rule |
| --- | --- | --- |
| Verified root-goal completion | Unknown | Reviewed successful roots / complete selected plan; disclose unreviewed and unposted separately |
| Observed subtask completion | Unknown | DONE non-root nodes / observed non-root nodes; report coverage |
| Whole-suite execution time | Unknown | Saved aware `started_at` to `finished_at`; missing endpoint stays unknown |
| Delivery times | Unknown | Actual per-root delivery samples, before quality review; include measured n |
| Human/LLM/tool cumulative times | Unknown | May overlap; do not sum into wall time |
| Token usage | Unknown | Actual provider usage and missing coverage; no assumed monetary cost |
| Search attempts/cache/denials/grants | Unknown | Per-task delta plus separate cumulative scenario observations |

Preserved development runs belong to their original implementation versions. Earlier
DONE roots with deferred quality remain unverified; interrupted manifests and pending
questions are not rewritten. Their results must not be pooled into this candidate's
measurements or silently excluded to improve its rate.

## Failure analysis and memory evolution

Two development defects motivated the candidate: delegation re-entry omitted an already
recorded same-task answer, and result handoffs needed reliable publication of explicitly
declared existing files. Earlier mandatory-context truncation was also a framework
failure. Identify these as framework/context or handoff defects when the saved input
establishes that fact; do not infer that a model ignored information it never received.

The candidate persists scoped question/answer history, pins restored answers, and saves
execution/delivery evidence. Its fixes are implemented but remain unverified. A later
report must link each observed failure, original evidence, repair version and actual
follow-up outcome, including unsuccessful cases.

Memory reporting uses project/role identity for each stable instructor persona and
distinguishes retrieval candidates, admitted context and conservative prompt-presence
observations. Saved active-record metadata, hashed lessons and phase observations do
not prove the model used a lesson or that it caused an improvement. Deletion totals,
missing telemetry and historical snapshots absent from old runs remain unknown.
No causal memory benefit or statistically significant timing gain is established here.

## Completing this report

After actual candidate verification, retain the raw run locally, reconstruct its report
and analysis using [the documented commands](SETUP.en.md), and publish a reviewed,
redacted summary with exact source/configuration/evidence references. Keep original raw
quality distinct from append-only offline review overlays. Record the real reviewer,
scope and limitations; a matching HumanIO record is not cryptographic identity proof.

Add the actual six-goal table, failure discussion and memory observations without
replacing prior failure evidence. Final closed-set evaluation, contribution screenshots
and presentation evidence remain separate pending [delivery requirements](DELIVERY.en.md).
