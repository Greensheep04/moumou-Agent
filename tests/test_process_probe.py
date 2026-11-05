"""Deterministic POSIX liveness probes, including a process exiting mid-read."""

import pathlib
from types import SimpleNamespace

import pytest
import test_process_runner as probe


@pytest.mark.parametrize(
    "state,expected",
    [("live", True), ("zombie", False), ("vanished", False), ("no_procfs", True)],
)
def test_posix_liveness_survives_proc_stat_race(monkeypatch, state, expected):
    def read_stat():
        if state == "vanished":
            raise FileNotFoundError("process exited after exists()")
        return "1234 (python) Z" if state == "zombie" else "1234 (python) S"

    stat = SimpleNamespace(exists=lambda: state != "no_procfs", read_text=read_stat)
    # Patch only within the probe; restore Path before pytest renders any failure.
    with monkeypatch.context() as patch:
        patch.setattr(probe, "os", SimpleNamespace(name="posix", kill=lambda pid, sig: None))
        patch.setattr(pathlib, "Path", lambda path: stat)
        result = probe.alive(1234)
    assert result is expected


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_posix_liveness_preserves_signal_errors(monkeypatch, error):
    def kill(pid, sig):
        raise error("probe signal failed")

    monkeypatch.setattr(probe, "os", SimpleNamespace(name="posix", kill=kill))
    if error is ProcessLookupError:
        assert probe.alive(1234) is False
    else:
        with pytest.raises(PermissionError):
            probe.alive(1234)
