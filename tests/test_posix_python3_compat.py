"""POSIX shells must use the active interpreter beyond the first command."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from moumou.sandbox.process import run_command

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell alias behavior")


def wrong_python(directory: Path) -> Path:
    directory.mkdir()
    executable = directory / "python3"
    executable.write_text("#!/bin/sh\nprintf 'wrong-python\\n'\nexit 23\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize("prefix", ["", "echo ready && ", "echo ready &&    "])
async def test_posix_python3_alias_overrides_wrong_path_without_rewriting_literals(
    tmp_path, prefix
):
    trap = wrong_python(tmp_path / "wrong interpreter path")
    code = "import json,sys; print(json.dumps([sys.executable, sys.argv[1]]))"
    argv = ["python3", "-c", code, "literal python3 and spaced argument"]
    result = await run_command(prefix + shlex.join(argv), tmp_path, 5, {"PATH": str(trap.parent)})

    assert result.ok, result
    executable, argument = json.loads(result.stdout.splitlines()[-1])
    assert executable == sys.executable
    assert argument == "literal python3 and spaced argument"
    assert trap.read_text(encoding="utf-8").endswith("exit 23\n")


async def test_posix_python3_alias_is_inherited_by_nested_shell_and_released(tmp_path):
    trap = wrong_python(tmp_path / "wrong interpreter path")
    before = {name: os.environ.get(name) for name in ("PATH", "MOUMOU_PYTHON_EXECUTABLE")}
    nested = shlex.join(["python3", "-c", "import sys; print(sys.executable)"])
    script = tmp_path / "nested probe.py"
    script.write_text(
        "import json,shutil,subprocess,sys\n"
        f"result = subprocess.run({nested!r}, shell=True, capture_output=True, text=True)\n"
        "print(json.dumps([sys.executable, shutil.which('python3'), "
        "result.returncode, result.stdout.strip(), result.stderr]))\n",
        encoding="utf-8",
    )
    result = await run_command(
        shlex.join([sys.executable, str(script)]),
        tmp_path, 5, {"PATH": str(trap.parent)},
    )

    assert result.ok, result
    current, alias, code, output, stderr = json.loads(result.stdout.splitlines()[-1])
    assert current == output == sys.executable
    assert code == 0 and stderr == ""
    assert Path(alias) != trap
    assert not Path(alias).parent.exists(), "temporary interpreter alias leaked"
    assert {name: os.environ.get(name) for name in before} == before


async def test_posix_argv_execution_keeps_the_explicit_program(tmp_path):
    trap = wrong_python(tmp_path / "explicit program")
    result = await run_command([str(trap)], tmp_path, 5)

    assert not result.ok and result.exit_code == 23
    assert result.stdout.strip() == "wrong-python"
