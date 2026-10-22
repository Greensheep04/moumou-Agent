"""Workspace port: isolation, artifact hand-off, and escape refusal."""

from __future__ import annotations

import pytest

from moumou.adapters.workspace import LocalDirs
from moumou.contracts import IntegrateResult, WorkspaceProvider


@pytest.fixture
def ws(tmp_path):
    return LocalDirs(tmp_path / "root")


def test_allocate_is_stable_and_separate(ws):
    a = ws.allocate("p", "c", "coder@t1")
    b = ws.allocate("p", "c", "tester@t1")

    assert a == ws.allocate("p", "c", "coder@t1"), "same agent must reuse its workspace"
    assert a != b, "two agents must never share a directory"
    assert a.is_dir() and b.is_dir()


def test_allocate_sanitises_ids(ws):
    d = ws.allocate("p", "c", "coder/../evil")
    assert d.is_relative_to(ws.root), "an id must not be able to walk out of the root"


def test_integrate_copies_and_reports(ws):
    src = ws.allocate("p", "c", "child")
    dst = ws.allocate("p", "c", "parent")
    (src / "sub").mkdir()
    (src / "sub" / "a.txt").write_text("hello", encoding="utf-8")

    result = ws.integrate(src, dst, ["sub/a.txt", "missing.txt"])

    assert isinstance(result, IntegrateResult)
    assert result.ok
    assert result.conflicts == []
    assert result.copied == ["sub/a.txt"]
    assert (dst / "sub" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_integrate_refuses_escaping_artifacts(ws):
    src = ws.allocate("p", "c", "child")
    dst = ws.allocate("p", "c", "parent")

    result = ws.integrate(src, dst, ["../secret.txt"])

    assert not result.ok
    assert result.error


def test_release_refuses_to_touch_outside_the_root(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(PermissionError):
        ws.release(outside)


def test_localdirs_satisfies_the_port(ws):
    assert isinstance(ws, WorkspaceProvider)
