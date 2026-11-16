"""Interactive ``HumanIO`` — a real person parked on an ``asyncio.Future``.

The design question this answers is "how do I wait for a human without blocking
every other agent?" The answer is a future per request:

    * ``ask`` registers a future, then awaits it;
    * the UI (CLI / web) calls ``answer`` with what the human typed;
    * if the surrounding task is cancelled mid-wait, ``ask`` pops its own future
      and re-raises ``CancelledError``, so no dangling wait is left behind.

``notify`` uses a queue so the UI can render notifications as they arrive.
"""

from __future__ import annotations

import asyncio

from moumou.contracts import HUMAN_ID, HumanRequest, HumanResponse, Message


class CliHuman:
    """The human's half of the conversation, backed by futures + queues."""

    def __init__(self, agent_id: str = HUMAN_ID):
        self.agent_id = agent_id
        self._requests: asyncio.Queue[HumanRequest] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[HumanResponse]] = {}
        self._notifications: asyncio.Queue[Message] = asyncio.Queue()

    # -- HumanIO ----------------------------------------------------------
    async def ask(self, req: HumanRequest) -> HumanResponse:
        """Park on a future until the UI answers; cancel cleanly if aborted."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[HumanResponse] = loop.create_future()
        self._pending[req.id] = future
        await self._requests.put(req)
        try:
            return await future
        except asyncio.CancelledError:
            # 铁律 4：被取消时撤回自己的 future，绝不留下永远挂起的等待。
            self._pending.pop(req.id, None)
            raise

    async def notify(self, msg: Message) -> None:
        await self._notifications.put(msg)

    # -- driven by the UI -------------------------------------------------
    def answer(self, request_id: str, response: HumanResponse) -> bool:
        """Resolve a pending request. Returns False if it was already gone."""
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(response)
        return True

    def reject_all(self, text: str = "(cancelled)") -> None:
        """Settle every pending request; used when the UI is shutting down."""
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(HumanResponse(request_id=request_id, decision=None, text=text))
        self._pending.clear()

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    async def next_request(self) -> HumanRequest:
        return await self._requests.get()

    async def next_notification(self) -> Message:
        return await self._notifications.get()

    def poll_request(self) -> HumanRequest | None:
        try:
            return self._requests.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def poll_notification(self) -> Message | None:
        try:
            return self._notifications.get_nowait()
        except asyncio.QueueEmpty:
            return None


class HumanAgent:
    """Frames the human as an ordinary member of the message bus.

    ``id`` is fixed to ``HUMAN_ID`` so DM / group / approval all share one
    addressing path. It owns a ``HumanIO`` and knows how to read and write the
    bus as that member.
    """

    def __init__(self, bus, io: CliHuman | None = None):
        self.bus = bus
        self.io = io or CliHuman()
        self.id = self.io.agent_id

    def subscribe(self, on_message) -> None:
        """Receive every message the human should see (broadcast or targeted)."""
        def _cb(msg: Message) -> None:
            if msg.recipients and self.id not in msg.recipients:
                return
            on_message(msg)

        self.bus.subscribe(self.id, _cb)

    async def say(self, text: str, *, project_id: str, conversation_id: str,
                  recipients: list[str] | None = None) -> Message:
        """Send a chat message as the human."""
        msg = Message(
            project_id=project_id,
            conversation_id=conversation_id,
            sender=self.id,
            recipients=recipients or [],
            kind="chat",
            content=text,
        )
        await self.bus.publish(msg)
        return msg
