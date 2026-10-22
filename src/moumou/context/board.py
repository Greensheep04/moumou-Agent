"""The shared conclusion board.

Two instances of the same role working in the same conversation should not repeat
each other's dead ends. That is what this stores: not lessons for next time, but
a short hand-off note for right now.

Scope is ``project:conversation:role`` -- narrower than long-term memory, which
is ``project:role``.
"""

from __future__ import annotations

import json
import sqlite3

from moumou.contracts import TaskNode

MAX_GOAL_CHARS = 200
MAX_NOTE_CHARS = 300
MAX_FILES = 10


class ConclusionBoard:
    def __init__(self, db, max_per_scope: int = 20):
        self.db = db
        self.max_per_scope = max_per_scope

    @staticmethod
    def scope(project_id: str, conversation_id: str, role: str) -> str:
        return f"{project_id}:{conversation_id}:{role}"

    def write(self, node: TaskNode) -> None:
        """Called once per task, when it reaches a terminal state."""
        payload = {
            "goal": node.goal[:MAX_GOAL_CHARS],
            "artifacts": node.artifacts[:MAX_FILES],
            "outcome": node.status,
            "note": (node.result or node.error or "")[:MAX_NOTE_CHARS],
        }
        scope = self.scope(node.project_id, node.conversation_id, node.role)

        self.db.conn.execute(
            "INSERT INTO conclusions (scope, task_id, role, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (scope, node.id, node.role, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self._prune(scope)

    def recent(self, scope: str, limit: int = 3) -> list[str]:
        if limit <= 0:
            return []
        rows = self.db.conn.execute(
            "SELECT task_id, payload FROM conclusions WHERE scope = ? ORDER BY seq DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [self._render(row["task_id"], json.loads(row["payload"])) for row in reversed(rows)]

    def count(self, scope: str) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM conclusions WHERE scope = ?", (scope,)
        ).fetchone()
        return int(row["n"])

    # -- internals -------------------------------------------------------
    def _prune(self, scope: str) -> None:
        self.db.conn.execute(
            "DELETE FROM conclusions WHERE scope = ? AND seq NOT IN "
            "(SELECT seq FROM conclusions WHERE scope = ? ORDER BY seq DESC LIMIT ?)",
            (scope, scope, self.max_per_scope),
        )

    @staticmethod
    def _render(task_id: str, payload: dict) -> str:
        files = ", ".join(payload.get("artifacts") or []) or "none"
        line = (
            f"{task_id} [{payload.get('outcome', '?')}]: "
            f"{payload.get('goal', '')} | files: {files}"
        )
        note = payload.get("note") or ""
        if note:
            line += f" — {note}"
        return line


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = ["ConclusionBoard", "sqlite3"]
