"""The primary entry must inspect instructor cases without credentials or an event loop."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from moumou.eval import midtest


def test_primary_defaults_use_instructor_sessions_and_real_human():
    args = midtest.parse_args([])
    assert args.cases == "Mid_Test"
    assert args.scenario == "all"
    assert args.human == "console"
    assert args.search_requests == 6
    assert args.max_tokens > 150000


def test_plan_never_loads_credentials_or_runs_an_event_loop(monkeypatch, capsys):
    from moumou.config import LLMSettings
    from moumou.eval import midtest_cases

    def forbidden(*args, **kwargs):
        raise AssertionError("plan attempted a runtime operation")

    case = SimpleNamespace(
        id="instructor-case",
        name="Instructor case",
        roles=[SimpleNamespace(name=name) for name in ["lead", "dev", "qa", "release"]],
        source_sha256={"task1": "a" * 64},
    )
    monkeypatch.setattr(midtest_cases, "load_midtest_cases", lambda path: [case])
    monkeypatch.setattr(LLMSettings, "from_env", forbidden)
    monkeypatch.setattr(midtest.asyncio, "run", forbidden)
    assert midtest.main(["--plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "instructor_midtest"
    assert result["planned_root_goals"] == 2
    assert result["scenarios"][0]["roles"] == ["lead", "dev", "qa", "release"]


def test_missing_scenario_is_an_error_instead_of_falling_back_to_yaml(monkeypatch, capsys):
    from moumou.eval import midtest_cases

    monkeypatch.setattr(midtest_cases, "load_midtest_cases", lambda path: [])
    assert midtest.main(["--plan", "--scenario", "missing"]) == 2
    assert "not found" in capsys.readouterr().err


def test_default_module_help_and_old_regression_help_remain_distinct():
    root = Path(__file__).resolve().parents[1]
    for module, expected in [("moumou", "instructor"), ("moumou.eval.cli", "evaluation")]:
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout.lower()


def test_second_task_metrics_exclude_first_task_and_do_not_certify_self_report():
    def node(identity, parent, status):
        return SimpleNamespace(
            id=identity, parent_id=parent, status=status, role="lead", acceptance=[], result=None
        )

    old = [node("t1", None, "DONE"), node("t1.1", "t1", "DONE")]
    current = node("t2", None, "DONE")
    selected = [current, node("t2.1", "t2", "DONE"), node("t2.2", "t2", "DEAD")]
    repo = SimpleNamespace(all_tasks=lambda: old + selected, get=lambda identity: current)
    trace = SimpleNamespace(
        query=lambda identity: [
            SimpleNamespace(kind="acceptance", name="verdict", payload={"ok": True})
        ]
    )
    result = midtest._task_record(SimpleNamespace(repo=repo, trace=trace), current, 12.5, "task_2")
    assert {row["id"] for row in result["nodes"]} == {"t2", "t2.1", "t2.2"}
    assert result["observed_subtasks"] == 2
    assert result["observed_subtasks_done"] == 1
    assert result["elapsed_s"] == 12.5
    assert result["agent_declared_complete"] is True
    assert result["root_machine_accepted"] is None
    assert result["verified_goal_success"] is None
