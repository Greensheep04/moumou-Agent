"""SQLite persistence: task state, the audit event log, and role storage.

The invariant this module protects: **a state change and its audit event are
written in the same transaction**. Split them and the timeline in your report
will eventually contradict the task table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from moumou.contracts import (
    MAX_WAKEUPS,
    Budget,
    RoleSpec,
    TaskNode,
    TaskResult,
    TaskStatus,
    can_transition,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    parent_id       TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    goal            TEXT NOT NULL,
    role            TEXT NOT NULL,
    deps            TEXT NOT NULL DEFAULT '[]',
    acceptance      TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL,
    retries         INTEGER NOT NULL DEFAULT 0,
    budget          TEXT NOT NULL,
    artifacts       TEXT NOT NULL DEFAULT '[]',
    branch          TEXT,
    error           TEXT,
    result          TEXT,
    wakeups         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    reason      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);

CREATE TABLE IF NOT EXISTS conclusions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conclusions_scope ON conclusions(scope, seq);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    project_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    tools       TEXT NOT NULL DEFAULT '[]',
    created_by  TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT,
    PRIMARY KEY (project_id, name)
);

CREATE TABLE IF NOT EXISTS role_revisions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    previous    TEXT NOT NULL,
    proposed    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    decision    TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);
"""


class IllegalTransition(RuntimeError):
    """Raised when code tries a state change the state machine forbids."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        self._ensure_column("tasks", "wakeups", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("roles", "version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("roles", "updated_at", "TEXT")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        have = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # -- small key/value store (e.g. the token-estimate correction) -------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def _to_node(row: sqlite3.Row) -> TaskNode:
    return TaskNode(
        id=row["id"],
        project_id=row["project_id"],
        conversation_id=row["conversation_id"],
        parent_id=row["parent_id"],
        depth=row["depth"],
        goal=row["goal"],
        role=row["role"],
        deps=json.loads(row["deps"]),
        acceptance=json.loads(row["acceptance"]),
        status=row["status"],
        retries=row["retries"],
        budget=Budget.model_validate_json(row["budget"]),
        artifacts=json.loads(row["artifacts"]),
        branch=row["branch"],
        error=row["error"],
        result=_parse_result(row["result"]),
        wakeups=row["wakeups"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _parse_result(raw: str | None) -> TaskResult | None:
    if not raw:
        return None
    try:
        return TaskResult.model_validate_json(raw)
    except Exception:
        return TaskResult(summary=raw)      # rows written before the structured hand-off


class SqliteTaskRepo:
    def __init__(self, db: Database):
        self.db = db

    # -- reads -----------------------------------------------------------
    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.db.conn.execute(sql, params).fetchall()

    def get(self, task_id: str) -> TaskNode | None:
        rows = self._rows("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return _to_node(rows[0]) if rows else None

    def children(self, task_id: str) -> list[TaskNode]:
        rows = self._rows("SELECT * FROM tasks WHERE parent_id = ? ORDER BY rowid", (task_id,))
        return [_to_node(r) for r in rows]

    def by_status(self, status: TaskStatus) -> list[TaskNode]:
        rows = self._rows("SELECT * FROM tasks WHERE status = ? ORDER BY rowid", (status,))
        return [_to_node(r) for r in rows]

    def ready(self, limit: int = 1) -> list[TaskNode]:
        rows = self._rows(
            "SELECT * FROM tasks WHERE status = 'READY' ORDER BY rowid LIMIT ?", (limit,)
        )
        return [_to_node(r) for r in rows]

    def all_tasks(self) -> list[TaskNode]:
        return [_to_node(r) for r in self._rows("SELECT * FROM tasks ORDER BY rowid")]

    def event_log(self, task_id: str) -> list[dict]:
        rows = self._rows("SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,))
        return [dict(r) for r in rows]

    def count_root_tasks(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM tasks WHERE parent_id IS NULL")[0]["n"])

    # -- writes ----------------------------------------------------------
    def add(self, node: TaskNode) -> None:
        self.db.conn.execute(
            """INSERT INTO tasks (id, project_id, conversation_id, parent_id, depth, goal, role,
                                   deps, acceptance, status, retries, budget, artifacts, branch,
                                   error, result, wakeups, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                node.id,
                node.project_id,
                node.conversation_id,
                node.parent_id,
                node.depth,
                node.goal,
                node.role,
                json.dumps(node.deps),
                json.dumps([a.model_dump() for a in node.acceptance]),
                node.status,
                node.retries,
                node.budget.model_dump_json(),
                json.dumps(node.artifacts),
                node.branch,
                node.error,
                node.result.model_dump_json() if node.result else None,
                node.wakeups,
                node.created_at.isoformat(),
                node.updated_at.isoformat(),
            ),
        )

    def transition(self, task_id: str, to: TaskStatus, reason: str) -> None:
        cur = self.db.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such task: {task_id}")
            src = row["status"]
            if not can_transition(src, to):
                raise IllegalTransition(f"{task_id}: {src} -> {to} ({reason})")

            now = _now()
            cur.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (to, now, task_id)
            )
            cur.execute(
                "INSERT INTO events (task_id, ts, from_status, to_status, reason) "
                "VALUES (?,?,?,?,?)",
                (task_id, now, src, to, reason),
            )
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    def set_deps(self, task_id: str, deps: list[str]) -> None:
        self.db.conn.execute(
            "UPDATE tasks SET deps = ?, updated_at = ? WHERE id = ?",
            (json.dumps(deps), _now(), task_id),
        )

    def set_result(self, task_id: str, result: TaskResult) -> None:
        self.db.conn.execute(
            "UPDATE tasks SET result = ?, updated_at = ? WHERE id = ?",
            (result.model_dump_json(), _now(), task_id),
        )

    def bump_wakeups(self, task_id: str) -> int:
        self.db.conn.execute(
            "UPDATE tasks SET wakeups = wakeups + 1 WHERE id = ?", (task_id,)
        )
        node = self.get(task_id)
        return node.wakeups if node else 0

    def add_artifacts(self, task_id: str, artifacts: list[str]) -> None:
        if not artifacts:
            return
        node = self.get(task_id)
        if node is None:
            return
        merged = list(dict.fromkeys([*node.artifacts, *artifacts]))
        self.db.conn.execute(
            "UPDATE tasks SET artifacts = ?, updated_at = ? WHERE id = ?",
            (json.dumps(merged), _now(), task_id),
        )

    def set_error(self, task_id: str, error: str) -> None:
        self.db.conn.execute(
            "UPDATE tasks SET error = ?, updated_at = ? WHERE id = ?", (error, _now(), task_id)
        )

    def bump_retries(self, task_id: str) -> int:
        self.db.conn.execute("UPDATE tasks SET retries = retries + 1 WHERE id = ?", (task_id,))
        node = self.get(task_id)
        return node.retries if node else 0

    # -- the one derived rule --------------------------------------------
    def promote(self) -> int:
        """PENDING -> READY when deps are satisfied; -> DEAD when they never will be."""
        changed = 0
        for node in self.by_status("PENDING"):
            deps = [self.get(d) for d in node.deps]
            if any(d is None for d in deps):
                self.transition(node.id, "DEAD", "missing_dependency")
            elif any(d.status == "DEAD" for d in deps):
                # A dead child wakes the parent so it can re-dispatch, instead of
                # taking the whole branch down with it.
                if node.wakeups < MAX_WAKEUPS:
                    self.bump_wakeups(node.id)
                    self.transition(node.id, "READY", "upstream_failed_review")
                else:
                    self.transition(node.id, "DEAD", "upstream_failed")
            elif all(d.status == "DONE" for d in deps):
                self.transition(node.id, "READY", "deps_satisfied")
            else:
                continue
            changed += 1
        return changed


