"""Messages on the bus.

Humans are ordinary members with the fixed id ``human`` — that single decision
collapses DM, group chat, and approval into one mechanism.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

HUMAN_ID = "human"

MessageKind = Literal[
    "chat",      # ordinary conversation
    "delegate",  # a parent task handing work to a child
    "report",    # a child reporting back
    "ask_human", # the agent wants clarification (HITL, non-blocking question)
    "approval",  # the agent is asking permission for a risky action (HITL)
    "steer",     # a human nudging a running task mid-flight
]


class Message(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    project_id: str
    conversation_id: str
    task_id: str | None = None

    sender: str                                  # agent id, or HUMAN_ID
    recipients: list[str] = Field(default_factory=list)   # empty = everyone in scope
    kind: MessageKind = "chat"
    content: str
    meta: dict = Field(default_factory=dict)


class Scope(BaseModel):
    """Addressing unit for history and subscriptions."""

    kind: Literal["dm", "group"] = "group"
    project_id: str
    conversation_id: str
    members: list[str] = Field(default_factory=list)


@runtime_checkable
class Channel(Protocol):
    """The message bus port.

    Humans are ordinary members with the fixed id ``human``, so direct
    messages, group chat and approvals all travel the same path.
    """

    async def publish(self, msg: Message) -> None: ...

    def subscribe(
        self, agent_id: str, cb: Callable[[Message], Awaitable[None] | None]
    ) -> None: ...

    def history(self, scope: Scope, limit: int = 50) -> list[Message]: ...
