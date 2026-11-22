# Performance and verification report

[简体中文](PERFORMANCE.md) · [English](PERFORMANCE.en.md)

As of 2026-11-19, real instructor sessions, explicitly assisted revisions and
independent content reviews exist, but **final-candidate acceptance is incomplete**.
Original runs, research reuse, operator-assisted revisions, forensic candidates and
latest framework checks are separate evidence, not one same-source, same-assistance six-goal score.

## Version and verification identity

| Evidence version | Recorded result | Limit |
| --- | --- | --- |
| Historical published `e35e820` | Native Windows / Conda `nlp`: 905 passed, 8 skipped, 209.43s; Ruff passed | Historical framework baseline, not later code or instructor quality |
| Published `085b12e` original instructor runs | `news-live-01` / `fund-live-01`: four roots and twelve subtasks DONE; content not all accepted | Preserve original failures and UI reattachment evidence |
| Uncommitted frozen source `e59ee40c…` | 1261 passed, 8 skipped; zero Ruff diagnostics; research-reuse runs `qn01` / `qf01` | Applies only to that frozen source and its four observed tasks |
| Uncommitted source `6a830670…` | 1307 passed, 8 skipped, 0 failed; pytest 153.65s; zero Ruff diagnostics; source/tests unchanged across checks | Actual assisted revisions are listed below, not a canonical six-goal rerun |
| Lineage-only stage `4c99184f…` | 1325 passed, 8 skipped, 0 failed; pytest 164.40s; zero Ruff diagnostics | Validates general delegated-context repair, not later tool checks or content acceptance |
| Earlier combined source `8f7f8d23…` | 1354 passed, 8 skipped, 0 failed; pytest 163.10s; zero Ruff diagnostics; source/tests stable across checks | Combined regression and the assisted runs below have separate evidence; this is not content acceptance |
| Historical quality integration `8bc0c2ca…` | Native Windows Conda `nlp`: 1538 passed, 8 skipped, 0 failures/errors; zero Ruff diagnostics; source/tests unchanged; pytest 179.14s, wrapper 179.734s | Includes real-engine offline CLI contracts; subsequent source and teacher-task/content acceptance remain separate |
| Historical checkpoint `556d78fc…` | Native Windows Conda `nlp`: 1601 passed, 8 skipped, 0 failures/errors; zero Ruff diagnostics; pytest 178.56s, wrapper 178.938s; source/tests unchanged across checks | Includes native-receipt handoff, verification guidance and explicit reasoning profile; its NewsTask1 trial was operator-stopped, with unknown content quality |
| Published failed-dependency baseline `be0d3f57…` | Native Windows Conda `nlp`: 1613 passed, 8 skipped, 0 failures/errors; zero Ruff diagnostics; pytest 180.15s, wrapper 180.562s; source/tests unchanged across checks; full02 exited 0 | Scheduling recovery was directly observed; the assisted NewsTask1 retest naturally ended DEAD with zero formal exports and null quality |
| November 19 uncommitted working tree `16981d95…` | Native Windows Conda `nlp`: 1668 passed, 8 skipped, 0 failures/errors; zero Ruff diagnostics; pytest 204.76s, wrapper 205.093s; source/tests unchanged across checks; full02 exited 0 | Explicit output guards and file-input handoff; assisted NewsTask1 root DONE with ten formal files and null human quality, not same-source six-goal acceptance |
| Later November 19 working tree `73e1c969…` | Native Windows Conda `nlp`: 1728 passed, 8 skipped, 0 failures/errors; zero Ruff diagnostics; pytest 213.17s, wrapper 213.531s; source/tests unchanged across checks; full02 exited 0 | Software Task1 root DEAD after pre-body HTTP 502 errors; zero formal exports, Task2 not executed. News/fund not started. This is not six-goal acceptance. |
| Subsequent November 19 working tree `79a0dbaa…` | Native Windows Conda `nlp`: 1779 passed, 8 skipped, 0 failures/errors; zero Ruff diagnostics; pytest 209.22s, wrapper 209.594s; source/tests unchanged; full02 exited 0 | All three original scenarios attempted: three Task1 roots DEAD after pre-body HTTP 502; three Task2 goals not executed; no formal exports. Content quality remains unknown. |

