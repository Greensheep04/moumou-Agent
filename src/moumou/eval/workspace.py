"""Fresh evaluation directories and committed fixture baselines."""

from pathlib import Path, PureWindowsPath

from moumou.adapters import GitWorkspaces
from moumou.sandbox.local import LocalJail
from moumou.workspace.git import run_git

BACKENDS = ("local", "git")


def create_run_dir(root: Path, fixture_id: str, repeat: int) -> Path:
    windows = PureWindowsPath(fixture_id)
    if (not fixture_id or fixture_id in (".", "..") or windows.drive or windows.root
            or "/" in fixture_id or "\\" in fixture_id or ":" in fixture_id
            or windows.is_reserved() or fixture_id.endswith((".", " "))):
        raise ValueError("fixture id must be one safe directory name")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 0:
        raise ValueError("repeat must be a non-negative integer")
    root = Path(root).resolve()
    run_dir = (root / fixture_id / f"r{repeat}").resolve()
    if not run_dir.is_relative_to(root):
        raise ValueError("run directory is outside output root")
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_setup(workspace: Path, setup: dict[str, str]) -> None:
    jail = LocalJail(workspace)
    targets = []
    for rel, content in setup.items():
        windows = PureWindowsPath(rel)
        if windows.drive or windows.root or ".." in windows.parts:
            raise ValueError("setup path must be relative and inside the workspace")
        target = jail.resolve(rel.replace("\\", "/"), mutable=True)
        targets.append((target, content))
    for target, content in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def git_workspace(run_dir: Path, setup: dict[str, str]) -> GitWorkspaces:
    seed = run_dir / "seed"
    seed.mkdir()
    write_setup(seed, setup)
    run_git(seed, "init", "--initial-branch=main")
    run_git(seed, "add", "--force", "--all", "--", ".")
    run_git(seed, "commit", "--allow-empty", "-m", "evaluation fixture baseline")
    return GitWorkspaces(run_dir / "workspaces", seed=seed)
