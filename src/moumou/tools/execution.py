"""Approved shell and Python execution in the caller's workspace."""

import asyncio
import hashlib
import os
import shlex
import sys
import tempfile
from pathlib import Path

from moumou.contracts import ToolResult
from moumou.sandbox.local import LocalJail
from moumou.sandbox.process import run_command, timeout_value
from moumou.sandbox.workspace import TOOL_TEMP_DIR, run_workspace_command
from moumou.tools.filesystem import _walk


def snapshot(jail):
    result = {}
    for path in _walk(jail, jail.root):
        relative = path.relative_to(jail.root)
        if path.name.startswith(".p1-") or relative.parts[0] == TOOL_TEMP_DIR:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        result[jail.relative(path)] = digest.digest()
    return result


class ShellTool:
    name = "shell"
    description = (
        "Run a shell command in the role workspace. Use approval_scope for package installs "
        "or external writes. Local test servers should bind to 127.0.0.1."
    )
    action_class = "shell"
    timeout_s = 120.0
    params_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 110},
            "approval_scope": {"type": "string", "enum": ["install", "external_write"]},
        },
        "required": ["command"],
    }

    async def run(self, args, ctx):
        try:
            cmd = args.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                raise ValueError("command must be non-empty text")
            return await self.execute(cmd, args, ctx)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc)[:1024])

    async def execute(self, command, args, ctx):
        timeout = timeout_value(args.get("timeout_s", 60))
        if timeout > 110:
            raise ValueError("tool timeout_s must be at most 110")
        jail = LocalJail(ctx.workspace)
        before = await asyncio.to_thread(snapshot, jail)
        if ctx.execution_root is not None:
            result = await run_workspace_command(
                command, jail.root, timeout, ctx.execution_root,
                external_access=ctx.external_access,
            )
            if (not result.ok and not ctx.external_access
                    and ("Operation not permitted" in result.stderr
                         or "Permission denied" in result.stderr)):
                result.error = (
                    f"{result.error}. Workspace execution confines writes to the role workspace "
                    "and network access to this host. For external reads use the source tools. "
                    "For an intended install or external network write, set approval_scope."
                )
        else:
            result = await run_command(command, jail.root, timeout, ctx.env)
        after = await asyncio.to_thread(snapshot, jail)
        result.artifacts = sorted(
            k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
        )
        return result


class RunPythonTool(ShellTool):
    name = "run_python"
    description = (
        "Run workspace Python using the project interpreter. Use approval_scope for package "
        "installs or external network writes. Use tempfile and loopback for local tests."
    )
    params_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "code": {"type": "string"},
            "args": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
            "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 110},
            "approval_scope": {"type": "string", "enum": ["install", "external_write"]},
        },
        "oneOf": [{"required": ["path"]}, {"required": ["code"]}],
    }

    async def run(self, args, ctx):
        temporary = None
        try:
            if not isinstance(args, dict) or ("path" in args) == ("code" in args):
                raise ValueError("provide exactly one of path or code")
            jail = LocalJail(ctx.workspace)
            extra = args.get("args", [])
            if isinstance(extra, str):
                extra = shlex.split(extra, posix=os.name != "nt")
            if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
                raise ValueError("args must be a list of strings")
            if "code" in args:
                if not isinstance(args["code"], str):
                    raise ValueError("code must be text")
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".py",
                    prefix=".p1-",
                    dir=jail.root,
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(args["code"])
                path = temporary
            else:
                path = jail.resolve(args["path"])
                if not path.is_file():
                    raise ValueError("no such Python file")
            return await self.execute([sys.executable, str(path), *extra], args, ctx)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc)[:1024])
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
