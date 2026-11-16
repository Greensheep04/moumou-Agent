"""Task fixtures: the file format the evaluation set is written in.

One YAML file = one task. The format is deliberately boring so that writing the
15-task set is a writing job, not an engineering job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from moumou.contracts import Acceptance, Budget


class HumanScript(BaseModel):
    """How the scripted human answers during a run. Makes HITL tasks batchable."""

    decisions: dict[str, str] = Field(default_factory=dict)  # action_class -> approve/reject
    default: str = "approve"
    answers: dict[str, str] = Field(default_factory=dict)    # question keyword -> answer
    default_answer: str = "(no answer scripted)"

    @field_validator("default")
    @classmethod
    def _valid_default(cls, v: str) -> str:
        if v not in ("approve", "reject"):
            raise ValueError("human.default must be 'approve' or 'reject'")
        return v


class ApprovalRequirement(BaseModel):
    """A decision and the corresponding tool effect must both be observable."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    decision: Literal["approve", "reject"]


class BehaviorRequirements(BaseModel):
    """Evaluation-only evidence checks; the shared acceptance protocol is unchanged."""

    model_config = ConfigDict(extra="forbid")

    approvals: list[ApprovalRequirement] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    min_children: int = Field(default=0, ge=0)
    memory_injection: bool = False
    parallel_children: bool = False

    @field_validator("required_tools")
    @classmethod
    def _nonblank_unique_tools(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("required_tools must contain unique, nonblank names")
        return values


class WarmupSpec(BaseModel):
    """A separate experience, never a copy of the held-out task's answers."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    setup: dict[str, str] = Field(default_factory=dict)
    acceptance: list[Acceptance] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)


class TaskFixture(BaseModel):
    id: str
    title: str
    scenario: str                       # one of the seven course scenarios
    goal: str
    acceptance: list[Acceptance] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    setup: dict[str, str] = Field(default_factory=dict)   # files seeded into the root workspace
    human: HumanScript = Field(default_factory=HumanScript)
    repeats: int = Field(default=1, ge=1, le=10)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    requirements: BehaviorRequirements = Field(default_factory=BehaviorRequirements)
    warmup: WarmupSpec | None = None

    @field_validator("scenario")
    @classmethod
    def _known_scenario(cls, v: str) -> str:
        allowed = {
            "from_scratch",
            "debug",
            "extend",
            "research",
            "needs_approval",
            "use_memory",
            "parallel",
        }
        if v not in allowed:
            raise ValueError(f"unknown scenario {v!r}; pick one of {sorted(allowed)}")
        return v


def load_task(path: Path) -> TaskFixture:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return TaskFixture(**data)


def load_tasks(directory: Path | str) -> list[TaskFixture]:
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"task directory not found: {directory}")

    fixtures = [load_task(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [f.id for f in fixtures]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return fixtures
