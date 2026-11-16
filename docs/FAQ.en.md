# FAQ

[简体中文](FAQ.md) · [English](FAQ.en.md)

## Environment

**`ModuleNotFoundError: No module named 'moumou'`**

Run from the repository root using the activated project environment. The target
native Windows environment is Conda `nlp`:

```powershell
conda activate nlp
python -m pip install -e ".[dev]"
```

Use `python -m pytest` when running framework tests so pytest uses that interpreter.
The direct Python commands in [setup](SETUP.en.md) do not require Make or uv.

**My subprocess uses the wrong Python**

Activate the environment before launching the project. Selecting an absolute
interpreter does not by itself update the PATH inherited by shell subprocesses.
For the local environment, `conda run --no-capture-output -n nlp python ...` is an
alternative to activation. The framework's `python3` compatibility handling
selects the active Python interpreter; it does not translate POSIX shell syntax
into Windows syntax.

## Credentials and search

**The model key is missing or the model name is rejected**

Create `.env` from `.env.example` in the repository root. Use the exact key,
compatible endpoint and model name supplied by the course in `DEEPSEEK_API_KEY`,
`DEEPSEEK_BASE_URL` and `DEEPSEEK_MODEL`. A model name for another provider is not
a substitute. A 401 can indicate an invalid key; check the provider response and
configuration without printing credentials into logs or commits.

**Does the model automatically search the internet?**

The current project performs live retrieval through the registered Tavily
`web_search` tool, configured with `TAVILY_API_KEY`. A model response alone is not
evidence that web retrieval occurred. Other search providers are not currently
configured by the formal runner.

**Why did search stop while model reasoning still works?**

The initial allowance is six fresh Tavily requests per scenario, shared by all
four agents and both tasks. Cache hits and concurrent deduplication have separate
records. Model tokens use a different allowance. An actual human can explicitly
grant more searches with `/grant-search N`; the same approved request ID is not
applied twice after recovery.

**A credential was accidentally committed**

Revoke and replace it with the issuing service, then coordinate repository cleanup.
Removing a file in a later commit does not erase its earlier history. Keep actual
credentials in ignored local configuration.

## Tasks, questions and quality

**Which task source is used?**

`python -m moumou` and `python -m moumou.eval.cli` default to `eval/tasks`: three
instructor teams, six goals. The retained self-authored bank uses the same Markdown
format and requires `--tasks eval/supplementary`. It has lower priority and separate
scores. `--plan` inspects sources without model calls.

**Why is a task `DONE` but its quality unknown?**

`DONE` is an execution state. Empty machine criteria establish only self-report;
they do not prove correctness. Quality remains unknown until a real recorded
review establishes it. Deferred review does not automatically pass a goal. See
[evaluation](EVAL.en.md) and the [metric contract](metrics.md).

**Should the agent ask the same question again after delegating?**

The current implementation stores original question/answer records in the same
project/conversation/task/role scope and restores answered facts into pinned
context. An unanswered recorded question keeps its original ID. Conflicting,
missing or corrupt history fails explicitly; no answer or reusable tool grant is
invented. These new recovery paths still await their regression execution.

**Can an old run be resumed just by passing its output path?**

No. The formal CLI requires a fresh output directory. Scoped pending-question
recovery inside the engine is separate from restarting an entire formal suite.
Preserve the original run and stopping evidence, then document any state-transfer
or new-run plan.

**How can quality be reviewed later?**

The offline review command asks a real Console/File HumanIO port and creates an
append-only review record. It does not rerun the model or modify the original task
result. Re-review requires the current review ID via `--supersedes`. Follow
[setup](SETUP.en.md) for the commands and evidence boundary.

## Framework checks and source changes

**Is `tests/` the instructor task bank?**

No. `tests/` contains code-function, interface, storage, orchestration and regression
checks, including checks of the task loader/evaluation framework itself. Its FakeLLM
and scripted human fixtures are test doubles. `eval/tasks/` contains the original
instructor Markdown tasks; `eval/supplementary/` contains the explicit lower-priority
self-authored tasks. All framework tests passing does not establish that the six
instructor goals passed. Keep meaningful regression sources with their functionality;
caches, temporary databases and generated test output are not submission sources.

**What is the difference between `make test` and pytest?**

`make test` / `make eval` run real instructor conversations. `make unit-test` or
`python -m pytest -q` run framework regression tests. Deterministic demos and
scripted human fixtures are framework checks, not measured model performance or
actual quality approvals.

**I want checks for one integration boundary**

```powershell
python -m pytest tests/test_tool_contract.py -q
python -m pytest tests/test_channel_contract.py -q
python -m pytest tests/test_contracts.py -q
```

These are suggested commands, not a claim that this development version passed.
Coordinate shared model/port changes with their consumers and update the relevant
tests and [English interface specification](INTERFACES.en.md).

## Git and saved evidence

**What should be reviewed before committing?**

Fetch remote `main`, compare it with the development branch, and inspect the exact
diff and staged paths. The P3 branch is `dev-P3`. Present a concise English commit
message and exact file scope for confirmation before staging or committing; merge
and push are separate actions. Do not include `.env`, local plans, run output or
private evidence. Meaningful regression sources belong with the code they protect.

**A Git connection fails through a local proxy**

Inspect the relevant repository/environment configuration. If a local proxy is
unavailable, use a reviewed command-scoped override where appropriate; avoid
changing everyone's global Git settings as a workaround.

**Can `runs/` be deleted?**

It contains local databases, continuing workspaces, dialogue, human decisions and
delivered artifacts. Preserve the entire relevant `runs/test/<id>/` directory
before cleanup. A report alone does not preserve its evidence. Do not delete an
active session or the only copy of submission evidence. The
[delivery checklist](DELIVERY.en.md) explains what must be retained separately
from the code repository.
