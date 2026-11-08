"""Regression coverage for the P1/main integration."""

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from moumou.contracts import Acceptance, ToolContext
from moumou.tools import default_tools


@pytest.mark.parametrize("prefix", ["", "echo ready && ", "echo ready &&    "])
async def test_python3_shell_alias_uses_project_interpreter(tmp_path, prefix):
    # A PATH without Python makes this independent of machine Python aliases.
    alias_path = tmp_path / "path with spaces"
    alias_path.mkdir()
    script = "import sys; print(sys.executable); print('literal python3 text')"
    args = ["python3", "-c", script]
    cmd = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
    env = {"PATH": str(alias_path)} if os.name == "nt" else {}
    tools = {tool.name: tool for tool in default_tools()}
    result = await tools["shell"].run(
        {"command": prefix + cmd},
        ToolContext(agent_id="a", task_id="t", workspace=tmp_path, env=env),
    )
    assert result.ok, result
    executable = result.stdout.strip().splitlines()[-2]
    assert Path(executable).resolve() == Path(sys.executable).resolve()
    assert "literal python3 text" in result.stdout
    assert not result.artifacts


async def test_pytest_acceptance_quotes_target_with_spaces(tmp_path):
    from moumou.contracts import TaskNode
    from moumou.orchestrator.acceptance import AcceptanceChecker

    target = tmp_path / "tests with spaces"
    target.mkdir()
    (target / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    task = TaskNode(
        id="t", project_id="p", conversation_id="c", goal="test", role="r",
        acceptance=[Acceptance(kind="pytest", target=target.name, timeout_s=30)],
    )
    ok, detail = await AcceptanceChecker().check(task, tmp_path)
    assert ok, detail
    assert "1 passed" in detail[0]["summary"]


async def test_git_delivery_and_acceptance_are_persisted_in_sqlite(tmp_path):
    from moumou.adapters import FakeLLM, GitWorkspaces, ScriptedHuman
    from moumou.demo.skeleton import responder
    from moumou.orchestrator import Engine, Settings
    from moumou.store import SqliteTraceStore
    from moumou.trace import Recorder

    root = tmp_path / "run"
    workspace = GitWorkspaces(tmp_path / "workspaces")
    with Engine(
        FakeLLM(responder), workspace=workspace, human=ScriptedHuman(),
        settings=Settings(root=root, project_id="p", conversation_id="c",
                          memory_enabled=False, role_review_enabled=False),
    ) as engine:
        task = engine.submit(
            "build hello",
            acceptance=[Acceptance(kind="file_exists", target="hello.py")],
        )
        report = await engine.run()
        assert report.done == 3 and report.dead == 0
        journal = {e.span_id: e for e in engine.trace.all_events()}
        assert any(e.name == "integrated_into_parent" for e in journal.values())
        assert any(e.kind == "acceptance" and e.payload["ok"] for e in journal.values())
        with SqliteTraceStore(root / "trace.db") as store:
            persisted = {e.span_id: e for e in store.query(task.id)}
            assert persisted == {k: e for k, e in journal.items() if e.task_id == task.id}
            assert persisted
        assert engine.trace.projection_error is None

    # Reopening imports the journal idempotently and preserves every task event.
    with Recorder(root / "trace.jsonl", sqlite_path=root / "trace.db") as trace:
        assert {e.span_id: e for e in trace.query(task.id)} == persisted


def test_trace_projection_recovers_after_runtime_failure(tmp_path, monkeypatch):
    from moumou.contracts import TraceEvent
    from moumou.store import SqliteTraceStore
    from moumou.trace import Recorder

    journal, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    first = TraceEvent(task_id="t", kind="tool", name="before")
    second = TraceEvent(task_id="t", kind="tool", name="during")
    with Recorder(journal, sqlite_path=database) as recorder:
        recorder.append(first)

        def fail(self, event):
            raise OSError("projection unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(SqliteTraceStore, "append", fail)
            recorder.append(second)
        assert isinstance(recorder.projection_error, OSError)
        assert {e.span_id for e in recorder.query("t")} == {first.span_id, second.span_id}
    with Recorder(journal, sqlite_path=database) as recorder:
        assert recorder.projection_error is None
        assert {e.span_id for e in recorder.query("t")} == {first.span_id, second.span_id}


async def test_cancellation_during_spawn_waits_for_owned_process(tmp_path, monkeypatch):
    from moumou.sandbox.process import run_command

    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(run_command(
        [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, 5,
    ))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done(), "cancellation returned before retrieving the owned process"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()


async def test_repeated_cancellation_waits_for_cleanup(tmp_path, monkeypatch):
    from moumou.sandbox.process import run_command

    original = asyncio.create_subprocess_exec
    spawned, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    processes = []

    async def tracked_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        wait = proc.wait

        async def delayed_wait():
            cleaning.set()
            await release.wait()
            return await wait()

        proc.wait = delayed_wait
        spawned.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked_spawn)
    task = asyncio.create_task(run_command(
        [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, 5,
    ))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        await asyncio.sleep(0.03)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done(), "second cancellation escaped before cleanup finished"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
