"""Execution trace: the append-only record everything else reads.

One JSONL line per span. The file is the single source of truth for the metrics,
the replay view, and the failure timelines in the report.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from moumou.contracts import SpanKind, TraceEvent
from moumou.store import SqliteTraceStore

logger = logging.getLogger(__name__)


class Recorder:
    """Keep JSONL for replay and optionally project its events into SQLite.

    JSONL is written first. The two files do not share an atomic transaction:
    a runtime SQLite failure pauses the projection and queries fall back to
    JSONL. Reopening imports the journal to recover missing database events.
    Invalid or conflicting journal records still require explicit repair.
    """

    def __init__(self, path: Path | str, *, sqlite_path: Path | str | None = None):
        self.path = Path(path)
        if sqlite_path is not None and Path(sqlite_path).resolve() == self.path.resolve():
            raise ValueError("JSONL and SQLite must use different files")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._by_task: dict[str, list[TraceEvent]] = {}
        self._store: SqliteTraceStore | None = None
        self._projection_error: Exception | None = None
        try:
            if sqlite_path is not None:
                self._store = SqliteTraceStore(sqlite_path)
                self._store.import_jsonl(self.path)
        except BaseException:
            self.close()
            raise

    @property
    def projection_error(self) -> Exception | None:
        """The failure that paused SQLite updates; None while healthy or disabled."""
        return self._projection_error

    def _pause_projection(self, exc: Exception) -> None:
        self._projection_error = exc
        logger.warning("SQLite trace projection paused; using JSONL at %s: %s", self.path, exc)

    # -- writing ---------------------------------------------------------
    def append(self, e: TraceEvent) -> None:
        self._fh.write(e.model_dump_json() + "\n")
        self._fh.flush()                       # publish JSONL before updating the index
        self._by_task.setdefault(e.task_id, []).append(e)
        if self._store is not None and self._projection_error is None:
            try:
                self._store.append(e)
            except Exception as exc:
                # Trace indexing must not replace a task exception/cancellation
                # from span()'s finally block. The journal already has the event.
                self._pause_projection(exc)

    @contextmanager
    def span(
        self,
        *,
        task_id: str,
        kind: SpanKind,
        name: str,
        agent_id: str | None = None,
        payload: dict | None = None,
    ) -> Iterator[TraceEvent]:
        """Time a block. The yielded event is mutable so callers can enrich it."""
        ev = TraceEvent(
            task_id=task_id,
            kind=kind,
            name=name,
            agent_id=agent_id,
            payload=payload or {},
        )
        try:
            yield ev
            ev.status = "ok"
        except asyncio.CancelledError:
            ev.status = "cancelled"
            raise
        except Exception as exc:
            ev.status = "error"
            ev.payload["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ev.t1 = datetime.now(UTC)
            self.append(ev)

    def record(self, *, task_id: str, kind: SpanKind, name: str, **payload) -> TraceEvent:
        """A zero-duration marker span (steering received, role created, ...)."""
        now = datetime.now(UTC)
        ev = TraceEvent(task_id=task_id, kind=kind, name=name, t0=now, t1=now, status="ok")
        ev.payload.update(payload)
        self.append(ev)
        return ev

    # -- reading ---------------------------------------------------------
    def query(self, task_id: str) -> list[TraceEvent]:
        if self._store is not None:
            if self._projection_error is None:
                try:
                    return self._store.query(task_id)
                except Exception as exc:
                    self._pause_projection(exc)
            # Always scan the complete journal, not the possibly partial cache
            # of a reopened Recorder. Match the store's start-time ordering.
            def order(event: TraceEvent) -> tuple[datetime, str]:
                start = event.t0
                if start.utcoffset() is None:
                    start = start.replace(tzinfo=UTC)
                return start, event.span_id

            return sorted(self._scan(task_id), key=order)
        if task_id in self._by_task:
            return list(self._by_task[task_id])
        return self._scan(task_id)

    def _scan(self, task_id: str) -> list[TraceEvent]:
        out: list[TraceEvent] = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                ev = TraceEvent.model_validate_json(line)
                if ev.task_id == task_id:
                    out.append(ev)
        self._by_task[task_id] = out
        return out

    def all_events(self) -> list[TraceEvent]:
        out: list[TraceEvent] = []
        with self.path.open(encoding="utf-8-sig") as fh:
            for line in fh:
                if line.strip():
                    out.append(TraceEvent.model_validate_json(line))
        return out

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            if self._store is not None:
                self._store.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
