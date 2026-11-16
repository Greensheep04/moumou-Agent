"""Every formal entry follows the same instructor protocol; planning stays offline."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from moumou.eval import cli

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("moumou", "moumou.eval.cli", "moumou.eval.report",
           "moumou.eval.analysis", "moumou.eval.freeze")

# Load the public entry point as __main__, while making model/environment/loop
# initialization fail loudly. No real credentials are needed or inspected.
OFFLINE_LAUNCH = """
import asyncio
import runpy
import sys
from unittest.mock import patch
import httpx
import moumou.adapters
import moumou.adapters.llm
import moumou.config

def forbidden(*args, **kwargs):
    raise AssertionError('offline entry point attempted runtime/model initialization')

module, *arguments = sys.argv[1:]
sys.argv = [module, *arguments]
with patch('asyncio.run', forbidden), \\
     patch('moumou.config.load_dotenv', forbidden), \\
     patch('moumou.config.LLMSettings.from_env', forbidden), \\
     patch('moumou.adapters.DeepSeekClient', forbidden), \\
     patch('moumou.adapters.llm.DeepSeekClient', forbidden), \\
     patch('httpx.AsyncClient', forbidden):
    runpy.run_module(module, run_name='__main__')
"""


def offline_command(module, *arguments):
    environment = os.environ.copy()
    for key in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "MOUMOU_EVAL_API_KEY"):
        environment.pop(key, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), environment.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-c", OFFLINE_LAUNCH, module, *map(str, arguments)],
        cwd=ROOT, env=environment, capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )



def test_primary_defaults_use_instructor_sessions_and_real_human():
    args = cli.parse_args([])
    assert args.tasks == "eval/tasks"
    assert args.scenario == "all"
    assert args.human == "console"
    assert args.search_requests == 6
    assert args.max_tokens > 150000


@pytest.mark.parametrize("task_count", [1, 2])
def test_plan_never_loads_credentials_or_runs_an_event_loop(monkeypatch, capsys, task_count):
    from moumou.config import LLMSettings
    from moumou.eval import cases

    def forbidden(*args, **kwargs):
        raise AssertionError("plan attempted a runtime operation")

    case = SimpleNamespace(
        id="instructor-case",
        name="Instructor case",
        roles=[SimpleNamespace(name=name) for name in ["lead", "dev", "qa", "release"]],
        source_sha256={"task1": "a" * 64},
        tasks=tuple(f"Task {index}" for index in range(task_count)),
        source_kind="instructor",
    )
    monkeypatch.setattr(cases, "load_cases", lambda path: [case])
    monkeypatch.setattr(LLMSettings, "from_env", forbidden)
    monkeypatch.setattr(cli.asyncio, "run", forbidden)
    assert cli.main(["--plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "instructor_test"
    assert result["planned_root_goals"] == task_count
    assert result["dataset_kind"] == "instructor"
    assert result["scenarios"][0]["roles"] == ["lead", "dev", "qa", "release"]


def test_missing_scenario_is_an_error_instead_of_starting_a_different_task(monkeypatch, capsys):
    from moumou.eval import cases

    monkeypatch.setattr(cases, "load_cases", lambda path: [])
    assert cli.main(["--plan", "--scenario", "missing"]) == 2
    assert "not found" in capsys.readouterr().err


def test_primary_and_evaluation_module_help_both_describe_instructor_scenarios():
    root = Path(__file__).resolve().parents[1]
    for module, expected in [("moumou", "instructor"), ("moumou.eval.cli", "instructor")]:
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
    result = cli._task_record(SimpleNamespace(repo=repo, trace=trace), current, 12.5, "task_2")
    assert {row["id"] for row in result["nodes"]} == {"t2", "t2.1", "t2.2"}
    assert result["observed_subtasks"] == 2
    assert result["observed_subtasks_done"] == 1
    assert result["elapsed_s"] == 12.5
    assert result["agent_declared_complete"] is True
    assert result["root_machine_accepted"] is None
    assert result["verified_goal_success"] is None


@pytest.mark.parametrize("module", MODULES)
def test_public_help_needs_no_credentials_client_or_event_loop(module):
    result = offline_command(module, "--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()


@pytest.mark.parametrize("module", ["moumou", "moumou.eval.cli"])
def test_all_formal_entry_plans_use_three_original_teams_without_runtime(module, tmp_path):
    output = tmp_path / "never-created"
    result = offline_command(module, "--plan", "--out", output)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["kind"] == "instructor_test" and plan["planned_root_goals"] == 6
    assert [row["id"] for row in plan["scenarios"]] == [
        "scenario_1", "scenario_2", "scenario_3",
    ]
    assert all(len(row["roles"]) == 4 for row in plan["scenarios"])
    assert not output.exists()


def test_supplementary_bank_uses_the_same_entry_only_when_explicitly_selected():
    result = offline_command("moumou.eval.cli", "--tasks", "eval/supplementary", "--plan")
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["kind"] == "instructor_test" and plan["dataset_kind"] == "supplementary"
    assert len(plan["scenarios"]) == 8 and plan["planned_root_goals"] == 15
    assert [case["task_count"] for case in plan["scenarios"]] == [2] * 7 + [1]
    assert all(case["source_kind"] == "supplementary" for case in plan["scenarios"])


@pytest.mark.parametrize("module,arguments", [
    ("moumou.eval.cli", ["--ablation", "--plan"]),
    ("moumou.eval.cli", ["--cases", "unused/path", "--plan"]),
    ("moumou.eval.cli", ["--repeats", "1"]),
    ("moumou.eval.cli", ["--tasks", "missing-evaluation-source", "--plan"]),
    ("moumou.eval.cli", ["--search-requests", "-1", "--plan"]),
    ("moumou.eval.report", []),
    ("moumou.eval.report", ["--run"]),
    ("moumou.eval.report", ["--run", ""]),
    ("moumou.eval.freeze", ["prepare", "--out", "never-created", "--model-config", "x.json"]),
])
def test_invalid_or_retired_arguments_fail_before_runtime(module, arguments):
    result = offline_command(module, *arguments)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "offline entry point attempted" not in result.stderr


def test_retired_ablation_command_rejects_execution_without_environment_or_client():
    result = offline_command("moumou.eval.ablation", "--plan")
    assert result.returncode == 2
    assert "retired" in result.stderr and "moumou.eval.cli" in result.stderr


def make_recipe(name):
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:\s*\n((?:\t[^\n]*\n?)+)", text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def test_formal_make_test_and_eval_share_one_instructor_entrypoint():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^EVAL_PY\s*\?=\s*python\s*$", text, re.MULTILINE)
    assert re.search(r"^test:\s*eval\s*$", text, re.MULTILINE)
    assert "-m moumou.eval.cli --tasks eval/tasks $(EVAL_ARGS)" in make_recipe("eval")
    assert '"$(EVAL_PY)" -m pytest $(UNIT_TEST_ARGS)' in make_recipe("unit-test")
    assert '-m moumou.eval.report --run "$(REPORT_RUN)" $(REPORT_ARGS)' in make_recipe("report")
    assert not re.search(r"^ablation(?:-plan)?:", text, re.MULTILINE)
    phony = re.search(r"^\.PHONY:\s*(.*)$", text, re.MULTILINE).group(1).split()
    assert {"eval", "test", "unit-test", "report"} <= set(phony)


def test_make_changes_preserve_original_install_and_cleanup_targets():
    assert make_recipe("install") == (
        '\t@test -d .venv || uv venv .venv\n\tuv pip install -e ".[dev]"\n'
    )
    assert make_recipe("clean") == (
        "\trm -rf runs .pytest_cache .ruff_cache\n"
        "\tfind . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} +\n"
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="optional GNU Make is not installed")
def test_make_test_and_eval_dry_runs_use_identical_commands():
    commands = []
    for target in ("eval", "test"):
        result = subprocess.run(
            [shutil.which("make"), "-n", target, "EVAL_PY=python", "EVAL_ARGS=--plan"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert result.returncode == 0, result.stderr
        commands.append(result.stdout.strip())
    assert commands[0] == commands[1]
    assert "-m moumou.eval.cli --tasks eval/tasks --plan" in commands[0]


@pytest.mark.skipif(shutil.which("make") is None, reason="optional GNU Make is not installed")
@pytest.mark.parametrize("target,arguments,expected", [
    ("unit-test", [], "-m pytest -q"),
    ("report", ["REPORT_RUN=runs/saved"], '-m moumou.eval.report --run "runs/saved"'),
])
def test_make_backend_and_report_dry_runs_preserve_their_purpose(target, arguments, expected):
    result = subprocess.run(
        [shutil.which("make"), "-n", target, "EVAL_PY=python", *arguments],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
