"""P2 模块测试：持久化消息总线 + 人类代理。

补充契约测试未覆盖、但验收标准明确要求的三个点：
  * 进程重启后 ``history()`` 仍能读回历史（持久化）；
  * DM 成员列表顺序无关；
  * ``HumanIO.ask()`` 挂起后可被取消、且不残留脏状态。
"""

from __future__ import annotations

import asyncio

import pytest

from moumou.agents import CliHuman
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
