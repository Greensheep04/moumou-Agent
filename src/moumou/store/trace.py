"""SQLite trace storage and atomic import of the existing JSONL format.

Each row retains a complete serialized TraceEvent. Separate task and timestamp
columns provide ordered queries without discarding payload or other defined
event fields. This module does not create missing parent-span relationships.

A store instance belongs to its creating thread. Independent instances may use
the same database; committed appends are visible to other connections. Use a
dedicated trace database because this implementation owns its user_version.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from moumou.contracts import TraceEvent

_SCHEMA_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
    span_id     TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    t0_us       INTEGER NOT NULL,
    event_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_events_task_time
    ON trace_events(task_id, t0_us, span_id);
"""


class TraceConflictError(ValueError):
    """An existing span_id was reused for different event data."""


class TraceImportError(ValueError):
    """A JSONL record is invalid; all additions from this import are rolled back."""

    def __init__(self, source_path: Path | str, line_number: int, reason: str):
        self.source_path = Path(source_path)
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"{self.source_path}: line {line_number}: {reason}")


@dataclass(frozen=True)
class TraceImportResult:
    """Counts of nonblank records in one successfully committed import."""

    read: int
    inserted: int
    skipped: int


def _timestamp_us(value: datetime) -> int:
    """Normalize the sort key to UTC; interpret naive timestamps as UTC.

    The serialized event keeps its original timestamp representation. Integer
    arithmetic avoids losing microsecond ordering through floating-point epochs.
    """
    if value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class SqliteTraceStore:
    """Append-only TraceStore with deterministic, task-scoped chronological reads.

    Repeating an identical span is a no-op. Reusing its id for different data
    raises TraceConflictError instead of overwriting an earlier trace.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise ValueError(f"unsupported trace schema version: {version}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
            if version == 0:
                self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except BaseException:
            self._conn.close()
            raise

    def append(self, e: TraceEvent) -> None:
        """Commit one event before returning; duplicate identical events are safe."""
        with self._conn:
            self._insert(e)

    def query(self, task_id: str) -> list[TraceEvent]:
        """Return only this task, ordered by UTC start time then span_id."""
        rows = self._conn.execute(
            "SELECT event_json FROM trace_events "
            "WHERE task_id = ? ORDER BY t0_us, span_id",
            (task_id,),
        ).fetchall()
        return [TraceEvent.model_validate_json(row[0]) for row in rows]

    def import_jsonl(self, source_path: Path | str) -> TraceImportResult:
        """Import UTF-8 JSONL atomically, preserving the source file.

        Blank lines and an optional first-line UTF-8 BOM are accepted. Error
        positions refer to physical lines, including blanks. Any invalid record
        or conflicting span rolls back all additions from this import, leaving
        pre-existing events intact.
        """
        source_path = Path(source_path)
        inserted = skipped = 0
        with source_path.open("rb") as source, self._conn:
            for line_number, raw in enumerate(source, start=1):
                try:
                    encoding = "utf-8-sig" if line_number == 1 else "utf-8"
                    line = raw.decode(encoding).strip()
                    if not line:
                        continue
                    event = TraceEvent.model_validate_json(line)
                except (UnicodeDecodeError, ValidationError) as exc:
                    raise TraceImportError(
                        source_path, line_number, "invalid UTF-8 or TraceEvent JSON"
                    ) from exc
                try:
                    added = self._insert(event)
                except TraceConflictError as exc:
                    raise TraceImportError(
                        source_path, line_number, "span_id conflicts with existing event"
                    ) from exc
                if added:
                    inserted += 1
                else:
                    skipped += 1
        return TraceImportResult(read=inserted + skipped, inserted=inserted, skipped=skipped)

    def _insert(self, event: TraceEvent) -> bool:
        """Insert in the caller's transaction without committing it early."""
        event_json = event.model_dump_json()
        cursor = self._conn.execute(
            "INSERT INTO trace_events(span_id, task_id, t0_us, event_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(span_id) DO NOTHING",
            (event.span_id, event.task_id, _timestamp_us(event.t0), event_json),
        )
        if cursor.rowcount == 1:
            return True

        stored_json = self._conn.execute(
            "SELECT event_json FROM trace_events WHERE span_id = ?", (event.span_id,)
        ).fetchone()[0]
        # Compare reconstructed models so JSON key ordering and equivalent
        # timezone-aware instants do not turn a repeated event into a conflict.
        stored = TraceEvent.model_validate_json(stored_json)
        incoming = TraceEvent.model_validate_json(event_json)
        if stored != incoming:
            raise TraceConflictError(f"span_id {event.span_id!r} already has different event data")
        return False

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteTraceStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
