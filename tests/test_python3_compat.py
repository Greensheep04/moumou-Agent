"""Native Windows checks for child-scoped python3 compatibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import TraceEvent
from moumou.eval.loader import load_task
from moumou.eval.runner import run_once
from moumou.tools.builtin import run_process

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows python3 alias")


@pytest.mark.parametrize("exit_code", [0, 7])
async def test_python3_preserves_script_path_arguments_literals_and_results(tmp_path, exit_code):
    script = tmp_path / "literal python3 script.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'exe': sys.executable, 'literal': 'python3', 'args': sys.argv[1:]}))\n"
        "print('diagnostic', file=sys.stderr)\n"
        "raise SystemExit(int(sys.argv[1]))\n",
        encoding="utf-8",
    )
    command = subprocess.list2cmdline(
        ["python3", str(script), str(exit_code), "argument with spaces"]
    )

    result = await run_process(command, tmp_path, 5)

    assert result.ok is (exit_code == 0)
    assert result.exit_code == exit_code
    assert result.stderr.strip() == "diagnostic"
    assert json.loads(result.stdout) == {
        "exe": sys.executable,
        "literal": "python3",
        "args": [str(exit_code), "argument with spaces"],
    }


@pytest.mark.parametrize("mode", ["-c", "-m"])
async def test_python3_supports_code_and_module_arguments(tmp_path, mode):
    code = "import json, sys; print(json.dumps([sys.executable, sys.argv[1]]))"
    if mode == "-m":
        (tmp_path / "compat_module.py").write_text(code, encoding="utf-8")
        program = "compat_module"
    else:
        program = code
    command = subprocess.list2cmdline(["python3", mode, program, "spaced value"])

    result = await run_process(command, tmp_path, 5)

    assert result.ok, result
    assert json.loads(result.stdout) == [sys.executable, "spaced value"]


async def test_python3_commands_keep_shell_chaining(tmp_path):
    command = 'python3 -c "print(\'first\')" && python3 -c "print(\'second\')"'

    result = await run_process(command, tmp_path, 5)

    assert result.ok, result
    assert result.stdout.splitlines() == ["first", "second"]


async def test_nested_shell_inherits_python3_mapping(tmp_path):
    script = tmp_path / "nested shell.py"
    script.write_text(
        "import json, subprocess\n"
        "command = 'python3 -c \"import sys; print(sys.executable)\"'\n"
        "result = subprocess.run(command, shell=True, capture_output=True, text=True)\n"
        "print(json.dumps([result.returncode, result.stdout.strip(), result.stderr]))\n",
        encoding="utf-8",
    )

    result = await run_process(subprocess.list2cmdline(["python3", str(script)]), tmp_path, 5)

    assert result.ok, result
    assert json.loads(result.stdout) == [0, sys.executable, ""]


async def test_python3_mapping_does_not_mutate_parent_environment_and_is_removed(tmp_path):
    names = ("PATH", "PATHEXT", "MOUMOU_PYTHON_EXECUTABLE")
    before = {name: os.environ.get(name) for name in names}
    script = tmp_path / "inspect alias.py"
    script.write_text(
        "import json, shutil, sys\n"
        "print(json.dumps([sys.executable, shutil.which('python3')]))\n",
        encoding="utf-8",
    )

    result = await run_process(subprocess.list2cmdline(["python3", str(script)]), tmp_path, 5)

    assert result.ok, result
    executable, shim = json.loads(result.stdout)
    assert executable == sys.executable
    assert Path(shim).name.lower() == "python3.cmd"
    assert not Path(shim).parent.exists(), "temporary compatibility directory leaked"
    assert {name: os.environ.get(name) for name in names} == before


@pytest.mark.parametrize("fixture_id", ["s1_hello", "s2_bugfix"])
async def test_existing_python3_fixtures_run_without_rewriting_them(tmp_path, fixture_id):
    source = Path(__file__).resolve().parents[1] / "eval/tasks" / f"{fixture_id}.yaml"
    original = source.read_bytes()
    fixture = load_task(source)
    command = next(item.target for item in fixture.acceptance if item.kind == "command")
    assert command.startswith("python3 ")

    if fixture_id == "s1_hello":
        filename = "hello.py"
        content = "print('hello, moumou')\n"
        actions = [
            ToolCall(id="write", name="write_file", args={"path": filename, "content": content}),
            ToolCall(id="verify", name="shell", args={"command": command}),
        ]
    else:
        filename = "calc.py"
        content = (
            "def add(a, b):\n    return a + b\n\n\n"
            "if __name__ == '__main__':\n    print(add(2, 3))\n"
        )
        actions = [
            ToolCall(id="read", name="read_file", args={"path": filename}),
            ToolCall(id="write", name="write_file", args={"path": filename, "content": content}),
            ToolCall(id="verify", name="shell", args={"command": command}),
        ]

    def respond(messages, tools):
        turn = sum(message["role"] == "assistant" for message in messages)
        if turn < len(actions):
            return LLMResponse(tool_calls=[actions[turn]])
        return LLMResponse(content="Completed and verified the fixture")

    result = await run_once(fixture, FakeLLM(respond), tmp_path / "evaluation", repeat=0)

    assert result.total == result.done == 1
    assert result.dead == 0 and result.errors == []
    assert result.completion_rate == 1.0
    workspace = Path(result.run_dir) / "workspaces" / fixture_id / "c1" / "planner@t1"
    assert (workspace / filename).read_text(encoding="utf-8") == content
    assert source.read_bytes() == original


async def test_bugfix_fixture_rejects_a_success_claim_without_the_fix(tmp_path):
    source = Path(__file__).resolve().parents[1] / "eval/tasks/s2_bugfix.yaml"
    original = source.read_bytes()
    fixture = load_task(source)
    llm = FakeLLM(lambda messages, tools: LLMResponse(content="Fixed and verified everything"))

    result = await run_once(fixture, llm, tmp_path / "evaluation", repeat=0)

    assert result.total == result.dead == 1
    assert result.done == 0 and result.completion_rate == 0.0
    assert result.errors
    run_dir = Path(result.run_dir)
    workspace = run_dir / "workspaces" / fixture.id / "c1" / "planner@t1"
    assert (workspace / "calc.py").read_text(encoding="utf-8") == fixture.setup["calc.py"]
    events = [
        TraceEvent.model_validate_json(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    checks = [
        e.payload
        for e in events
        if e.kind == "acceptance" and e.payload.get("kind") == "command"
    ]
    assert checks, "the original Python command acceptance must actually run"
    assert all(
        not item["ok"] and item["exit_code"] == 1 and "AssertionError" in item["stderr"]
        for item in checks
    )
    assert source.read_bytes() == original
