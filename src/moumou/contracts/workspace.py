"""The workspace port.

One isolated directory tree per agent instance. P1 owns the production
implementation (path jail, optional Docker backend); ``LocalDirs`` below is a
reference implementation so the framework can run before P1 lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class IntegrateResult(BaseModel):
    """Outcome of moving one agent's artifacts into another's workspace.

    A git-backed implementation can hit conflicts, so this is a result object
    rather than a plain list of paths. Conflicts are not an error: the child did
    its job, integrating it is the parent's problem.
    """

    ok: bool = True
    copied: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)   # relative paths that could not merge
    error: str | None = None


@runtime_checkable
class WorkspaceProvider(Protocol):
    def allocate(self, project_id: str, conversation_id: str, agent_id: str) -> Path:
        """Return an existing, empty-or-reusable directory for this agent."""
        ...

    def release(self, path: Path) -> None:
        """Drop the workspace. Implementations must never delete outside their root."""
        ...

    def integrate(self, src: Path, dst: Path, artifacts: list[str]) -> IntegrateResult:
        """Bring a child's artifacts into the parent's workspace.

        This is the seam where "several agents worked in isolation" becomes "one
        verifiable result". The reference implementation copies files; a
        git-backed one merges branches and reports conflicting paths instead of
        giving up.
        """
        ...
