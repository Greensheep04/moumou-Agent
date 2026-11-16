"""Executable acceptance checks.

This is what makes "Task Completion Rate" a number instead of an opinion. An
empty check list falls back to self-report, and that fallback is recorded so the
report can say how often it happened.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from moumou.contracts import Acceptance, TaskNode
from moumou.tools.builtin import resolve_in, run_process


class AcceptanceChecker:
    def __init__(self, trace=None):
        self.trace = trace

    async def check(self, node: TaskNode, workspace: Path) -> tuple[bool, list[dict]]:
        if not node.acceptance:
            return True, [
                {"kind": "self_report", "ok": True, "note": "no machine checks declared"}
            ]

        results = [await self._one(node, item, workspace) for item in node.acceptance]
        return all(r["ok"] for r in results), results

    async def _one(self, node: TaskNode, item: Acceptance, workspace: Path) -> dict:
        name = f"{item.kind}:{item.target[:60]}"
        if self.trace is not None:
            with self.trace.span(task_id=node.id, kind="acceptance", name=name) as span:
                out = await self._evaluate(item, workspace)
                span.payload.update(out)
            return out
        return await self._evaluate(item, workspace)

    async def _evaluate(self, item: Acceptance, workspace: Path) -> dict:
        base = {"kind": item.kind, "target": item.target}
        try:
            if item.kind == "command":
                return await self._command(item, workspace, base)
            if item.kind == "pytest":
                return await self._pytest(item, workspace, base)

            path = resolve_in(workspace, item.target)

            if item.kind == "file_exists":
                return {**base, "ok": path.exists()}
            if item.kind == "file_not_exists":
                return {**base, "ok": not path.exists()}
            if item.kind == "file_contains":
                if not path.is_file():
                    return {**base, "ok": False, "error": "file missing"}
                text = path.read_text(encoding="utf-8", errors="replace")
                return {**base, "ok": bool(re.search(item.expect or "", text))}
            if item.kind == "json_key":
                return self._json_key(item, path, base)

            return {**base, "ok": False, "error": f"unknown kind {item.kind}"}

        except PermissionError as exc:
            return {**base, "ok": False, "error": str(exc)}
        except Exception as exc:                     # never let a check crash the run
            return {**base, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- individual kinds ------------------------------------------------
    async def _command(self, item: Acceptance, workspace: Path, base: dict) -> dict:
        result = await run_process(item.target, workspace, item.timeout_s)
        want = int(item.expect or "0")
        return {
            **base,
            "ok": result.exit_code == want,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-1500:],
            "stderr": result.stderr[-1500:],
        }

    async def _pytest(self, item: Acceptance, workspace: Path, base: dict) -> dict:
        """Run pytest and keep the summary line, which is the useful part."""
        target = item.target.strip() or "."
        result = await run_process(f"python3 -m pytest -q {target}", workspace, item.timeout_s)
        want = int(item.expect or "0")
        tail = (result.stdout or "").strip().splitlines()
        return {
            **base,
            "ok": result.exit_code == want,
            "exit_code": result.exit_code,
            "summary": tail[-1] if tail else "",
            "stdout": result.stdout[-1500:],
            "stderr": result.stderr[-1500:],
        }

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
