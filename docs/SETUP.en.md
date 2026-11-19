# Environment Setup

[简体中文](SETUP.md) · [English](SETUP.en.md)

## Runtime and installation

Use Python 3.11 or newer, Git, and the course-provided DeepSeek or HKGAI service.
Native Windows is supported. The local development environment uses Conda `nlp`
with Python 3.12; WSL is not required. GNU Make is optional.

From the repository root in PowerShell:

```powershell
conda activate nlp
python -m pip install -e ".[dev]"
```

Use the activated environment's `python` for the commands below. On another OS,
activate its Python environment and use the same module commands. Framework tests
and offline report reconstruction do not require model or search credentials.

An environment's full `python.exe` path alone does not activate its `PATH`. Tool
commands and acceptance subprocesses inherit the launching process's `PATH`, so use the activated
shell above, or let Conda activate it for the default instructor entry:

```powershell
conda run --no-capture-output -n nlp python -m moumou
```

When using an additional Git worktree, the environment's editable installation
may still point to another checkout. From the worktree root, bind imports for
the current PowerShell session and inspect the loaded module before testing:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "src"
python -c "import moumou.eval.cli; print(moumou.eval.cli.__file__)"
python -m moumou.eval.cli --plan
```

The printed path must belong to the intended checkout. This changes only the
current process environment; it does not reinstall packages, alter the machine's
Python settings, or require copying credentials between worktrees.

## Local credentials

Copy the template if `.env` does not already exist, then edit the local file:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Course-provided model key; required for real task execution |
| `DEEPSEEK_MODEL` | Model identifier supplied for your DeepSeek/HKGAI service |
| `DEEPSEEK_BASE_URL` | Compatible service endpoint, including its required API prefix |
| `DEEPSEEK_TIMEOUT_S` | Per-model-request timeout in seconds; default 180 |
| `TAVILY_API_KEY` | Search credential for tasks that require live research |

The `DEEPSEEK_*` names are also used with HKGAI. For the course HKGAI service,
the public endpoint is `https://api1.hkgai.net/v1` and the configured model is
`t2_hkgai-v3_fp8_1m_e7`. Put only your actual local credential in `.env`.
Environment variables take precedence over `.env`. Keys and runtime artifacts
remain outside Git.

## Verify the framework

```powershell
python -m pytest -q
python -m ruff check src tests
```

These are automated implementation checks, separate from instructor task quality.
The Make equivalent for pytest is `make unit-test`. `make test` and `make eval`
run the instructor evaluation and need a configured model and real human input.

## Run the instructor tasks

The canonical task source is [eval/tasks](../eval/tasks/README.md): three teams,
each with the original four role prompts and two ordered task texts. Inspect the
sources without credentials, an event loop, model calls, or a new run directory:

```powershell
python -m moumou --plan
python -m moumou.eval.cli --tasks eval/tasks --plan
```

The fifteen self-authored tasks are retained under
[eval/supplementary](../eval/supplementary/README.md), using the same Markdown team/task
protocol in eight groups (the last has one task). They have lower priority, are not
selected automatically, and are not included in instructor scores. To inspect or run
that bank, explicitly use `--tasks eval/supplementary` with the same CLI.

Both application commands use the same formal evaluation protocol:

```powershell
python -m moumou
python -m moumou.eval.cli --tasks eval/tasks
```

Defaults are all three scenarios, real console interaction, real quality review,
and a fresh directory under `runs/test/`. To select one team or use file input:

```powershell
python -m moumou --scenario scenario_1 --human files --out runs/test/NEW_SESSION
```

Buffered model responses remain the default. The optional school SSE client can
be selected explicitly using `--llm-transport stream` on the same entry point.
It accepts only complete responses, records attempt metadata separately, and
does not automatically fall back to buffered requests or retry HTTP failures.
Short transport checks do not establish long-task reliability or factual quality.

The school stream client also accepts an explicit reasoning setting:

```powershell
python -m moumou --llm-transport stream --llm-reasoning-effort none --out runs/test/NEW_REASONING_SESSION
```

Omitting `--llm-reasoning-effort` leaves it at `None` and sends no such request
field. Buffered mode rejects the option. The manifest, stream sidecar and attempt
diagnostics record the requested setting, not proof that the provider honored it.
On one fixed request to the configured school endpoint and model, `none` produced
a complete `read_source` tool call without executing it; default and
`thinking.type=disabled` trials ended with `finish_reason=length`. This limited
diagnostic is neither an instructor-task pass nor a general HTTP 504 fix.

Current source `be0d3f57…` completed full native Windows Conda `nlp` regression:
1613 passed, 8 skipped, no failures/errors and zero Ruff diagnostics, with stable
source/tests. Its assisted NewsTask1 retest used unchanged inputs, explicit
stream/none, six reused public records and no new Tavily allowance. It naturally
ended DEAD with zero formal exports and unknown quality. Scheduling recovery was
observed, but missing review-file handoffs, incomplete stream responses and content
corrections remain; framework checks do not establish teacher acceptance.

Historical checkpoint `556d78fc…` retains its separate 1601 passed and 8 skipped.
Its assisted NewsTask1 trial was operator-stopped after reporter token-budget
exhaustion/retry exposed a failed-dependency scheduling bug. Raw `running` and
missing final result/export/transport sidecar are retained; content quality is
unknown. It is not the new retest. Versioned outcomes and stop evidence are in
[performance](PERFORMANCE.en.md).

`--out` must name a directory that does not exist. A scenario keeps one Engine,
four private continuing role workspaces, memory, and actual Task 1 history for
Task 2. The workflow and checks are negotiated during execution; the harness
does not replace the original requests with scripted questions or fixed answers.

