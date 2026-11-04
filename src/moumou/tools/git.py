"""Structured local Git operations; remote writes use a separate approval class."""

import asyncio
import re

from moumou.contracts import ToolResult
from moumou.sandbox.local import LocalJail
from moumou.sandbox.process import run_command
from moumou.tools.execution import snapshot
from moumou.workspace.git import git_args


def ref_name(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("invalid branch or remote name")
    return value


class GitTool:
    name = "git"
    description = "Run structured local Git status/diff/add/commit/log/branch/merge operations."
    action_class = "git_commit"
    timeout_s = 40.0
    params_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["status", "diff", "add", "commit", "log", "branch", "merge"],
            },
            "paths": {"type": "array", "items": {"type": "string"}},
            "message": {"type": "string"},
            "name": {"type": "string"},
            "branch": {"type": "string"},
        },
        "required": ["operation"],
    }

    async def run(self, args, ctx):
        try:
            jail = LocalJail(ctx.workspace)
            op = args.get("operation")
            if op == "status":
                cmd = ["status", "--short"]
            elif op == "diff":
                cmd = ["diff", "--no-ext-diff", "--no-textconv"]
            elif op == "log":
                cmd = ["log", "--oneline", "-20"]
            elif op == "add":
                paths = args.get("paths")
                if not isinstance(paths, list) or not paths:
                    raise ValueError("paths must be a non-empty list")
                paths = [jail.relative(jail.resolve(p, mutable=True)) for p in paths]
                cmd = ["--literal-pathspecs", "add", "--", *paths]
            elif op == "commit":
                message = args.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("message must be non-empty text")
                cmd = ["commit", "--no-gpg-sign", "-m", message]
            elif op == "branch":
                cmd = ["branch", "--", ref_name(args["name"])] if "name" in args else ["branch"]
            elif op == "merge":
                cmd = ["merge", "--no-edit", "--no-gpg-sign", ref_name(args.get("branch"))]
            else:
                raise ValueError("unsupported Git operation")
            before = await asyncio.to_thread(snapshot, jail) if op == "merge" else {}
            result = await run_command(
                git_args(jail.root, *cmd), jail.root, 30, ctx.env, strip_git_env=True
            )
            if op == "merge":
                after = await asyncio.to_thread(snapshot, jail)
                result.artifacts = sorted(
                    k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
                )
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc)[:1024])


class GitPushTool:
    name = "git_push"
    description = "Push a named local branch to a configured remote; requires human approval."
    action_class = "git_push"
    timeout_s = 70.0
    params_schema = {
        "type": "object",
        "properties": {
            "remote": {"type": "string", "default": "origin"},
            "branch": {"type": "string"},
        },
        "required": ["branch"],
    }

    async def run(self, args, ctx):
        try:
            jail = LocalJail(ctx.workspace)
            remote = ref_name(args.get("remote", "origin"))
            branch = ref_name(args.get("branch"))
            result = await run_command(
                git_args(
                    jail.root,
                    "push",
                    "--porcelain",
                    "--",
                    remote,
                    f"refs/heads/{branch}:refs/heads/{branch}",
                ),
                jail.root,
                60,
                {**ctx.env, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
                strip_git_env=True,
            )
            for field in ("stdout", "stderr", "error"):
                text = getattr(result, field)
                if text:
                    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", text)
                    setattr(result, field, text)
            return result
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc)[:1024])
