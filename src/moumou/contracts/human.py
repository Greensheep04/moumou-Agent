"""The human port.

One request/response shape covers all three ways a human is involved:

    kind="approval" — may I run this risky tool?      (decision + optional note)
    kind="question" — I am unclear, please clarify    (free text answer)
    kind="review"   — please review this proposal     (decision + optional note)

The same port therefore backs the terminal UI, the chat UI, and the scripted
batch runner used by evaluation. That is what makes HITL testable.
"""

from __future__ import annotations

import uuid
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .message import Message

HumanRequestKind = Literal["approval", "question", "review"]


class HumanRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str
    agent_id: str
    kind: HumanRequestKind = "approval"
    summary: str                                   # one line, shown as the card title
    action: str = ""                               # tool name, for approvals
    action_class: str = ""                         # one of ActionClass
    payload: dict = Field(default_factory=dict)    # full arguments
    options: list[str] = Field(default_factory=list)   # empty => free-text answer
    context: str = ""                              # extra body text for the card


class HumanResponse(BaseModel):
    request_id: str
    decision: Literal["approve", "reject"] | None = None
    text: str | None = None


@runtime_checkable
class HumanIO(Protocol):
    async def ask(self, req: HumanRequest) -> HumanResponse: ...
    async def notify(self, msg: Message) -> None: ...