The 905 result belongs to published
[e35e820](https://github.com/Greensheep04/moumou-Agent/commit/e35e8204a3ff2d1b798e1b055bd3cc9706f0a6e9).
The original instructor source fingerprint is
`61322bee1059661b79b4edb6afb00303f9da0c5ab35ce81c4366ce507bc80485`;
the research-reuse fingerprint is
`e59ee40c4cf5e3a13945833b6eb4132c120eea6bcc975898f38b954a4c360d27`.
The 6a assisted revisions used
`6a83067070128fc18c978192665e4346ad43b3ff7b566e9f43b68632c6916157`;
lineage-only source is
`4c99184f860abfc9d7194ecd4cb3c71a926277d79967488b9af381a0e9d2cec8`;
the earlier combined source is
`8f7f8d23f38d93bf8d58a73ab1947769dafe3f3e29920aa38a5aab9ad858bde6`.
The historical quality-integration source is
`8bc0c2cafba6e0122068ca4deb944dfe1cef301479cf93b5ec94a18755a4c72a`;
the earlier checkpoint verified by full regression is
`556d78fc624333a2b408568bc83a636c2e820dc0a9d1a3aaeecf34b2bb7840c6`;
the published failed-dependency baseline verified by full regression is
`be0d3f57cdbfe71ac92b5655defbaca2f7fe68e4828b53182d9ce0e43456e81b`.
Those earlier development executions recorded Git HEAD `085b12e` with uncommitted
changes; later publication does not rewrite their historical execution identity.
The working tree validated on November 19 was uncommitted source
`16981d95be32be69e273b751c65fa4af6c0d332ff852e714e92a48f38424742e`
at Git HEAD `1e134e5`. The commit field alone does not identify executed source.
The later validation that day also used an uncommitted working tree:
`73e1c969557bd89d6ec96a17295226eac764af23506b64e2fd1e70f0f3dd735f`.
Its 212 source/test/config files were frozen with IDENTITY SHA-256
`c5849b034e66c3e50fc1b13974d3c27da45f473f82c21a9af1ba7c48c28fdd57`.
This qualification does not rewrite the earlier stages.

The first full attempt on the dependency repair recorded the same 1613 pytest
passes but failed Ruff with one E501 line-length finding. After a line-wrap-only
correction, full02 completed with the result above. The earlier lint failure is
retained; it is not counted as a wholly passing validation.

The repair allows failed-input review only by a parent of the failed children,
after all declared dependencies are terminal, retaining `MAX_WAKEUPS`. Other
consumers fail rather than receiving unusable upstream input; promotion repeats
to a fixed point. These rules do not bypass acceptance or content-quality checks.

The `news-dependency-retest-01` original NewsTask1 trial naturally ended root DEAD,
with four of six observed subtasks DONE, zero formal exports and null quality.
Actual delivery time was 4202.906s. It retained the same five input files, original
roles/goal and operator feedback from the interrupted baseline, with explicit
stream/none, six reused public records and no new Tavily allowance. This is an
assisted single-goal trial, not a canonical six-goal score or recovery of an older
failed run. Backend exited 1; the UI exited 0; separate native-process observation
confirmed no remaining writers.

The failed checker's original downstream editor was not launched, and its own
parent was allowed to recover, directly exercising the repaired scheduler. The
recovery plan still omitted required file dependencies: a replacement checker
read already-owned package files, but the replacement editor received none and
truthfully reported an incomplete source-only review.

The 174 recorded model calls include four errors with unknown usage. Known input
and output subtotals are 7,208,691 and 93,014 tokens; full totals remain null.
All four failed stream attempts returned HTTP200 and began within 1.1s. Each
produced roughly 219k–251k ordinary content characters, zero reasoning characters,
and no tool calls, terminal finish or DONE before its local deadline. This is
sustained incomplete generation, not evidence of no response, a network-delay
cause or the older reasoning-only failure. Transport IDs are not explicit
dialogue-task mappings. Forty-five answered human requests accumulated 3129.672s
of waiting; overlapping waits must not be subtracted as independent wall time.

Stopped-run source audit confirmed six imports, zero new Tavily/grants, 42 successful
reads (16 fresh, 26 cached), no read failures and no missing native versions. Rich
replay preserved 784 spans. These are integrity/rendering results, not goal success.
All 33 file-check receipts remain unapplied to the zero-file formal snapshot.

Independent forensic review found the main article and both channel files identical
across reporter, checker and chief. Main length is 987 whitespace tokens, or 900
excluding headings/draft notes, with four named people and three channels including
the main article. Core claims are substantially supported. Remaining issues are
November text still treating 2026/27 as approaching, a stronger-than-supported
forum-to-programme causal claim, review prose conflating undated publication with
the explicit effective cohort, and missing actual-file editorial review. Source
independence is correctly limited to HKUST's own account.

All 97 source-string observations independently recomputed as 87 matches and 10
non-matches. The 33 file observations record 12 matches and 21 non-matches; 23
still correspond to final private bytes and independently recompute as 8 matches
and 15 non-matches. Ten observations concern an earlier edited whole-file version
whose complete bytes were not recovered in this bounded audit; their 4 recorded
matches and 6 non-matches are not new independent confirmations. Selected slices
do not establish whole-quotation coverage, exact final glyphs, truth or acceptance.
No content approval or general HTTP504 fix is established.

The 8bc checkpoint adds actual-file quotation checks, public-source access
audits, and delivery-time binding of tool-owned checks to immutable exports.
Buffered transport remains the default. The optional SSE client passed three
actual short school requests (text, assembled tool call, tool-result round trip)
with no retries and no new Tavily requests. This does not prove that long requests
or HTTP504 failures are resolved, or that generated claims are correct. Existing
failed sessions and nullable human-quality decisions are retained. Its 8
skips are 5 POSIX-only cases and 3 optional GNU Make cases; skips are not passes.

Checkpoint `556d78fc…` adds bounded native-receipt summaries for
completed direct dependencies, generic verification-note guidance and an explicit
`--llm-transport stream --llm-reasoning-effort none` profile. Its completed full
framework regression is recorded separately above. The eight skips are five
POSIX-only checks and three optional GNU Make cases; skips are not passes.

Its assisted original NewsTask1 trial `news-receipt-profile-01` used explicit
stream/none, six reused public records and no new Tavily allowance. Reporter
`budget:tokens` exhaustion/retry exposed a P1 state-machine bug that let dependency
consumers proceed too early. The operator stopped the trial: backend exit1, UI
exit0, and separate process evidence confirmed no native writers remained.

This is an interrupted failure baseline. The exact raw manifest still says
`running` and is preserved unchanged alongside external stop evidence. There is
no final Task1 result, export snapshot or stream sidecar. Do not manufacture
natural DEAD closure or a verified zero-file export manifest. Content and quality
remain unknown. This baseline belongs to `556d78fc…` and is not the new retest.

Its independent stopped-run audit counted 180 model requests, 178 completed
responses, one `SchoolStreamTimeout` and one open request interrupted at stop.
Known usage subtotals were 8,768,590 input and 91,707 output tokens; missing usage
keeps whole-run totals null. Search evidence recorded six imports, zero new Tavily
requests and zero grants. Twenty-three recorded quote-check observations comprised
21 matches and two non-matches; these are not independent final-file coverage or
content-quality certification.

A separate fixed-request school-interface diagnostic observed one complete tool
response with `reasoning_effort="none"`; the baseline and `thinking=disabled`
trials reached that diagnostic's completion cap with reasoning only. No returned
tool was executed. This is limited evidence for an explicit option, not a teacher
task result, a default change or a general HTTP504 reliability fix.

The first assisted NewsTask1 run on `8bc0c2ca…` subsequently ended DEAD after two
local stream-attempt timeouts. All three subtasks completed, but formal root
exports remain zero; recorded execution time was 2279.984s. Both failed attempts
returned HTTP200 and large amounts of reasoning data, with no content, tool call,
finish or DONE before the configured 180s limit. This is a local incomplete-response
timeout, not an observed HTTP504. All 48 file-check receipts remain unapplied to
formal exports. Post-exit source audit confirmed six imported public searches,
zero new provider requests/grants, 12 successful reads and no lost source versions;
Rich replay preserved 403 original spans. These integrity observations do not
change the failed root or null human quality. Candidate-content review is forensic.

An additional-worktree import check confirmed that the Conda editable installation
can point to a different checkout. The current entry was verified with an explicit
per-process source path; see [setup](SETUP.en.md) for checking the loaded module.
The 6a full suite used native Windows Conda `nlp`, Python 3.12.16: pytest reported
153.65s, subprocess time was 153.968s, and Ruff exited zero. Both the 6a and 8f
combined suite's eight skips were five POSIX shell alias/argv behaviors and three
dry-runs requiring optional GNU Make; missing Make is not platform inapplicability.
The lineage-only stage took 164.40s in pytest, 164.735s in the invoking subprocess.
The 8f combined stage took 163.10s in pytest, 163.453s in the subprocess and
exited zero; Ruff also exited zero. Source and test fingerprints each remained
stable before/after. These completed framework checks are not instructor content scores.
The lineage stage's 105-file source copy and combined stage's 106-file copy were
separately hash-verified file by file. They retain actual execution identity, not
proof of instructor completion on either source.

Implemented changes preserve current-goal lineage, known requester inputs and scope
through delegation. `verify_source_quote` locally matches wording against an actual
saved source metadata/version and retains that binding. The quote stage recorded
109 focused passes and zero Ruff diagnostics; a separate real GitProvider→Teacher
tool-binding check recorded 1 passed, 28 deselected. Do not add these counts into a
new full-suite or content score. A match establishes text presence under its stated
mode, not truth, attribution, research independence or quality approval.

## November 19: output guards and declared-input working tree

Working source `16981d95…` completed the full native Windows Conda `nlp` regression
listed above. The first full attempt on `20325a0f…` also recorded 1668 pytest passes,
8 skips and no failures/errors, but ten Ruff findings made the overall result exit 1.
After correction, full02 exited 0. The first failure is preserved, not counted as a
wholly passing attempt. Output limits and input declarations are explicit options;
default buffered transport and generic P1 policy remain unchanged. See
[evaluation](EVAL.en.md) for use.

The original-NewsTask1 assisted trial `news-quality-controls-retest-01` used that
working source, the original four roles and task, and labelled operator guidance.
Its explicit profile was stream/none, requested completion cap 16384, ordinary-content
bound 32768 and declared-input checking. It neither restarted an old failure nor
executed original continuous Task2 or all six goals. The root naturally ended
agent-DONE with ten formal files in 2160.953s delivery wall time; three of six
subtasks were DONE and three DEAD. Backend and Rich UI both exited 0, and native
process observation found no remaining writers. Human quality and verified goal
success remain null.

The original reporter first encountered a connection interruption. At native retry,
dynamic calibration raised the same mandatory opening estimate from 7190 to 8027,
exceeding the fixed 8000 allowance. The original checker and editor did not run
prematurely; the chief automatically created a recovery chain. Its checker and
editor received and checked the declared article/review files before model calls,
and all three recovery-role tasks completed. One checker ordinary-content response
hit the local bound after 31.813s; partial output was discarded, and a native retry
recovered. These observations exercise handoff and output guards, while opening
allowance, connection failures and model deviation from tool execution remain
limitations. They prove neither a general HTTP504 fix nor provider compliance with
the requested completion cap.

The 128 model calls include two errors with unknown usage. Known input/output
subtotals are 4,622,804 and 71,085 tokens; complete totals remain null. Twenty-four
actual tool approvals accumulated 1617.581s waiting; this is not independent wall
time to subtract from delivery or report as a net execution time. Six original
public records were imported, with zero new Tavily requests or grants. All 30 source
reads succeeded: 9 fresh and 21 cached. Zero new searches does not mean no internet
access. Rich replay preserved 599 spans; integrity or rendering checks do not
establish content quality.

Cold audit independently recomputed 41 quotation receipts applicable to formal file
versions; 16 remained unapplied. These do not establish whole-article quotation
coverage. Quotation-coverage, factual-grade and human-quality fields remain null.
The completed independent content report counted 929 main-body words after removing
headings and the draft note, versus 1032 in the whole file. Adaptation body counts
after metadata/draft-note removal were 165/107/283. The core programme facts and
attribution of at least three named people are supported, but this is not a quality
PASS or independent reporting; the body-only counting definition must stay explicit.

Three content follow-ups remain. `final_review.md` describes five pages cited by
the main article, while the article uses four and the package retains five; clarify
the counting scope. Anchor announcement wording to its original date, without
calling an original quotation fabricated solely for retaining future tense.
`verification_report.md` includes trimmed, ellipsized and punctuation-omitting
excerpts in a table described as exact article quotations; label these as slices.
The independent inventory contains 25 top-level double-quoted units, including
course names, labels and a title, not 25 speech quotations. Nineteen full units
match literally; six differ in terminal comma/period placement while their
substantive wording occurs in the appropriate sources. Selected-slice matches do
not make every full quotation byte-identical, and these punctuation differences
are not confirmed substantive misinformation.

The coordinator's reading-scope statement is also too broad:
`coordinator_verification_note.md` says nine files were "read in full", but the
chief's observed `read_file` pages of `verification_report.md` cover only bytes
0–16384, leaving 3014 bytes of its 19398-byte file outside those responses. Its
whole-file hash scripts return hashes, not the full text to the model. The editor
independently read the file completely in three pages, so the verified same-version
article/checker/editor chain still holds. Do not claim full coordinator review of
all formal files or infer understanding from this record.

Actual HumanIO processed individual technical permissions without filling quality
fields. The operator twice mistook JSON display escaping for script escaping in
approval notes, then parsed the actual arguments and explicitly withdrew both
warnings in a later real approval. The original errors and correction are retained.
This is therefore an assisted validation, not a blind test. Different profiles,
recovery paths and human waits do not support causal performance claims from two
wall-clock observations. Other instructor goals still require continuous execution
and independent content acceptance on the same final source.

## Subsequent November 19: bounded HTTP retry and three closed failed attempts

Source `79a0dbaa765eb5268c38d99425c2db786d197ee02b03c2ffbb5db9ade5f23fd6`
adds explicit, default-off retries for pre-body HTTP 502/503/504 within the already
approved CLI/client/test paths. With two extra retries, each call permits at most
three requests and actual 5s/15s waits under the shared 180s cooperative deadline.
It neither retries partial/protocol errors nor changes the native worker retry
policy. The targeted run passed 209 checks; full01 passed 1779 tests but failed
one Ruff line-length check. After wrapping that line, full02 qualified as above.
All 212 frozen source/test/config files still matched after the teacher attempts.

The original software, news and fund scenarios were then invoked separately on
this same source, each preserving its native two-task session. All three Task1
roots naturally ended DEAD: each had three model calls, nine HTTP attempts, all
502 before body/events/tools. Across the three scenarios there were 27 HTTP
attempts, zero executed tools and no formal exports. Task2 did not execute in any
scenario; missing Task2 results are not converted into content failures or
standalone substitutes. Backend exits were all 1; the three attached P2 UIs exited
0 and native writers were confirmed absent. Raw archives contain respectively
16/20/20 files (342,329/527,950/615,543 bytes), with original/copy/original hashes
unchanged. Failed-request usage and human quality remain null; known-token zero
is not measured zero billing. The cause of the 502 responses is not established.

There were no new Tavily requests or extra synthetic API probes. News actually
imported six existing public records; fund actually imported the disclosed six
plus four through two authentic native seed receipts and the scoped data adapter
described below. This source used `run_teacher_resilient_acceptance_20261119.py`,
SHA-256 `66d521504d71a32c1093342bf5ff3691bcc0d9d7e5cf0acf922da3861830f4a8`;
the earlier `a528…` runner belongs to the separate `73e1c969…` protocol. The fund
receipt explicitly records `single_source_cli_reproduction=false`: this bound
two-source data adapter is required for full reproduction, rather than one
ordinary `--reuse-sources` CLI argument. Registration is not evidence that the
model read these sources.
This round has zero agent-completed goals out of six planned, with three API
failures and three unexecuted goals. It does not establish six-goal content
acceptance. Further content validation depends on usable school API responses.

## Earlier November 19: same-frozen-source six-goal validation plan

Working tree `73e1c969…` adds an explicit teacher opening quota and a request policy
selected by offered tools. After 115 targeted checks passed, the first full run
recorded 1728 passes, 8 skips and no failures/errors, but one new-test import-order
finding failed Ruff and the overall run exited 1. After that import and test
description corrections, full02 qualified with the 1728/8, zero-Ruff result above.
The first failure is retained; targeted counts are not added to the full suite.
The default opening remains 8000, generic P1 policy and shared EMA are unchanged,
and required-when-tools does not replace argument schema, Gate or quality checks.

The user authorized the original three scenarios on this same frozen source,
each with a real continuous Task1→Task2, giving six root goals. Each keeps its four
original roles, actual Task1 results, private workspaces, memory and real HumanIO;
old private products or human answers are not imported. The selected profile is
stream/none, requested completion cap 16384, ordinary-content bound 32768, declared
inputs, opening quota 32768 and required-when-tools, with zero new Tavily and no
extra capability probe. This source's software run has now naturally closed with
exit 1: Task1 root and Release are DEAD after six pre-body HTTP 502 failures,
following 50 completed stream attempts. Dev and QA completed; QA's 33 passing
checks do not replace the missing formal delivery. Formal exports are zero and
Task2 was not executed. The UI exited 0 and native writers were confirmed absent;
92 raw files (4,991,821 bytes) were sealed unchanged. News and fund have not
started. Failed-request usage and human quality remain unknown. Neither the plan
nor regression establishes six-goal acceptance.

Public-source reproduction needs separate disclosure. News is planned to reuse
the original news run's six public records. Fund is planned to reuse the original
six from `fund-live-01` and the existing four from `fund2-primary-revision-01`.
This is not ten records in one cache or four new searches in this round. A scoped
data-initialization adapter in the local one-shot runner calls native
`reuse_public_sources` twice, limited to that fund store, then restores the original
function. It replaces neither Engine, LLM, roles nor continuous Task1→Task2 flow.
Original IDs, timestamps, source manifest/cache hashes and separate import receipts
are preserved. The fund manifest will record
`kind="explicit_public_source_reuse_set"` and actual `source_runs`, without
inventing a single `source_run`.

That adapter is part of an additionally bound harness identity, not a new
multi-source repository CLI argument. At preparation, runner
`run_teacher_final_acceptance_20261119.py` had SHA-256
`a52847006eef7b40d714e3451f73c435c4097ceb72d8ddbc2fa8550737f20d98`.
The source fingerprint excludes it; launch binding separately checks its bytes and
source set. Full reproduction needs the matching runner, binding and public-source
evidence, rather than a plain single-source `--reuse-sources` command. Not-yet-started
fund imports are not recorded as completed, and available records do not establish
that the model read or used them, or that they are independent sources.

## Configuration, assistance and research scope

- Canonical input is the [original instructor Markdown](../eval/tasks/README.md):
  three four-role groups, two sequential tasks each; six roots when all are selected.
- Primary environment: native Windows, activated Conda `nlp`. This round configured
  school HKGAI model `t2_hkgai-v3_fp8_1m_e7`; request configuration/source identity is
  recorded per manifest. Actual provider routing identity has not been independently established for every call.
- Task 2 reuses group identities, workspaces, deliveries and memory. These are
  dependent tasks, not independent repeated trials. Historical supplementary or
  single-case evidence stays separate; it is not a current bundled bank or proof
  that the six instructor goals passed.
- Original news/fund runs used six new Tavily requests each, twelve total.
  The research-reuse round explicitly imported six public search records per group
  and made zero new Tavily requests. Reused records are not fresh independent research;
  actual retrieval of known public originals is recorded separately. Zero Tavily does not mean no network access.
- A separately authorized 6a Fund2 additional-public-research run,
  `fund2-primary-revision-01`, spent all four additional
  Tavily requests. Its later correction reused only those four actual provider-cache
  records, not four plus six older imported events. News/Fund1 corrections reused
  their own original six public records with no new Tavily requests. Old private
  products, human answers, memories and grades were not imported.
- Existing editorial choices and clearly labeled synthetic-client fixtures,
  engineering permissions and procedural clarifications were supplied through actual HITL.
  This is assisted engineering acceptance, not an unattended blind benchmark.
  Synthetic data is not the owner's finances; tool permission is not fact approval or suitability approval.
- School model allowance and Tavily metering are separate. No monetary cost or causal
  efficiency improvement is inferred from tokens, different source inputs or operator waits.

Four separate critic diagnostics produced no qualified review: three ended with
`length` and empty content, and the fourth returned HTTP504. None establishes a quality PASS.
One critic response reported e10; normalized responses in the 6a Fund2 additional-public-research
revision also all reported e10 rather than configured e7. The cause is unknown. These
are returned identifiers within specified evidence scopes, not independent verification
of every call's actual model identity or authorization to change configuration.

## Original published-version observations

These rows belong only to the original `085b12e` runs.

| Goal | Saved execution | Original independent content result | Delivery wall time |
| --- | --- | --- | --- |
| News 1: campus AI report | Root +3 children DONE; 7 files | Factual failure: an official source specified the credit split, but the package called it unconfirmed | 952.140s |
| News 2: detector fact-check | Root +3 children DONE; 6 files | Conditional: rate units and some source support needed qualification | 2188.282s |
| Fund 1: simulated allocation | Root +3 children DONE; 1 file | Conditional arithmetic passed; referenced companions and some AUM/date support were missing | 1985.531s |
| Fund 2: semiconductor HTML | Root +3 children DONE; one 11-slide HTML | Browser functionality passed; price attribution, FX convention and some evidence remained incomplete | 1068.094s |

The two software tasks have earlier acceptance evidence. This report has not reverified
their exact original version/timing metadata, and they were not rerun after the current edits.
No guessed timings or combined candidate score are supplied.
Each original news/fund Rich UI had one read failure followed by reattachment.
Later successful sessions do not erase those failures.

## Frozen e59 round: independent content review

Both `qn01` and `qf01` backends and Rich UIs exited normally. Four roots and twelve
children reached DONE. Formal status remains `agent_completed_quality_unreviewed`,
each `verified_goal_success=null`, with human-quality decisions unknown.
These are independent assistant reviews, not teacher grades or changes to saved quality fields.

| Goal | Delivered and supported | Corrections/qualifications still needed | Delivery wall time |
| --- | --- | --- | --- |
| News 1 | 9 files; 949 whitespace-word main article, 4 named people, three channels; credit split, three outcomes and July dates corrected | Social copy presents rewritten wording as a direct quotation; absence claims need reviewed-source scope | 3828.672s |
| News 2 | 5 files; published-study figures checked, unsupported old 26%/9% removed, secondary provenance and 2023 corpus limits explicit | True positive called an error; hypothetical papers become accused people; scope not applied throughout verdict; main text does not explain percentage points | 927.110s |
| Fund 1 | All 4 referenced documents delivered; 80/10/10 allocation and six conditional stress formulas correct; fee unknowns disclosed | 1983 system inception conflated with the 2005 band; holding through 30% treated as a ceiling; return/drawdown definitions mixed; 70/30 still gives −33.5% under its scenario | 1208.281s |
| Fund 2 | One 12-slide HTML; principal figures and forecast/actual labels supported; native Chrome 12/12 and navigation passed | Unsupported priced-in/exposure/correlation claims; primary driver becomes sole driver; revenue does not identify price/volume; latest verified actual period is July | 3672.843s |

No extra companion requirement was imposed on Fund 2 and no News 1 word limit was
imposed on News 2. Internal risk judgments may remain judgments; they are not measured facts.
News 2's published text uses 61.3%→11.6%, whereas preprint v3 uses 61.22%→11.77%.
Bind figures to their actual version rather than mixing endpoints or calling this mere one-decimal rounding.

## Explicitly assisted revisions on 6a

These runs retained original roles and the formal CLI but added labeled operator
correction feedback or synthetic inputs. They are not unmodified blind teacher tests.
Each row has its own run identity; repeated revisions do not add independent successes
to the standard six goals. Formal human-quality fields remain unknown.

| Run label / original task | Formal execution and export | Independent content observation | Saved wall time |
| --- | --- | --- | --- |
| `fund1-reviewed-revision-01` / Fund1 | Root +3 children DONE; 4 formal files | Chronology, conditional shocks and the old 70/30 issue improved; risk companion loses known synthetic inputs, compares thresholds incorrectly and extrapolates S&P500-member evidence to VXF; corrections remain | 670.766s |
| `fund2-primary-revision-01` / Fund2 | Root +3 children DONE; 3 formal files, 13-slide HTML | Priced-in/correlation/sole-driver caveats improved; Q2 forecasts become realized prices, a dominant revenue price share is not established, and policy chronology needs qualification. Browser use was interrupted; the earlier 12-slide pass does not transfer | 1285.922s |
| `news-reviewed-revision-01` / News1 | Root DEAD after HTTP 504; 3 children DONE; 0 formal exports | Seven declared child candidates equal parent integration copies. Main article: 860 whitespace tokens, 4 named people, three channels and fact record; old social quote corrected and core content supported within reviewed sources. This is forensic review, not formal delivery | 4466.375s |
| Same news run / News2 | Never started after News1 failure; no delivery | No result or content verdict for this round; do not invent Task2 completion or impose News1's word limit | Not run |
| `fund2-reviewed-revision-01` / Fund2 | Root DEAD after HTTP 504; 3 children DONE; 0 formal exports | Three declared candidates equal parent integration copies; main forecast table, decomposition limit and rescission chronology improved. Forecast tense in risk summaries, source type and date scope still need correction; forensic candidates only | 1109.234s |

The failed runs retain their official zero-file manifests, HTTP 504 errors, retries
and original quality. Candidate copies and later improvements do not overwrite them.
The news editor reached DONE after a retry while its root still failed. Wall time
includes actual human waits. News source verification covered 29 successful original-page
reads, 16 metadata versions and 4 unique text bodies, not 16 independent sources; the failed
Fund2 round covered 13 successful reads and 6 versions. Hash/version bindings establish
examined bytes, not automatic factual approval.

## Actual context-assisted runs on 8f: November 18 cutoff

These use the same `8f7f8d23…` source, original four roles and explicitly identified
operator feedback, as separate assisted supplementary runs. Formal human quality and
`verified_goal_success` remain null; they are not pooled into the original six goals.

| Run / original task | Actual execution and formal export | Independent observation and limits | Delivery wall time |
| --- | --- | --- | --- |
| `fund1-context-revision-01` / Fund1 | Root +3 children DONE; 4 files. Numeric backend exit was not recovered and remains unknown; UI exited 0; native process observation confirmed no longer running | Known synthetic profile, conditional arithmetic and quote binding improved. Two corrections remain: a summary assigns the current FX band to 1983, and a general drawdown definition incorrectly requires multiple years | 1035.797s |
| `news-context-revision-01` / News1 | Root DEAD after HTTP504; 3 children DONE; 0 formal exports; backend exit1/UI exit0 | Forensic candidates support the core content, with NC01: final quote glyphs do not match the claimed exact-verification scope. No formal delivery or human acceptance | 2179.094s |
| Same news run / News2 | Task2 never started; no formal delivery | News1 forensic findings cannot fill this missing phase | Not run |
| `fund2-context-revision-01` / Fund2 | Root DEAD after HTTP504; 3 children DONE; 0 formal exports; backend exit1/UI exit0 | Forecast/actual, price-dominance, exposure and rescission issues improved; four corrections remain below. The 9-slide candidate passed 19 native-Chrome functional observations, as forensic functionality only | 1549.031s |
| `news-task2-standalone-01` / original News2 | Root DONE; 3 formal files; backend exit0/UI exit0; original S2T2 independently mapped to supplementary S2T1/t1 | Core corrections independently supported; N2C-01/02 editorial clarifications recommended. Not continuous Task2 or recovery of failed Task1; raw human quality null | 1820.594s |

The four Fund2 corrections concern the 39.31% volatility date (31 October, not
11 November); blanket Q1-unknown wording ignoring limited support in an already-read
April secondary retrospective; a snippet-only claim despite seven successful original-
page responses to risk; and treating missing revenue decomposition as proof that every
counterfactual assumption is false. Preserve source type, individual metric dates and
qualifications rather than upgrading these to a complete primary realized-price series.

Closed-run source checks are below. Successful reads are not independent-source counts
or proof that the model received or understood the full page:

| Run | Imported public cache rows | Successful page responses (fresh/cached) | Controlled failures | New Tavily / added grants | Missing raw/text versions |
| --- | --- | --- | --- | --- | --- |
| Fund1 context | 6 | 12 (5/7) | 4 | 0 / 0 | 0 / 0 |
| News context | 6 | 24 (6/18) | 0 | 0 / 0 | 0 / 0 |
| Fund2 context | 4 from the PRIMARY run's own cache | 19 (8/11) | 5 | 0 / 0 | 0 / 0 |
| Standalone News2 | 6 | 20 (7/13) | 3 | 0 / 0 | 0 / 0 |

Standalone News2's final article corrects detection categories, papers-versus-people,
2023/2026 scope and percentage-point/relative-change errors. N2C-01 asks it to distinguish
a developer statement reproduced by news from a directly read primary page. N2C-02
asks it to specify the population behind "most flags wrong": P(flag|human) alone does
not establish P(human|flag) in a mixed population. Draft errors corrected in the final
remain historical, not new final-article failures. All 27 quote calls (13 exact,
14 whitespace-only) were independently recomputed; this supports the submitted strings
and version bindings, not global exact certification of every final Markdown sentence.

Standalone News2 reuses only the original news six public cache rows, with initial
budget zero; terminal audit also confirms zero actual new provider attempts or grants.
It recorded 63 outer model calls, one recovered HTTP504 and one call with unknown
usage. Known input is 2,097,782 tokens and output 49,459; complete totals remain null.
All validation model backends/Rich UIs stopped at November 18 14:52. The independent
content opinion saved afterward does not rewrite raw null human quality.
No old private answers, products, memories or grades are imported. New page fetches
retain actual retrieval times; caches retain old search times. Publication date,
observation period, forecast horizon and the end-2026 perspective remain distinct.

## Metric interpretation

| Metric | Reportable scope |
| --- | --- |
| Verified root-goal success | Formal quality confirmation is incomplete; DONE does not establish candidate 6/6 or 100% |
| Observed subtask state | Each original/e59 round separately has 12/12 DONE; supplementary runs are separate, and child DONE does not replace a failed root's formal delivery |
| Delivery time | Per-root wall-time samples above, including approvals/operator waits; not pure model time |
| Complete six-task runtime | No complete same-source candidate sample; do not splice separate runs |
| Human/LLM/tool cumulative time | May overlap and even exceed delivery wall time; do not directly sum or subtract |
| Tokens/cost | HTTP errors and other calls can lack usage; retain null/missing coverage and known subtotals, never fill them with zero or invent total cost/speed gains |
| Research/search | Separate fresh search, explicit reuse, cache, denials and original-page retrieval; reviewer access is not SUT research |
| Memory benefit | Retrieval, admission, prompt presence and use differ; no causal learning benefit established |

## Failure analysis and follow-up

The frozen e59 checks and real sessions establish some cross-module execution,
evidence-capture and interaction behavior. They also expose semantic errors that can
survive surface source checking. Answer meaning, metric definitions, forecast/observed
status, dates/units and causal scope still need validation; more citations or disclaimers
alone do not establish a correct conclusion.

Preserve original question-recovery, handoff and Windows-read failure evidence.
Later fixes require new source identity and evidence, rather than rewriting old outcomes.
Typed approval NOTE, explicit dependencies, source-version retention, search coverage
and the Git own-root repair passed framework checks on 6a; its actual assisted revisions
are above. Later lineage passed a separate full suite, and quote checking passed focused
and real Git-workspace binding checks. The combined full suite completed with 1354 passed,
8 skipped, 0 failed and zero Ruff diagnostics. Actual 8f assisted runs are separately
listed above; they do not establish a complete same-source six-goal content result.
Historical e59/6a evidence
and failed critics do not validate later code or establish content acceptance.
Saved memory metadata, lesson hashes and prompt presence prove their stated observations,
not model use or causal improvement.

Known limits also remain: Worker failure retries and HTTP POST retries within one model
call are separate layers, so one layer's logs do not establish the other's attempt count.
A tool's check of one string does not prove the final artifact's quote avoided glyph
drift. A complete page fetch/archive is not proof that all pages were served to or read
by the model. Check final artifact bytes, retained source version and actual served ranges,
rather than treating HTTP success, a hash or a literal match as complete verification.

Raw logs, immutable deliveries, source versions and reviews remain local; public pages
contain redacted summaries. Follow [setup](SETUP.en.md) to rebuild reports while separating
execution identity, builder identity, original quality and appended review.
The final closed set, actual member-contribution screenshots, week-13 presentation,
person-operated P2 sign-off and six-goal acceptance for the final submitted version
remain pending [delivery requirements](DELIVERY.en.md).
