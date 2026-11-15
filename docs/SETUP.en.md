# Environment Setup

[简体中文](SETUP.md) · [English](SETUP.en.md)

> From a clean machine to passing tests and a working model connection: about five minutes.

> The Chinese [SETUP.md](SETUP.md) is the authoritative version. This file is a translation.

## 1. Requirements

| Item | Requirement | Notes |
| --- | --- | --- |
| OS | macOS / Linux | On Windows, use WSL2 |
| Python | **3.11 or newer** | The development environment runs 3.14.6 |
| Package manager | [uv](https://docs.astral.sh/uv/) (recommended) | Plain `venv` + `pip` also works |
| Git | Any recent version | |
| Network | Reachable `api.deepseek.com` | Not needed for tests or the demo |
| Docker | Optional | Only needed for P1's sandbox backend |

Dependency versions are constrained by `pyproject.toml`. The essentials are `pydantic>=2.7`,
`httpx>=0.27`, `pyyaml>=6.0`, `pytest>=8`, `ruff>=0.6`.

## 2. Install

```bash
git clone https://github.com/Greensheep04/moumou-Agent.git
cd moumou-Agent
make install
```

`make install` does two things: creates `.venv` in the repository root, then installs the
project in editable mode together with its development dependencies.

If uv is not installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or skip uv entirely:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`make install` only creates `.venv` when it does not already exist, so re-running it is safe.

## 3. Configure the model

```bash
cp .env.example .env
```

Then edit `.env` and fill in your own key:

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | **yes** | none | Request one at <https://platform.deepseek.com/api_keys> |
| `DEEPSEEK_MODEL` | no | `deepseek-flash` | Stable id for DeepSeek-V4.1-Flash-1017 |
| `DEEPSEEK_BASE_URL` | no | `https://api.deepseek.com` | Usually unchanged |
| `DEEPSEEK_TIMEOUT_S` | no | `180` | Per-request timeout; raise it for long tasks |

A few notes:

- `.env` is gitignored and will **not** be committed
- Use your own key so quotas and failures stay traceable per person
- Real environment variables take precedence over `.env`, which makes CI injection easy

## 4. Verify

Two steps. The first needs no key:

```bash
make test
```

Expected tail: `127 passed`.

The second needs a key:

```bash
make smoke
```

It performs three checks in sequence: a plain chat round-trip, a request with function
calling, and a listing of the tools the agent advertises. Expected output:

```
base_url : https://api.deepseek.com
model    : deepseek-flash

[1] plain chat  1.57s
    content   : 'pong'
    tokens    : 38 in / 14 out

[2] tool call   1.13s
    tool      : echo  args={'text': 'hi'}

[3] agent advertises 8 tools: ['ask_human', 'delete_file', 'echo', 'read_file',
                              'run_python', 'shell', 'submit_plan', 'write_file']

OK — the API path is wired correctly.
```

If step 2 returns no tool call, function calling is not working and decomposition will suffer —
investigate that first.

## 5. Common commands

| Command | Purpose | Needs a key |
| --- | --- | --- |
| `make install` | Create the environment, install dependencies | No |
| `make test` | Run the full test suite | No |
| `make lint` | Lint the code | No |
| `make fmt` | Auto-fix what can be fixed | No |
| `make demo` | End-to-end demo (scripted model, seconds) | No |
| `make demo-memory` | Memory loop demo: failure → reflection → reuse | No |
| `make smoke` | Verify the key and model id | **Yes** |
| `make eval` | Run the evaluation set | **Yes** |
| `make clean` | Remove runtime artifacts | No |

All commands run from the **repository root** and go through `.venv/bin/python`, so the system
environment is never touched.

## 6. Extra requirements per module

| Module | Extra requirements |
| --- | --- |
| P1 execution layer | Docker (optional; the local sandbox does not need it) |
| P2 interaction layer | A terminal with ANSI support (for the TUI) |
| P3 evaluation layer | No extra runtime dependency; see the native Windows / Conda entry point below |

### P3: native Windows / Conda entry point

P3 currently uses native Windows with the Conda `nlp` environment, verified on Python 3.12.16.
The `make` / `.venv/bin/python` commands above describe the original Unix-style workflow.
From PowerShell at the project root, P3 can use:

```powershell
conda activate nlp
python -m pip install -e ".[dev]"
python -m moumou.eval.cli --help
```

Configure the course-provided DeepSeek or HKGAI service in the local `.env`. The loader still
uses `DEEPSEEK_*` variable names; set the compatible endpoint and model without committing
the key. A model-backed P3 run creates a fresh batch directory by default:

```powershell
python -m moumou.eval.cli --tasks eval/tasks --only s1_hello
```

This command uses the configured model API. The default output is a unique directory under
`runs/eval/`. An explicit `--out` selects the exact batch directory; an existing nonempty batch
or repeat directory is rejected rather than reused. Use `--workspace-backend git` to exercise
P1 worktree delivery, or keep the default `local` backend. `--eval-id` optionally supplies the
identifier recorded in the batch manifest.

Each batch saves `manifest.json`, `results.jsonl` and `report.md`. The manifest identifies the
current source snapshot, Git version, fixture hashes and public run configuration; credentials
are excluded. Each `<fixture_id>/r<repeat>/` retains its `result.json`, `trace.jsonl`, SQLite
`trace.db`, task and memory databases, and private workspaces. Results are persisted after each
attempt, including failures, so an interrupted batch retains earlier completed results.

For inspection after interruption, `moumou.eval.artifacts.load_results(batch_path)` reads the
per-run result files; it can recover records whose JSONL append was interrupted. This is an
inspection path, not automatic continuation of an old batch. Per-run writes and the batch
journal do not form a shared atomic transaction. Recorded Engine metrics retain their
documented scope; complete time/token accounting is separate evaluation work.

For a supervised whole-attempt deadline on native Windows, opt in explicitly:

```powershell
python -m moumou.eval.cli --tasks eval/tasks --only s1_hello --hard-timeout 900
```

With `--hard-timeout`, each attempt runs in a fresh Python process. The parent starts the
worker suspended, assigns its Windows Job Object, then resumes it. The deadline includes
startup, execution and client shutdown; process-tree cleanup has a separate bounded grace
period. A candidate result becomes authoritative only after a normal worker exit, identity
validation and verified cleanup. Timeout, startup/worker failure and malformed results are
saved as failed attempts rather than accepted as success. Previously written traces remain.

Without this option, the original in-process integration API is retained. Hard worker mode
currently requires native Windows; it does not claim POSIX containment across detached
sessions. The OS process-creation call itself cannot be preempted by the Python supervisor.
The worker rebuilds its own model client; credentials are passed through a private child
environment field, excluded from the JSON request, command line and artifact metadata.

Within framework-launched shell commands on Windows and POSIX, bare `python3` resolves through
a temporary alias to the interpreter running the framework, including compound and nested
commands. The parent and system PATH are unchanged;
use the activated environment's `python` or `conda run -n nlp python` in your own terminal.
This is command-name compatibility, not general POSIX shell-syntax translation.
Local development-test counts are not a clean-clone result when their test changes have not
been uploaded; FakeLLM fixture checks are separate from real-model evaluation scores.

### P3 fixture set, metrics and ablation preflight

The self-built set contains fifteen fixtures: two per course scenario and one additional
composite task. Each YAML defaults to three repeats. Use `--repeats 1` for an explicit smoke
run. Strong artifact checks and recorded behavior are separate; research additionally needs
the P1 search tool's local `TAVILY_API_KEY`. Without that configuration, real research
validation is pending even when its offline acceptance examples pass.

```powershell
# No API call and no output directory creation; lists 180 planned held-out attempts.
python -m moumou.eval.cli --ablation --plan

# A single controlled group; explicit controls disable role review by default.
python -m moumou.eval.cli --only s1_hello --repeats 1 --no-memory --max-slots 1 --hard-timeout 180

# Four groups for one representative task, one repeat each; this uses the model API.
python -m moumou.eval.cli --ablation --only s6_csv_memory --repeats 1 --hard-timeout 180
```

Memory fixtures in the ablation entry point run a separate warmup, export only actual
long-term records, and seed fresh databases for all four groups. An empty warmup does not
invent a lesson. Role review is fixed off and the conclusion board remains active, so this
is long-term experience memory ablation. `--ablation` rejects independent memory/slot/review
overrides; these would invalidate the declared four groups. Warmup has a cooperative
in-process budget, distinct from the native Windows hard deadline for held-out workers.

`metrics.json`, per-attempt `llm-telemetry.json`, task-terminal timing, unknown-usage coverage
and behavioral checks are described in the English [metric contract](metrics.md). Tokens
from scripted clients and incomplete provider usage are not presented as measured cost.

### Offline reports and a frozen experiment plan

The three P3 entry points work directly in an activated native Windows Conda environment:

```powershell
python -m moumou.eval.cli --help
python -m moumou.eval.report --run runs/EXISTING_BATCH --out runs/rebuilt-report.md
python -m moumou.eval.ablation --plan
```

Report input may be a normal batch or a four-group ablation root. No key or network is
needed to regenerate it. Existing raw results remain unchanged. Normal eval/ablation CLI
runs also rebuild the detailed report when execution finishes. Known research fixtures are
rejected before model-client creation if `TAVILY_API_KEY` is missing; dry plans still work.

With GNU Make installed, equivalent wrappers are available. `EVAL_PY` defaults to `python`
from the active environment; it may be an explicit interpreter path. The original non-P3
Make targets retain their existing interpreter setting.

```powershell
make report REPORT_RUN=runs/EXISTING_BATCH
make ablation-plan
make ablation
```

`make ablation` defaults to a dry plan. Executing model-backed runs requires explicitly
overriding `ABLATION_ARGS`; this is separate from generating or verifying a freeze. Windows
users without GNU Make can use the Python commands above without installing another tool.

To prepare a snapshot, provide a public model JSON (provider, model, base_url, timeout_s,
max_retries, temperature=0.2) and a boolean capability JSON, for example
`{"web_search": false}`. Never put API keys in either file. Keep them under local `runs/`.

```powershell
python -m moumou.eval.freeze prepare --out runs/frozen-exp --model-config runs/model-public.json --capabilities runs/capabilities.json --repeats 3 --hard-timeout 180 --max-steps 20 --max-tokens 150000 --max-wall-s 150 --max-retries 0 --warmup-max-steps 25 --warmup-max-tokens 150000 --warmup-max-wall-s 150 --warmup-max-retries 0
python -m moumou.eval.freeze verify runs/frozen-exp
python -m moumou.eval.freeze check-start runs/frozen-exp --scope full
```

These example budgets are explicit per-node limits, not an experiment-wide cost ceiling.
Warmup overrides are separate from held-out overrides. The freeze captures actual settings
and runtime file copies and makes no model requests. A failed full start check returns exit
code 2 and zero model calls. With all fifteen bundled tasks and no search capability, the
plan remains 180 attempts: 144 eligible and 36 blocked/not started. `check-start --scope
non-network` only inspects that selection; it does not start 144 runs or provide resume.

Keep runtime files unchanged after freezing. Checksums detect changed files; the snapshot
does not automatically execute its copies or prevent edits during a running call. A later
commit/checkout or dependency update may require a fresh freeze, with the old archive kept.
Existing Windows/Ubuntu CI already runs all offline tests, Ruff and the P1 demo; these P3
changes do not alter workflow triggers or require keys in CI.

### Explicit non-network execution and offline analysis

When search credentials are unavailable, explicitly select the non-network fixtures:

```powershell
python -m moumou.eval.ablation --non-network --plan
python -m moumou.eval.ablation --non-network --repeats 3 --hard-timeout 180 --out runs/NEW_NON_NETWORK_BATCH
python -m moumou.eval.analysis --run runs/EXISTING_BATCH
python -m moumou.eval.analysis --run runs/EXISTING_BATCH --out runs/analysis.json
```

The execution command uses the model API and the task YAML budgets; it does not automatically
apply budget overrides from a separately prepared freeze. For guarded programmatic execution,
load the frozen effective fixtures and pass `freeze.check_attempt` as `before_attempt`, with
the actual public model configuration supplied in its context. Keep runtime files unchanged.

The bundled full schedule stays at 180 rows. Explicit non-network execution selects 144;
36 are saved as deferred/not started with a reason, preserving their original group order
and paths. Finishing that selection leaves status `partial` and CLI exit code 1; exit code 0
means the entire declared plan completed. Read actual result counts to assess task outcomes.
Use a fresh output directory for a new experiment. Neither selection nor report generation
resumes an earlier batch or silently retries only failed outcomes.

Analysis is entirely offline and can also read historical ordinary batches. It distinguishes
observed failures from deferred or unexplained missing results, links evidence, inventories
saved successful-attempt checks and compares paired memory-on/off observations. It does not
re-execute machine acceptance or claim a causal memory benefit from one pair. Keep generated
reports and original evidence under local `runs/`; they remain outside Git.

## 7. Directories and artifacts

### Default instructor conversation entry

`python -m moumou` defaults to `Mid_Test/`, all three instructor scenarios, real console
input, and six new Tavily requests per scenario shared by its two tasks. Source Markdown
is preserved verbatim. It is not converted into the independent fifteen-task YAML dataset.

```powershell
python -m moumou --plan
python -m moumou --human console
python -m moumou --scenario scenario_1 --human files --out runs/midtest/NEW_SESSION
```

Each scenario has one Engine, four fixed teacher roles, private persistent role workspaces
and two consecutive tasks. Task 2 receives Task 1's real outputs and history references.
No default automated human answers are used. File mode writes actual requests under
`SCENARIO_ROOT/human/pending/`; an external human writes the matching response JSON to
`SCENARIO_ROOT/human/responses/REQUEST_ID.json`. A response contains `request_id` and actual
`text`, or an explicit `decision` of `approve`/`reject` for an approval. Never fabricate a
response, reuse an old response or put API credentials into a reply.

When explicitly authorizing extra search quota in response to a question, the human can
enter `/grant-search N`. Ordinary replies never grant quota automatically. Cache entries
retain their original retrieval time; fresh requests, cache hits and denied requests have
separate audit records. Runtime output includes complete redacted model messages and real
human exchanges in `dialogue.jsonl`, per-task records and a session report. These are local
artifacts, not committed source files.

Per-node limits prevent runaway loops; they are not a restriction on the school's model
allowance. Real human waiting is included in the cooperative task deadline and separately
measured. This interactive path is not the earlier native hard-worker benchmark. `DONE`
with no machine criteria remains agent-reported completion. The default human quality
review waits after saving the deliverables, before Task 2 is posted. An explicit rejection
is a quality outcome with exit code 1, not an infrastructure error. `--quality-review deferred`
leaves quality unknown and never marks instructor goals verified automatically.

The older `moumou.eval.cli`, report and ablation modules remain available for independent
regression work. Consolidating those old entry points into the instructor entry is deferred
to the following development day; they are not silently redirected in this change.

| Path | Contents | Committed |
| --- | --- | --- |
| `.venv/` | Virtual environment | No |
| `.env` | Local secrets | **No** |
| `runs/` | Runtime artifacts: databases, workspaces, traces, eval reports | No |
| `eval/tasks/` | Evaluation task set | Yes |
| `docs/` | Documentation | Yes |

`make clean` removes `runs/`, `.pytest_cache`, `.ruff_cache` and every `__pycache__`.
If `runs/` holds an evaluation report worth keeping, copy it out first.

## 8. Troubleshooting

Common problems are collected in [FAQ](FAQ.md) (Chinese): missing modules, keys not taking
effect, git push failures, tests running stale code. When you hit a new one, adding it to the
FAQ saves the next person the same detour.
