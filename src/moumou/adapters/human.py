"""Reference ``HumanIO`` implementations.

Nothing here touches a terminal. ``CliHuman`` (P2) owns the interactive one and
``YamlHuman`` (P3) owns the fixture-driven one; these two just make the
framework testable.
"""

from __future__ import annotations

import inspect

from moumou.contracts import HumanRequest, HumanResponse, Message


class AutoApproveHuman:
    """Approves everything. Fine for development, obviously wrong for a demo."""

    def __init__(self, decision: str = "approve", answer: str = "(auto-answer)"):
        self.decision = decision
        self.answer = answer
        self.requests: list[HumanRequest] = []
        self.notifications: list[Message] = []

    async def ask(self, req: HumanRequest) -> HumanResponse:
        self.requests.append(req)
        if req.kind == "question":
            return HumanResponse(request_id=req.id, text=self.answer)
        return HumanResponse(request_id=req.id, decision=self.decision, text="auto")

    async def notify(self, msg: Message) -> None:
        self.notifications.append(msg)


class ScriptedHuman:
    """Answers per action class (or per kind), with an optional callback.

    ``decisions`` maps an ``action_class`` to "approve" / "reject".
    ``answers`` maps a substring of the question to a canned reply.
    """

    def __init__(
        self,
        decisions: dict[str, str] | None = None,
        default: str = "approve",
        answers: dict[str, str] | None = None,
        default_answer: str = "(scripted answer)",
        on_ask=None,
    ):
        self.decisions = decisions or {}
        self.default = default
        self.answers = answers or {}
        self.default_answer = default_answer
        self.on_ask = on_ask
        self.requests: list[HumanRequest] = []

    async def ask(self, req: HumanRequest) -> HumanResponse:
        self.requests.append(req)

        if self.on_ask is not None:
            result = self.on_ask(req)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, HumanResponse):
                return result
            if result in ("approve", "reject"):
                return HumanResponse(request_id=req.id, decision=result)

        if req.kind == "question":
            for needle, answer in self.answers.items():
                if needle in req.summary:
                    return HumanResponse(request_id=req.id, text=answer)
            return HumanResponse(request_id=req.id, text=self.default_answer)

        decision = self.decisions.get(req.action_class, self.default)
        return HumanResponse(
            request_id=req.id, decision=decision, text=f"scripted:{req.action_class}"
        )

    async def notify(self, msg: Message) -> None:
        pass
