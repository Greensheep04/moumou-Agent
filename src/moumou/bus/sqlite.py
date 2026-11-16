"""Persistent Channel with explicit conversation and subscriber scope.

Unbound instances retain the two-argument Channel contract and may store several
conversations. Production callers can bind one instance to a project/conversation
or supply a scope per subscription. A group history with no members is an operator
conversation view, not an authenticated user's private history.

Fast synchronous callbacks stay on the publishing event-loop thread. They must
not block: Python cannot interrupt them safely. A callback that returns too late
is unsubscribed. Known thread-safe blocking callbacks may explicitly opt into
offloading; timed-out threads may finish later and are never claimed to be killed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import sqlite3
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from time import monotonic

from moumou.contracts import Message, Scope

logger = logging.getLogger(__name__)
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
"""


@dataclass(eq=False)
class _Subscription:
    agent_id: str
    callback: Callable[[Message], Awaitable[None] | None]
    scope: Scope | None
    offload_sync: bool


def _validate_scope(scope: Scope) -> None:
    if not scope.project_id or not scope.conversation_id:
        raise ValueError("subscription/history needs a project and conversation")
    if scope.kind == "dm" and len(set(scope.members)) != 2:
        raise ValueError("DM scope needs exactly two distinct members")


def _visible_in_scope(msg: Message, scope: Scope) -> bool:
    if (msg.project_id, msg.conversation_id) != (scope.project_id, scope.conversation_id):
        return False
    members = set(scope.members)
    if scope.kind == "dm":
        recipients = set(msg.recipients)
        return (len(recipients) == 1 and msg.sender not in recipients
                and {msg.sender, *recipients} == members)
    if not members:
        # Existing Channel tests use this operator view even for undelivered DMs.
        return True
    # A member-filtered group view is the union of the supplied participants'
    # visible messages. Pass one viewer ID for a viewer-specific conversation.
    return msg.sender in members or not msg.recipients or bool(members & set(msg.recipients))


def _discard_awaitable(value) -> None:
    if inspect.iscoroutine(value):
        value.close()
    elif isinstance(value, asyncio.Future):
        value.cancel()


