"""Application-level path checks, not an OS sandbox."""
from pathlib import Path, PureWindowsPath


class LocalJail:
    def __init__(self, workspace: Path):
        self.root = Path(workspace).resolve(strict=True)

    def resolve(self, path: str, *, mutable: bool = False) -> Path:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("path must be a non-empty string")
        candidate = (self.root / path).resolve()
        if not candidate.is_relative_to(self.root):
            raise PermissionError("path_outside_workspace")
        relative = candidate.relative_to(self.root)
        lexical = Path(path)
        if any(p.casefold() == ".git" for p in (*lexical.parts, *relative.parts)):
            raise PermissionError("protected_path")
        if any(":" in p or PureWindowsPath(p).is_reserved() for p in relative.parts):
            raise PermissionError("invalid_path")
        if mutable and candidate == self.root:
            raise PermissionError("protected_path")
        return candidate

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

