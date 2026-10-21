"""The scheduling loop.

Event-driven on purpose: a polling ``sleep`` would inject tens of milliseconds of
noise into every measured task duration, which is one of the two metrics the
course actually grades.
"""

from __future__ import annotations

import asyncio

from moumou.contracts import TERMINAL_STATUSES


class Scheduler:
    def __init__(self, repo, runner, trace=None, max_slots: int = 4, max_rounds: int = 500):
        self.repo = repo
        self.runner = runner
        self.trace = trace
        self.max_slots = max_slots
        self.max_rounds = max_rounds
        self._running: dict[str, asyncio.Task] = {}
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True
        for task in self._running.values():
            task.cancel()

    async def run(self) -> None:
        try:
            for _ in range(self.max_rounds):
                if self._aborted:
                    break

                self.repo.promote()
                self._fill_slots()

                if not self._running:
                    break

                await self._collect_one()

            self.repo.promote()
        finally:
            for task in self._running.values():
                task.cancel()
            self._running.clear()

    # -- internals -------------------------------------------------------
    def _fill_slots(self) -> None:
        while len(self._running) < self.max_slots:
            ready = self.repo.ready(limit=1)
            if not ready:
                return
            node = ready[0]
            self.repo.transition(node.id, "RUNNING", "dispatched")
            fresh = self.repo.get(node.id)
            self._running[node.id] = asyncio.create_task(self.runner.run(fresh))

    async def _collect_one(self) -> None:
        by_task = {t: tid for tid, t in self._running.items()}
        done, _ = await asyncio.wait(
            set(by_task), return_when=asyncio.FIRST_COMPLETED
        )

        for task in done:
            task_id = by_task[task]
            self._running.pop(task_id, None)

            if not task.cancelled():
                exc = task.exception()
                if exc is not None and self.trace is not None:
                    self.trace.record(
                        task_id=task_id,
                        kind="task",
                        name="runner_crashed",
                        error=f"{type(exc).__name__}: {exc}",
                    )

            node = self.repo.get(task_id)
            if node is not None and node.status == "FAILED":
                self._handle_failure(node)

    def _handle_failure(self, node) -> None:
        if node.retries < node.budget.max_retries:
            self.repo.bump_retries(node.id)
            self.repo.transition(node.id, "READY", "retry")
        else:
            self.repo.transition(node.id, "DEAD", "retries_exhausted")

    # -- reporting -------------------------------------------------------
    def summary(self) -> dict:
        tasks = self.repo.all_tasks()
        done = sum(1 for t in tasks if t.status == "DONE")
        dead = sum(1 for t in tasks if t.status == "DEAD")
        pending = [t for t in tasks if t.status not in TERMINAL_STATUSES]
        return {
            "total": len(tasks),
            "done": done,
            "dead": dead,
            "unfinished": len(pending),
            "completion_rate": done / len(tasks) if tasks else 0.0,
        }
