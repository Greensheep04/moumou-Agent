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
| P3 evaluation layer | None |

## 7. Directories and artifacts

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
