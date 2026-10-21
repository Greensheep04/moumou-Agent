"""Execution trace.

Everything downstream — the metrics, the replay view, the failure timeline in
the report — reads this and nothing else. One append-only record per span.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

SpanKind = Literal[
    "task",          # one scheduler slot for one TaskNode
    "llm",           # one model round-trip
    "tool",          # one tool invocation
    "approval",      # time spent waiting on a human decision
    "question",      # time spent waiting on a human answer
    "steer",         # a mid-flight human nudge was received
    "delegate",      # a decomposition produced children
    "role",          # a role was created or reused
    "acceptance",    # one acceptance check ran
    "memory",        # a memory write or retrieve
]


class TraceEvent(BaseModel):
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_span: str | None = None
    task_id: str
    agent_id: str | None = None

    kind: SpanKind
    name: str
    t0: datetime = Field(default_factory=lambda: datetime.now(UTC))
    t1: datetime | None = None
    status: Literal["ok", "error", "cancelled"] | None = None

    tokens_in: int = 0
    tokens_out: int = 0
    payload: dict = Field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        if self.t1 is None:
            return None
        return (self.t1 - self.t0).total_seconds() * 1000.0
