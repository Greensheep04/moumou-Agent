"""Internal fixture parsing uses isolated contract probes, not the instructor bank."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from moumou.eval.loader import TaskFixture, load_task, load_tasks


def test_multiple_temporary_fixtures_are_valid(tmp_path):
    for index in range(3):
        (tmp_path / f"probe{index}.yaml").write_text(
            f"id: probe{index}\ntitle: Parser probe\nscenario: from_scratch\ngoal: g\n",
            encoding="utf-8",
        )
    fixtures = load_tasks(tmp_path)
    assert len(fixtures) == 3
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
    for scenario in known:
        assert TaskFixture(id="probe", title="t", scenario=scenario, goal="g").scenario == scenario


def test_executable_acceptance_needs_expect(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: x\ntitle: t\nscenario: from_scratch\ngoal: g\n"
        "acceptance:\n  - kind: command\n    target: 'true'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="expect"):
        load_task(bad)


def test_unknown_scenario_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: x\ntitle: t\nscenario: make_coffee\ngoal: g\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="unknown scenario"):
        load_task(bad)


def test_duplicate_ids_are_rejected(tmp_path):
    body = "id: dup\ntitle: t\nscenario: from_scratch\ngoal: g\n"
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate task ids"):
        load_tasks(tmp_path)


def test_loader_defaults_and_setup_files(tmp_path):
    source = tmp_path / "setup.yaml"
    source.write_text(
        "id: setup\ntitle: t\nscenario: from_scratch\ngoal: g\n"
        "setup:\n  prepared.txt: test-only marker\n", encoding="utf-8",
    )
    fixture = load_task(source)
    assert fixture.setup == {"prepared.txt": "test-only marker"}

    bare = TaskFixture(id="x", title="t", scenario="from_scratch", goal="g")
    assert bare.budget.max_steps > 0
    assert bare.human.default == "approve"
    assert bare.acceptance == []
