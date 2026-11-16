"""Execution trace: the append-only record everything else reads.

One JSONL line per span. The file is the single source of truth for the metrics,
the replay view, and the failure timelines in the report.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from moumou.contracts import SpanKind, TraceEvent


class Recorder:
    """Writes spans to JSONL and serves them back for replay and metrics."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._by_task: dict[str, list[TraceEvent]] = {}

    # -- writing ---------------------------------------------------------
    def append(self, e: TraceEvent) -> None:
        self._fh.write(e.model_dump_json() + "\n")
        self._fh.flush()                       # crash-safe: never lose a span
        self._by_task.setdefault(e.task_id, []).append(e)

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
        if task_id in self._by_task:
            return list(self._by_task[task_id])
        return self._scan(task_id)

    def _scan(self, task_id: str) -> list[TraceEvent]:
        out: list[TraceEvent] = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8") as fh:
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
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(TraceEvent.model_validate_json(line))
        return out

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
