# Instructor Task Evaluation

[简体中文](EVAL.md) · [English](EVAL.en.md)

## One protocol with explicit task sources

[eval/tasks/README.md](../eval/tasks/README.md) is the canonical instructor dataset.
It contains the original role initialization and two task texts for each team:

| Scenario | Fixed members | Ordered requests |
| --- | --- | --- |
| `scenario_1` | Technical Lead, Software Developer, Test Engineer, Release Manager | Booking system, then usage dashboard |
| `scenario_2` | Editor-in-Chief, Reporter, Fact-Checker, Editor | Campus news report, then AI-writing-detector fact-check |
| `scenario_3` | Fund Manager, Research Analyst, Risk Manager, Marketing Specialist | Simulated investment proposal, then semiconductor HTML slides |

All ten source Markdown files are retained as instructor inputs. The loader keeps
source hashes and original prompts; it does not invent acceptance tests or answers.
The complete instructor suite has six root goals. Task 2 uses the same four-member group,
private role workspaces, experience, actual Task 1 results and dialogue references.
Workflow, implementation choices and suitable checks are determined with the human.

The formal team adapter sends the complete original role prompt, current goal and all
acceptance criteria. It bounds optional history, dependency reports, board content and
lessons first, preserving their exact auxiliary content in private
`.evaluation/context/<digest-prefix>.json`. New filenames use the first 32 digest hex
characters; references and validation metadata retain full SHA-256, different-content
collisions are rejected, and existing archives are not renamed. If mandatory instructions exceed the opening budget,
`ContextAdmissionError` stops admission before any LLM call. Check actual recorded input
before attributing a missed requirement to the model; framework truncation is a system
failure, and its unfinished goals remain in the planned denominator.

Delegated openings also pin validated ancestor goals from the current repository in
root-to-parent order, with UTF-8 SHA-256 hashes. References preserve qualifications
and unknowns separately from the child's own goal, role and acceptance. Only `parent_id`
defines ancestry, never `deps`, a prior phase or a role name. Parent-private answers,
approvals, memories and files are not shared, and no search authorization is added.
Both builders budget full references as mandatory and retain them across re-entry,
answer refresh and compaction. Transmitted availability is not correct model use.

`eval/tasks/` is the only bundled bank, containing the instructor mid-test's ten
original Markdown files. The protocol can still explicitly select compatible
externally supplied Markdown directories with `--tasks`. External datasets keep
separate results and an actual-task-plan denominator; they are neither an automatic
fallback nor part of instructor scores. Historical scripted-answer metadata does not
authorize real human answers or approvals.

If an externally supplied memory-transfer task declares a warmup prerequisite,
the team must perform and record that preparation; the CLI does not
run the old ablation schedule or automatically seed memories. Claims of transfer need
actual preparation and retrieval evidence.

From an activated environment at the repository root:

```powershell
# Source inspection only; no key, model call or output directory.
python -m moumou --plan

# The same formal entry, using actual console input and quality review.
python -m moumou
python -m moumou.eval.cli --tasks eval/tasks

# One scenario, real file-based input, fresh output directory.
python -m moumou --scenario scenario_1 --human files --out runs/test/NEW_SESSION

```

`make test` and `make eval` call this protocol. Use `make unit-test` or
`python -m pytest -q` for deterministic framework regression tests. Those tests
are implementation evidence, not instructor task-completion scores.
They may check source loading and the evaluation entry, but the original teacher
tasks live in `eval/tasks/`, not `tests/`. Regression source is useful repository
material; caches, temporary files and local test output are not submission artifacts.

## Human interaction and quality

The formal harness uses real questions, tool permissions and post-delivery quality
reviews. Console mode asks the person directly. File mode retains requests under
`SCENARIO_ROOT/human/pending/` and accepts a matching `HumanResponse` under
`human/responses/REQUEST_ID.json`; [setup](SETUP.en.md) documents the fields.
There are no predefined answers or automatic approvals.

A task's saved result is available before the quality request. Explicit human
approval records `human_accepted`; rejection records `human_rejected` and skips
later dependent tasks in that scenario. Ordinary task-stage errors, rejection and
incomplete delivery do not prevent independent configured groups from running.
Initialization/configuration or cleanup failure is a global infrastructure stop;
cancellation/KeyboardInterrupt also stops the suite. Unattempted groups remain unknown
in the full denominator. Agent `DONE`, a green child test suite and a working launcher remain
separate evidence. An empty root acceptance list is self-report, not machine
verification. Deferred review (`--quality-review deferred`) remains unknown and
never produces a verified quality success merely because the agents stopped.

