"""Scheduler shutdown must await cooperative runner cleanup before storage closes."""

import asyncio

import pytest

from moumou.contracts import TaskNode
from moumou.orchestrator.scheduler import Scheduler
from moumou.orchestrator.state import Database, SqliteTaskRepo


async def test_cancelled_scheduler_waits_for_runner_finally_before_return(tmp_path):
    db = Database(tmp_path / "state.db")
    repo = SqliteTaskRepo(db)
    node = TaskNode(id="t1", project_id="p", conversation_id="c", role="worker", goal="wait")
    repo.add(node)
    repo.transition(node.id, "READY", "setup")
    started, cleanup_started, allow_cleanup, cleaned = (asyncio.Event() for _ in range(4))

    class Runner:
        async def run(self, current):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                repo.transition(current.id, "DEAD", "cancelled_cleanup")
                cleaned.set()

    scheduler = Scheduler(repo, Runner())
    running = asyncio.create_task(scheduler.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        running.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        assert not running.done(), "scheduler returned before the runner's database cleanup"
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert cleaned.is_set() and scheduler._running == {}
        assert repo.get(node.id).status == "DEAD"
    finally:
        allow_cleanup.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        db.close()


async def test_round_limit_drains_other_running_tasks_before_return(tmp_path):
    db = Database(tmp_path / "state.db")
    repo = SqliteTaskRepo(db)
    for task_id in ("fast", "slow"):
        repo.add(TaskNode(id=task_id, project_id="p", conversation_id="c", role="worker",
                          goal=task_id))
        repo.transition(task_id, "READY", "setup")
    slow_started, cleaned = asyncio.Event(), asyncio.Event()

    class Runner:
        async def run(self, current):
            if current.id == "fast":
                await slow_started.wait()
                repo.transition(current.id, "DONE", "finished")
                return
            slow_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                repo.transition(current.id, "DEAD", "round_limit_cleanup")
                cleaned.set()

    scheduler = Scheduler(repo, Runner(), max_slots=2, max_rounds=1)
    try:
        await asyncio.wait_for(scheduler.run(), timeout=2)
        assert cleaned.is_set() and scheduler._running == {}
        assert repo.get("fast").status == "DONE"
        assert repo.get("slow").status == "DEAD"
    finally:
        db.close()
