"""Attempt-scoped feedback for repeated successful shell and Python requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections import OrderedDict

from moumou.contracts import ToolResult

_EXECUTION_TOOLS = frozenset({"shell", "run_python"})
_MAX_ENTRIES = 512
_MAX_BYTES = 16 * 1024 * 1024
_MAX_COMPLETIONS = 16
_REASON_LIMIT = 500


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


def _file_digest(path, expected):
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    identity = _identity(expected)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if _identity(os.fstat(handle.fileno())) != identity:
            return None
        data = handle.read(expected.st_size + 1)
        if len(data) != expected.st_size or _identity(os.fstat(handle.fileno())) != identity:
            return None
    return hashlib.sha256(data).hexdigest()


def _workspace_identity(workspace):
    """Hash a small regular tree. Let the gate decide when observations are uncertain."""
    pending = [workspace]
    entries = {}
    total_bytes = 0
    try:
        while pending:
            path = pending.pop()
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return None
            digest = None
            if stat.S_ISDIR(info.st_mode):
                for child in path.iterdir():
                    if len(entries) + len(pending) + 1 >= _MAX_ENTRIES:
                        return None
                    pending.append(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total_bytes += info.st_size
                if total_bytes > _MAX_BYTES or (digest := _file_digest(path, info)) is None:
                    return None
            else:
                return None
            entries[path] = (_identity(info), digest)
        if any(_identity(path.lstat()) != identity for path, (identity, _) in entries.items()):
            return None
        return hashlib.sha256(json.dumps(sorted(
            (path.relative_to(workspace).as_posix(), identity, digest)
            for path, (identity, digest) in entries.items()
        )).encode()).digest()
    except OSError:
        return None


class ExecutionProgress:
    def __init__(self, gate, trace):
        self.gate = gate
        self.trace = trace
        self.completed = OrderedDict()

    def describe(self, tool_specs):
        for spec in tool_specs:
            function = spec["function"]
            if function["name"] in _EXECUTION_TOOLS:
                function["parameters"].setdefault("properties", {})["repeat_reason"] = {
                    "type": "string", "maxLength": _REASON_LIMIT,
                    "description": "Explain why new execution is needed despite an unchanged "
                                   "workspace. The request follows the usual approval policy.",
                }

    def reset(self):
        self.completed.clear()

    async def execute(self, call, node, agent, workspace):
        tracked = call.name in _EXECUTION_TOOLS
        key = None
        force_repeat = tracked and "repeat_reason" in call.args
        if force_repeat:
            reason = call.args["repeat_reason"]
            if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_LIMIT:
                return ToolResult(
                    ok=False, error="invalid_execution_repeat_reason",
                    stderr="Provide a nonempty repeat_reason of at most 500 characters.",
                )
        if tracked:
            arguments = {key: value for key, value in call.args.items() if key != "repeat_reason"}
            try:
                encoded = json.dumps(
                    arguments, sort_keys=True, separators=(",", ":"), allow_nan=False,
                )
                key = call.name, hashlib.sha256(encoded.encode()).digest()
            except (TypeError, ValueError):
                pass  # The ordinary tool still validates its own arguments.
        previous = self.completed.pop(key, None)
        if previous is not None and not force_repeat:
            previous_identity, previous_call_id = previous
            if await asyncio.to_thread(_workspace_identity, workspace) == previous_identity:
                self.completed[key] = previous
                self.trace.record(task_id=node.id, agent_id=agent.id, kind="task",
                                  name="execution_repeat_skipped", tool=call.name,
                                  previous_call_id=previous_call_id, skipped_call_id=call.id)
                return ToolResult(
                    ok=False, error="duplicate_execution_no_progress",
                    stderr="This command already completed with the same arguments and unchanged "
                           "workspace. Use the prior result to write, delegate, or deliver the "
                           "next step. For a needed fresh result, provide a repeat_reason.",
                )
        result = await self.gate.execute(call, node, agent, workspace)
        if result.artifacts or call.name in {"git", "git_push"}:
            self.reset()
        elif key is not None and result.ok and result.exit_code == 0 and not result.truncated:
            identity = await asyncio.to_thread(_workspace_identity, workspace)
            if identity is not None:
                self.completed[key] = (identity, call.id)
                if len(self.completed) > _MAX_COMPLETIONS:
                    self.completed.popitem(last=False)
        return result
