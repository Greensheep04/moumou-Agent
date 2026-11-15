"""Load the instructor's three plain-text teams and two sequential tasks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from moumou.contracts import RoleSpec


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    roles: tuple[RoleSpec, ...]
    task1: str
    task2: str
    source_paths: dict[str, Path]
    source_sha256: dict[str, str]

    @property
    def leader(self) -> str:
        return self.roles[0].name


def _read(path: Path, root: Path) -> tuple[str, str]:
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("instructor case file is missing or escapes Mid_Test")
    raw = path.read_bytes()
    if len(raw) > 1024**2:
        raise ValueError("instructor case file exceeds 1 MiB")
    return raw.decode("utf-8-sig").replace("\r\n", "\n"), hashlib.sha256(raw).hexdigest()


def _task(text: str) -> str:
    blocks = re.findall(r"^```text[^\S\n]*\n(.*?)^```[^\S\n]*$", text, re.M | re.S)
    if len(blocks) != 1 or not blocks[0].strip():
        raise ValueError("each instructor task must contain one nonempty text block")
    return blocks[0].rstrip("\n")


def load_midtest_cases(root: Path | str) -> list[Scenario]:
    """Read original prompts/tasks in README order; no model or fixture conversion."""
    root = Path(root).resolve()
    readme = root / "README.md"
    readme_text, readme_sha = _read(readme, root)
    links = re.findall(r"\[[^\]]+\]\(([^\n)]+/01_role_initialization\.md)\)", readme_text)
    if not links:
        raise ValueError("Mid_Test README does not link any role-initialization files")
    scenarios, ids, folders = [], set(), set()
    for link in links:
        path = (root / unquote(link)).resolve()
        role_text, role_sha = _read(path, root)
        if path.parent in folders:
            raise ValueError("Mid_Test README repeats a scenario folder")
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
            project_id=scenario_id, created_by="human", reason="instructor-defined team member",
        ) for title, identity, prompt in prompts)
        task_paths = {"task1": path.parent / "02_task_1.md",
                      "task2": path.parent / "03_task_2.md"}
        tasks, hashes = {}, {"readme": readme_sha, "roles": role_sha}
        for name, task_path in task_paths.items():
            task_text, hashes[name] = _read(task_path, root)
            tasks[name] = _task(task_text)
        scenarios.append(Scenario(
            id=scenario_id, name=heading[2], roles=roles,
            task1=tasks["task1"], task2=tasks["task2"],
            source_paths={"readme": readme, "roles": path, **task_paths},
            source_sha256=hashes,
        ))
    return scenarios


__all__ = ["Scenario", "load_midtest_cases"]
