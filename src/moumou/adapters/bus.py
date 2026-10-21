"""In-memory message bus — a reference implementation of the ``Channel`` port.

P2 owns the production one (persistent, with a CLI attached). This one exists so
the framework can run, and so ``publish`` semantics are pinned down by an
executable example.
"""

from __future__ import annotations

import inspect
from collections import deque
from collections.abc import Awaitable, Callable

from moumou.contracts import Message, Scope


class InMemoryBus:
    def __init__(self, maxlen: int = 5000):
        self._log: deque[Message] = deque(maxlen=maxlen)
        self._subs: dict[str, list[Callable[[Message], Awaitable[None] | None]]] = {}
        self._pending: dict[str, list[Message]] = {}

    async def publish(self, msg: Message) -> None:
        self._log.append(msg)
        targets = msg.recipients or list(self._subs)
        for agent_id in targets:
            for cb in self._subs.get(agent_id, []):
                result = cb(msg)
                if inspect.isawaitable(result):
                    await result

    def subscribe(
        self, agent_id: str, cb: Callable[[Message], Awaitable[None] | None]
    ) -> None:
        self._subs.setdefault(agent_id, []).append(cb)

    def history(self, scope: Scope, limit: int = 50) -> list[Message]:
        out = [m for m in self._log if m.conversation_id == scope.conversation_id]
        if scope.members:
            members = set(scope.members)
            out = [
                m
                for m in out
                if m.sender in members or not m.recipients or members & set(m.recipients)
            ]
        return out[-limit:]

    # convenience for the orchestrator; not part of the port
    def drain(self, agent_id: str) -> list[Message]:
        out = self._pending.pop(agent_id, [])
        return out

    def remember_steering(self, agent_id: str, msg: Message) -> None:
        self._pending.setdefault(agent_id, []).append(msg)
