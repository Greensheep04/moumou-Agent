"""P2 模块测试：持久化消息总线 + 人类代理，待执行的兼容性回归。

补充契约测试未覆盖、但验收标准明确要求的三个点：
  * 进程重启后 ``history()`` 仍能读回历史（持久化）；
  * DM 成员列表顺序无关；
  * ``HumanIO.ask()`` 挂起后可被取消、且不残留脏状态。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from moumou.agents import CliHuman, HumanAgent
from moumou.bus import SqliteBus
from moumou.contracts import HumanRequest, HumanResponse, Message, Scope


async def test_bus_persists_across_reopen(tmp_path):
    path = tmp_path / "bus.db"

    bus = SqliteBus(db_path=path)
    await bus.publish(
        Message(project_id="p", conversation_id="c", sender="a", content="hello")
    )
    bus.close()

    reopened = SqliteBus(db_path=path)
    history = reopened.history(Scope(project_id="p", conversation_id="c"))
    assert [m.content for m in history] == ["hello"]
    reopened.close()


async def test_dm_history_is_order_independent(tmp_path):
    bus = SqliteBus(db_path=tmp_path / "bus.db")
    await bus.publish(
        Message(project_id="p", conversation_id="c", sender="a", recipients=["b"], content="dm")
    )

    forward = Scope(kind="dm", project_id="p", conversation_id="c", members=["a", "b"])
    reverse = Scope(kind="dm", project_id="p", conversation_id="c", members=["b", "a"])

    assert [m.content for m in bus.history(forward)] == ["dm"]
    assert [m.content for m in bus.history(reverse)] == ["dm"]
    bus.close()


async def test_slow_subscriber_is_dropped(tmp_path):
    bus = SqliteBus(db_path=tmp_path / "bus.db", callback_timeout_s=0.1)

    async def slow(msg: Message) -> None:
        await asyncio.sleep(10)

    bus.subscribe("a", slow)
    # 若慢订阅者拖死总线，这里会等 10 秒；超时丢弃后应快速返回。
    await bus.publish(
        Message(project_id="p", conversation_id="c", sender="a", recipients=["a"], content="x")
    )
    bus.close()


async def test_human_ask_is_cancellable():
    human = CliHuman()
    task = asyncio.create_task(
        human.ask(
            HumanRequest(task_id="t1", agent_id="a@t1", kind="approval", summary="go?")
        )
    )
    await asyncio.sleep(0.01)
    assert human.has_pending

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not human.has_pending, "取消后不应留下挂起的审批"


async def test_human_answer_resolves_ask():
    human = CliHuman()
    req = HumanRequest(task_id="t1", agent_id="a@t1", kind="question", summary="what?")
    task = asyncio.create_task(human.ask(req))

    await asyncio.sleep(0.01)
    pending = human.poll_request()
    assert pending is not None
    assert human.answer(pending.id, HumanResponse(request_id=pending.id, text="answer"))

    resp = await asyncio.wait_for(task, timeout=1)
    assert resp.text == "answer"


def message(text, *, sender="a", recipients=None, project="p", conversation="c", ts=None):
    values = dict(project_id=project, conversation_id=conversation, sender=sender,
                  recipients=recipients or [], content=text)
    if ts is not None:
        values["ts"] = ts
    return Message(**values)


async def test_dm_history_contains_only_the_exact_two_party_exchange(tmp_path):
    with SqliteBus(tmp_path / "dm.db") as bus:
        for item in (
            message("a to b", recipients=["b"]),
            message("b to a", sender="b", recipients=["a"]),
            message("a to c private", recipients=["c"]),
            message("c to b private", sender="c", recipients=["b"]),
            message("group broadcast"),
            message("multicast", sender="a", recipients=["b", "c"]),
            message("other conversation", recipients=["b"], conversation="elsewhere"),
            message("other project", recipients=["b"], project="elsewhere"),
        ):
            await bus.publish(item)
        for members in (["a", "b"], ["b", "a"]):
            observed = bus.history(Scope(kind="dm", project_id="p", conversation_id="c",
                                         members=members))
            assert [item.content for item in observed] == ["a to b", "b to a"]


async def test_empty_group_members_retains_operator_conversation_history(tmp_path):
    with SqliteBus(tmp_path / "operator.db") as bus:
        await bus.publish(message("undelivered private", recipients=["nobody"]))
        await bus.publish(message("another conversation", conversation="other"))
        observed = bus.history(Scope(project_id="p", conversation_id="c", members=[]))
        assert [item.content for item in observed] == ["undelivered private"]
        assert bus.history(Scope(project_id="p", conversation_id="c"), limit=0) == []
        with pytest.raises(ValueError, match="nonnegative"):
            bus.history(Scope(project_id="p", conversation_id="c"), limit=-1)


async def test_history_orders_timestamp_instants_and_keeps_conversation_boundaries(tmp_path):
    with SqliteBus(tmp_path / "order.db") as bus:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        await bus.publish(message("later inserted first", ts=start + timedelta(seconds=1)))
        await bus.publish(message("earlier", ts=start))
        await bus.publish(message("other", conversation="other", ts=start))
        assert [item.content for item in bus.history(Scope(project_id="p", conversation_id="c"))] == [
            "earlier", "later inserted first",
        ]


async def test_explicit_subscription_scopes_filter_reused_agent_ids(tmp_path):
    with SqliteBus(tmp_path / "scopes.db") as bus:
        first, second = [], []
        bus.subscribe("human", first.append, scope=Scope(project_id="p", conversation_id="one"))
        bus.subscribe("human", second.append, scope=Scope(project_id="p", conversation_id="two"))
        await bus.publish(message("first", recipients=["human"], conversation="one"))
        await bus.publish(message("second", recipients=["human"], conversation="two"))
        await bus.publish(message("other project", recipients=["human"], project="elsewhere"))
        assert [item.content for item in first] == ["first"]
        assert [item.content for item in second] == ["second"]


async def test_bound_bus_rejects_foreign_publish_before_persistence(tmp_path):
    with SqliteBus(tmp_path / "bound.db", project_id="p", conversation_id="c") as bus:
        await bus.publish(message("allowed"))
        with pytest.raises(ValueError, match="bound conversation"):
            await bus.publish(message("wrong", project="other"))
        with pytest.raises(ValueError, match="bound conversation"):
            bus.subscribe("human", lambda msg: None,
                          scope=Scope(project_id="p", conversation_id="other"))
        assert [item.content for item in bus.history(Scope(project_id="p", conversation_id="c"))] == [
            "allowed",
        ]


async def test_slow_or_failing_subscribers_are_unsubscribed_without_hiding_peer_delivery(tmp_path):
    async with SqliteBus(tmp_path / "subscribers.db", callback_timeout_s=0.02) as bus:
        blocked, errors, peer = [], [], []

        async def slow(msg):
            blocked.append(msg.id)
            await asyncio.Event().wait()

        def fail(msg):
            errors.append(msg.id)
            raise ValueError("synthetic consumer failure")

        bus.subscribe("a", slow)
        bus.subscribe("a", fail)
        bus.subscribe("a", peer.append)
        await bus.publish(message("one", recipients=["a"]))
        await bus.publish(message("two", recipients=["a"]))
        assert len(blocked) == len(errors) == 1
        assert [item.content for item in peer] == ["one", "two"]


async def test_default_sync_callback_keeps_publishing_event_loop_affinity(tmp_path):
    with SqliteBus(tmp_path / "affinity.db") as bus:
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()

        def callback(msg):
            assert asyncio.get_running_loop() is loop
            queue.put_nowait(msg.content)

        bus.subscribe("a", callback)
        await bus.publish(message("loop-safe", recipients=["a"]))
        assert queue.get_nowait() == "loop-safe"


async def test_opted_in_blocking_callback_times_out_without_claiming_thread_termination(tmp_path):
    entered, release = threading.Event(), threading.Event()
    bus = SqliteBus(tmp_path / "offload.db", callback_timeout_s=0.02)
    calls = []

    def blocking(msg):
        calls.append(msg.id)
        entered.set()
        release.wait(2)

    bus.subscribe("a", blocking, offload_sync=True)
    try:
        await asyncio.wait_for(bus.publish(message("one", recipients=["a"])), timeout=1)
        assert entered.is_set() and not release.is_set()
        await bus.publish(message("two", recipients=["a"]))
        assert len(calls) == 1
    finally:
        release.set()
        await bus.aclose()


async def test_offloaded_sync_function_returning_coroutine_is_awaited_on_loop(tmp_path):
    async with SqliteBus(tmp_path / "returned-coroutine.db") as bus:
        loop, seen = asyncio.get_running_loop(), []

        async def finish(msg):
            assert asyncio.get_running_loop() is loop
            seen.append(msg.content)

        def callback(msg):
            return finish(msg)

        bus.subscribe("a", callback, offload_sync=True)
        await bus.publish(message("complete", recipients=["a"]))
        assert seen == ["complete"]


async def test_aclose_awaits_cooperative_callback_cleanup(tmp_path):
    bus = SqliteBus(tmp_path / "cleanup.db")
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def callback(msg):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    bus.subscribe("a", callback)
    published = asyncio.create_task(bus.publish(message("waiting", recipients=["a"])))
    try:
        await started.wait()
        with pytest.raises(RuntimeError, match="aclose"):
            bus.close()
        await bus.aclose()
        assert cleaned.is_set()
        await published
        with pytest.raises(RuntimeError, match="closed"):
            await bus.publish(message("too late"))
    finally:
        if not published.done():
            published.cancel()
        await asyncio.gather(published, return_exceptions=True)
        await bus.aclose()


async def test_human_agent_awaits_async_callback_and_filters_its_binding(tmp_path):
    with SqliteBus(tmp_path / "human-agent.db") as bus:
        human = HumanAgent(bus, project_id="p", conversation_id="c")
        seen = []

        async def callback(msg):
            await asyncio.sleep(0)
            seen.append(msg.content)

        human.subscribe(callback)
        await bus.publish(message("visible", recipients=["human"]))
        await bus.publish(message("foreign", recipients=["human"], conversation="other"))
        await bus.publish(message("not for human", recipients=["b"]))
        assert seen == ["visible"]
        with pytest.raises(ValueError, match="human conversation"):
            await human.say("wrong", project_id="p", conversation_id="other")


@pytest.mark.parametrize("damage", ["wrong_id", "empty", "wrong_option", "decision"])
async def test_invalid_answer_does_not_consume_the_original_question(damage):
    human = CliHuman()
    req = HumanRequest(id="original", task_id="t1", agent_id="a", kind="question",
                       summary="Choose a value", options=["keep", "cancel"])
    asked = asyncio.create_task(human.ask(req))
    try:
        await asyncio.sleep(0)
        payload = HumanResponse(request_id=req.id, text="keep")
        if damage == "wrong_id":
            payload.request_id = "different"
        elif damage == "empty":
            payload.text = "  "
        elif damage == "decision":
            payload.decision = "approve"
        else:
            payload.text = "unoffered"
        with pytest.raises(ValueError):
            human.answer(req.id, payload)
        assert human.has_pending and not asked.done()
        assert human.answer(req.id, HumanResponse(request_id=req.id, text="keep"))
        assert (await asked).text == "keep"
    finally:
        human.close()
        await asyncio.gather(asked, return_exceptions=True)


@pytest.mark.parametrize("kind", ["approval", "review"])
async def test_decision_requires_explicit_allowed_decision_before_consumption(kind):
    human = CliHuman()
    req = HumanRequest(task_id="t1", agent_id="a", kind=kind, summary="Decide explicitly",
                       options=["approve", "reject"])
    asked = asyncio.create_task(human.ask(req))
    try:
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="explicit"):
            human.answer(req.id, HumanResponse(request_id=req.id, text="unspecified"))
        assert human.has_pending
        assert human.answer(req.id, HumanResponse(request_id=req.id, decision="reject",
                                                 text="Actual fixture feedback"))
        assert (await asked).decision == "reject"
    finally:
        human.close()
        await asyncio.gather(asked, return_exceptions=True)


async def test_duplicate_request_id_never_replaces_a_pending_or_consumed_request():
    human = CliHuman()
    req = HumanRequest(id="same-id", task_id="t1", agent_id="a", kind="question", summary="Original")
    asked = asyncio.create_task(human.ask(req))
    try:
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="already been used"):
            await human.ask(req.model_copy(update={"summary": "Different question"}))
        assert human.answer(req.id, HumanResponse(request_id=req.id, text="Original answer"))
        assert (await asked).text == "Original answer"
        with pytest.raises(ValueError, match="already been used"):
            await human.ask(req)
    finally:
        human.close()
        await asyncio.gather(asked, return_exceptions=True)


async def test_cancelled_questions_are_skipped_and_shutdown_never_invents_an_answer():
    human = CliHuman()
    req = HumanRequest(id="cancel-me", task_id="t1", agent_id="a", kind="question", summary="Wait")
    asked = asyncio.create_task(human.ask(req))
    await asyncio.sleep(0)
    asked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked
    assert human.poll_request() is None and not human.has_pending
    next_req = req.model_copy(update={"id": "next-question"})
    waiting = asyncio.create_task(human.ask(next_req))
    await asyncio.sleep(0)
    human.reject_all("This shutdown text must never become a business answer")
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not human.has_pending and human.poll_request() is None
    assert human.answer(next_req.id, HumanResponse(request_id=next_req.id, text="late")) is False
    human.close()


async def test_request_snapshot_and_bound_human_scope_remain_original():
    human = CliHuman(project_id="p", conversation_id="c")
    req = HumanRequest(id="snapshot", project_id="p", conversation_id="c", role="a",
                       task_id="t1", agent_id="a", kind="question", summary="Original",
                       options=["original"])
    asked = asyncio.create_task(human.ask(req))
    try:
        await asyncio.sleep(0)
        req.options[:] = ["mutated"]
        queued = human.poll_request()
        assert queued.options == ["original"]
        queued.options[:] = ["also mutated"]
        with pytest.raises(ValueError, match="options"):
            human.answer(req.id, HumanResponse(request_id=req.id, text="mutated"))
        assert human.answer(req.id, HumanResponse(request_id=req.id, text="original"))
        assert (await asked).text == "original"
        with pytest.raises(ValueError, match="human conversation"):
            await human.ask(req.model_copy(update={"id": "foreign", "conversation_id": "other"}))
    finally:
        await human.aclose()
        await asyncio.gather(asked, return_exceptions=True)


async def test_human_close_wakes_queue_waiters_without_synthetic_messages():
    human = CliHuman()
    request_waiter = asyncio.create_task(human.next_request())
    notification_waiter = asyncio.create_task(human.next_notification())
    await asyncio.sleep(0)
    await human.aclose()
    for waiter in (request_waiter, notification_waiter):
        with pytest.raises(RuntimeError, match="closed"):
            await waiter


async def test_publish_freezes_the_same_message_facts_for_persistence_and_delivery(
        tmp_path, monkeypatch):
    bus = SqliteBus(tmp_path / "publish-snapshot.db")
    entered, release, received = asyncio.Event(), asyncio.Event(), []
    original_deliver = bus._deliver

    async def delayed_delivery(subscription, item):
        entered.set()
        await release.wait()
        await original_deliver(subscription, item)

    monkeypatch.setattr(bus, "_deliver", delayed_delivery)
    bus.subscribe("b", received.append)
    outgoing = message("original", recipients=["b"])
    outgoing.meta = {"nested": ["original"]}
    published = asyncio.create_task(bus.publish(outgoing))
    try:
        await entered.wait()
        outgoing.content = "mutated after persistence"
        outgoing.meta["nested"][:] = ["mutated"]
        outgoing.project_id = "foreign"
        release.set()
        await published
        saved = bus.history(Scope(project_id="p", conversation_id="c"))
        assert len(saved) == len(received) == 1
        assert saved[0].content == received[0].content == "original"
        assert saved[0].meta == received[0].meta == {"nested": ["original"]}
        assert saved[0].project_id == received[0].project_id == "p"
    finally:
        release.set()
        if not published.done():
            published.cancel()
        await asyncio.gather(published, return_exceptions=True)
        await bus.aclose()
