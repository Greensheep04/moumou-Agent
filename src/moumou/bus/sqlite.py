"""SQLite-backed ``Channel`` implementation.

Why SQLite: ``publish`` must persist *before* it delivers, and ``history`` must
survive a process restart. SQLite gives us an append-only table with a monotonic
``seq`` primary key, so ascending time order is an ``ORDER BY seq`` and exact
insertion order is preserved even when two messages share the same wall-clock
timestamp.

Concurrency: the whole framework runs in a single asyncio event loop, so the
connection is never used from two OS threads at once. Writes are autocommitted
one ``INSERT`` at a time, which keeps a crash from losing a message.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import tempfile
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

from moumou.contracts import Message, Scope

logger = logging.getLogger(__name__)

#: A slow subscriber is dropped after this many seconds and never gets to stall
#: the rest of the bus. The framework's own callbacks are trivial (append to a
#: list), so this only bites a genuinely stuck consumer.
DEFAULT_CALLBACK_TIMEOUT_S = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    ts              TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    task_id         TEXT,
    sender          TEXT NOT NULL,
    recipients      TEXT NOT NULL DEFAULT '[]',
    kind            TEXT NOT NULL DEFAULT 'chat',
    content         TEXT NOT NULL DEFAULT '',
    meta            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_project_conv ON messages(project_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
"""


def _now_iso(msg: Message) -> str:
    return msg.ts.isoformat()


class SqliteBus:
    """A durable pub-sub message bus satisfying the ``Channel`` protocol."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        callback_timeout_s: float = DEFAULT_CALLBACK_TIMEOUT_S,
    ):
        if db_path is None:
            db_path = Path(tempfile.mkdtemp(prefix="moumou-bus-")) / "bus.db"
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.callback_timeout_s = callback_timeout_s

        # check_same_thread=False is safe here: asyncio runs on one thread, and
        # the lock below guards the connection against accidental cross-thread
        # misuse. isolation_level=None keeps every INSERT autocommitted so a
        # crash or close never loses a message.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

        self._subs: dict[str, list[Callable[[Message], Awaitable[None] | None]]] = {}

    # -- Channel: publish -------------------------------------------------
    async def publish(self, msg: Message) -> None:
        # 铁律 1：先落盘，再投递。
        self._persist(msg)

        targets = msg.recipients if msg.recipients else list(self._subs)
        if not targets:
            return

        # 铁律 2：每个回调放进独立 task，超时丢弃，绝不拖死总线。
        tasks = [
            asyncio.create_task(self._deliver(cb, msg))
            for agent_id in targets
            for cb in self._subs.get(agent_id, [])
        ]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- Channel: subscribe ----------------------------------------------
    def subscribe(
        self, agent_id: str, cb: Callable[[Message], Awaitable[None] | None]
    ) -> None:
        self._subs.setdefault(agent_id, []).append(cb)

    # -- Channel: history -------------------------------------------------
    def history(self, scope: Scope, limit: int = 50) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE project_id = ? AND conversation_id = ? "
                "ORDER BY seq ASC",
                (scope.project_id, scope.conversation_id),
            ).fetchall()

        out = [_to_message(r) for r in rows]
        if scope.members:
            members = set(scope.members)
            out = [
                m
                for m in out
                if m.sender in members or not m.recipients or members & set(m.recipients)
            ]
        return out[-limit:]

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SqliteBus:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals --------------------------------------------------------
    def _persist(self, msg: Message) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages "
                "(id, ts, project_id, conversation_id, task_id, sender, recipients, "
                " kind, content, meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.id,
                    _now_iso(msg),
                    msg.project_id,
                    msg.conversation_id,
                    msg.task_id,
                    msg.sender,
                    json.dumps(msg.recipients),
                    msg.kind,
                    msg.content,
                    json.dumps(msg.meta),
                ),
            )

    async def _deliver(
        self, cb: Callable[[Message], Awaitable[None] | None], msg: Message
    ) -> None:
        try:
            result = cb(msg)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.callback_timeout_s)
        except TimeoutError:
            logger.warning("slow subscriber dropped for message %s", msg.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad subscriber must not break the bus
            logger.warning("subscriber error for message %s: %s", msg.id, exc)


def _to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        ts=row["ts"],
        project_id=row["project_id"],
        conversation_id=row["conversation_id"],
        task_id=row["task_id"],
        sender=row["sender"],
        recipients=json.loads(row["recipients"]),
        kind=row["kind"],
        content=row["content"],
        meta=json.loads(row["meta"]),
    )
