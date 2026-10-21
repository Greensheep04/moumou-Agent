"""The task fixture format must stay parseable — P3 writes 15 of these."""

from __future__ import annotations

from pathlib import Path

import pytest

from moumou.eval.loader import TaskFixture, load_task, load_tasks

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS = REPO_ROOT / "eval" / "tasks"


def test_bundled_fixtures_are_valid():
    fixtures = load_tasks(TASKS)
    assert len(fixtures) >= 3, "at least one sample per implemented scenario"
    for fixture in fixtures:
        assert fixture.id and fixture.goal.strip()
        assert fixture.scenario
        assert fixture.repeats >= 1


def test_scenarios_are_drawn_from_the_course_list():
    known = {
        "from_scratch",
        "debug",
        "extend",
        "research",
        "needs_approval",
        "use_memory",
        "parallel",
    }
    assert {f.scenario for f in load_tasks(TASKS)} <= known


def test_executable_acceptance_needs_expect(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: x\ntitle: t\nscenario: from_scratch\ngoal: g\n"
        "acceptance:\n  - kind: command\n    target: 'true'\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="expect"):
        load_task(bad)


def test_unknown_scenario_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: x\ntitle: t\nscenario: make_coffee\ngoal: g\n", encoding="utf-8"
    )
    with pytest.raises(Exception, match="unknown scenario"):
        load_task(bad)


def test_duplicate_ids_are_rejected(tmp_path):
    body = "id: dup\ntitle: t\nscenario: from_scratch\ngoal: g\n"
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate task ids"):
        load_tasks(tmp_path)


def test_setup_files_are_plain_strings():
    fixture = load_task(TASKS / "s2_bugfix.yaml")
    assert isinstance(fixture.setup, dict)
    assert "def add" in fixture.setup["calc.py"]


def test_defaults_are_sane():
    fixture = TaskFixture(id="x", title="t", scenario="from_scratch", goal="g")
    assert fixture.budget.max_steps > 0
    assert fixture.human.default == "approve"
    assert fixture.acceptance == []
