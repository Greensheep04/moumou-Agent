"""Decomposition plan — the structured output of the planner agent.

The planner never writes prose we have to parse: it calls a ``submit_plan``
tool whose argument schema is this model. Validation then happens in code, not
in the prompt.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from .task import Acceptance

_SNAKE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,15}$")


class RoleDraft(BaseModel):
    """A role the planner says it needs. Project/id fields are filled in later."""

    name: str
    title: str
    description: str
    tools: list[str] = Field(default_factory=list)
    reason: str                                   # mandatory: curbs role explosion

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not _SNAKE.match(v):
            raise ValueError(f"role name {v!r} must be snake_case")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a new role must explain why no existing role fits")
        return v


class SubtaskSpec(BaseModel):
    key: str                                      # local id, referenced by sibling deps
    goal: str
    role: str                                     # existing role name or one from plan.roles
    deps: list[str] = Field(default_factory=list)
    acceptance: list[Acceptance] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if not _KEY.match(v):
            raise ValueError(f"subtask key {v!r} must be short snake_case")
        return v

    @field_validator("goal")
    @classmethod
    def _goal_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("subtask goal must not be blank")
        return v


class Plan(BaseModel):
    reasoning: str = ""
    roles: list[RoleDraft] = Field(default_factory=list)
    subtasks: list[SubtaskSpec]
