# P1 — Execution and Tools

[简体中文](P1-执行与工具层.md) · [English](P1-execution-and-tools.en.md)

> The most decoupled part of the project. You are responsible for **one interface (`Tool`)** and do
> not need to know how the agents make decisions.

| Item | Detail |
| --- | --- |
| Course features covered | 5 isolated execution environment · 6 web search · 7 file and code execution · 8 Git/GitHub |
| Estimated effort | about 26% (≈18 person-days, the second heaviest in the group) |
| Upstream dependency | P0's `contracts/` (delivered by the end of week 1) |
| Downstream consumers | P0's orchestration core, calling you through the `Tool` protocol |
| Key risk | if `git worktree` parallelism and merging are unstable, the whole "parallel agents" demo collapses → **do this in week 3** |

---

## 1. What you own

| Directory | Technical name | Purpose |
| --- | --- | --- |
| `sandbox/` | execution sandbox (workspace isolation) | one private directory per agent, no writing outside it; timeouts and output truncation |
| `workspace/` | workspace management (git worktree) | split one repository into several clones, each agent on its own branch, merge at the end |
| `tools/` | tool layer (function calling) | the tools exposed to the model |

---

## 2. Interfaces to implement (frozen)

```python
# contracts/tool.py — provided by P0; you write the implementations
from pathlib import Path
from typing import Protocol, Literal
from pydantic import BaseModel

ActionClass = Literal[
    "fs_read", "fs_write", "fs_delete", "shell",
    "git_commit", "git_push", "network", "install",
]


class ToolContext(BaseModel):
    agent_id: str
    task_id: str
    workspace: Path                 # this agent's own directory; read and write only here
    env: dict[str, str] = {}


class ToolResult(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None        # required when ok=False; one line, shown to the model
    artifacts: list[str] = []       # produced files, relative to the workspace
    truncated: bool = False


class Tool(Protocol):
    name: str                       # unique, e.g. "shell_exec"
    description: str                # one sentence for the model
    params_schema: dict             # JSON Schema for function calling
    action_class: ActionClass       # decides whether approval is required
    timeout_s: float                # the framework's outer timeout
    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

### The tools to deliver

Names and arguments are yours to choose; the capabilities are not:

| Tool | Must support | action_class |
| --- | --- | --- |
| `fs_read` | read files, list directories, grep contents | `fs_read` |
| `fs_write` | create / overwrite / append | `fs_write` |
| `fs_delete` | delete files / directories | `fs_delete` |
| `shell_exec` | run arbitrary shell commands | `shell` |
| `python_exec` | run a Python script or snippet | `shell` |
| `fs_apply_patch` | old→new text replacement (optional but recommended: far cheaper in tokens than rewriting a file) | `fs_write` |
| `git` | status / diff / add / commit / log / branch / merge | commit needs no approval |
| `git_push` | push to GitHub | `git_push` ← **approval required** |
| `web_search` | search the web, return structured results (title / url / snippet) | `network` |

---

## 3. Implementation conventions (external behaviour only)

1. **Turn exceptions into results.** Wrap failures as `ok=False` plus a readable `error`.
   That error goes back to the model verbatim so it can correct itself; raising breaks the
   orchestration loop.
2. **Clean up child processes.** The framework applies an outer `asyncio.wait_for`; when the
   coroutine is cancelled it is worth killing the child too, otherwise dozens of tasks leave the
   machine slower.
3. **Keep paths inside the workspace.** Resolve `..`, absolute paths and symlinks before checking;
   on escape return `ok=False, error="path_outside_workspace"`.
4. **Truncate output.** Clip stdout/stderr above 8KB and mark it `[truncated]`; a single `cat` of a
   large file would otherwise blow up the model's context.
5. **Report artifact paths.** New and modified files go into `artifacts`; the orchestration layer
   needs them for integration and display.
6. **Approval and logging are the framework's job.** It asks before the call and wraps a trace span
   around it, so a tool should not print its own logs or pop its own confirmation dialogs.

---

## 4. Acceptance

Provided: `tests/test_tool_contract.py`. Passing it is the definition of done. Roughly it checks:

- every tool declares `name` / `params_schema` / `action_class`, and the schema is well formed
- normal input returns `ok=True`
- a deliberately broken input (missing file, bad command) returns `ok=False` rather than raising
- `../../etc/passwd` is refused
- `while True: pass` returns within the timeout and the process is really gone
- 100MB of output is truncated

---

## 5. Yours to decide

- `LocalJail` or `Docker`: **LocalJail is the baseline, Docker earns extra credit**
- git worktree versus plain `cp -r`
- which search API (Tavily / Serper / DuckDuckGo)
- file size limits and default timeouts
- code organisation and whether to use a base class
- the wording of error messages

---

## 6. Out of scope

- no prompts, no decisions, **no LLM calls**
- no memory implementation
- no message transport (that is P2)
- no metrics (that is P3)

---

## 7. Two things to be able to explain at the demo

1. **Our isolation is application-level path checking, not kernel-level isolation.**
   A Docker backend would give real isolation but is switched off by default for demo speed.
   State this limit honestly in the architecture document — "we know where the boundary is" scores
   better than pretending.
2. **`git worktree` lets several agents work on different branches of the same repository in
   parallel, merging at the end.** This is the most direct implementation of "isolated environment
   plus final integration"; show `git log --graph` during the demo.