Independent quality assessment can use the full saved dialogue and outputs, with
its actual evaluator and decision recorded. The default CLI waits for human
review; it does not fabricate an independent model grade.

Same-task delegation/re-entry restores recorded answered questions from the scoped
question ledger, with original IDs and provenance. It resumes an original pending
question rather than inventing a reply. These facts remain in pinned opening context;
approval grants are never reused. Human-port exchanges can include explicitly authorized
operator assistance, which must remain distinct from a new manual answer or quality review.

Later quality decisions use the append-only `moumou.eval.review` interface described in
[setup](SETUP.en.md). Raw task quality remains available separately from an effective
review overlay; the overlay does not resume execution or rewrite the original result.

## Credentials, search and limits

Use the course DeepSeek/HKGAI configuration and keep credentials in local `.env`;
see [setup](SETUP.en.md). `TAVILY_API_KEY` is needed for live search. The initial
`--search-requests 6` allowance covers both tasks and all four roles of one
scenario. Cache hits, deduplicated callers and actual provider attempts have
separate records. Additional search requires explicit human authorization.
This allowance does not limit the school's LLM access.

Per-node execution bounds are cooperative loop safeguards. Record real waiting,
failed calls and interruptions. A scenario wall time is not the sum of concurrent
agent spans, nor a claim of a whole-process hard deadline.

## Results and interruption

Every launch uses a fresh `runs/test/<run_id>/` directory unless a new `--out` is
specified. `test.json` records the plan and scenario state; `active.json` identifies
the current phase. Per-scenario task results, full `dialogue.jsonl`, databases,
search audit and private role outputs remain in that directory. Runtime files
and credentials are not committed.

Preserve failed and interrupted evidence. A forced stop can leave `test.json`
with an unfinished `running` record; do not rewrite it as a successful close.
Actual stop/process evidence and pending human requests must accompany that
archive. The CLI does not automatically resume an existing output directory.
A later continuation or new trial needs an explicit, documented state-transfer
plan, preserving the original record and unknown quality decisions.

New deliveries save immutable artifact blobs with hashes and inline memory/failure
captures at their recorded stage. A scenario-close state snapshot can retain unfinished
nodes as diagnostics; it is not a delivered task. Old missing captures remain unknown:
later workspace files and live memory.db are not historical substitutes.

```powershell
python -m moumou.eval.report --run runs/test/EXISTING_SESSION
```

Offline reconstruction uses saved task evidence and does not run a model, rerun
acceptance, or answer pending human requests. Older result archives may still be
read for historical analysis; they are not another formal task bank or launch path.
An unindexed task result or inconsistent task identity is an integrity error, not
evidence to silently include or discard; repair the archive explicitly before scoring.

The report's `suite_wall_time_s` uses only saved, timezone-aware `started_at` and
`finished_at`. Either missing endpoint leaves it unknown; invalid, timezone-naive or
reversed values fail validation. It includes waits and unfinished-stage time within
that interval. `delivery_time_s` covers only saved delivery samples: `n=0, sum=0` is
not zero total execution time. Keep `report_builder.source_sha256` separate from the
original manifest's `code` startup snapshot; reconstructing does not change run provenance.

Offline `moumou.eval.analysis` and `moumou.eval.replay` expose saved failure/memory
observations and a P2-compatible read-only JSON view. Original event/line hashes and
scenario+task identities are retained; answered, pending and staged responses are
separate. See [the performance report](PERFORMANCE.en.md) for the candidate's actual
verification status. A usage example, exported schema or historical regression result
does not establish current task quality or P2 consumer sign-off.

## Reporting checklist

- State the source hashes, selected scenarios, model, runtime and actual output root.
- Keep the complete selected root-goal denominator, including unposted or unreviewed goals.
- Report agent completion, machine acceptance and reviewed quality separately.
- Distinguish each task's subtree and phase calls from cumulative scenario totals.
- Report delivery time, review waiting, measured token coverage and search attempts.
- Link actual dialogue, outputs and checks; describe failures and remaining uncertainty.

