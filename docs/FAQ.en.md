# FAQ

[简体中文](FAQ.md) · [English](FAQ.en.md)

## Environment

**`ModuleNotFoundError: No module named 'moumou'`**

Use the Python inside the virtual environment:

```bash
.venv/bin/python -m pytest -q
```

Or simply `make test`. Do not use the system Python.

**My code changed but the tests still run the old version**

Same cause: a bare `pytest` may resolve to a globally installed one. Use `make` or
`.venv/bin/python` consistently.

**`make install` cannot find uv**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

uv is not required; you can create the environment by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## API key

**`make smoke` reports `DEEPSEEK_API_KEY is not set`**

`.env` is not in the repository root, or you are running from another directory. All commands
expect to be run from the repository root.

**401**

Wrong key, or a stray space pasted into `.env`. Check that nothing follows
`DEEPSEEK_API_KEY=` and that there are no quotes around the value.

**Model name rejected**

Use `deepseek-flash`. The older `deepseek-v4-flash` still works, but the vendor has marked it as
superseded.

**Can several people share one key**

Technically yes, but it is not recommended — quotas get mixed together and problems become hard to
trace. Each person should use their own.

## Git

**What to check before pushing**

```bash
git status
```

Confirm `.env` is not staged. That file must never appear in a commit.

**I committed a key by accident**

Revoke the key in the platform console immediately (that matters more than rewriting history), then
request a new one.

**`git push` fails with `Failed to connect to 127.0.0.1:7888`**

The global git config sets a proxy for github.com, but that proxy is not running. Two options:

```bash
# bypass it for this command only
git -c 'http.https://github.com/.proxy=' push

# or remove the setting permanently
git config --global --unset 'http.https://github.com/.proxy'
```

**How to branch**

One long-lived branch per person, for example `feat/p1-sandbox`, `feat/p2-cli`, `feat/p3-eval`.
Rebase onto main before merging so the history stays linear.

**How to write a commit message**

One line saying what changed. Do not write "update". For example:

```
add path-jail sandbox with escape tests
```

## Tests

**I only want to run my own part**

```bash
pytest tests/test_tool_contract.py -q      # P1
pytest tests/test_channel_contract.py -q   # P2
pytest tests/test_contracts.py -q          # contracts — nobody may leave these red
```

**A contract test is red and it is not my code**

First check whether someone changed `src/moumou/contracts/`. That directory is frozen; changing a
field affects everyone. If it turns out to be a real requirement, raise it in the group and let P0
make the change and synchronise everyone.

## Running

**Does `make demo` call the real model**

No. It uses a scripted model, finishes in seconds and spends nothing. Run it as often as you like.

**Can I delete `runs/`**

Yes. It holds run artifacts (databases, workspaces, logs) and is entirely gitignored. If you want
to keep evidence, copy `runs/eval/report.md` out first.
