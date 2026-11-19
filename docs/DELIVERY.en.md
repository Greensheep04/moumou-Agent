# Project delivery guide

[简体中文](DELIVERY.md) · [English](DELIVERY.en.md)

This index maps course requirements to source, operating instructions and evidence.
As of 2026-11-18, actual sessions and explicitly assisted revisions exist, but final
acceptance is incomplete. Historical runs, later repairs, forensic candidates and
framework checks remain separate; they are not one same-source six-goal result.

## Current delivery status

- Published `085b12e` retains original instructor evidence. Four news/fund tasks
  delivered outputs with factual, source, dependency and UI-recovery issues.
  Earlier software tasks are separate evidence and were not rerun after the current
  edits; do not pool these into a new candidate six-goal score.
- Uncommitted frozen source `e59ee40c…` recorded 1261 passed, 8 skipped and zero
  Ruff diagnostics on native Windows / Conda `nlp`. Research-reuse runs `qn01` / `qf01`
  delivered four roots and twelve subtasks; backends and Rich UIs exited normally.
  Quotation/metric inference, fund dates/tolerance meaning and investment-evidence
  issues remain. Formal human-quality decisions are unknown.
- After 6a recorded 1307 passed, 8 skipped, 0 failed and zero Ruff diagnostics, actual
  assisted revisions ran separately. Fund1 formally delivered four files but needs
  content correction. The Fund2 additional-public-research run spent its four authorized
  new Tavily requests and retained semantic issues. Later News1 and Fund2 revision
  roots ended DEAD after HTTP 504, with zero formal exports. The seven News1 candidates
  support the reviewed content; Fund2 candidates improved but need qualifications.
  Forensic copies are not formal delivery; that news run never started Task2.
- General lineage and local version-bound quote checking are implemented. Lineage
  alone recorded 1325 passed, 8 skipped and zero Ruff diagnostics; quote-focused checks
  recorded 109 passes, with a separate real GitProvider→Teacher binding check of 1 passed,
  28 deselected. Latest combined source `8f7f8d23…` recorded 1354 passed, 8 skipped,
  0 failed, pytest 163.10s and zero Ruff diagnostics, with stable source/tests before/after.
  Three November 18 context-assisted runs are closed: Fund1 DONE with four formal files
  and two remaining corrections, numeric backend exit unknown; News1/Fund2 DEAD after
  HTTP504 with zero formal exports, leaving news Task2 unrun. News forensics retain
  NC01 scope overclaim; Fund2 has four corrections. Its 9-slide candidate's 19 passing
  browser-function observations are not root delivery or content acceptance. Human
  quality stays null.
- Separate original-News2→supplementary-S2T1 is DONE with three formal files, backend/UI
  exit0 and 1820.594s delivery time. It is not continuous with failed News Task1;
  independent review supports the core corrections with N2C-01/02 clarifications,
  without changing raw null human quality. At the November 18 14:52 cutoff all validation
  model backends/Rich UIs stopped. Four terminal audits confirm zero new Tavily/grants,
  reusing 6/6/4/6 public rows. Final archival still requires explicit scope review.

See [performance](PERFORMANCE.en.md) for exact fingerprints, assistance, timings and
per-goal content findings. These are mid-term/development observations, not unseen
final-closed-set scores or proof of a causal memory benefit.

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

Use `verify_source_quote` for local wording checks against an actual saved source
metadata/version where appropriate. Comparing a model's pasted wording with itself
is not independent source evidence. Matching does not replace semantic, attribution
or factual review and is not quality approval. New validation records actual run/read
times; old cache times, publication dates, observation periods and forecast horizons
remain distinct.

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
| Public originals and version bindings | Original/final URL, source retrieval time, raw/text hashes and bytes, access limits; observer-copy time separate from retrieval time |
| Scenario-close diagnostic | Observed unfinished state; not an additional delivery or a new denominator |
| Offline review records | Explicit quality decision, source/result binding and any supersession chain |
| Derived report/analysis/replay | Builder identity separate from original execution identity; original evidence unchanged |

A forced stop can leave a raw `running` status. Keep that file plus actual external stop
evidence, without fabricating a graceful finish. Do not sample a later mutable workspace
or live memory database and label it as an earlier delivery. File copies are selected
byte captures; they do not guarantee a transactionally consistent live database backup.

Explicit research reuse must identify imported material/original dates, additional
page retrieval and operator assistance. An observer copy is not a second independent
fetch or new SUT search. Tool approval is not factual or final quality approval.
Judge package completeness against the actual request and referenced dependencies:
neither invent companion requirements nor omit files the delivery actually relies on.

When a root is DEAD with a zero-file formal manifest, declared child files may only
be copied separately for forensic review after strict path/alias/hash and unchanged-
original checks. Supported candidate content does not invent a root submission or
formal export. Label it `FORENSIC_NOT_FORMAL_EXPORT`, preserving unstarted goals and
unknown quality. Missing usage on HTTP errors stays missing/null, never zero-filled.
Human, LLM and tool cumulative times may overlap and cannot be summed/subtracted into
a new wall-clock measure.

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

Checks have explicit command evidence. Historical CI or checks of another source
version do not establish final-candidate acceptance. Keep hosted history separate
from local file state. Public documentation gives redacted run labels, fingerprints
and summaries; private raw evidence remains local.

## Remaining acceptance gates

- Retain combined `8f7f8d23…` results of 1354/8/0 and zero Ruff diagnostics with stable
  before/after fingerprints. Framework success is not content acceptance; later source
  changes need separate verification.
- Retain standalone News2's completed core-support review and N2C-01/02 qualifications;
  address 8f Fund1's two,
  Fund2's four and news quote-scope findings, and preserve DEAD/unrun/unknown-quality
  outcomes. Do not splice six-goal evidence across versions or assistance modes.
- Inspect generated CLI/web/database deliverables against the actual agreed criteria.
- Complete final-candidate P0/P1/P2 integration and person-operated P2 sign-off; retain memory observations without causal overclaims.
- Complete the performance report, real contribution screenshots and presentation evidence.
- Evaluate the separately released final closed set when available; do not relabel mid-term
  or supplementary results as unseen final-set results.