class SqliteBus:
    """SQLite-backed Channel; owns its connection and delivery tasks."""

    def __init__(
        self, db_path: Path | str | None = None,
        callback_timeout_s: float = DEFAULT_CALLBACK_TIMEOUT_S, *,
        project_id: str | None = None, conversation_id: str | None = None,
    ):
        if (project_id is None) != (conversation_id is None):
            raise ValueError("bind both project_id and conversation_id, or neither")
        if project_id is not None and (not project_id or not conversation_id):
            raise ValueError("bound project and conversation must not be empty")
        if (isinstance(callback_timeout_s, bool) or not isinstance(callback_timeout_s, (int, float))
                or not math.isfinite(callback_timeout_s) or callback_timeout_s <= 0):
            raise ValueError("callback timeout must be positive and finite")
        if db_path is None:
            db_path = Path(tempfile.mkdtemp(prefix="moumou-bus-")) / "bus.db"
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.project_id = project_id
        self.conversation_id = conversation_id
        self.callback_timeout_s = float(callback_timeout_s)
        self._lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._close_finished = asyncio.Event()
        self._subs: dict[str, list[_Subscription]] = {}
        self._deliveries: set[asyncio.Task] = set()
        self._offloads: set[asyncio.Task] = set()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        except BaseException:
            self._conn.close()
            raise

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("message bus is closed")

    def _check_binding(self, project_id: str, conversation_id: str) -> None:
        if self.project_id is not None and (project_id, conversation_id) != (
            self.project_id, self.conversation_id,
        ):
            raise ValueError("message/scope does not belong to this bound conversation")

    async def publish(self, msg: Message) -> None:
        self._ensure_open()
        # Persist and deliver the same entry-time facts, even if the caller
        # mutates its own Message while awaiting concurrent delivery.
        msg = Message.model_validate(msg).model_copy(deep=True)
        self._check_binding(msg.project_id, msg.conversation_id)
        self._persist(msg)
        targets = list(dict.fromkeys(msg.recipients)) if msg.recipients else list(self._subs)
        tasks = []
        for agent_id in targets:
            for sub in tuple(self._subs.get(agent_id, ())):
                if sub.scope is not None and not _visible_in_scope(msg, sub.scope):
                    continue
                task = asyncio.create_task(self._deliver(sub, msg))
                self._deliveries.add(task)
                task.add_done_callback(self._delivery_finished)
                tasks.append(task)
        if tasks:
            # Preserve the Channel contract: returned deliveries are complete.
            await asyncio.gather(*tasks, return_exceptions=True)

    def subscribe(
        self, agent_id: str, cb: Callable[[Message], Awaitable[None] | None], *,
        scope: Scope | None = None, offload_sync: bool = False,
    ) -> None:
        self._ensure_open()
        if not isinstance(agent_id, str) or not agent_id or not callable(cb):
            raise ValueError("subscription needs an agent ID and callback")
        if type(offload_sync) is not bool:
            raise ValueError("offload_sync must be explicit boolean")
        if scope is not None:
            scope = Scope.model_validate(scope).model_copy(deep=True)
            _validate_scope(scope)
            self._check_binding(scope.project_id, scope.conversation_id)
            if scope.kind == "dm" and agent_id not in scope.members:
                raise ValueError("DM subscriber must belong to its scope")
        elif self.project_id is not None:
            scope = Scope(project_id=self.project_id, conversation_id=self.conversation_id)
        self._subs.setdefault(agent_id, []).append(
            _Subscription(agent_id, cb, scope, offload_sync),
        )

    def unsubscribe(self, agent_id: str, cb=None) -> None:
        """Remove this participant's callback(s); additive to the Channel port."""
        subscriptions = self._subs.get(agent_id, ())
        remaining = [sub for sub in subscriptions if cb is not None and sub.callback is not cb]
        if remaining:
            self._subs[agent_id] = remaining
        else:
            self._subs.pop(agent_id, None)

    def history(self, scope: Scope, limit: int = 50) -> list[Message]:
        self._ensure_open()
        scope = Scope.model_validate(scope)
        _validate_scope(scope)
        self._check_binding(scope.project_id, scope.conversation_id)
        if type(limit) is not int or limit < 0:
            raise ValueError("history limit must be a nonnegative integer")
        if limit == 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE project_id = ? AND conversation_id = ? ORDER BY seq",
                (scope.project_id, scope.conversation_id),
            ).fetchall()
        messages = [_to_message(row) for row in rows]
        messages = [message for message in messages if _visible_in_scope(message, scope)]
        # Compare actual instants, retaining insertion order for equal timestamps.
        # Naive historical timestamps are interpreted as UTC for ordering only.
        messages.sort(key=lambda message: message.ts.replace(tzinfo=UTC)
                      if message.ts.utcoffset() is None else message.ts.astimezone(UTC))
        return messages[-limit:]

    def close(self) -> None:
        """Close after publishers finish; active dispatch requires await aclose()."""
        if self._closed:
            return
        if any(not task.done() for task in self._deliveries):
            raise RuntimeError("active deliveries require await bus.aclose()")
        self._finish_close()

    async def aclose(self) -> None:
        """Drain cooperative deliveries; explicitly offloaded threads may outlive this port."""
        if self._closed:
            return
        if self._closing:
            await self._close_finished.wait()
            return
        self._closing = True
        current = asyncio.current_task()
        pending = [task for task in self._deliveries if task is not current and not task.done()]
        try:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._finish_close()

    def _finish_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._subs.clear()
        try:
            with self._lock:
                self._conn.close()
        finally:
            self._closing = False
            self._close_finished.set()

    def __enter__(self) -> SqliteBus:
        self._ensure_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    async def __aenter__(self) -> SqliteBus:
        self._ensure_open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _persist(self, msg: Message) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages "
                "(id, ts, project_id, conversation_id, task_id, sender, recipients, "
                "kind, content, meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (msg.id, msg.ts.isoformat(), msg.project_id, msg.conversation_id, msg.task_id,
                 msg.sender, json.dumps(msg.recipients), msg.kind, msg.content,
                 json.dumps(msg.meta)),
            )

    def _drop(self, sub: _Subscription) -> None:
        remaining = [item for item in self._subs.get(sub.agent_id, ()) if item is not sub]
        if remaining:
            self._subs[sub.agent_id] = remaining
        else:
            self._subs.pop(sub.agent_id, None)

    def _delivery_finished(self, task: asyncio.Task) -> None:
        self._deliveries.discard(task)
        if not task.cancelled():
            # Observe failures even when a cancelled publisher cannot gather them.
            task.exception()

    async def _deliver(self, sub: _Subscription, msg: Message) -> None:
        started = monotonic()
        try:
            delivered = msg.model_copy(deep=True)
            if sub.offload_sync and not inspect.iscoroutinefunction(sub.callback):
                result = await self._call_offloaded(sub.callback, delivered)
            else:
                result = sub.callback(delivered)
            remaining = self.callback_timeout_s - (monotonic() - started)
            if remaining <= 0:
                _discard_awaitable(result)
                raise TimeoutError
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=remaining)
        except TimeoutError:
            self._drop(sub)
            logger.warning("slow subscriber unsubscribed for message %s", msg.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._drop(sub)
            logger.warning(
                "subscriber unsubscribed after %s for message %s", type(exc).__name__, msg.id,
            )

    async def _call_offloaded(self, callback, message):
        # Shield the worker task so a timeout can still retrieve/dispose a late
        # coroutine result on this loop. Physical thread termination is not safe.
        worker = asyncio.create_task(asyncio.to_thread(callback, message))
        self._offloads.add(worker)
        worker.add_done_callback(self._offloads.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), timeout=self.callback_timeout_s)
        except BaseException:
            worker.add_done_callback(_discard_late_offload)
            raise


def _discard_late_offload(worker: asyncio.Task) -> None:
    if worker.cancelled():
        return
    try:
        result = worker.result()
    except Exception:
        return
    _discard_awaitable(result)


def _to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"], ts=row["ts"], project_id=row["project_id"],
        conversation_id=row["conversation_id"], task_id=row["task_id"], sender=row["sender"],
        recipients=json.loads(row["recipients"]), kind=row["kind"], content=row["content"],
        meta=json.loads(row["meta"]),
    )
