"""Published P3 commands must keep help, plans and report validation offline."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("moumou.eval.cli", "moumou.eval.report", "moumou.eval.ablation")

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


@pytest.mark.parametrize("module", MODULES)
def test_public_entry_point_help_needs_no_model_environment_or_event_loop(module):
    result = offline_command(module, "--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()


@pytest.mark.parametrize("module,extra", [
    ("moumou.eval.cli", ["--ablation"]),
    ("moumou.eval.ablation", []),
])
def test_ablation_plan_is_synchronous_and_creates_no_output(module, extra, tmp_path):
    output = tmp_path / "not-created"
    result = offline_command(module, *extra, "--plan", "--only", "s1_hello",
                             "--repeats", "1", "--out", output)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["planned_runs"] == 4 and len(plan["groups"]) == 4
    assert not output.exists()


@pytest.mark.parametrize("module,arguments", [
    ("moumou.eval.report", []),
    ("moumou.eval.report", ["--run"]),
    ("moumou.eval.report", ["--run", ""]),
    ("moumou.eval.cli", ["--plan"]),
    ("moumou.eval.cli", ["--repeats", "0"]),
    ("moumou.eval.ablation", ["--repeats", "0"]),
    ("moumou.eval.ablation", ["--plan", "--only", "does-not-exist"]),
])
def test_invalid_pure_arguments_fail_without_model_or_event_loop(module, arguments):
    result = offline_command(module, *arguments)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "offline entry point attempted" not in result.stderr


def make_recipe(name):
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:\s*\n((?:\t[^\n]*\n?)+)", text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def test_make_p3_targets_use_the_activated_interpreter_and_default_to_a_dry_plan():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^EVAL_PY\s*\?=\s*python\s*$", text, re.MULTILINE)
    assert re.search(r"^ABLATION_ARGS\s*\?=\s*--plan\s*$", text, re.MULTILINE)
    assert "-m moumou.eval.cli --tasks eval/tasks $(EVAL_ARGS)" in make_recipe("eval")
    assert '-m moumou.eval.report --run "$(REPORT_RUN)" $(REPORT_ARGS)' in make_recipe("report")
    assert "-m moumou.eval.ablation $(ABLATION_ARGS)" in make_recipe("ablation")
    assert "-m moumou.eval.ablation --plan" in make_recipe("ablation-plan")
    phony = re.search(r"^\.PHONY:\s*(.*)$", text, re.MULTILINE).group(1).split()
    assert {"eval", "report", "ablation", "ablation-plan"} <= set(phony)


def test_make_p3_changes_preserve_original_install_and_cleanup_targets():
    assert make_recipe("install") == (
        '\t@test -d .venv || uv venv .venv\n\tuv pip install -e ".[dev]"\n'
    )
    assert make_recipe("clean") == (
        "\trm -rf runs .pytest_cache .ruff_cache\n"
        "\tfind . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} +\n"
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="optional GNU Make is not installed")
@pytest.mark.parametrize("target,arguments,module", [
    ("report", ["REPORT_RUN=runs/saved", "REPORT_ARGS=--out runs/rebuilt.md"], "report"),
    ("ablation", [], "ablation"),
    ("ablation-plan", [], "ablation"),
])
def test_make_dry_run_exposes_real_commands_when_make_is_available(target, arguments, module):
    result = subprocess.run(
        [shutil.which("make"), "-n", target, "EVAL_PY=python", *arguments],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"-m moumou.eval.{module}" in result.stdout
    if target.startswith("ablation"):
        assert "--plan" in result.stdout