The [metric contract](metrics.md) defines these fields. A successful implementation
regression or a completed self-built experiment does not establish that all six
instructor goals passed. Only actual execution and recorded quality evidence can
do so.

The [delivery checklist](DELIVERY.en.md) maps these records to the architecture,
performance report, contribution screenshots and final presentation requirements.

## Research evidence and delivery quality

The formal teacher entry point registers `source_history` and `read_source` alongside
the metered search tool. Public queries, source URLs, snippets, original retrieval times
and request identities are shared inside one scenario. Search snippets remain previews.
Read the actual HTML/plain-text passage, its dates and units before marking a claim
supported; the page reader records original/final URLs, content hashes and read time.
Cached reads retain their original time. Unsupported formats, inaccessible pages and
missing evidence remain explicit limitations, never a successful fact check.

The reader accepts registered public sources. It verifies TLS, pins validated public
addresses and rejects private targets, credential URLs, search-engine bypasses, access
barriers and oversized/compressed responses. If system DNS returns unusable addresses,
it may use fixed Cloudflare HTTPS DNS; resolver provenance is retained and no local
network settings are changed. A registered arxiv PDF permits only its same-ID,
same-version HTML/abstract format, with the derivation recorded. It does not parse PDF.

For explicit research reuse, a new session can import only public provider-cache results:

```powershell
python -m moumou.eval.cli --scenario scenario_2 --reuse-sources runs/test/PRIOR_SESSION --search-requests 0 --quality-review deferred --out runs/test/NEW_SESSION
```

The source session must have a terminal saved manifest and a stable public cache.
Original retrieval timestamps/request IDs are preserved; old query text is marked
unknown where the cache did not retain it. Human answers, deliverables, memories and
grading decisions are not imported. `source_reuse` records this in the new manifest;
imports count as neither fresh searches nor new-run search-cache hits. Report such a
run as research reuse, separately from a fresh research benchmark.

The teacher adapter also checks final root document references. A missing or undeclared
local companion/asset produces correctable `result_rejected` feedback before publication.
Explicitly include intended public files or correct their references; nothing is added
automatically. This checks packaging, not factual accuracy. Final semantic review must
still compare consequential claims with source passages and independently recompute
rates, units and arithmetic. No automatic human-quality approval follows.

Successful page captures now retain immutable raw/text blobs and canonical metadata
versions, with a separate latest-URL cache pointer. The original retrieval identity,
time, source request and explicit format derivation are archived before hashing;
cached serving preserves them. A read's `metadata_sha256`, `raw_sha256` and
`text_sha256` identify the captured evidence, rather than whichever page is latest.
Legacy backfill retains only the verified current row and labels its origin. Missing
pre-retention content remains unavailable, and supplemental operator copies retain
their separate provenance. Never migrate an original closed acceptance archive just
to make a historical retention check pass.

The formal entry also registers the local read-only `verify_source_quote` tool, bound
once after team configuration to actual provider-returned role workspaces. Supply a
real read's `metadata_sha256` and a quote. It checks only that retained version, without
HTTP, search or latest-version fallback. Roles cannot supply their own source body,
path or namespace. Default `exact` mode preserves code points; `collapse_whitespace`
changes Unicode whitespace only, never numbers, dates, case or punctuation. Results
separate lexical match `true`, non-match `false` and unavailable evidence `null`.
Coordinates identify retained extractor text or explicitly normalized derived text.
Tool `ok`, lexical presence and factual correctness are different observations;
read surrounding passages, check attribution/scope and independently recompute values.

Corrective supplementary sessions keep separate plans, source and authorization scope;
they are not pooled into the canonical six goals. Preserve `DEAD`, unposted stages and
zero formal artifacts. Candidate files recovered from private role directories support
only explicitly labelled local forensic checks, never a formal root delivery. See the
[performance report](PERFORMANCE.en.md) for versioned results. Implementation regression
does not manufacture human acceptance, closed-set scores or contribution screenshots.

For example, the `6a830670` sessions `news-reviewed-revision-01` and
`fund2-reviewed-revision-01` ended `DEAD` after repeated HTTP 504 failures, with no
formal root artifact export. News Task 2 did not run. Local forensic checks of candidate
files do not alter those formal states or establish an instructor-task pass.

