"""Bounded read recovery is transport handling, never a human quality decision."""

from __future__ import annotations

import errno
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_p2_session import make_session

from moumou.ui.cli import run_session
from moumou.ui.session import SessionReadError


def human_evidence(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in (root / "scenario_1/human").rglob("*.json")}


def test_actually_missing_manifest_is_restored_between_bounded_read_attempts(tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    before = human_evidence(root)
    manifest = root / "test.json"
    parked = root / "test.parked"
    manifest.replace(parked)
    delays = []

    def restore(delay):
        delays.append(delay)
        parked.replace(manifest)

    monkeypatch.setattr("moumou.ui.session.time.sleep", restore)
    assert session.current().phase == "task_1"
    assert delays == [0.01]
    assert session.read_recoveries[-1]["error"] == "FileNotFoundError"
    assert human_evidence(root) == before
    assert session.manifest()["status"] == "running"


def test_missing_required_manifest_stops_after_three_attempts_without_inferred_state(
        tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    before = human_evidence(root)
    (root / "test.json").unlink()
    delays = []
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    with pytest.raises(FileNotFoundError, match="attempts=3.*no state or decision"):
        session.pending()
    assert delays == [0.01, 0.03]
    assert human_evidence(root) == before


@pytest.mark.parametrize("operation", ["stat", "read_bytes"])
def test_permission_race_rechecks_path_then_retries_the_read(tmp_path, monkeypatch, operation):
    root, session, _ = make_session(tmp_path)
    target = root / "test.json"
    original = getattr(Path, operation)
    calls = 0
    delays = []

    def denied_once(path, *args, **kwargs):
        nonlocal calls
        if path == target:
            calls += 1
            if calls == 1:
                raise PermissionError(errno.EACCES, "simulated transient read denial", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, denied_once)
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    value, _ = session._read(target)
    assert value["status"] == "running"
    assert calls >= 2 and delays == [0.01]
    assert session.read_recoveries[-1]["attempts"] == 2
    assert session.read_recoveries[-1]["error"] == "PermissionError"


def test_permanent_permission_failure_reports_operation_path_and_original_error(
        tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    target = root / "test.json"
    read = Path.read_bytes
    calls, delays = [], []

    def denied(path):
        if path == target:
            calls.append(path)
            raise PermissionError(errno.EACCES, "simulated persistent denial", str(path))
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    with pytest.raises(SessionReadError) as captured:
        session.current()
    message = str(captured.value)
    assert "operation=read JSON" in message and "path=test.json" in message
    assert "attempts=3" in message and "errno=13" in message and "winerror=" in message
    assert "no state or decision inferred" in message
    assert isinstance(captured.value.__cause__, PermissionError)
    assert len(calls) == 3 and delays == [0.01, 0.03]
    assert not session.read_recoveries


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows exception fields")
def test_available_native_error_code_is_preserved_in_diagnostic_and_cause(tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    target = root / "test.json"
    original = Path.read_bytes

    def denied(path):
        if path == target:
            raise OSError(errno.EACCES, "SIMULATED explicit Windows access denial", str(path), 5)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.setattr("moumou.ui.session.time.sleep", lambda delay: None)
    with pytest.raises(SessionReadError, match="attempts=3.*winerror=5") as captured:
        session.current()
    assert captured.value.__cause__.winerror == 5


@pytest.mark.parametrize("error", [ValueError("unknown validation error"),
                                   OSError(errno.EINVAL, "unknown OS error")])
def test_unclassified_errors_are_never_retried(tmp_path, monkeypatch, error):
    root, session, _ = make_session(tmp_path)
    delays = []

    def reject(path):
        raise error

    monkeypatch.setattr(session, "_validate_path", reject)
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    with pytest.raises(type(error)) as captured:
        session._read(root / "test.json")
    assert captured.value is error and not delays


def test_alias_appearing_after_first_failure_is_rejected_before_second_read(tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    target = root / "test.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"status":"DONE"}\n', encoding="utf-8")
    resolve, read = Path.resolve, Path.read_bytes
    changed, calls, delays = False, [], []

    def now_alias(path, *args, **kwargs):
        if changed and path == target:
            return outside
        return resolve(path, *args, **kwargs)

    def first_read_denied(path):
        if path == target:
            calls.append(path)
            raise PermissionError(errno.EACCES, "simulated concurrent transition", str(path))
        return read(path)

    def changed_on_retry(delay):
        nonlocal changed
        delays.append(delay)
        changed = True

    monkeypatch.setattr(Path, "resolve", now_alias)
    monkeypatch.setattr(Path, "read_bytes", first_read_denied)
    monkeypatch.setattr("moumou.ui.session.time.sleep", changed_on_retry)
    with pytest.raises(ValueError, match="escapes RUN or uses a path alias"):
        session._read(target)
    assert calls == [target] and delays == [0.01]


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows ntpath double-probe behavior")
def test_exact_extended_prefix_race_revalidates_ordinary_path_before_reading(tmp_path, monkeypatch):
    import ntpath

    root, session, _ = make_session(tmp_path)
    target = root / "active.json"
    original = ntpath._getfinalpathname
    calls = []
    delays = []

    def second_probe_disappears(path):
        if ntpath.normcase(str(path)) == ntpath.normcase(str(target)):
            calls.append(str(path))
            if len(calls) == 2:
                raise OSError(errno.ENOENT, "SIMULATED second-probe disappearance", str(path), 2)
        return original(path)

    monkeypatch.setattr(ntpath, "_getfinalpathname", second_probe_disappears)
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    assert session.current().phase == "task_1"
    assert delays == [0.01]
    recovery = session.read_recoveries[-1]
    assert recovery["error"] == "_WindowsPathRace" and recovery["attempts"] == 2
    assert "resolved=\\\\?\\" in recovery["detail"]
    assert target.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended path spelling")
@pytest.mark.parametrize("outside", [False, True])
def test_persistent_extended_spelling_or_real_escape_never_becomes_accepted(
        tmp_path, monkeypatch, outside):
    root, session, _ = make_session(tmp_path)
    target = root / "test.json"
    original = Path.resolve
    resolved = Path("\\\\?\\" + str(tmp_path / "outside.json" if outside else target))
    delays = []
    reads = []

    def spelling(path, *args, **kwargs):
        return resolved if path == target else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spelling)
    monkeypatch.setattr(Path, "read_bytes", lambda path: reads.append(path))
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    error = ValueError if outside else SessionReadError
    with pytest.raises(error):
        session._read(target)
    assert not reads
    assert delays == ([] if outside else [0.01, 0.03])


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows junction negative case")
def test_real_junction_cannot_redirect_read_recovery(tmp_path, monkeypatch):
    import _winapi

    root, session, _ = make_session(tmp_path)
    folder = root / "scenario_1"
    parked = root / "original-scenario"
    folder.replace(parked)
    outside = tmp_path / "outside"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(folder))
    delays = []
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)
    try:
        with pytest.raises(ValueError, match="aliases"):
            session.pending()
        assert not delays and list(outside.iterdir()) == []
    finally:
        os.rmdir(folder)  # Remove only the junction, never its target directory.
        parked.replace(folder)


@contextmanager
def exclusive_windows_read(path):
    """Hold a real Windows file handle that denies sharing; never change ACLs."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    opened = True

    def release():
        nonlocal opened
        if opened:
            assert close(handle)
            opened = False

    try:
        yield release
    finally:
        release()


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows sharing-denial fixture")
@pytest.mark.parametrize("release_on_retry", [True, False])
def test_real_windows_share_lock_recovers_or_exhausts_without_touching_human_evidence(
        tmp_path, monkeypatch, release_on_retry):
    root, session, _ = make_session(tmp_path)
    before = human_evidence(root)
    target = root / "test.json"
    original = target.read_bytes()
    delays = []
    with exclusive_windows_read(target) as release:
        # The kernel identifies this controlled lock as sharing violation 32.
        # CPython's CRT-backed read may expose only errno=13, winerror=None;
        # the UI must report available fields rather than inventing that code.
        with pytest.raises(OSError) as probe:
            with exclusive_windows_read(target):
                pytest.fail("exclusive handle unexpectedly allowed a second open")
        assert probe.value.winerror == 32

        def on_retry(delay):
            delays.append(delay)
            if release_on_retry:
                release()

        monkeypatch.setattr("moumou.ui.session.time.sleep", on_retry)
        if release_on_retry:
            assert session.current().phase == "task_1"
            assert delays == [0.01]
            recovery = session.read_recoveries[-1]
            assert recovery["error"] == "PermissionError" and recovery["errno"] == 13
            assert recovery["winerror"] in (None, 32)
        else:
            with pytest.raises(SessionReadError, match="attempts=3.*errno=13") as captured:
                session.current()
            assert captured.value.__cause__.winerror in (None, 32)
            assert delays == [0.01, 0.03]
    assert target.read_bytes() == original and human_evidence(root) == before
    assert session.manifest()["status"] == "running"


def test_history_read_retries_without_duplicate_rows_or_changed_evidence(tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    history = root / "scenario_1/dialogue.jsonl"
    content = b'{"event":"recorded"}\n'
    history.write_bytes(content)
    original = Path.open
    calls = []

    def denied_once(path, *args, **kwargs):
        if path == history:
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(errno.EACCES, "SIMULATED transient history denial", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_once)
    monkeypatch.setattr("moumou.ui.session.time.sleep", lambda delay: None)
    rows = session.history()
    assert len(rows) == 1 and rows[0].record == {"event": "recorded"}
    assert rows[0].line_number == 1 and rows[0].source == "scenario_1/dialogue.jsonl"
    assert len(calls) == 2
    assert history.read_bytes() == content


class Display:
    def __init__(self):
        self.lines = []
        self.requests = []

    def info(self, text):
        self.lines.append(text)

    def banner(self, *args, **kwargs):
        pass

    def request(self, request):
        self.requests.append(request)


async def test_cli_recovers_then_quits_without_answering_or_inventing_done(tmp_path, monkeypatch):
    root, session, item = make_session(tmp_path)
    before = human_evidence(root)
    target = root / "test.json"
    original = Path.read_bytes
    reads = 0

    def deny_once(path):
        nonlocal reads
        if path == target:
            reads += 1
            if reads == 1:
                raise PermissionError(errno.EACCES, "simulated transient denial", str(path))
        return original(path)

    async def quit_input(prompt):
        return "/quit"

    monkeypatch.setattr(Path, "read_bytes", deny_once)
    monkeypatch.setattr("moumou.ui.session.time.sleep", lambda delay: None)
    display = Display()
    await run_session(session, console=display, input_fn=quit_input, poll_interval_s=0.001)
    assert [request.id for request in display.requests] == [item.request_id]
    assert human_evidence(root) == before
    assert not any("DONE" in line for line in display.lines)
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "running"


async def test_cli_stops_on_persistent_required_metadata_loss_before_collecting_input(
        tmp_path, monkeypatch):
    root, session, _ = make_session(tmp_path)
    before = human_evidence(root)
    (root / "test.json").unlink()
    delays = []
    monkeypatch.setattr("moumou.ui.session.time.sleep", delays.append)

    async def forbidden_input(prompt):
        pytest.fail("persistent metadata failure must not collect or manufacture an answer")

    display = Display()
    with pytest.raises(FileNotFoundError, match="attempts=3"):
        await run_session(session, console=display, input_fn=forbidden_input, poll_interval_s=0.001)
    assert delays == [0.01, 0.03]
    assert not display.requests and human_evidence(root) == before
