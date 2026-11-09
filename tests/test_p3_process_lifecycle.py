"""Local P3 lifecycle regressions alongside the team's public tool contracts.

Keep these behavior checks independent of the public contract test module.
The P1 executor owns process containment; these tests observe its effects.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from moumou.contracts import ToolContext
from moumou.tools import default_tools
from moumou.tools.builtin import python_command


@pytest.fixture(scope="module")
def tools() -> dict:
    return {t.name: t for t in default_tools()}


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    return ToolContext(agent_id="a@t1", task_id="t1", workspace=tmp_path)


def process_is_running(pid: int) -> bool:
    """Observe process state without sending a signal on Windows."""
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:                  # ERROR_INVALID_PARAMETER: no such PID
                return False
            raise ctypes.WinError(error)
        try:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return state == 258             # WAIT_TIMEOUT: process has not exited
        finally:
            kernel32.CloseHandle(handle)

    # A killed orphan may briefly remain as a zombie on Linux. It cannot run
    # code or retain pipes, so treat that state as terminated as well.
    stat = Path(f"/proc/{pid}/stat")
    try:
        if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except FileNotFoundError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def sleeping_process_tree(workspace: Path) -> tuple[Path, tuple[Path, Path]]:
    """The shell runs a Python parent which launches a pipe-inheriting child."""
    descendant = workspace / "tree descendant.py"
    descendant.write_text(
        "import os, time\nfrom pathlib import Path\n"
        "Path('grandchild.pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = workspace / "tree parent.py"
    parent.write_text(
        "import os, shutil, subprocess, sys\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, {str(descendant)!r}])\n"
        "Path('alias.path').write_text(shutil.which('python3') or '', encoding='utf-8')\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        "child.wait()\n",
        encoding="utf-8",
    )
    return parent, (workspace / "child.pid", workspace / "grandchild.pid")


def read_pids(paths: tuple[Path, Path]) -> list[int]:
    pids = []
    for path in paths:
        try:
            pids.append(int(path.read_text().strip()))
        except (FileNotFoundError, ValueError):
            continue
    return pids


async def wait_for_running_tree(task, paths: tuple[Path, Path]) -> list[int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pids = read_pids(paths)
        if len(pids) == 2:
            assert all(process_is_running(pid) for pid in pids), "tree exited before the check"
            return pids
        if task.done():
            pytest.fail(f"command finished before both processes started: {task.result()}")
        await asyncio.sleep(0.02)
    pytest.fail("the Python parent and descendant did not both report their PIDs")


async def assert_tree_stopped(pids: list[int]) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if not any(process_is_running(pid) for pid in pids):
            return
        await asyncio.sleep(0.02)
    assert not any(process_is_running(pid) for pid in pids), "a process outlived the tool"


def alias_environment() -> dict[str, str | None]:
    return {
        name: os.environ.get(name)
        for name in ("PATH", "PATHEXT", "MOUMOU_PYTHON_EXECUTABLE")
    }


def assert_invocation_released(workspace: Path, before: dict[str, str | None]) -> None:
    assert alias_environment() == before, "the command changed its caller's environment"
    if sys.platform == "win32":
        alias = Path((workspace / "alias.path").read_text(encoding="utf-8"))
        assert alias.name.lower() == "python3.cmd"
        assert not alias.parent.exists(), "the temporary Python alias outlived the command"


async def cleanup_test_tree(task, paths: tuple[Path, Path]) -> None:
    """Only a failed test reaches live PIDs; assertions run before this fallback."""
    if not task.done():
        task.cancel()
    for pid in reversed(read_pids(paths)):
        if not process_is_running(pid):
            continue
        if sys.platform == "win32":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
            subprocess.run(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        await asyncio.wait_for(task, timeout=3)
    except (asyncio.CancelledError, TimeoutError):
        pass


async def test_shell_timeout_releases_tree_and_invocation(tools, ctx):
    script, paths = sleeping_process_tree(ctx.workspace)
    before = alias_environment()
    started = time.monotonic()
    task = asyncio.create_task(
        tools["shell"].run({"command": python_command(str(script)), "timeout_s": 2}, ctx)
    )
    try:
        pids = await wait_for_running_tree(task, paths)
        result = await asyncio.wait_for(asyncio.shield(task), timeout=4)
        elapsed = time.monotonic() - started
        assert not result.ok
        assert result.error and "timeout" in result.error.lower()
        assert elapsed < 5, "the tool waited for descendants instead of killing them"
        await assert_tree_stopped(pids)
        assert_invocation_released(ctx.workspace, before)
    finally:
        await cleanup_test_tree(task, paths)


async def test_cancel_releases_tree_and_invocation(tools, ctx):
    """An abort must terminate both the Python command and its descendant."""
    script, paths = sleeping_process_tree(ctx.workspace)
    before = alias_environment()
    task = asyncio.create_task(
        tools["shell"].run({"command": python_command(str(script)), "timeout_s": 60}, ctx)
    )
    try:
        pids = await wait_for_running_tree(task, paths)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=4)
        assert time.monotonic() - started < 5, "cancellation did not finish promptly"
        await assert_tree_stopped(pids)
        assert_invocation_released(ctx.workspace, before)
    finally:
        await cleanup_test_tree(task, paths)


async def test_cancel_during_spawn_releases_suspended_process_and_alias(tools, ctx, monkeypatch):
    script, paths = sleeping_process_tree(ctx.workspace)
    before = alias_environment()
    created, release = asyncio.Event(), asyncio.Event()
    processes, alias_dirs = [], []
    original_spawn = asyncio.create_subprocess_shell

    async def delayed_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        if sys.platform == "win32":
            alias_dir = Path(kwargs["env"]["PATH"].split(os.pathsep)[0])
            assert (alias_dir / "python3.cmd").is_file()
            alias_dirs.append(alias_dir)
        created.set()
        await release.wait()  # The OS process exists; the executor has not received its handle.
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", delayed_spawn)
    task = asyncio.create_task(
        tools["shell"].run({"command": python_command(str(script)), "timeout_s": 60}, ctx)
    )
    try:
        await asyncio.wait_for(created.wait(), timeout=5)
        assert process_is_running(processes[0].pid)
        if sys.platform == "win32":
            # P1 starts the root suspended, then attaches the Job before resuming it.
            assert not any(path.exists() for path in paths)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation returned before retrieving the owned process"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=4)
        assert processes[0].returncode is not None
        await assert_tree_stopped([processes[0].pid, *read_pids(paths)])
        if sys.platform == "win32":
            assert not any(path.exists() for path in paths), "cancelled code was resumed"
        assert alias_environment() == before
        assert all(not directory.exists() for directory in alias_dirs)
    finally:
        release.set()
        await cleanup_test_tree(task, paths)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=3)


async def test_second_cancel_waits_for_tree_and_invocation_cleanup(tools, ctx, monkeypatch):
    script, paths = sleeping_process_tree(ctx.workspace)
    before = alias_environment()
    cleaning, release = asyncio.Event(), asyncio.Event()
    original_spawn = asyncio.create_subprocess_shell

    async def tracked_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        original_wait = process.wait

        async def delayed_wait():
            cleaning.set()
            await release.wait()
            return await original_wait()

        process.wait = delayed_wait
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", tracked_spawn)
    task = asyncio.create_task(
        tools["shell"].run({"command": python_command(str(script)), "timeout_s": 60}, ctx)
    )
    try:
        pids = await wait_for_running_tree(task, paths)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "a second cancellation abandoned process cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=4)
        await assert_tree_stopped(pids)
        assert_invocation_released(ctx.workspace, before)
    finally:
        release.set()
        await cleanup_test_tree(task, paths)


@pytest.mark.parametrize("exit_code", [0, 7])
async def test_shell_preserves_output_and_exit_code_with_current_python(tools, ctx, exit_code):
    script = ctx.workspace / "script with spaces.py"
    script.write_text(
        "import sys\nprint(sys.executable)\nprint('diagnostic', file=sys.stderr)\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    result = await tools["shell"].run(
        {"command": python_command(str(script)), "timeout_s": 5}, ctx
    )
    assert result.ok is (exit_code == 0)
    assert result.exit_code == exit_code
    assert result.stdout.strip() == sys.executable
    assert result.stderr.strip() == "diagnostic"
