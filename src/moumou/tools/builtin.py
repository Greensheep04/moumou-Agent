"""Reference tools.

These exist so the framework runs end to end before P1 lands. They implement
the same ``Tool`` contract P1 will, so swapping them out is a one-line change.

Two rules every tool here follows, because the contract tests enforce them:
  * never raise — turn failures into ``ToolResult(ok=False, error=...)``
  * never escape the workspace — everything goes through ``resolve_in``
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from moumou.contracts import ToolContext, ToolResult
from moumou.contracts.plan import Plan
from moumou.tools.registry import flatten_schema

MAX_OUTPUT = 8 * 1024


def resolve_in(workspace: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``workspace`` or raise PermissionError."""
    ws = Path(workspace).resolve()
    candidate = (ws / rel).resolve()
    if not candidate.is_relative_to(ws):
        raise PermissionError(f"path escapes workspace: {rel}")
    return candidate


def _clip(text: str) -> tuple[str, bool]:
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + "\n[truncated]", True
    return text, False


async def run_process(cmd: str, cwd: Path, timeout: float) -> ToolResult:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(ok=False, error=f"timeout after {timeout}s")

    stdout, t1 = _clip(out.decode("utf-8", "replace"))
    stderr, t2 = _clip(err.decode("utf-8", "replace"))
    code = proc.returncode or 0
    return ToolResult(
        ok=code == 0,
        stdout=stdout,
        stderr=stderr,
        exit_code=code,
        error=None if code == 0 else f"exit code {code}",
        truncated=t1 or t2,
    )


class EchoTool:
    name = "echo"
    description = "Echo text back. Useful for connectivity checks."
    action_class = "fs_read"
    timeout_s = 5.0
    params_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, stdout=str(args.get("text", "")))


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace."
    action_class = "fs_read"
    timeout_s = 10.0
    params_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "path relative to workspace"}},
        "required": ["path"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        if not isinstance(rel, str) or not rel:
            return ToolResult(ok=False, error="`path` must be a non-empty string")
        try:
            p = resolve_in(ctx.workspace, rel)
        except PermissionError as e:
            return ToolResult(ok=False, error=str(e))
        if not p.is_file():
            return ToolResult(ok=False, error=f"no such file: {rel}")
        text, truncated = _clip(p.read_text(encoding="utf-8", errors="replace"))
        return ToolResult(ok=True, stdout=text, truncated=truncated)


class WriteFileTool:
    name = "write_file"
    description = "Create or overwrite a UTF-8 text file inside the workspace."
    action_class = "fs_write"
    timeout_s = 10.0
    params_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        rel, content = args.get("path"), args.get("content")
        if not isinstance(rel, str) or not rel:
            return ToolResult(ok=False, error="`path` must be a non-empty string")
        if not isinstance(content, str):
            return ToolResult(ok=False, error="`content` must be a string")
        try:
            p = resolve_in(ctx.workspace, rel)
        except PermissionError as e:
            return ToolResult(ok=False, error=str(e))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, stdout=f"wrote {len(content)} bytes", artifacts=[rel])


class DeleteFileTool:
    name = "delete_file"
    description = "Delete a file inside the workspace. Requires human approval."
    action_class = "fs_delete"
    timeout_s = 10.0
    params_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        if not isinstance(rel, str) or not rel:
            return ToolResult(ok=False, error="`path` must be a non-empty string")
        try:
            p = resolve_in(ctx.workspace, rel)
        except PermissionError as e:
            return ToolResult(ok=False, error=str(e))
        if not p.is_file():
            return ToolResult(ok=False, error=f"no such file: {rel}")
        p.unlink()
        return ToolResult(ok=True, stdout=f"deleted {rel}")


class ShellTool:
    name = "shell"
    description = "Run a shell command in the workspace. Requires human approval."
    action_class = "shell"
    timeout_s = 120.0
    params_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "number", "default": 60},
        },
        "required": ["command"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(ok=False, error="`command` must be a non-empty string")
        try:
            timeout = float(args.get("timeout_s", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        return await run_process(command, ctx.workspace, timeout)


class RunPythonTool:
    name = "run_python"
    description = "Run a Python file in the workspace and return its output."
    action_class = "shell"
    timeout_s = 120.0
    params_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "args": {"type": "string", "default": ""}},
        "required": ["path"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        if not isinstance(rel, str) or not rel:
            return ToolResult(ok=False, error="`path` must be a non-empty string")
        try:
            p = resolve_in(ctx.workspace, rel)
        except PermissionError as e:
            return ToolResult(ok=False, error=str(e))
        if not p.is_file():
            return ToolResult(ok=False, error=f"no such file: {rel}")
        extra = str(args.get("args", ""))
        cmd = f"python3 {shlex.quote(str(p))} {extra}".strip()
        return await run_process(cmd, ctx.workspace, self.timeout_s)


class AskHumanTool:
    """Marker tool: intercepted by the runner and routed to ``HumanIO``.

    It exists so the agent can negotiate ambiguity (a course requirement) rather
    than guessing, and so the UI can render the question as a chat card.
    """

    name = "ask_human"
    description = (
        "Ask the human a clarifying question and wait for their answer. Use this "
        "when the goal is genuinely ambiguous and guessing would waste work."
    )
    action_class = "fs_read"
    timeout_s = 1.0
    params_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "one specific question"},
            "why": {"type": "string", "description": "why you cannot proceed without it"},
        },
        "required": ["question"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=False, error="ask_human is handled by the orchestrator")


class SubmitPlanTool:
    """Marker tool: the runner intercepts it and never dispatches it.

    It exists so the planner's output is schema-constrained by the model itself
    rather than by prompt wording we would have to parse.
    """

    name = "submit_plan"
    description = (
        "Submit a decomposition plan. Call this exactly once when the task must be "
        "split across sub-agents. Do not call it for tasks you can finish yourself."
    )
    action_class = "fs_read"
    timeout_s = 1.0
    params_schema = flatten_schema(Plan.model_json_schema())

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=False, error="submit_plan is handled by the orchestrator")


def default_tools() -> list:
    return [
        EchoTool(),
        ReadFileTool(),
        WriteFileTool(),
        DeleteFileTool(),
        ShellTool(),
        RunPythonTool(),
        AskHumanTool(),
        SubmitPlanTool(),
    ]