# -- roles -----------------------------------------------------------------


class RoleLimitExceeded(RuntimeError):
    pass


class SqliteRoleRegistry:
    """Roles are invented on the fly, then reused for the life of the project."""

    def __init__(self, db: Database, max_roles: int = 8):
        self.db = db
        self.max_roles = max_roles

    def get(self, project_id: str, name: str) -> RoleSpec | None:
        row = self.db.conn.execute(
            "SELECT * FROM roles WHERE project_id = ? AND name = ?", (project_id, name)
        ).fetchone()
        return self._to_spec(row) if row else None

    def list(self, project_id: str) -> list[RoleSpec]:
        rows = self.db.conn.execute(
            "SELECT * FROM roles WHERE project_id = ? ORDER BY created_at", (project_id,)
        ).fetchall()
        return [self._to_spec(r) for r in rows]

    def count(self, project_id: str) -> int:
        return int(
            self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM roles WHERE project_id = ?", (project_id,)
            ).fetchone()["n"]
        )

    def create(self, spec: RoleSpec) -> RoleSpec:
        existing = self.get(spec.project_id, spec.name)
        if existing is not None:
            return existing                       # query-before-create, enforced
        if self.count(spec.project_id) >= self.max_roles:
            raise RoleLimitExceeded(
                f"project {spec.project_id!r} already has {self.max_roles} roles; reuse one"
            )
        self.db.conn.execute(
            "INSERT INTO roles (project_id, name, title, description, tools, created_by, "
            "reason, created_at, version, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                spec.project_id,
                spec.name,
                spec.title,
                spec.description,
                json.dumps(spec.tools),
                spec.created_by,
                spec.reason,
                spec.created_at.isoformat(),
                spec.version,
                spec.updated_at.isoformat() if spec.updated_at else None,
            ),
        )
        return spec

    def revise(
        self, project_id: str, name: str, description: str
    ) -> RoleSpec | None:
        """Replace a role's description and bump its version."""
        self.db.conn.execute(
            "UPDATE roles SET description = ?, version = version + 1, updated_at = ? "
            "WHERE project_id = ? AND name = ?",
            (description, datetime.now(UTC).isoformat(), project_id, name),
        )
        return self.get(project_id, name)

    def log_revision(
        self,
        *,
        project_id: str,
        role: str,
        version: int,
        previous: str,
        proposed: str,
        reason: str,
        decision: str,
        note: str | None = None,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO role_revisions (project_id, role, version, previous, proposed, "
            "reason, decision, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                role,
                version,
                previous,
                proposed,
                reason,
                decision,
                note,
                datetime.now(UTC).isoformat(),
            ),
        )

    def revision_log(self, project_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM role_revisions WHERE project_id = ? ORDER BY seq", (project_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _to_spec(row: sqlite3.Row) -> RoleSpec:
        return RoleSpec(
            name=row["name"],
            title=row["title"],
            description=row["description"],
            tools=json.loads(row["tools"]),
            created_by=row["created_by"],
            reason=row["reason"],
            project_id=row["project_id"],
            created_at=row["created_at"],
            version=row["version"],
            updated_at=row["updated_at"],
        )
