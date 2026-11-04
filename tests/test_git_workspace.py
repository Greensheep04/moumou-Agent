"""Delivery isolation and conflict recovery against real temporary repositories."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from moumou import adapters


def provider(tmp_path):
    assert hasattr(adapters, "GitWorkspaces"), "P1 worktree provider is missing"
    return adapters.GitWorkspaces(tmp_path / "managed")


def test_stable_allocations_do_not_collide(tmp_path):
    ws = provider(tmp_path)
    a = ws.allocate("p", "c", "a/b")
    b = ws.allocate("p", "c", "a?b")
    assert a != b and a == ws.allocate("p", "c", "a/b")
    assert (a / ".git").is_file() and (b / ".git").is_file()


def test_only_declared_files_are_delivered_and_repeat_updates_work(tmp_path):
    ws = provider(tmp_path)
    src, dst = [ws.allocate("p", "c", name) for name in ("child", "parent")]
    (src / "out.txt").write_text("first")
    (src / "private.txt").write_text("private")
    result = ws.integrate(src, dst, ["out.txt"])
    assert result.ok and result.copied == ["out.txt"]
    assert (dst / "out.txt").read_text() == "first"
    assert not (dst / "private.txt").exists()
    (src / "out.txt").write_text("second")
    assert ws.integrate(src, dst, ["out.txt"]).ok
    assert (dst / "out.txt").read_text() == "second"
    assert ws.integrate(src, dst, ["out.txt"]).ok
    (src / "out.txt").unlink()
    assert ws.integrate(src, dst, ["out.txt"]).ok
    assert not (dst / "out.txt").exists()


def test_conflict_rolls_back_then_another_delivery_can_succeed(tmp_path):
    ws = provider(tmp_path)
    a, b, parent = [ws.allocate("p", "c", n) for n in ("a", "b", "parent")]
    (a / "same.txt").write_text("A")
    (b / "same.txt").write_text("B")
    assert ws.integrate(a, parent, ["same.txt"]).ok
    conflict = ws.integrate(b, parent, ["same.txt"])
    assert not conflict.ok and conflict.conflicts == ["same.txt"]
    assert (parent / "same.txt").read_text() == "A"
    assert not ws.git(parent, "status", "--porcelain").stdout.strip()
    (b / "other.txt").write_text("next")
    assert ws.integrate(b, parent, ["other.txt"]).ok
    assert (parent / "other.txt").read_text() == "next"


def test_parallel_deliveries_are_serialized(tmp_path):
    ws = provider(tmp_path)
    parent = ws.allocate("p", "c", "parent")
    a, b = [ws.allocate("p", "c", n) for n in ("a", "b")]
    (a / "a.txt").write_text("A")
    (b / "b.txt").write_text("B")
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda pair: ws.integrate(pair[0], parent, [pair[1]]), [(a, "a.txt"), (b, "b.txt")]
            )
        )
    assert all(r.ok for r in results)
    assert (parent / "a.txt").read_text() == "A"
    assert (parent / "b.txt").read_text() == "B"


def test_dirty_destination_and_escaping_artifact_are_rejected(tmp_path):
    ws = provider(tmp_path)
    src, dst = [ws.allocate("p", "c", n) for n in ("a", "parent")]
    (src / "ok.txt").write_text("ok")
    assert not ws.integrate(src, dst, ["../escape"]).ok
    (dst / "dirty.txt").write_text("keep")
    result = ws.integrate(src, dst, ["ok.txt"])
    assert not result.ok and "dirty" in result.error
    assert (dst / "dirty.txt").read_text() == "keep"
    with pytest.raises(PermissionError):
        ws.release(tmp_path)
    with pytest.raises(PermissionError):
        ws.release(ws.root)


def test_seed_repository_and_safe_release(tmp_path):
    import subprocess

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    ws = adapters.GitWorkspaces(tmp_path / "managed", seed=seed)
    path = ws.allocate("p", "c", "a")
    ws.release(path)
    assert not path.exists()
    assert ws.allocate("p", "c", "a").exists()
