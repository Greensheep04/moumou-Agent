"""Task model — the spine of the whole system.

Scheduling, memory similarity, and the completion-rate metric all hang off
these fields, so they are deliberately explicit rather than free-form.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: Hard cap on decomposition depth. depth=0 is a top-level task.
MAX_DEPTH = 2
#: How many times a parent may be woken because a child died, before it gives up.
MAX_WAKEUPS = 2
#: Upper bound on the hand-off text a parent renders for one child.
MAX_RESULT_CHARS = 3_000
#: Hard cap on how many subtasks one decomposition round may emit.
MAX_SUBTASKS = 6

TaskStatus = Literal[
    "PENDING",        # created, waiting for deps
    "READY",          # deps satisfied, eligible for scheduling
    "RUNNING",        # an agent is working on it
    "WAITING_HUMAN",  # parked on a human approval
    "DONE",           # finished and verified
    "FAILED",         # this attempt failed; may be retried
    "DEAD",           # terminal failure, no more retries
]

TERMINAL_STATUSES: frozenset[str] = frozenset({"DONE", "DEAD"})

#: The single source of truth for legal state transitions.
#: Anything not listed here is a bug in the scheduler, not a feature.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"READY", "DEAD"}),          # DEAD = an upstream dep died
    "READY": frozenset({"RUNNING"}),
    # PENDING here means "handed off to children": the parent's deps are
    # rewritten to its child ids, so it re-enters READY once they all finish.
    # DEAD is reachable from RUNNING for a human abort.
    "RUNNING": frozenset({"WAITING_HUMAN", "DONE", "FAILED", "PENDING", "DEAD"}),
    "WAITING_HUMAN": frozenset({"RUNNING", "FAILED", "DEAD"}),
    "FAILED": frozenset({"READY", "DEAD"}),           # READY = retry
    "DONE": frozenset(),
    "DEAD": frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    """Return True if ``src -> dst`` is a legal state change."""
    return dst in LEGAL_TRANSITIONS.get(src, frozenset())


AcceptanceKind = Literal[
    "command",        # run ``target``; ``expect`` is the required exit code (default "0")
    "pytest",         # run pytest on ``target``; ``expect`` is the exit code (default "0")
    "file_exists",    # ``target`` must exist relative to the workspace
    "file_not_exists",
    "file_contains",  # ``expect`` is a regex that must match ``target``'s content
    "json_key",       # ``expect`` is "a.b.c" or "a.b.c=<json literal>"
]


class Acceptance(BaseModel):
    """One machine-checkable acceptance criterion.

    Kept structured on purpose: a free-text criterion cannot be executed, and
    without executable criteria the completion-rate metric is just an opinion.
    """

    kind: AcceptanceKind
    target: str
    expect: str | None = None
    timeout_s: int = Field(default=120, ge=1)
    description: str = ""

    @field_validator("target")
    @classmethod
    def _target_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("acceptance.target must not be empty")
        return v

    @model_validator(mode="after")
    def _expect_required_where_needed(self) -> Acceptance:
        if self.kind in ("command", "file_contains", "json_key") and self.expect is None:
            raise ValueError(f"acceptance kind {self.kind!r} requires `expect`")
        return self


class TaskResult(BaseModel):
    """What a finished task hands to its parent.

    A plain reply still works -- the worker wraps it in ``summary`` -- but when the
    agent calls ``submit_result`` the parent gets fields it can render instead of a
    truncated paragraph.
    """

    summary: str = ""
    files: list[str] = Field(default_factory=list)
    notes: str = ""
    blockers: list[str] = Field(default_factory=list)

    def as_text(self, limit: int = MAX_RESULT_CHARS) -> str:
        parts: list[str] = []
        if self.summary:
            parts.append(self.summary)
        if self.files:
            parts.append("files: " + ", ".join(self.files))
        if self.notes:
            parts.append("notes: " + self.notes)
        if self.blockers:
            parts.append("blockers: " + "; ".join(self.blockers))
        return "\n".join(parts)[:limit]


class Budget(BaseModel):
    """Per-task resource ceiling. Blowing any of these fails the task.

    ``max_tokens`` is a raw cumulative count, not a bill: with prefix caching most
    of a growing transcript is billed at the cache-hit rate. It exists to stop a
    runaway loop, not to model cost.
    """

    max_steps: int = Field(default=30, ge=1)
    max_tokens: int = Field(default=12_000_000, ge=1_000)
    max_wall_s: int = Field(default=900, ge=10)
    max_retries: int = Field(default=2, ge=0, le=5)


class TaskNode(BaseModel):
    """A node in the task DAG."""

    id: str
    project_id: str
    conversation_id: str
    parent_id: str | None = None
    depth: int = Field(default=0, ge=0, le=MAX_DEPTH)

    goal: str
    role: str                                   # RoleSpec.name, invented at runtime
    deps: list[str] = Field(default_factory=list)
    acceptance: list[Acceptance] = Field(default_factory=list)

    status: TaskStatus = "PENDING"
    retries: int = 0
    budget: Budget = Field(default_factory=Budget)

    artifacts: list[str] = Field(default_factory=list)   # relative to the workspace
    branch: str | None = None                            # this task's git branch
    error: str | None = None
    result: TaskResult | None = None                     # structured hand-off to the parent
    wakeups: int = Field(default=0, ge=0)                # times a dead child woke this task

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("goal", "role")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _no_self_dependency(self) -> TaskNode:
        if self.id in self.deps:
            raise ValueError(f"task {self.id!r} depends on itself")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class TaskSummary(BaseModel):
    """Cheap projection used by the evaluation layer and the CLI."""

    id: str
    goal: str
    role: str
    status: TaskStatus
    depth: int
    retries: int
    created_at: datetime
    updated_at: datetime
