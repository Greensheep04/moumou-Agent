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

The lower-priority [supplementary bank](../eval/supplementary/README.md) retains the
fifteen self-authored tasks as Markdown using the same role/task conventions. Its
eight groups contain two tasks each except the last, which has one. It requires an
explicit `--tasks eval/supplementary`, never runs as an automatic fallback and has its
own results and 15-goal denominator. Original setup and check information is retained;
historical scripted-answer metadata does not authorize real human answers or approvals.

For memory-transfer tasks, the original warmup is an explicit preparation prerequisite
in the task text. The team must perform and record that preparation; the CLI does not
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

# Optional self-authored tasks, after the instructor work; same protocol.
python -m moumou.eval.cli --tasks eval/supplementary --plan
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
