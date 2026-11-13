"""Real native-Windows whole-attempt deadlines, containment and result acceptance."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from moumou.adapters import DeepSeekClient, FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.contracts import Acceptance, Budget, TraceEvent
from moumou.eval import isolation
from moumou.eval import process as supervisor
from moumou.eval.artifacts import load_results
from moumou.eval.cli import parse_args
from moumou.eval.loader import TaskFixture
from moumou.eval.runner import run_all

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job containment")
FACTORY = "eval_worker_fixtures:make_client"
DEADLINE = 3.0


def fixture(name="sample", mode="normal"):
    return TaskFixture(
        id=name, title="Supervised offline attempt", scenario="from_scratch", goal=mode,
        setup={"answer.txt": "correct"},
        acceptance=[Acceptance(kind="file_contains", target="answer.txt", expect="^correct$")],
        budget=Budget(max_steps=3, max_retries=0, max_tokens=1000),
    )


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def running(pid: int) -> bool:
    """Treat observation failures as failures, never as proof that a process exited."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return False
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return state == 258
    finally:
        kernel.CloseHandle(handle)


def pids_in(run_dir):
    pids = set()
    for name in ("probe-pids.json", "descendants.json", "nested-pids.json"):
        path = Path(run_dir) / name
        if path.is_file():
            pids.update(read_json(path).values())
    return pids


def stopped(pids):
    assert pids, "the worker probe did not announce any process"
    assert all(type(pid) is int and pid > 0 for pid in pids), "invalid probe process identity"
    assert not any(running(pid) for pid in pids), "an owned process survived containment cleanup"


def kill_failed_probe(pids):
    """Fallback is after assertions and affects only PIDs recorded by this test."""
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
    for pid in pids:
        if type(pid) is not int or pid <= 0:
            continue
        if running(pid):
            subprocess.run(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=5, check=False,
            )


