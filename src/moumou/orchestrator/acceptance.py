"""Executable acceptance checks.

This is what makes "Task Completion Rate" a number instead of an opinion. An
empty check list falls back to self-report, and that fallback is recorded so the
report can say how often it happened.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from moumou.contracts import Acceptance, TaskNode, ToolResult
from moumou.sandbox.workspace import requests_package_install, run_workspace_command
from moumou.tools.builtin import python_command, resolve_in, run_process


def validate_check(item: Acceptance) -> None:
    if item.kind in {"command", "pytest"}:
        try:
            int("0" if item.expect is None else item.expect)
        except ValueError as exc:
            raise ValueError(f"{item.kind} expect must be an integer process exit code") from exc
    elif item.kind == "file_contains":
        try:
            re.compile(item.expect or "")
        except (re.error, OverflowError) as exc:
            raise ValueError(f"file_contains expect is an invalid regex: {exc}") from exc


def command_entrypoint(command: str) -> str | None:
    """识别常用的单条脚本启动命令。"""
    if any(char in command for char in "\r\n;&|<>()$`\\%*?[]"):
        return None
    try:
        args = shlex.split(command, comments=True)
    except ValueError:
        return None
    if not args:
        return None
    if args[0].startswith("./"):
        return args[0]
    interpreter = Path(args[0]).name.casefold()
    python = re.fullmatch(r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?", interpreter)
    if not python and interpreter not in {"sh", "bash", "zsh", "dash"}:
        return None
    if len(args) > 1 and not args[1].startswith("-"):
        return args[1]
    return None


def process_details(result, expected: int, *, pytest_check: bool = False) -> dict:
    details = {"expected_exit_code": expected, "exit_code": result.exit_code,
               "ok": result.exit_code == expected,
               "stdout": result.stdout[-1500:], "stderr": result.stderr[-1500:]}
    if not details["ok"]:
        code = result.exit_code
        if (result.error or "").startswith("timeout after "):
            kind = "execution_timeout"
        elif code is None or code < 0 or code in {126, 127}:
            kind = "execution_error"
        elif pytest_check and code in {2, 3, 4, 5}:
            kind = "pytest_execution_error"
        else:
            kind = "check_failed"
        details["failure_kind"] = kind
        if result.error:
            details["error"] = result.error
    return details


class AcceptanceChecker:
    def __init__(self, trace=None, subtree_verdict=None, *, execution_root=None):
        self.trace = trace
        #: ``callable(root_task_id) -> (ok, detail)`` supplied by the engine; the
        #: checker deliberately has no repository handle of its own.
        self.subtree_verdict = subtree_verdict
        self.execution_root = execution_root

    async def check(
        self, node: TaskNode, workspace: Path, scan_root: Path | None = None
    ) -> tuple[bool, list[dict]]:
        """Run the declared checks.

        ``workspace`` is the directory they run in (an isolated acceptance root
        when one was prepared). ``scan_root`` is the real working tree, used by
        ``file_not_exists`` so a file that merely was not copied into the
        isolated root cannot be mistaken for an absent file.
        """
        if not node.acceptance:
            return True, [
                {"kind": "self_report", "ok": True, "note": "no machine checks declared"}
            ]

        results = [await self._one(node, item, workspace, scan_root)
                   for item in node.acceptance]
        return all(r["ok"] for r in results), results

    async def _one(
        self, node: TaskNode, item: Acceptance, workspace: Path,
        scan_root: Path | None = None,
    ) -> dict:
        name = f"{item.kind}:{item.target[:60]}"
        if self.trace is not None:
            with self.trace.span(task_id=node.id, kind="acceptance", name=name) as span:
                out = await self._evaluate(item, workspace, node, scan_root)
                span.payload.update(out)
            return out
        return await self._evaluate(item, workspace, node, scan_root)

    async def _evaluate(
        self, item: Acceptance, workspace: Path, node=None, scan_root: Path | None = None
    ) -> dict:
        base = {"kind": item.kind, "target": item.target}
        if item.kind in {"file_contains", "json_key"}:
            base["expect"] = item.expect
        try:
            if item.kind == "command":
                return await self._command(item, workspace, base)
            if item.kind == "pytest":
                return await self._pytest(item, workspace, base)
            if item.kind == "subtree_accepted":
                if self.subtree_verdict is None:
                    return {**base, "ok": False, "error": "no subtree evidence available"}
                ok, detail = self.subtree_verdict(node.id)
                return {**base, "ok": bool(ok), **detail}

            path = resolve_in(workspace, item.target)

            if item.kind == "file_exists":
                return {**base, "ok": path.exists()}
            if item.kind == "file_not_exists":
                # A file that exists but was not copied into the acceptance root
                # must still count as existing.
                present = path.exists()
                if not present and scan_root is not None:
                    try:
                        present = resolve_in(scan_root, item.target).exists()
                    except (ValueError, OSError):
                        present = False
                return {**base, "ok": not present}
            if item.kind == "file_contains":
                if not path.exists():
                    return {**base, "ok": False, "error": "file missing"}
                if path.is_dir():
                    # A directory is a common mistake for this check; say so
                    # instead of claiming the path is missing, which sends the
                    # agent looking for a file that was never meant to exist.
                    return {
                        **base, "ok": False,
                        "error": "target is a directory; file_contains needs a file path "
                                 "(e.g. tests/test_app.py), or use file_exists for a "
                                 "directory",
                    }
                if not path.is_file():
                    return {**base, "ok": False, "error": "target is not a regular file"}
                text = path.read_text(encoding="utf-8", errors="replace")
                return {**base, "ok": bool(re.search(item.expect or "", text))}
            if item.kind == "json_key":
                return self._json_key(item, path, base)

            return {**base, "ok": False, "error": f"unknown kind {item.kind}"}

        except PermissionError as exc:
            return {**base, "ok": False, "failure_kind": "execution_error", "error": str(exc)}
        except Exception as exc:                     # never let a check crash the run
            return {**base, "ok": False, "failure_kind": "execution_error",
                    "error": f"{type(exc).__name__}: {exc}"}

    # -- individual kinds ------------------------------------------------
    async def _run(self, command, workspace, timeout):
        if self.execution_root is None:
            return await run_process(command, workspace, timeout)
        if requests_package_install(command):
            return ToolResult(
                ok=False, error="Install dependencies through a separately approved tool call.",
            )
        return await run_workspace_command(command, workspace, timeout, self.execution_root)

    async def _command(self, item: Acceptance, workspace: Path, base: dict) -> dict:
        want = int(item.expect or "0")
        result = await self._run(item.target, workspace, item.timeout_s)
        return {**process_details(result, want), **base}

    async def _pytest(self, item: Acceptance, workspace: Path, base: dict) -> dict:
        """Run pytest and keep the summary line, which is the useful part."""
        target = item.target.strip() or "."
        want = int(item.expect or "0")
        result = await self._run(
            python_command("-m", "pytest", "-q", target), workspace, item.timeout_s
        )
        tail = (result.stdout or "").strip().splitlines()
        return {**process_details(result, want, pytest_check=True), **base,
                "summary": tail[-1] if tail else ""}

    @staticmethod
    def _json_key(item: Acceptance, path: Path, base: dict) -> dict:
        """``expect`` is ``dotted.path`` or ``dotted.path=<json literal>``."""
        if not path.is_file():
            return {**base, "ok": False, "error": "file missing"}

        data = json.loads(path.read_text(encoding="utf-8"))
        expr = item.expect or ""
        key, sep, raw = expr.partition("=")
        node = data
        for part in filter(None, key.split(".")):
            if isinstance(node, list):
                node = node[int(part)]
            elif isinstance(node, dict):
                if part not in node:
                    return {**base, "ok": False, "error": f"missing key: {part}"}
                node = node[part]
            else:
                return {**base, "ok": False, "error": f"cannot descend into {part!r}"}

        if not sep:
            return {**base, "ok": True, "value": node}

        try:                              # `n=3` compares as int, `s=abc` as str
            want = json.loads(raw)
        except json.JSONDecodeError:
            want = raw
        return {**base, "ok": node == want, "value": node}
