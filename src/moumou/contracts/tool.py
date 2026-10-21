"""The tool port.

This is the only thing P1 has to satisfy. Tools know nothing about the
orchestrator, the LLM, approvals, or tracing — the framework wraps every call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

ActionClass = Literal[
    "fs_read",
    "fs_write",
    "fs_delete",
    "shell",
    "git_commit",
    "git_push",
    "network",
    "install",
]

#: Default approval policy. A policy object may override this, but the
#: framework never lets a role grant itself more permissions than this table.
DEFAULT_APPROVAL: dict[str, bool] = {
    "fs_read": False,
    "fs_write": False,
    "fs_delete": True,
    "shell": True,
    "git_commit": False,
    "git_push": True,
    "network": False,
    "install": True,
}


class ToolContext(BaseModel):
    """Everything a tool is allowed to know about its caller."""

    agent_id: str
    task_id: str
    workspace: Path            # the tool may not touch anything outside this
    env: dict[str, str] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None   # required when ok=False; shown to the LLM verbatim
    artifacts: list[str] = Field(default_factory=list)   # relative to the workspace
    truncated: bool = False


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str           # shown to the model; one sentence on when to use it
    params_schema: dict        # JSON Schema, fed straight to the model's function calling
    action_class: str          # one of ActionClass
    timeout_s: float

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
