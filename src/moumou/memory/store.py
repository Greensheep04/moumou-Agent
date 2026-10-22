"""Reference memory store: SQLite plus character-bigram retrieval.

Why not FTS5 trigram? Measured against real Chinese sentences it is too brittle:

    "依赖安装"  -> hit
    "安装依赖"  -> miss   (same words, different order)
    "依赖"      -> miss   (shorter than one trigram)

A memory corpus is small (tens to low hundreds per scope), so scoring with
character-bigram overlap in Python is cheap, order tolerant, and has no index to
keep in sync. That is the trade this file makes.

This is the reference implementation for the ``MemoryStore`` port. P3 owns the
production version and may replace it; the interface is what matters.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from moumou.contracts import MemoryRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    scope        TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    situation    TEXT NOT NULL,
    action       TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    lesson       TEXT NOT NULL,
    score        REAL NOT NULL DEFAULT 1.0,
    uses         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
"""

#: Bound the scan; a scope should never accumulate this many live memories.
MAX_CANDIDATES = 500


def memory_scope(project_id: str, role: str) -> str:
    return f"{project_id}:{role}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def bigrams(text: str) -> set[str]:
    """Character bigrams, ignoring spaces and punctuation."""
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def overlap_score(query: set[str], text: str) -> float:
    if not query:
        return 0.0
    return len(query & bigrams(text)) / len(query)


class SqliteMemoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- writes ----------------------------------------------------------
    def write(self, rec: MemoryRecord) -> str:
        self.conn.execute(
            """INSERT INTO memories (id, scope, project_id, role, task_id, situation, action,
                                     outcome, lesson, score, uses, created_at, last_used_at,
                                     deleted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.id,
                memory_scope(rec.project_id, rec.role),
                rec.project_id,
                rec.role,
                rec.task_id,
                rec.situation,
                rec.action,
                rec.outcome,
                rec.lesson,
                rec.score,
                rec.uses,
                rec.created_at.isoformat(),
                rec.last_used_at.isoformat() if rec.last_used_at else None,
                rec.deleted_at.isoformat() if rec.deleted_at else None,
            ),
        )
        return rec.id

    def update_score(self, rec_id: str, delta: float, used: bool = True) -> None:
        self.conn.execute(
            "UPDATE memories SET score = MAX(0.0, MIN(3.0, score + ?)) WHERE id = ?",
            (delta, rec_id),
        )
        if used:
            self.conn.execute(
                "UPDATE memories SET uses = uses + 1, last_used_at = ? WHERE id = ?",
                (_now(), rec_id),
            )

    def mark_deleted(self, rec_ids: list[str]) -> None:
        now = _now()
        for rec_id in rec_ids:
            self.conn.execute(
                "UPDATE memories SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, rec_id),
            )

    def hard_delete_expired(self, days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM memories WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
        )
        return cursor.rowcount or 0

    # -- reads -----------------------------------------------------------
    def search(self, scope: str, query: str, k: int = 5) -> list[MemoryRecord]:
        query = (query or "").strip()
        if not query or k <= 0:
            return []

        wanted = bigrams(query)
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND deleted_at IS NULL LIMIT ?",
            (scope, MAX_CANDIDATES),
        ).fetchall()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            hit = overlap_score(wanted, f"{row['situation']} {row['lesson']}")
            if hit > 0:
                scored.append((hit, row))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [self._to_record(row) for _, row in scored[:k]]

    def all(self, scope: str) -> list[MemoryRecord]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND deleted_at IS NULL ORDER BY created_at",
            (scope,),
        ).fetchall()
        return [self._to_record(row) for row in rows]

    def evict_candidates(self, scope: str, threshold: float = 0.3) -> list[str]:
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE scope = ? AND deleted_at IS NULL AND score < ?",
            (scope, threshold),
        ).fetchall()
        return [row["id"] for row in rows]

    def count(self, scope: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE scope = ? AND deleted_at IS NULL",
            (scope,),
        ).fetchone()
        return int(row["n"])

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            project_id=row["project_id"],
            role=row["role"],
            task_id=row["task_id"],
            situation=row["situation"],
            action=row["action"],
            outcome=row["outcome"],
            lesson=row["lesson"],
            score=row["score"],
            uses=row["uses"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            deleted_at=row["deleted_at"],
        )

    def dump(self) -> str:
        """Handy when debugging a run."""
        rows = self.conn.execute("SELECT * FROM memories").fetchall()
        return json.dumps(
            [self._to_record(row).model_dump(mode="json") for row in rows], ensure_ascii=False
        )
