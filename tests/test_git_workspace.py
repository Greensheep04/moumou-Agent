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


def test_dependency_edit_and_delete_use_imported_file_baseline(tmp_path):
    ws = provider(tmp_path)
    a, b, parent = [ws.allocate("p", "c", n) for n in ("a", "b", "parent")]
    (a / "x.txt").write_text("first")
    assert ws.integrate(a, parent, ["x.txt"]).ok
    assert ws.integrate(a, b, ["x.txt"]).ok
    (b / "x.txt").write_text("revised")
    revised = ws.integrate(b, parent, ["x.txt"])
    assert revised.ok, revised
    assert (parent / "x.txt").read_text() == "revised"
    (b / "x.txt").unlink()
    deleted = ws.integrate(b, parent, ["x.txt"])
    assert deleted.ok, deleted
    assert not (parent / "x.txt").exists()


def test_dependency_subset_and_committed_changes_keep_provenance(tmp_path):
    ws = provider(tmp_path)
    a, b, parent = [ws.allocate("p", "c", n) for n in ("a", "b", "parent")]
    (a / "x.txt").write_text("first")
    (a / "extra.txt").write_text("extra")
    assert ws.integrate(a, parent, ["x.txt", "extra.txt"]).ok
    assert ws.integrate(a, b, ["x.txt"]).ok
    (b / "x.txt").write_text("revised")
    (b / "private.txt").write_text("private")
    ws.git(b, "add", ".")
    ws.git(b, "commit", "-m", "commit own work")
    result = ws.integrate(b, parent, ["x.txt"])
    assert result.ok, result
    assert (parent / "x.txt").read_text() == "revised"
    assert (parent / "extra.txt").read_text() == "extra"
    assert not (parent / "private.txt").exists()


def test_newly_selected_path_uses_its_own_imported_baseline(tmp_path):
    ws = provider(tmp_path)
    a, b, parent = [ws.allocate("p", "c", n) for n in ("a", "b", "parent")]
    (b / "own.txt").write_text("own")
    assert ws.integrate(b, parent, ["own.txt"]).ok
    ws.git(b, "add", "own.txt")
    ws.git(b, "commit", "-m", "own work before receiving dependency")
    (a / "later.txt").write_text("first")
    assert ws.integrate(a, b, ["later.txt"]).ok
    assert ws.integrate(a, parent, ["later.txt"]).ok
    (b / "later.txt").write_text("revised")
    assert ws.integrate(b, parent, ["later.txt"]).ok
    assert (parent / "later.txt").read_text() == "revised"


def test_import_does_not_hide_preexisting_committed_own_work(tmp_path):
    ws = provider(tmp_path)
    seed, a, b, parent = [ws.allocate("p", "c", n) for n in ("seed", "a", "b", "parent")]
    (seed / "x.txt").write_text("first\nmiddle\nlast\n")
    for dst in (a, b, parent):
        assert ws.integrate(seed, dst, ["x.txt"]).ok
    (b / "x.txt").write_text("FIRST\nmiddle\nlast\n")
    ws.git(b, "add", "x.txt")
    ws.git(b, "commit", "-m", "own edit")
    (a / "x.txt").write_text("first\nmiddle\nLAST\n")
    assert ws.integrate(a, b, ["x.txt"]).ok
    assert ws.integrate(a, parent, ["x.txt"]).ok
    result = ws.integrate(b, parent, ["x.txt"])
    assert result.ok, result
    assert (parent / "x.txt").read_text() == "FIRST\nmiddle\nLAST\n"
