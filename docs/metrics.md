# Evaluation metric contract

This English contract is for P0 instrumentation and P2/report consumers. The implementation
is `moumou.eval.metrics.summarize(manifest, results)`. Read authoritative attempt files with
`moumou.eval.artifacts.load_results(batch_path)`; do not reconstruct scores from console text.
Each batch writes `metrics.json` and a Markdown report. These definitions apply to self-built
fixtures; they do not claim that the instructor's closed test set has been evaluated.

## Completion

The root-goal denominator is every attempt in `manifest.runs`, including planned attempts
whose result is missing after interruption. Missing results are separately counted and do
not imply that execution started. A verified root success requires `status == succeeded`,
`root_status == DONE`, `root_machine_accepted == true`, and `root_success == true`.
The last field also requires every declared behavioral check to pass. Empty acceptance
self-report is unverified and never raises the verified goal score.

Subtasks are observed nodes with a non-null `parent_id`; the root is excluded. Report both
the DONE fraction and the machine-verified DONE fraction. A root failure can coexist with
successful subtasks. Nodes without machine criteria and unknown criteria are counted
separately. A killed worker can leave unknown nodes, so observed-subtask rates are explicitly
coverage-limited; they are not estimates of unobserved work.

## Time boundaries

| Field | Start and end |
| --- | --- |
| `task_elapsed_s` | `Engine.submit` to the root terminal scheduler callback, before root reflection |
| `maintenance_s` | Root terminal callback to closing Engine-owned resources |
| `elapsed_s` | Runner attempt setup to result preparation; supervised mode also includes process launch and cleanup |
| `wall_s` | Legacy Engine scheduler interval, excluding its later maintenance and role review |

Task time includes workspace seeding performed after submit, machine acceptance, and child
reflection completed before the root terminates. It is measured with a monotonic clock;
`submitted_at` and `terminal_at` provide UTC correlation. Failure and timeout attempt times
remain reported. A worker killed before a terminal callback has an unknown task-terminal
time rather than a fabricated zero. The parent still records its attempt elapsed time.
HTTP-client shutdown happens outside `engine.close()` and is not part of `maintenance_s`;
supervised `elapsed_s` includes that later shutdown and containment cleanup.

`metrics.spans` reports LLM, tool, approval and question cumulative durations, interval unions,
overlaps and missing endpoints. Concurrent spans may overlap and must not be summed as wall
time. These traces retain P0's capture coverage. Client telemetry independently covers all
wrapped chat calls, including reflection and role review, with execution/post-terminal phases.

## Usage and repeated trials

`LLMResponse.usage_present` and whitelisted `usage_raw` preserve whether the provider actually
returned usage. The old non-null integer token fields remain compatible with budget logic.
The evaluation wrapper records calls before and after awaiting the client, in the attempt's
`llm-telemetry.json`, without storing prompts, model responses or credentials.

`total_tokens_in` and `total_tokens_out` are complete wrapper-observed successful-call totals
only when every observed call has the relevant provider measurement. Otherwise they are
null; telemetry retains known subtotals and missing-call coverage. Cache usage is independently
nullable. FakeLLM counters are scripted diagnostics, not measured provider usage. Internal
HTTP retries, failed-request billing and unreported provider charges remain unknown; there
is no assumed price or fabricated monetary cost.

Structured statistics are grouped by fixture, scenario and whole batch. Numeric summaries
carry `n`, `mean` and sample standard deviation (`n - 1`); one measurement has no standard
deviation. Goal outcome statistics include planned missing attempts as failures, while
duration statistics include only measured durations and expose their sample count. Legacy
`runner.aggregate` output remains labeled as Engine diagnostics and uses its historical
definition; use `metrics.json` for the metric contract above.

## Behavioral and experiment evidence

Fixture `requirements` supplement artifact checks: approvals require recorded decisions and
matching effects; search requires a successful search tool call; memory requires known lesson
text actually present in the outgoing system Lessons section. Candidate retrieval IDs alone
are insufficient. Memory-off checks reject any observed Lessons injection.

Parallel fixtures require real delegated independent siblings and observed positive-duration
child spans. Parallel-on requires overlap; max-slots-one controls require no overlap. Root
artifact acceptance remains separate from these checks. Supervised parents re-evaluate
behavior from saved trace and client metadata before accepting a candidate. This consistency
check is not a security boundary against hostile code editing its own evidence files.

The four ablation groups change long-term experience memory and task slots (4 or 1), with
role review disabled throughout. The conclusion board remains enabled. Warmup uses a separate
conversation and workspace; only exported MemoryRecord values seed each fresh held-out run.
Warmup time/usage is separate. Its in-process cooperative deadline is distinct from the native
Windows held-out worker deadline. Independent repeats rebuild the same starting state per
group and never share a mutable memory database. Run a dry plan before the default formal
15-fixture, three-repeat, four-group experiment (180 held-out attempts).