After each task's deliverables are saved, the default quality review waits for
an actual `approve` or `reject`, with optional feedback. Rejection stops later
dependent tasks in that group; independent groups can continue. `--quality-review deferred` explicitly leaves quality unknown; finishing
that mode does not certify that the instructor goals passed.

## Real human input

Console mode displays requests and waits for the person's answer. For an approval
or review, enter `approve` or `reject`, optionally followed by a feedback note.

File mode stores each original request under:

```text
SCENARIO_ROOT/human/requests/REQUEST_ID.json
SCENARIO_ROOT/human/pending/REQUEST_ID.json
```

An external human/controller places the actual `HumanResponse` in:

```text
SCENARIO_ROOT/human/responses/REQUEST_ID.json
```

The response must echo `request_id`. Questions require actual `text`; approvals
and reviews require an explicit `decision` (`approve` or `reject`). Accepted
responses are retained under `human/consumed/` and in the dialogue. Do not write
predefined replies or infer quality approval from permission to execute a tool.
If a controller legitimately reuses a standing user grant, record its source and
distinguish that policy decision from a newly entered human answer.

## Search allowance and execution bounds

`--search-requests 6` gives each scenario an initial allowance of six new Tavily
HTTP attempts, shared by its four members and both tasks. The full three-team
suite therefore starts with 18. Cache hits and concurrent deduplication are
reported separately. An explicit human answer `/grant-search N` to a question
can add requests; a model cannot grant its own allowance.

This search allowance is separate from the school's model access. Per-node step,
token and cooperative time bounds guard runaway execution. They are not monetary
budgets or a hard deadline for the entire scenario. Actual human waiting and
post-delivery quality-review waiting are recorded with their own boundaries.

## Saved artifacts and reports

| Location | Contents |
| --- | --- |
| `RUN/test.json` | Suite plan, source hashes, public configuration and saved state |
| `RUN/active.json` | Current scenario, phase and human-input directory |
| `RUN/report.md` | Derived human-readable suite report |
| `RUN/scenario_N/task_1-result.json` | Task 1 delivery, subtree and quality observations |
| `RUN/scenario_N/task_2-result.json` | Task 2 observations, once posted and delivered |
| `RUN/scenario_N/dialogue.jsonl` | Complete redacted model requests/responses and real human exchanges |
| `RUN/scenario_N/workspaces/` | Four private continuing role directories and actual outputs |
| `RUN/scenario_N/human/` | Durable human requests and responses |
| `RUN/scenario_N/search/` | Search allowance, cache and request audit |
| `RUN/scenario_N/task_N-artifacts/` | Immutable delivery manifest and captured hashed files, when recorded |
| `RUN/scenario_N/state-snapshot.json` | Scenario-close node diagnostics, not additional deliveries |
| `RUN/reviews/<UUID32>/` | Append-only offline review intent, original HumanIO evidence and decision |

Rebuild a saved report without model calls or credentials:

```powershell
python -m moumou.eval.report --run runs/test/EXISTING_SESSION
```

Report reconstruction does not execute tests, resume a session or approve a task.
There is no automatic resume option. If a process is forcibly stopped, its raw
manifest may still say `running`; preserve that record and use explicit stop and
process-cleanup evidence to describe the actual state before planning continuation.

### Saved analysis, replay and later quality review

These commands operate on an existing saved session. The current candidate's
verification status is recorded in [the performance report](PERFORMANCE.en.md);
usage examples do not establish that checks were executed.

```powershell
python -m moumou.eval.analysis --run runs/test/EXISTING_SESSION
python -m moumou.eval.replay --run runs/test/EXISTING_SESSION --out runs/test/EXISTING_SESSION/replay-v1.json
python -m moumou.eval.review --run runs/test/EXISTING_SESSION --scenario scenario_1 --phase task_1 --human console
```

Analysis/replay do not call a model, rerun acceptance or resume the suite. Replay JSON
must use a fresh output file; original records and existing evidence are not replaced.
Old missing captures remain unknown, rather than reading current workspaces/memory.db
as earlier state. The same-task scoped question resume mechanism is separate from
starting a new CLI suite in an already existing output directory.

Review asks for an explicit Console/File HumanIO approve/reject; there is no decision
flag or automatic approval. File mode places its pending queue in that new review's
human directory. Re-review requires `--supersedes CURRENT_TIP_UUID32`; all older reviews
remain. The report overlays effective quality while preserving the raw result and its
source/artifact bindings. Matching recorded I/O is not cryptographic human identity.

Review publication holds `reviews/.publish.lock`; an existing lock needs explicit
operator inspection/reconciliation, not automatic stale-lock deletion. Search-grant
callbacks use the approval request identity so resumed callbacks cannot add the same
allowance twice.

## Make equivalents

| Command | Purpose |
| --- | --- |
| `make test` / `make eval` | Run the same instructor task protocol |
| `make unit-test` | Run framework pytest checks without model calls |
| `make report REPORT_RUN=runs/test/EXISTING_SESSION` | Rebuild a saved report offline |

Set `EVAL_PY` to an explicit interpreter path when needed. Native Windows users
without Make can use the equivalent Python commands above. Within framework
commands, the `python3` compatibility alias selects the active interpreter; it
does not translate POSIX shell syntax into Windows syntax. Use native commands
and `python` in your own terminal.

No GitHub Actions workflow is bundled in the proposed delivery tree. Run checks
explicitly in the chosen environment and retain their actual output. Deleting the
workflow file locally does not alter historical hosted runs; it takes effect on a
remote branch only after that deletion is committed and pushed to that branch.

See [evaluation workflow](EVAL.en.md), [interfaces](INTERFACES.en.md), the
[metric contract](metrics.md) and [delivery checklist](DELIVERY.en.md).
