"""P1 execution tests use the active interpreter and observable child lifetimes."""

import asyncio
import json
import os
import shlex
import subprocess
import sys

import pytest

from moumou.contracts import ToolContext
from moumou.tools import default_tools


def command(args):
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            # Linux zombies have exited but await reaping by their adopted parent.
            stat = __import__("pathlib").Path(f"/proc/{pid}/stat")
            return not (stat.exists() and stat.read_text().split()[2] == "Z")
        except ProcessLookupError:
            return False
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
    kernel.CloseHandle(ctypes.c_void_p(handle))
    return code.value == 259


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(agent_id="a", task_id="t", workspace=tmp_path, env={"P1_TEST": "works"})


@pytest.fixture
def tools():
    return {t.name: t for t in default_tools()}


async def test_python_snippet_uses_environment_and_reports_artifact(tools, ctx):
    result = await tools["run_python"].run(
        {"code": "import os; open('out.txt','w').write(os.environ['P1_TEST']); print('ok')"}, ctx
    )
    assert result.ok, result
    assert result.stdout.strip() == "ok"
    assert result.artifacts == ["out.txt"]
    assert (ctx.workspace / "out.txt").read_text() == "works"
    assert not list(ctx.workspace.glob(".p1-*.py"))


async def test_both_output_streams_are_drained_and_bounded(tools, ctx):
    result = await tools["run_python"].run(
        {"code": "import sys; sys.stdout.write('中'*100000); sys.stderr.write('e'*100000)"}, ctx
    )
    assert result.ok and result.truncated
    assert len(result.stdout.encode()) <= 8204
    assert len(result.stderr.encode()) <= 8204
    assert "\ufffd" not in result.stdout


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "bad"])
async def test_invalid_timeout_never_executes(tools, ctx, timeout):
    cmd = command([sys.executable, "-c", "open('bad.txt','w').write('bad')"])
    result = await tools["shell"].run({"command": cmd, "timeout_s": timeout}, ctx)
    assert not result.ok
    assert not (ctx.workspace / "bad.txt").exists()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancel_cleans_descendant_tree(tools, ctx, cancel):
    child_script = "import time; time.sleep(60)"
    parent = (
        "import subprocess,sys,time,json;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
        "open('pids.json','w').write(json.dumps([__import__('os').getpid(),p.pid]));"
        "time.sleep(60)"
    )
    task = asyncio.create_task(
        tools["shell"].run(
            {"command": command([sys.executable, "-c", parent]), "timeout_s": 2}, ctx
        )
    )
    pid_file = ctx.workspace / "pids.json"
    try:
        for _ in range(100):
            if pid_file.exists() and pid_file.stat().st_size:
                break
            if task.done():
                pytest.fail(f"process exited before PID announcement: {task.result()}")
            await asyncio.sleep(0.02)
        pids = json.loads(pid_file.read_text())
        assert all(alive(pid) for pid in pids)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert not result.ok and "timeout" in result.error.lower()
        for _ in range(100):
            if not any(alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.02)
        assert not any(alive(pid) for pid in pids)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def test_completed_parent_cleans_background_child_before_stream_eof(tools, ctx):
    script = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "print('parent completed',flush=True)"
    )
    result = await tools["shell"].run(
        {"command": command([sys.executable, "-c", script]), "timeout_s": 2}, ctx
    )
    assert result.ok, result
    assert result.stdout.strip() == "parent completed"


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launch containment")
async def test_windows_process_cannot_run_before_job_attachment(ctx, monkeypatch):
    import time

    from moumou.sandbox import winjob
    from moumou.sandbox.process import run_command

    marker = ctx.workspace / "started.txt"
    original = winjob.Job

    class DelayedJob:
        def __init__(self, pid):
            time.sleep(0.3)
            assert not marker.exists(), "process ran before assignment to its Job"
            self.inner = original(pid)

        def resume(self, pid):
            self.inner.resume(pid)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(winjob, "Job", DelayedJob)
    result = await run_command(
        [sys.executable, "-c", "open('started.txt','w').write('started')"],
        ctx.workspace,
        2,
    )
    assert result.ok, result
    assert marker.read_text() == "started"
