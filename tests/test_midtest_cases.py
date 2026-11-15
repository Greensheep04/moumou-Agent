"""Instructor prompts are original inputs, not rewritten YAML acceptance fixtures."""

from __future__ import annotations

import hashlib

import pytest

from moumou.eval.midtest_cases import load_midtest_cases

MEMBERS = (
    (("lead", "Technical Lead"), ("dev", "Software Developer"),
     ("qa", "Test Engineer"), ("release", "Release Manager")),
    (("chief_editor", "Editor-in-Chief"), ("reporter", "Reporter"),
     ("fact_checker", "Fact-Checker"), ("editor", "Editor")),
    (("fund_manager", "Fund Manager"), ("research", "Investment Research Analyst"),
     ("risk", "Risk Manager"), ("marketing", "Marketing Specialist")),
)


def write_cases(root):
    root.mkdir(parents=True, exist_ok=True)
    links = []
    for index, members in enumerate(MEMBERS, 1):
        directory = root / f"scenario_{index} with spaces"
        directory.mkdir()
        links.append(f"[Team {index}]({directory.name}/01_role_initialization.md)")
        text = f"# Scenario {index}: Original team {index}\n\n| Agent ID | Role |\n|---|---|\n"
        text += "\n".join(f"| `{name}` | {title} |" for name, title in members) + "\n\n"
        for name, title in members:
            text += (f"## {title} ({name})\n\n```text\n"
                     f"You are the original {name}; preserve this precise responsibility.\n```\n\n")
        (directory / "01_role_initialization.md").write_text(text, encoding="utf-8")
        for number in (1, 2):
            (directory / f"0{number + 1}_task_{number}.md").write_text(
                f"# Task {number}\n\n```text\nOriginal task {number} for team {index}.\n"
                "Keep Unicode café and exact punctuation!\n```\n", encoding="utf-8")
    (root / "README.md").write_text("\n".join(links), encoding="utf-8")
    return root


def test_cases_preserve_all_four_teacher_ids_prompts_tasks_and_input_hashes(tmp_path):
    cases = load_midtest_cases(write_cases(tmp_path / "Mid_Test"))
    assert [case.id for case in cases] == ["scenario_1", "scenario_2", "scenario_3"]
    for number, case in enumerate(cases, 1):
        assert tuple(role.name for role in case.roles) == tuple(
            name for name, _ in MEMBERS[number - 1])
        assert case.leader == MEMBERS[number - 1][0][0]
        assert case.task1 == (
            f"Original task 1 for team {number}.\nKeep Unicode café and exact punctuation!"
        )
        assert case.task2.startswith(f"Original task 2 for team {number}.")
        for role in case.roles:
            assert role.description == (
                f"You are the original {role.name}; preserve this precise responsibility."
            )
            assert role.created_by == "human" and role.tools == []
        for name, path in case.source_paths.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == case.source_sha256[name]


@pytest.mark.parametrize("damage", ["role_table", "missing_prompt", "missing_task", "two_blocks"])
def test_invalid_teacher_inputs_are_rejected_instead_of_invented(tmp_path, damage):
    root = write_cases(tmp_path / "cases")
    directory = root / "scenario_1 with spaces"
    role_path = directory / "01_role_initialization.md"
    if damage == "role_table":
        role_path.write_text(role_path.read_text(encoding="utf-8").replace("`dev`", "`other`"),
                             encoding="utf-8")
    elif damage == "missing_prompt":
        role_path.write_text(role_path.read_text(encoding="utf-8").replace(
            "## Test Engineer (qa)", "## Test Engineer"), encoding="utf-8")
    elif damage == "missing_task":
        (directory / "03_task_2.md").unlink()
    else:
        path = directory / "02_task_1.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n```text\nAnother task\n```\n",
                        encoding="utf-8")
    with pytest.raises(ValueError):
        load_midtest_cases(root)


def test_readme_cannot_reference_a_team_outside_its_source_root(tmp_path):
    root = write_cases(tmp_path / "cases")
    (root / "README.md").write_text(
        "[outside](../other/01_role_initialization.md)", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or escapes"):
        load_midtest_cases(root)


def test_duplicate_scenarios_are_rejected(tmp_path):
    root = write_cases(tmp_path / "cases")
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n" + text.splitlines()[0], encoding="utf-8")
    with pytest.raises(ValueError, match="repeats"):
        load_midtest_cases(root)