Model plans must explicitly state `deps` for every child. A missing field is a
correctable proposal error, not permission to infer an edge from the goal's prose.
Successful approval notes reach the next role observation as scoped metadata,
separately from the tool's stdout/stderr. They do not add a search allowance, create
a reusable authorization or stand in for a quality review. File-search coverage also
reports large-file skips and output limits; an empty match list is not an exhaustive
claim about unexamined text.

Independent content audits may still reject an agent-completed, structurally intact
delivery. Keep those findings, model/protocol failures and assisted revisions separate
from implementation regressions and raw human-quality fields. Research reuse and
operator clarification must remain visible when comparing sessions from different
source versions; do not pool them into a single six-goal final benchmark.

## Final-file quotes, served ranges and optional streaming

The formal entry also registers `verify_artifact_quote` and `source_access_audit`,
binding actual role workspaces after team configuration. The former reads a
zero-based half-open Unicode `[start,end)` slice from an actual UTF-8 file in the
caller's own workspace. It returns whole-file, slice and exact-source-version
hashes, accepting no separately pasted quote. Any later file edit makes the old
check inapplicable to the new whole file. A byte-identical child copy can establish
content identity, never a manager's reading or verification.

The latter defaults to the current actor and task; explicit arguments can select
other bound group actors or prior tasks. `SourceAuditStore` queries the existing
public-source store read-only, without initialization, migration, search or access
to private answers/memory. Successes, failures, cache status, versions and served
character intervals describe only the returned event window. Follow its cursor;
missing legacy versions or pagination remain unknown. Fetching a whole page,
returning some characters to a role and model understanding are different facts.
Zero records in a limited window do not establish that the actor never read a page.

Before human quality review, delivery writes
`task_1-artifact-quote-evidence.json` or `task_2-artifact-quote-evidence.json`.
The task result's `artifact_quote_evidence` references its path, SHA-256, status and
counts. This sidecar binds tool-owned records from the current task tree to the
immutable delivery snapshot, separating `matched`, `not_found`, `unknown` and
`unapplied`. Changed whole files, prior-phase records and missing evidence never
become matches. Dropped records make evidence incomplete; a corrupt snapshot
remains unavailable. Coverage, factual and quality judgments in this sidecar stay
null. It does not replace human review; deferred quality remains unknown.

Before a dependent role starts, the team context can include a bounded summary of
native file-check receipts. The trusted current repository must list a same-scope,
`DONE` direct dependency; only files declared in both `result.files` and accepted
`artifacts` qualify. The summary reads the recipient's own integrated files and
applies receipts only to the same current bytes. A previous root task can qualify
when explicitly listed as a direct dependency. This does not expose the
producer's private files, answers or full history, or follow transitive tasks.

The opening summary is limited to 2000 characters and 8000 UTF-8 bytes. Full,
compact and empty variants pass through the normal context budget; mandatory
goals, lineage references and same-task human answers remain intact. Missing,
omitted or incomplete receipts mean unknown prior checks, not that the producer
performed none. Selected-slice matches do not certify every quote or content
quality. See the [interface contract](INTERFACES.en.md) for binding and scope.

`--llm-transport buffered|stream` defaults to `buffered`, retaining the existing
client. Explicit SSE selection uses the same entry and a fresh output directory:

```powershell
python -m moumou --llm-transport stream --out runs/test/NEW_STREAM_SESSION
```

The optional school reasoning setting requires stream mode:

```powershell
python -m moumou --llm-transport stream --llm-reasoning-effort none --out runs/test/NEW_REASONING_SESSION
```

Omitting the option leaves `None` and sends no `reasoning_effort` request field;
buffered mode rejects it. `test.json` records `model.reasoning_effort`, the stream
sidecar records `reasoning_effort`, and each attempt records
`requested_reasoning_effort`. These describe the request, not guaranteed provider
behavior. One fixed-request diagnostic on the configured school endpoint and model
returned a complete `read_source` tool call with `none`, without executing the
tool; default and `thinking.type=disabled` trials ended with `finish_reason=length`.
This is not an instructor-task result or a general HTTP 504 remedy.

CLI `stream` defaults to one attempt per model call; explicit bounded HTTP retries
are described below. It never automatically downgrades to
buffered; by default no completion-token cap is requested; the node's cumulative execution
budget still applies. Only a complete, valid SSE response reaches `RecordingLLM`;
partial text or tool calls are not executed. Each scenario's `stream-transport.json`
records attempts, timings, completion and observed usage separately. Its call IDs
are independent of dialogue IDs: do not infer correspondence or phases from order.
Missing usage is not zero or a complete billing total. Worker retries, legacy
transport retries and new HTTP attempts remain separate. Optional streaming does not establish that long-request
HTTP 504 failures are solved, rewrite previous failures or create instructor scores.
See the [interface contract](INTERFACES.en.md) for fields.

