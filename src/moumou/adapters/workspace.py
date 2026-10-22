"""Reference ``WorkspaceProvider``: one plain directory per agent instance.

No jail, no containers — P1 replaces this with the real sandbox. The only thing
this guarantees is that two agents never share a directory.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from moumou.contracts import IntegrateResult

_UNSAFE = re.compile(r"[^a-zA-Z0-9._@#-]")


def _safe(part: str) -> str:
    return _UNSAFE.sub("_", part) or "_"


class LocalDirs:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def allocate(self, project_id: str, conversation_id: str, agent_id: str) -> Path:
        path = self.root / _safe(project_id) / _safe(conversation_id) / _safe(agent_id)
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError(f"workspace escapes root: {resolved}")
        return resolved

    def release(self, path: Path) -> None:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError(f"refusing to delete outside root: {resolved}")
        # intentionally does not delete: artifacts are evidence

    def integrate(self, src: Path, dst: Path, artifacts: list[str]) -> IntegrateResult:
        """Naive integration: copy each artifact up to the parent.

        P1 replaces this with ``git merge`` of the child's branch. Copying can
        never conflict, so ``conflicts`` is always empty here -- but the result
        shape is the same one the git backend has to fill in.
        """
        src_root, dst_root = Path(src).resolve(), Path(dst).resolve()
        for root in (src_root, dst_root):
            if not root.is_relative_to(self.root):
                return IntegrateResult(
                    ok=False, error=f"integration escapes root: {root}"
                )

        copied: list[str] = []
        for rel in artifacts:
            source = (src_root / rel).resolve()
            target = (dst_root / rel).resolve()
            if not source.is_relative_to(src_root) or not target.is_relative_to(dst_root):
                return IntegrateResult(
                    ok=False, copied=copied, error=f"artifact escapes workspace: {rel}"
                )
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(rel)
        return IntegrateResult(ok=True, copied=copied)
