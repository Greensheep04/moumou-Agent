from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from time import monotonic

ROOT_CLOSEOUT_SECONDS = 60


class ExecutionDeadline(Exception):
    def __init__(self, scope):
        self.scope = scope
        super().__init__(f"{scope} execution deadline exceeded")


class ExecutionClock:
    def __init__(self, scope, limit_s, *, closeout_s=None, started=None):
        self.scope = scope
        self.limit_s = limit_s
        self.closeout_s = closeout_s
        self.started = monotonic() if started is None else started
        self._paused_at = None
        self._completed_wait_s = 0.0
        self._waiters = 0
        self._timeout = None

    @property
    def active_seconds(self):
        now = monotonic() if self._paused_at is None else self._paused_at
        return now - self.started - self._completed_wait_s

    @property
    def paused_seconds(self):
        now = monotonic()
        current = 0.0 if self._paused_at is None else now - self._paused_at
        return self._completed_wait_s + current

    @property
    def remaining(self):
        return self.limit_s - self.active_seconds

    @property
    def closing(self):
        return self.closeout_s is not None and self.active_seconds >= self.closeout_s

    @contextmanager
    def pause(self):
        if self._waiters == 0:
            self._paused_at = monotonic()
            if self._timeout is not None and self.remaining > 0:
                self._timeout.reschedule(None)
        self._waiters += 1
        try:
            yield
        finally:
            self._waiters -= 1
            if self._waiters == 0:
                self._completed_wait_s += monotonic() - self._paused_at
                self._paused_at = None
                if self._timeout is not None and not self._timeout.expired():
                    self._timeout.reschedule(asyncio.get_running_loop().time() + self.remaining)

    @asynccontextmanager
    async def enforce(self):
        seconds = self.remaining
        if seconds <= 0:
            raise ExecutionDeadline(self.scope)
        timeout = asyncio.timeout(None if self._waiters else seconds)
        self._timeout = timeout
        try:
            async with timeout:
                yield
        except TimeoutError as exc:
            if timeout.expired():
                raise ExecutionDeadline(self.scope) from exc
            raise
        finally:
            self._timeout = None
