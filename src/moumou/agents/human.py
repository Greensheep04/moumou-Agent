"""In-memory HumanIO for the explicit P2 demo and caller-owned controllers.

The formal evaluator retains its durable FileHuman backend. This queue adapter
does not claim process recovery, journal persistence or reusable tool permission.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from moumou.contracts import HUMAN_ID, HumanRequest, HumanResponse, Message


@dataclass
class _Pending:
    request: HumanRequest
    future: asyncio.Future[HumanResponse]


class CliHuman:
    """Bind a validated response to one original request before consuming it."""

    def __init__(
        self, agent_id: str = HUMAN_ID, *,
        project_id: str | None = None, conversation_id: str | None = None,
    ):
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("human agent ID must not be empty")
        if (project_id is None) != (conversation_id is None):
            raise ValueError("bind both project_id and conversation_id, or neither")
        if project_id is not None and (not project_id or not conversation_id):
            raise ValueError("bound project and conversation must not be empty")
        self.agent_id = agent_id
        self.project_id = project_id
        self.conversation_id = conversation_id
        self._requests: asyncio.Queue[str | None] = asyncio.Queue()
        self._notifications: asyncio.Queue[Message | None] = asyncio.Queue()
        self._pending: dict[str, _Pending] = {}
        self._known_ids: set[str] = set()
        self._closed = False

    def _check_scope(self, project_id: str, conversation_id: str) -> None:
        if self.project_id is not None and (project_id, conversation_id) != (
            self.project_id, self.conversation_id,
        ):
            raise ValueError("request/message does not belong to this human conversation")

    async def ask(self, req: HumanRequest) -> HumanResponse:
        if self._closed:
            raise RuntimeError("human port is closed")
        request = HumanRequest.model_validate(req).model_copy(deep=True)
        if not request.id.strip() or not request.summary.strip():
            raise ValueError("request needs its original ID and non-empty summary")
        self._check_scope(request.project_id, request.conversation_id)
        if request.id in self._known_ids:
            raise ValueError("human request ID has already been used")
        future = asyncio.get_running_loop().create_future()
        pending = _Pending(request, future)
        self._known_ids.add(request.id)
        self._pending[request.id] = pending
        self._requests.put_nowait(request.id)
        try:
            return await future
        finally:
            if self._pending.get(request.id) is pending:
                self._pending.pop(request.id, None)

    async def notify(self, msg: Message) -> None:
        if self._closed:
            raise RuntimeError("human port is closed")
        self._check_scope(msg.project_id, msg.conversation_id)
        self._notifications.put_nowait(msg.model_copy(deep=True))

    def answer(self, request_id: str, response: HumanResponse) -> bool:
        """Invalid answers raise without removing a still-answerable request."""
        pending = self._pending.get(request_id)
        if pending is None:
            return False
        if pending.future.done():
            self._pending.pop(request_id, None)
            return False
        parsed = HumanResponse.model_validate(response).model_copy(deep=True)
        request = pending.request
        if parsed.request_id != request.id:
            raise ValueError("response request_id does not match its original request")
        if request.kind == "question":
            if parsed.decision is not None:
                raise ValueError("a question answer cannot contain an approval decision")
            if parsed.text is None or not parsed.text.strip():
                raise ValueError("a non-empty answer to the original question is required")
            if request.options and parsed.text not in request.options:
                raise ValueError("answer is outside the original question options")
        else:
            if parsed.decision not in {"approve", "reject"}:
                raise ValueError("approval/review requires explicit approve or reject")
            if request.options and parsed.decision not in request.options:
                raise ValueError("decision is outside the original request options")
        self._pending.pop(request_id)
        pending.future.set_result(parsed)
        return True

    def cancel_all(self) -> None:
        """Cancel waits; never record shutdown text as a business answer."""
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

    def reject_all(self, text: str = "(cancelled)") -> None:
        """Legacy shutdown alias; text is deliberately not turned into a response."""
        self.cancel_all()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_all()
        self._requests.put_nowait(None)
        self._notifications.put_nowait(None)

    async def aclose(self) -> None:
        """Settle owned futures; the caller still owns/awaits Engine or UI tasks."""
        futures = [pending.future for pending in self._pending.values()]
        self.close()
        if futures:
            await asyncio.gather(*futures, return_exceptions=True)

    @property
    def has_pending(self) -> bool:
        return any(not pending.future.done() for pending in self._pending.values())

    def _request_if_pending(self, request_id: str) -> HumanRequest | None:
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return None
        return pending.request.model_copy(deep=True)

    async def next_request(self) -> HumanRequest:
        while True:
            if self._closed:
                raise RuntimeError("human port is closed")
            request_id = await self._requests.get()
            if request_id is None:
                self._requests.put_nowait(None)
                raise RuntimeError("human port is closed")
            request = self._request_if_pending(request_id)
            if request is not None:
                return request

    async def next_notification(self) -> Message:
        if self._closed:
            raise RuntimeError("human port is closed")
        message = await self._notifications.get()
        if message is None:
            self._notifications.put_nowait(None)
            raise RuntimeError("human port is closed")
        return message

    def poll_request(self) -> HumanRequest | None:
        if self._closed:
            return None
        while True:
            try:
                request_id = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                return None
            if request_id is None:
                return None
            request = self._request_if_pending(request_id)
            if request is not None:
                return request

    def poll_notification(self) -> Message | None:
        if self._closed:
            return None
        try:
            return self._notifications.get_nowait()
        except asyncio.QueueEmpty:
            return None


class HumanAgent:
    """Human member wrapper with an optional explicit conversation binding."""

    def __init__(
        self, bus, io: CliHuman | None = None, *,
        project_id: str | None = None, conversation_id: str | None = None,
    ):
        if (project_id is None) != (conversation_id is None):
            raise ValueError("bind both project_id and conversation_id, or neither")
        if project_id is not None and (not project_id or not conversation_id):
            raise ValueError("bound project and conversation must not be empty")
        self.bus = bus
        self.io = io or CliHuman(project_id=project_id, conversation_id=conversation_id)
        self.id = self.io.agent_id
        self.project_id = project_id if project_id is not None else self.io.project_id
        self.conversation_id = (
            conversation_id if conversation_id is not None else self.io.conversation_id
        )
        if self.io.project_id is not None and self.project_id is not None and (
            self.io.project_id, self.io.conversation_id,
        ) != (self.project_id, self.conversation_id):
            raise ValueError("HumanAgent and CliHuman conversation bindings disagree")

    def _matches(self, msg: Message) -> bool:
        return (self.project_id is None or (msg.project_id, msg.conversation_id) == (
            self.project_id, self.conversation_id,
        ))

    def subscribe(self, on_message) -> None:
        def callback(msg: Message):
            if not self._matches(msg) or (msg.recipients and self.id not in msg.recipients):
                return None
            # Return the awaitable so the bus can await asynchronous consumers.
            return on_message(msg)

        self.bus.subscribe(self.id, callback)

    async def say(
        self, text: str, *, project_id: str, conversation_id: str,
        recipients: list[str] | None = None,
    ) -> Message:
        message = Message(project_id=project_id, conversation_id=conversation_id,
                          sender=self.id, recipients=recipients or [], kind="chat", content=text)
        if not self._matches(message):
            raise ValueError("message does not belong to this human conversation")
        await self.bus.publish(message)
        return message
