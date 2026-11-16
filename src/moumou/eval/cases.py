"""Load declared Markdown teams and their original sequential tasks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from moumou.contracts import RoleSpec


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    roles: tuple[RoleSpec, ...]
    task1: str
    task2: str | None = None
    source_paths: dict[str, Path] = field(default_factory=dict)
    source_sha256: dict[str, str] = field(default_factory=dict)
    source_kind: str = "instructor"

    @property
    def leader(self) -> str:
        return self.roles[0].name

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(task for task in (self.task1, self.task2) if task is not None)


def _read(path: Path, root: Path) -> tuple[str, str]:
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("instructor case file is missing or escapes evaluation source root")
    raw = path.read_bytes()
    if len(raw) > 1024**2:
        raise ValueError("instructor case file exceeds 1 MiB")
    return raw.decode("utf-8-sig").replace("\r\n", "\n"), hashlib.sha256(raw).hexdigest()


def _task(text: str) -> str:
    blocks = re.findall(r"^```text[^\S\n]*\n(.*?)^```[^\S\n]*$", text, re.M | re.S)
    if len(blocks) != 1 or not blocks[0].strip():
        raise ValueError("each instructor task must contain one nonempty text block")
    return blocks[0].rstrip("\n")


def load_cases(root: Path | str) -> list[Scenario]:
    """Read original prompts/tasks in README order; no model or fixture conversion."""
    root = Path(root).resolve()
    readme = root / "README.md"
    readme_text, readme_sha = _read(readme, root)
    markers = re.findall(r"<!--\s*evaluation-bank:\s*([^\n]*?)\s*-->", readme_text)
    if (len(markers) > 1 or any(marker not in {"instructor", "supplementary"}
                               for marker in markers)
            or (not markers and "evaluation-bank:" in readme_text)):
        raise ValueError("unrecognized or ambiguous evaluation-bank marker")
    source_kind = markers[0] if markers else "instructor"
    links = re.findall(r"\[[^\]]+\]\(([^\n)]+/01_role_initialization\.md)\)", readme_text)
    if not links:
        raise ValueError("instructor cases README does not link any role-initialization files")
    declared_tasks = set()
    for link in re.findall(r"\[[^\]]+\]\(([^\n)]+/(?:02_task_1|03_task_2)\.md)\)",
                           readme_text):
        task_path = (root / unquote(link)).resolve()
        if not task_path.is_relative_to(root):
            raise ValueError("declared task link escapes the evaluation source root")
        _read(task_path, root)
        declared_tasks.add(task_path)
    scenarios, ids, folders = [], set(), set()
    for link in links:
        path = (root / unquote(link)).resolve()
        role_text, role_sha = _read(path, root)
        if path.parent in folders:
            raise ValueError("instructor cases README repeats a scenario folder")
        folders.add(path.parent)
        heading = re.search(r"^# Scenario (\d+): (.+)$", role_text, re.M)
        if heading is None:
            raise ValueError("instructor team needs its Scenario N heading")
        scenario_id = f"scenario_{heading[1]}"
        if scenario_id in ids:
            raise ValueError("instructor scenario identifiers are duplicated")
        ids.add(scenario_id)
        prompts = re.findall(
            r"^## (.+?) \(([a-z][a-z0-9_]+)\)[^\S\n]*\n\s*"
            r"```text[^\S\n]*\n(.*?)^```[^\S\n]*$", role_text, re.M | re.S,
        )
        members = re.findall(r"^\|\s*`([a-z][a-z0-9_]+)`\s*\|\s*([^|]+?)\s*\|$",
                             role_text, re.M)
        if (len(prompts) != 4 or len(members) != 4
                or [(identity, title) for title, identity, _ in prompts] != members
                or len({identity for _, identity, _ in prompts}) != 4):
            raise ValueError("instructor role table and four original prompts disagree")
        roles = tuple(RoleSpec(
            name=identity, title=title, description=prompt.rstrip("\n"),
            project_id=scenario_id, created_by="human",
            reason=f"{source_kind}-defined team member",
        ) for title, identity, prompt in prompts)
        first, second = ((path.parent / name).resolve()
                         for name in ("02_task_1.md", "03_task_2.md"))
        if first not in declared_tasks:
            raise ValueError("README must explicitly link Task 1 for every declared scenario")
        task_paths = {"task1": first}
        if second in declared_tasks:
            task_paths["task2"] = second
        tasks, hashes = {}, {"readme": readme_sha, "roles": role_sha}
        for name, task_path in task_paths.items():
            task_text, hashes[name] = _read(task_path, root)
            tasks[name] = _task(task_text)
        scenarios.append(Scenario(
            id=scenario_id, name=heading[2], roles=roles,
            task1=tasks["task1"], task2=tasks.get("task2"),
            source_paths={"readme": readme, "roles": path, **task_paths},
            source_sha256=hashes, source_kind=source_kind,
        ))
    return scenarios


__all__ = ["Scenario", "load_cases"]