Published baseline source `be0d3f57…` completed full native Windows Conda `nlp` regression:
1613 passed, 8 skipped, 0 failures/errors and zero Ruff diagnostics; pytest 180.15s
(wrapper 180.562s), with stable source/tests. A parent can review failure only for
its own children after all dependencies are terminal, subject to `MAX_WAKEUPS`.
Failure propagation continues to a fixed point; this does not waive acceptance or
quality checks.

The `news-dependency-retest-01` NewsTask1 trial used the same five
input files and unchanged original roles, goal and operator-feedback bytes as the
interrupted baseline, explicit stream/none, six reused public records and no new
Tavily allowance. It naturally ended root DEAD with zero formal exports in
4202.906s; quality remains null. The failed checker's downstream editor did not
run, and the parent recovered, directly exercising the scheduling repair. The
recovery plan still omitted necessary file dependencies; the replacement editor
could not inspect the package.

Four failed stream attempts returned HTTP200 and began within 1.1s, but generated
219k–251k ordinary content characters without tool calls, a finish reason or DONE
before the local deadline; reasoning characters were zero. This is incomplete
generation, not proof of no response or network delay. Transport IDs do not map
themselves to dialogue tasks. Forty-five real human replies accumulated 3129.672s
of waiting, which can overlap and must not be subtracted from delivery time.
Source integrity and 784-span replay checks passed, but all 33 file receipts are
unapplied to the zero-file formal snapshot. Candidate content remains forensic:
time framing, causal wording, review-date distinctions and final editorial review
need correction. This assisted single-goal run is not a canonical six-goal score.

Historical checkpoint `556d78fc…` completed full native Windows Conda `nlp` regression:
1601 passed, 8 skipped, 0 failures/errors and zero Ruff diagnostics. Pytest took
178.56s (wrapper 178.938s), with unchanged source/tests. These checks include the
reasoning option and receipt handoff; they do not establish teacher content quality.
The earlier `8bc0c2ca…` checkpoint retains its separate 1538 passed and 8 skipped.
The original-NewsTask1 assisted trial on `556d78fc…` used explicit stream mode with
`reasoning_effort=none`, six reused public records and zero new Tavily allowance.
Reporter token-budget exhaustion/retry exposed a P1 failed-dependency scheduling
bug, and the operator stopped the trial. The raw manifest still says `running`;
there is no final Task1 result, export snapshot or stream sidecar. Preserve the
exact raw record with separate stop evidence, rather than inventing natural DEAD
closure or a verified zero-file export. Content and quality remain unknown.
That interrupted baseline and its 1601-pass framework checkpoint remain separate
from the current source and new retest.

The generic quality protocol also preserves individual measurement/effective dates,
conditional-probability directions and population boundaries. It distinguishes an
original speaker from the secondary page actually read, and limited support from
missing evidence or disproof. These are review instructions, not an automatic
semantic grader; their effect on a new teacher run remains unverified.

## Explicit output limits and file inputs

`--llm-completion-cap` and `--llm-max-content-chars` require explicit stream mode. The former requests provider `max_tokens` without assuming compliance; the latter bounds ordinary-content Unicode code points locally, separately from the 262144-character tool-argument limit. Omission retains no completion cap and the existing 2097152-character content bound. Invalid or buffered combinations fail before configuration/output creation. Limit failures, length finishes and incomplete responses discard partial output and execute no partial tools.

```powershell
python -m moumou --llm-transport stream --llm-reasoning-effort none --llm-completion-cap 16384 --llm-max-content-chars 32768 --require-declared-inputs --out runs/test/NEW_BOUNDED_SESSION
```

`--require-declared-inputs` defaults to false and is independent of transport. Each planned child explicitly supplies `inputs: [{producer, path}]` or `[]`; a producer must be a sibling key also listed in `deps`. Scoped completed producer files are checked before native copying, and received bytes are verified before the consumer's first model call. The first-admission receipt is not evidence of understanding, final review or human acceptance. Omitted requirements are not inferred. See the [input interface](INTERFACES.en.md#explicit-file-inputs).