async def wait_marker(path, task, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return read_json(path)
        if task.done():
            pytest.fail(f"batch ended before the probe started: {task.result()}")
        await asyncio.sleep(0.02)
    pytest.fail("worker did not publish its startup marker")


async def run_probes(tasks, root, timeout=DEADLINE):
    return await run_all(
        tasks, None, root, hard_timeout_s=timeout, worker_factory=FACTORY,
    )


@WINDOWS
async def test_queued_fake_model_runs_in_a_distinct_reaped_process(tmp_path):
    fake = FakeLLM([LLMResponse(content="ready", tokens_in=7, tokens_out=3)])
    result, = await run_all([fixture()], fake, tmp_path, hard_timeout_s=DEADLINE)
    assert result.status == "succeeded" and result.root_success is True
    assert result.execution_mode == "process" and result.hard_timeout_s == DEADLINE
    assert result.worker_pid != os.getpid()
    assert result.worker_exit_code == 0 and result.cleanup_complete is True
    assert not running(result.worker_pid)
    assert fake.calls == [], "the parent's FakeLLM was executed instead of the worker"
    assert len(load_results(tmp_path)) == 1
    assert read_json(tmp_path / "manifest.json")["settings"]["worker"]["execution_mode"] == (
        "process"
    )


@WINDOWS
@pytest.mark.parametrize("mode", ["busy", "sleep"])
async def test_sync_hang_is_killed_and_the_next_fixture_finishes(tmp_path, mode):
    started = time.monotonic()
    results = await run_probes([fixture("hung", mode), fixture("next")], tmp_path)
    hung, next_result = results
    try:
        assert hung.error_type == "timeout" and hung.status == "error"
        assert hung.root_success is False and hung.wall_s is None
        assert hung.cleanup_complete is True and hung.elapsed_s < DEADLINE + 6
        assert (Path(hung.run_dir) / "probe-pids.json").exists()
        stopped({hung.worker_pid, *pids_in(hung.run_dir)})
        assert next_result.status == "succeeded" and next_result.root_success is True
        assert len(load_results(tmp_path)) == 2
        assert time.monotonic() - started < 15
    finally:
        kill_failed_probe({hung.worker_pid, *pids_in(hung.run_dir)})


@WINDOWS
@pytest.mark.parametrize("mode,marker", [
    ("descendants", "descendants.json"), ("nested_tool", "nested-pids.json"),
])
async def test_timeout_reaps_raw_and_nested_tool_descendants(tmp_path, mode, marker):
    hung, following = await run_probes([fixture("tree", mode), fixture("next")], tmp_path)
    pids = {hung.worker_pid, *pids_in(hung.run_dir)}
    try:
        assert (Path(hung.run_dir) / marker).is_file(), "the descendant probe never ran"
        assert len(pids) >= 3
        assert hung.error_type == "timeout" and hung.cleanup_complete is True
        stopped(pids)
        assert following.status == "succeeded"
    finally:
        kill_failed_probe(pids)


@WINDOWS
@pytest.mark.parametrize("mode,exit_code", [("crash", 23), ("exit_after_child", 17)])
async def test_abrupt_worker_exit_is_recorded_and_background_children_are_reaped(
    tmp_path, mode, exit_code
):
    crashed, following = await run_probes([fixture("crashed", mode), fixture("next")], tmp_path)
    pids = {crashed.worker_pid, *pids_in(crashed.run_dir)}
    try:
        assert crashed.error_type == "worker_crash" and crashed.worker_exit_code == exit_code
        assert crashed.cleanup_complete is True and crashed.root_success is False
        assert (Path(crashed.run_dir) / "probe-pids.json").is_file()
        if mode == "exit_after_child":
            assert (Path(crashed.run_dir) / "descendants.json").is_file()
            assert len(pids) == 3
        stopped(pids)
        assert following.status == "succeeded"
    finally:
        kill_failed_probe(pids)


@WINDOWS
async def test_timeout_preserves_trace_and_artifacts_written_before_the_hang(tmp_path):
    result, = await run_probes([fixture(mode="trace_then_block")], tmp_path)
    run = Path(result.run_dir)
    assert result.error_type == "timeout" and result.cleanup_complete is True
    events = [TraceEvent.model_validate_json(line) for line in (run / "trace.jsonl").read_text(
        encoding="utf-8"
    ).splitlines() if line.strip()]
    assert any(event.kind == "llm" for event in events)
    assert any(event.kind == "tool" for event in events)
    marker = run / "workspaces" / result.fixture_id / "c1/planner@t1/trace-marker.txt"
    assert marker.read_text(encoding="utf-8") == "preserve before timeout"
    assert (run / "llm-telemetry.json").is_file()
    assert result.metrics["llm"]["calls_total"] >= 1
    assert result.total_tokens_in is None and result.total_tokens_out is None
    assert result.usage_missing_calls >= 1
    assert read_json(run / "result.json")["error_type"] == "timeout"
    stopped({result.worker_pid, *pids_in(run)})


@WINDOWS
@pytest.mark.parametrize("mode", [
    "candidate_nonce", "candidate_identity", "candidate_count_string", "candidate_nan",
    "candidate_status", "candidate_oversize", "candidate_truncated", "candidate_wrong_target",
    "candidate_wrong_root", "candidate_deep",
])
async def test_corrupt_candidate_is_not_published_as_success_and_next_run_continues(tmp_path, mode):
    corrupt, following = await run_probes([fixture("bad", mode), fixture("next")], tmp_path)
    assert corrupt.error_type == "invalid_worker_result" and corrupt.root_success is False
    assert corrupt.worker_exit_code == 0 and corrupt.cleanup_complete is True
    candidate = Path(corrupt.artifacts["worker_candidate"])
    assert candidate.is_file(), "the malformed candidate was not actually produced"
    assert read_json(Path(corrupt.run_dir) / "result.json")["status"] == "error"
    assert following.status == "succeeded"
    assert len(load_results(tmp_path)) == 2


@WINDOWS
@pytest.mark.parametrize("mode,error_type", [
    ("candidate_then_hang", "timeout"), ("candidate_then_crash", "worker_crash"),
])
async def test_a_success_candidate_cannot_hide_later_hang_or_crash(tmp_path, mode, error_type):
    failed, following = await run_probes([fixture("late", mode), fixture("next")], tmp_path)
    candidate = read_json(failed.artifacts["worker_candidate"])
    assert candidate["result"]["status"] == "succeeded"
    assert failed.error_type == error_type and failed.root_success is False
    assert failed.cleanup_complete is True and following.status == "succeeded"
    stopped({failed.worker_pid, *pids_in(failed.run_dir)})


@WINDOWS
async def test_factory_environment_credentials_are_cleared_before_running_tools(tmp_path):
    result, = await run_probes([fixture(mode="factory_env_probe")], tmp_path)
    assert result.status == "succeeded" and result.cleanup_complete is True
    assert (Path(result.run_dir) / "probe-pids.json").is_file()
    stopped({result.worker_pid, *pids_in(result.run_dir)})
    request = Path(result.artifacts["worker_request"]).read_text(encoding="utf-8")
    assert "sk-offline-factory-environment-placeholder" not in request


@WINDOWS
async def test_unimportable_factory_is_saved_for_each_attempt_without_crashing_batch(tmp_path):
    results = await run_all(
        [fixture("first"), fixture("second")], None, tmp_path, hard_timeout_s=DEADLINE,
        worker_factory="nonexistent_d05_worker_module:make_client",
    )
    assert len(results) == 2
    assert all(result.error_type == "worker_start_failed" for result in results)
    assert all(result.cleanup_complete is True for result in results)
    assert all(not running(result.worker_pid) for result in results)
    assert len(load_results(tmp_path)) == 2
    manifest = read_json(tmp_path / "manifest.json")
    assert manifest["status"] == "completed" and manifest["completed_runs"] == 2


@WINDOWS
async def test_spawn_failure_is_structured_and_next_attempt_still_executes(tmp_path, monkeypatch):
    real = supervisor.subprocess
    count = 0

    def start(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("injected owned-worker startup failure")
        return real.Popen(*args, **kwargs)

    proxy = SimpleNamespace(**vars(real))
    proxy.Popen = start
    monkeypatch.setattr(supervisor, "subprocess", proxy)
    failed, following = await run_probes([fixture("cannot_start"), fixture("next")], tmp_path)
    assert failed.error_type == "worker_start_failed" and failed.worker_pid is None
    assert failed.cleanup_complete is True
    assert "injected owned-worker startup failure" in failed.error_message
    assert following.status == "succeeded" and count == 2


@WINDOWS
async def test_job_attachment_failure_never_runs_uncontained_code(tmp_path, monkeypatch):
    original = supervisor._JobControl
    count = 0

    def attach(pid):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("injected Job attachment failure")
        return original(pid)

    monkeypatch.setattr(supervisor, "_JobControl", attach)
    failed, following = await run_probes([fixture("unattached"), fixture("next")], tmp_path)
    assert failed.error_type == "worker_start_failed" and failed.cleanup_complete is True
    assert failed.worker_pid and not running(failed.worker_pid)
    assert not (Path(failed.run_dir) / "probe-pids.json").exists()
    assert following.status == "succeeded"


@WINDOWS
async def test_cancel_before_resume_reaps_the_real_suspended_worker(tmp_path, monkeypatch):
    def pending_cancel(self, pid):
        raise asyncio.CancelledError

    monkeypatch.setattr(supervisor._JobControl, "resume", pending_cancel)
    with pytest.raises(asyncio.CancelledError):
        await run_probes([fixture("cancelled")], tmp_path)
    saved, = load_results(tmp_path)
    assert saved["status"] == "cancelled" and saved["cleanup_complete"] is True
    assert not running(saved["worker_pid"])
    assert not (Path(saved["run_dir"]) / "probe-pids.json").exists()
    assert read_json(tmp_path / "manifest.json")["status"] == "interrupted"


@WINDOWS
async def test_batch_cancellation_reaps_descendants_and_preserves_previous_result(tmp_path):
    task = asyncio.create_task(run_probes(
        [fixture("ready"), fixture("tree", "descendants"), fixture("never")], tmp_path, 15,
    ))
    run_dir = tmp_path / "tree/r0"
    pids = set()
    try:
        await wait_marker(run_dir / "descendants.json", task)
        pids = pids_in(run_dir)
        assert len(pids) == 3 and all(running(pid) for pid in pids)
        previous = (tmp_path / "ready/r0/result.json").read_bytes()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        saved = load_results(tmp_path)
        assert [row["status"] for row in saved] == ["succeeded", "cancelled"]
        assert saved[1]["cleanup_complete"] is True
        assert (tmp_path / "ready/r0/result.json").read_bytes() == previous
        assert not (tmp_path / "never/r0").exists()
        stopped(pids)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kill_failed_probe(pids)


@WINDOWS
async def test_repeated_cancel_waits_for_bounded_real_cleanup(tmp_path, monkeypatch):
    original = supervisor._cleanup
    cleaned, release = asyncio.Event(), asyncio.Event()

    async def delayed(process, job, timeout_s):
        result = await original(process, job, timeout_s)
        cleaned.set()
        await release.wait()
        return result

    monkeypatch.setattr(supervisor, "_cleanup", delayed)
    task = asyncio.create_task(run_probes([fixture(mode="descendants")], tmp_path, 15))
    pids = set()
    try:
        await wait_marker(tmp_path / "sample/r0/descendants.json", task)
        pids = pids_in(tmp_path / "sample/r0")
        task.cancel()
        await asyncio.wait_for(cleaned.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "the second cancellation escaped before cleanup was observed"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        row, = load_results(tmp_path)
        assert row["status"] == "cancelled" and row["cleanup_complete"] is True
        stopped(pids)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kill_failed_probe(pids)


@WINDOWS
async def test_unconfirmed_cleanup_is_bounded_recorded_and_stops_the_batch(tmp_path, monkeypatch):
    original = supervisor._cleanup

    async def unconfirmed(process, job, timeout_s):
        await original(process, job, timeout_s)
        await asyncio.Event().wait()  # Real process is already dead; only observation is delayed.

    monkeypatch.setattr(supervisor, "_cleanup", unconfirmed)
    monkeypatch.setattr(isolation, "CLEANUP_TIMEOUT_S", 0.25)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cleanup"):
        await run_probes([fixture("first"), fixture("never")], tmp_path)
    assert time.monotonic() - started < 5
    row, = load_results(tmp_path)
    assert row["error_type"] == "cleanup_failed" and row["cleanup_complete"] is False
    assert not running(row["worker_pid"])
    assert not (tmp_path / "never/r0").exists()
    assert read_json(tmp_path / "manifest.json")["status"] == "error"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "1"])
async def test_invalid_hard_deadline_fails_before_creating_worker(tmp_path, timeout):
    fake = FakeLLM([LLMResponse(content="ready")])
    with pytest.raises(ValueError, match="finite positive"):
        await run_all([fixture()], fake, tmp_path / "batch", hard_timeout_s=timeout)
    assert not (tmp_path / "batch").exists()
    assert fake.calls == []


@pytest.mark.parametrize("fixture_id", [".workers", "manifest.json"])
async def test_protocol_reserved_fixture_ids_fail_before_model_or_worker_start(
    tmp_path, fixture_id
):
    fake = FakeLLM([LLMResponse(content="ready")])
    with pytest.raises(ValueError):
        await run_all([fixture(fixture_id)], fake, tmp_path / "batch")
    assert fake.calls == [] and not (tmp_path / "batch").exists()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_cli_rejects_nonpositive_or_nonfinite_hard_timeout(value):
    with pytest.raises(SystemExit):
        parse_args(["--hard-timeout", value])


def test_callable_fake_client_requires_an_explicit_trusted_factory():
    fake = FakeLLM(lambda messages, tools: LLMResponse(content="ready"))
    with pytest.raises(ValueError, match="worker_factory"):
        isolation.prepare_client(fake, None)
    assert fake.calls == []
    public, secret = isolation.prepare_client(None, FACTORY)
    assert public == {"kind": "factory", "reference": FACTORY} and secret == {}


def test_model_credential_is_separate_from_the_serializable_worker_request(monkeypatch):
    from moumou import config

    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    key = "sk-offline-worker-placeholder-key"
    client = DeepSeekClient(api_key=key, base_url="https://api.invalid", model="offline-model")
    public, secret = isolation.prepare_client(client, None)
    assert public["kind"] == "deepseek" and public["model"] == "offline-model"
    assert key not in json.dumps(public) and "api_key" not in public
    assert secret == {"MOUMOU_EVAL_API_KEY": key}
    assert client._client is None, "the parent created a live HTTP client"


async def test_unsupported_platform_is_rejected_before_spawning(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(NotImplementedError, match="Windows"):
        await supervisor.supervise(
            [sys.executable, "-c", "raise SystemExit(99)"], cwd=tmp_path,
            env={}, timeout_s=1,
        )
