"""The workspace port.

One isolated directory tree per agent instance. P1 owns the production
implementation (path jail, optional Docker backend); ``LocalDirs`` below is a
reference implementation so the framework can run before P1 lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkspaceProvider(Protocol):
    def allocate(self, project_id: str, conversation_id: str, agent_id: str) -> Path:
        """Return an existing, empty-or-reusable directory for this agent."""
        ...

    def release(self, path: Path) -> None:
        """Drop the workspace. Implementations must never delete outside their root."""
        ...

    def integrate(self, src: Path, dst: Path, artifacts: list[str]) -> list[str]:
        """Bring a child's artifacts into the parent's workspace.

        This is the seam where "several agents worked in isolation" becomes "one
        verifiable result". The reference implementation copies files; P1's real
        one merges git branches.
        """
        ...