Earlier working-tree stage `16981d95…` qualified with 1668 passed, 8 skipped and Ruff0 on native Windows Conda `nlp`; that qualification is separate from published BE0, subsequent source and instructor content review. See [performance evidence](PERFORMANCE.en.md).

## Explicit opening quota and tool-request policy

`--opening-context-tokens` selects an estimated-token opening quota for the formal
team, retaining 8000 when omitted. An explicit value must be an integer from 500
to 2147483647 and no greater than the configured `prompt_ceiling`; this is a runtime
guard, not a provider-capacity statement. The team adapter copies the context
policy for `TeamContextBuilder` only. Generic P1 policy, shared EMA calibration
and cumulative node budgets are unchanged. Full roles, current and ancestor goals,
acceptance and answered facts remain mandatory; an unfit opening is rejected,
without deleting requirements or resetting calibration. This option is independent
of transport.

`--llm-tool-choice auto|required-when-tools` requires explicit stream mode;
omission retains the original auto behavior. `required-when-tools` sends
`tool_choice="required"` only with nonempty tool schemas in that request. Tool-free
requests retain their native plain format without a `tool_choice` field. This is
policy selection before a request, never a failure fallback. A required response
must contain complete calls with valid JSON-object arguments and names from that
request's offered set, a consistent `tool_calls` finish and DONE. Otherwise it
fails, discards partial output and never falls back to auto or executes partial
tools. It does not establish full argument JSON Schema validity, factual truth,
tool execution or permission; actual execution still passes through Gate.

Inspect the selected configuration without model calls first:

```powershell
python -m moumou --plan --llm-transport stream --llm-reasoning-effort none --llm-completion-cap 16384 --llm-max-content-chars 32768 --require-declared-inputs --opening-context-tokens 32768 --llm-tool-choice required-when-tools
```

`opening_context_policy` records requested/effective values and the prompt ceiling
in plans and manifests. Stream policy and actual per-attempt choices are separately
recorded; see the [interface fields](INTERFACES.en.md#opening-quota-and-tool-request-policy).
The 1728/8, zero-Ruff qualification and software failure on `73e1c969…` remain a
separate earlier stage. The November 19 working tree `79a0dbaa…` qualified with 1779
passes, 8 skips and zero Ruff findings. All three new `resilient01` scenarios were
attempted: Task1 roots ended DEAD after pre-body HTTP502, no Task2 ran, and formal
files/new Tavily requests were zero. This is not six-goal content acceptance.
News actually imported six public records; fund imported the authentic 6+4 through
an additionally identity-bound data adapter. Import is not model reading or full
single-argument `--reuse-sources` reproduction. Testing is paused at the user's
request; consumed claims must not restart automatically. See
[performance](PERFORMANCE.en.md) for stage identities, reproduction limits and
unknown quality.

## Bounded transient HTTP retries

`--llm-transient-http-retries 0|1|2` selects additional HTTP retries. Omission is
None with an effective client value 0; even explicit 0 requires stream. Value 2 permits
at most three HTTP request starts. Only 502/503/504 headers before any body, SSE
event or tool fragment are eligible, using fixed 5s then 15s backoff. Requests and
backoff share the original cooperative `timeout_s` deadline (180s in this round),
without renewing each request's allowance; insufficient time fails. Other HTTP
statuses, transport errors, partial 200 responses, protocol/length/output-bound
failures and cancellation never trigger this policy. It does not downgrade to auto
or buffered, or dispatch partial tools.

```powershell
python -m moumou --plan --llm-transport stream --llm-transient-http-retries 2
```

Nonzero HTTP retry policy cannot combine with legacy Python `max_attempts=2` or
`retry_transport=True`. Zero retains the old path and its existing parameter
constraints. This is separate from Worker retry and adds no Tavily allowance.
Plans, manifests and the sidecar preserve selection, HTTP limits, actual request
starts and backoff outcomes; see the [interface fields](INTERFACES.en.md#bounded-http-retries-and-usage).
Even if a later request succeeds, preceding failed-request usage is unknown, so
the whole call returns `usage_present=False` and `usage_raw={}`. The successful
attempt's known usage remains in the sidecar. Do not treat the final successful
request or retained numeric token fields as a complete call bill, or failed costs
as zero. Explicit retry is fault tolerance, not provider reliability or content
acceptance.
