"""Agent identity: roles are invented by the model, not pre-registered.

Three isolation scopes follow from this module:

    role definition  -> project + role        (reusable across conversations)
    conversation ctx -> project + conv + role (never leaves the conversation)
    long-term memory -> project + role        (accumulates across conversations)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: Upper bound on distinct roles a single project may invent. Once hit, the
#: planner may only reuse roles it already has.
MAX_ROLES_PER_PROJECT = 8

_SNAKE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class RoleSpec(BaseModel):
    """A role, generated on the fly by the planner and then reused.

    ``reason`` is mandatory for model-created roles: it forces the planner to
    justify a new role instead of emitting five near-duplicate ones.
    """

    name: str                                   # snake_case, unique within a project
    title: str                                  # human readable, e.g. "API designer"
    description: str                            # the job; feeds the system prompt
    tools: list[str] = Field(default_factory=list)
    created_by: Literal["model", "human"] = "model"
    reason: str | None = None
    project_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE.match(v):
            raise ValueError(f"role name {v!r} must be snake_case (a-z0-9_)")
        return v

    @model_validator(mode="after")
    def _model_roles_need_a_reason(self) -> RoleSpec:
        if self.created_by == "model" and not (self.reason and self.reason.strip()):
            raise ValueError("model-created roles must supply a reason")
        return self


class AgentInstance(BaseModel):
    """One running instance of a role, e.g. ``coder#2``."""

    id: str
    role: str
    project_id: str
    conversation_id: str
    workspace: str                              # absolute path, allocated by the sandbox
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def context_scope(self) -> str:
        """Shared by instances of the same role inside one conversation."""
        return f"{self.project_id}:{self.conversation_id}:{self.role}"

    @property
    def memory_scope(self) -> str:
        """Shared by instances of the same role inside one project."""
        return f"{self.project_id}:{self.role}"
