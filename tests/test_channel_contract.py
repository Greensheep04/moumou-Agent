"""Channel contract test — **owned by P0, enforced on P2**.

Run it against your implementation:

    pytest tests/test_channel_contract.py -q

To point it at your bus, change ``make_bus`` below to construct yours. Nothing
else in this file needs editing; if these pass, the orchestrator can talk
through your channel.
"""

from __future__ import annotations

import pytest

from moumou.adapters import InMemoryBus
from moumou.contracts import Channel, Message, Scope


@pytest.fixture
def make_bus():
    """Swap this for your implementation: ``return SqliteBus(...)``."""
    return lambda: InMemoryBus()


def msg(conv: str = "c1", sender: str = "a", recipients=None, kind: str = "chat", text="hi"):
    return Message(
        project_id="p",
        conversation_id=conv,
        sender=sender,
        recipients=recipients or [],
        kind=kind,
        content=text,
    )


async def test_direct_message_reaches_only_its_recipient(make_bus):
    bus = make_bus()
    seen = {"a": [], "b": [], "c": []}
    for agent in seen:
        bus.subscribe(agent, lambda m, a=agent: seen[a].append(m))

    await bus.publish(msg(sender="a", recipients=["b"], text="secret"))

    assert [m.content for m in seen["b"]] == ["secret"]
    assert seen["a"] == [] and seen["c"] == [], "a DM must not leak to other members"


async def test_empty_recipients_means_broadcast(make_bus):
    bus = make_bus()
    seen = {"a": [], "b": [], "c": []}
    for agent in seen:
        bus.subscribe(agent, lambda m, a=agent: seen[a].append(m))

    await bus.publish(msg(sender="a", recipients=[], text="standup"))

    assert all(len(seen[k]) == 1 for k in seen), "broadcast must reach every subscriber"


async def test_async_callbacks_are_awaited(make_bus):
    bus = make_bus()
    received = []

    async def callback(message):
        received.append(message.content)

    bus.subscribe("a", callback)
    await bus.publish(msg(recipients=["a"], text="ping"))

    assert received == ["ping"]


async def test_history_is_ascending_and_scoped_to_the_conversation(make_bus):
    bus = make_bus()
    for i in range(5):
        await bus.publish(msg(sender="a", text=f"m{i}"))
    await bus.publish(msg(conv="c2", sender="x", text="other"))

    scope = Scope(kind="group", project_id="p", conversation_id="c1", members=["a", "b"])
    history = bus.history(scope, limit=50)

    assert [m.content for m in history] == ["m0", "m1", "m2", "m3", "m4"]
    assert all(m.conversation_id == "c1" for m in history)


async def test_history_honours_the_limit(make_bus):
    bus = make_bus()
    for i in range(10):
        await bus.publish(msg(text=f"m{i}"))

    scope = Scope(project_id="p", conversation_id="c1", members=["a"])
    assert len(bus.history(scope, limit=3)) == 3


async def test_publishing_to_nobody_does_not_raise(make_bus):
    bus = make_bus()
    await bus.publish(msg(recipients=["ghost"]))       # nobody subscribed
    assert bus.history(Scope(project_id="p", conversation_id="c1")) != []


async def test_steering_survives_until_drained(make_bus):
    """Steering is published by a human and pulled by the agent at a step boundary."""
    bus = make_bus()
    await bus.publish(msg(sender="human", recipients=[], kind="steer", text="focus on tests"))
    await bus.publish(msg(sender="human", recipients=[], kind="steer", text="and the docs"))

    scope = Scope(project_id="p", conversation_id="c1")
    steering = [m for m in bus.history(scope) if m.kind == "steer"]
    assert [m.content for m in steering] == ["focus on tests", "and the docs"]


def test_bus_satisfies_the_channel_port(make_bus):
    """The orchestrator only relies on these three methods."""
    bus = make_bus()
    for method in ("publish", "subscribe", "history"):
        assert callable(getattr(bus, method, None)), f"missing Channel.{method}"
    assert isinstance(bus, Channel), "bus does not satisfy the Channel protocol"
