# P2 — Interaction and Channel

[简体中文](P2-交互与通信层.md) · [English](P2-interaction-and-channel.en.md)

> The lightest part of the project, but **half of the demo depends on you**. Everyone else shows an
> architecture diagram; you get to show "the agents actually arguing in a group chat".

| Item | Detail |
| --- | --- |
| Course features covered | 1 communication and collaboration · 3 the interface side of HITL |
| Estimated effort | about 17% (≈12 person-days) |
| Upstream dependency | P0's `contracts/` (delivered by the end of week 1) |
| Downstream consumers | P0's orchestration core, P3's evaluation harness (the other `HumanIO` implementation) |
| Key risk | you are light on work, but deliver `HumanIO` **early**, otherwise P3's automated evaluation cannot run |

---

## 1. What you own

| Directory | Technical name | Purpose |
| --- | --- | --- |
| `bus/` | message bus (pub-sub) | how agents talk to each other and to humans; 1:1 and group |
| `agents/human.py` | human-as-agent | treat the person as an ordinary member with the fixed id `human` |
| `ui/cli.py` | command line interface (TUI) | one terminal window for group chat, direct messages, approvals and logs |
| `ui/gradio_app.py` | web interface (optional, extra credit) | the same in a browser; working is enough, do not spend time on polish |
| `ui/replay.py` | trace replay | replay one task from start to finish |

---

## 2. Interfaces to implement (frozen)

```python
# contracts/message.py — provided by P0; you write the implementations
from typing import Protocol, Callable, Awaitable, Literal
from pydantic import BaseModel


class Scope(BaseModel):
    kind: Literal["dm", "group"]
    project_id: str
    conversation_id: str
    members: list[str]          # dm: [agent_a, agent_b]; group: every member


class Channel(Protocol):
    async def publish(self, msg: Message) -> None: ...
    def subscribe(
        self, agent_id: str, cb: Callable[[Message], Awaitable[None]]
    ) -> None: ...
    def history(self, scope: Scope, limit: int = 50) -> list[Message]: ...


# contracts/human.py
class HumanIO(Protocol):
    async def ask(self, req: HumanRequest) -> HumanResponse: ...
    async def notify(self, msg: Message) -> None: ...
```

### The one design decision that makes the rest simple

**A human is an ordinary member with `agent_id == "human"`.**

Direct messages, group chat and approvals then become **one mechanism** instead of three:

- direct message = `Scope(kind="dm", members=[agent, "human"])`
- group chat = `Scope(kind="group", members=[...every agent..., "human"])`
- approval = a message with `kind="ask_human"` plus `HumanIO.ask()` waiting for the answer

---

## 3. Implementation conventions (external behaviour only)

1. **`publish` persists before delivering.** A crash must not lose messages, and replay depends on
   them.
2. **Subscriber callbacks must not block the bus.** Give each callback its own task; a slow
   subscriber should be dropped with a warning rather than stalling everyone.
3. **`history` returns ascending by time.**
4. **`ask` must be cancellable.** When a task is aborted, a pending approval has to be withdrawn
   instead of waiting forever.
5. **The UI must not leave background tasks behind**, otherwise the terminal refuses to exit.
6. **The UI does not make business decisions.** Approval policy lives in the orchestration layer;
   the interface shows the question and returns the answer.

---

## 4. Acceptance

Provided: `tests/test_channel_contract.py`. Point `make_bus` in that file at your implementation;
green means done:

- two fake agents exchange a DM and a third agent **never sees it**
- a group message reaches every member
- `history()` still returns the past after a process restart (an in-memory queue fails this, which
  is why SQLite or JSONL is suggested)
- a pending `HumanIO.ask()` can be cancelled
- the CLI can display an `ask_human` message and accept yes/no

---

## 5. Yours to decide

- Rich / Textual / plain `print`, whatever you prefer
- whether to build the Gradio view, and what it looks like
- the replay presentation (timeline / tree / table / step by step)
- SQLite or JSONL for messages, and the table design
- panel layout and colours

---

## 6. Out of scope

- **no prompt assembly** (context belongs to P0)
- no decisions, no tool calls, no LLM calls
- no metrics, no evaluation
- no approval policy (which operations need approval is P0's policy table)

---

## 7. The highest-value extra

`ui/replay.py`: read one task's full trace from the trace store and replay it — who dispatched what,
which tool each agent called, where it waited for a human, how long it all took.

Running this during the demo beats an architecture diagram ten times over, and it costs almost no
extra work: P3 already stores the data, you only render it.
