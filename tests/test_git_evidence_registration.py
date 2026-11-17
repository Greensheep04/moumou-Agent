"""Precise framework exclusions must not mask ordinary worktree changes."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from moumou.adapters import GitWorkspaces
from moumou.orchestrator.worker import InvalidResultFiles, _declared_artifacts
from moumou.sandbox.local import LocalJail


def evidence(workspace, content=b'{"schema_version": 1}'):
    relative = ".evaluation/context/" + hashlib.sha256(content).hexdigest()[:32] + ".json"
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return relative


def exclude_path(provider, workspace):
    return Path(provider.git(
        workspace, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
    ).stdout.strip())


def test_exact_registration_preserves_local_rules_and_existing_allocation(tmp_path):
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("p", "c", "lead")
    exclude = exclude_path(provider, workspace)
    original = b"# operator rule without final newline\noperator-only.txt"
    exclude.write_bytes(original)
    relative = evidence(workspace)
    assert provider.git(workspace, "status", "--porcelain").stdout.strip()
    provider.register_private_evidence(workspace, relative)
    expected = original + b"\n/" + relative.encode("ascii") + b"\n"
    assert exclude.read_bytes() == expected
    assert not provider.git(workspace, "status", "--porcelain").stdout.strip()
    assert not (workspace / ".gitignore").exists()
    reopened = GitWorkspaces(provider.root)
    assert reopened.allocate("p", "c", "lead") == workspace
    reopened.register_private_evidence(workspace, relative)
    assert exclude.read_bytes() == expected, "repeat registration must not alter the rules"
    assert not provider.git(workspace, "config", "--local", "--get", "core.excludesFile",
                            check=False).stdout.strip()


@pytest.mark.parametrize("relative", [
    ".evaluation/user.txt", ".evaluation/context/*.json", "../outside.txt",
    "/.evaluation/prior-dialogue.txt", ".evaluation/context/../prior-dialogue.txt",
])
def test_registration_never_hides_unapproved_paths(tmp_path, relative):
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("p", "c", "lead")
    exclude = exclude_path(provider, workspace)
    before = exclude.read_bytes()
    with pytest.raises(ValueError, match="unsupported"):
        provider.register_private_evidence(workspace, relative)
    assert exclude.read_bytes() == before


def test_wrong_digest_or_tracked_evidence_is_never_auto_hidden(tmp_path):
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("p", "c", "lead")
    relative = evidence(workspace)
    target = workspace / relative
    original = target.read_bytes()
    exclude = exclude_path(provider, workspace)
    before = exclude.read_bytes()
    target.write_bytes(b"different evidence")
    with pytest.raises(ValueError, match="digest"):
        provider.register_private_evidence(workspace, relative)
    assert exclude.read_bytes() == before
    target.write_bytes(original)
    provider.git(workspace, "add", "--", relative)
    with pytest.raises(ValueError, match="tracked"):
        provider.register_private_evidence(workspace, relative)
    assert exclude.read_bytes() == before
    assert provider.git(workspace, "status", "--porcelain").stdout.strip()


def test_release_keeps_registered_evidence_in_its_workspace(tmp_path):
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("p", "c", "lead")
    relative = evidence(workspace)
    original = (workspace / relative).read_bytes()
    provider.register_private_evidence(workspace, relative)
    assert not provider.git(workspace, "status", "--porcelain").stdout.strip()
    before = provider.git(workspace, "rev-parse", "HEAD").stdout
    with pytest.raises(ValueError, match="private evidence"):
        provider.release(workspace)
    assert (workspace / relative).read_bytes() == original
    assert provider.git(workspace, "rev-parse", "HEAD").stdout == before


def link_directory(link, target):
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link):
    if os.name == "nt":
        os.rmdir(link)  # Remove only the junction, never its target tree.
    else:
        link.unlink()


def test_registration_rejects_directory_alias_even_inside_its_workspace(tmp_path):
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("p", "c", "lead")
    ordinary = workspace / "ordinary"
    ordinary.mkdir()
    link = workspace / ".evaluation"
    link_directory(link, ordinary)
    try:
        relative = evidence(workspace)
        exclude = exclude_path(provider, workspace)
        before = exclude.read_bytes()
        with pytest.raises(ValueError, match="aliases"):
            provider.register_private_evidence(workspace, relative)
        assert exclude.read_bytes() == before
    finally:
        remove_link(link)


def test_private_evidence_stays_readable_but_is_not_a_deliverable(tmp_path):
    relative = evidence(tmp_path)
    jail = LocalJail(tmp_path)
    assert jail.resolve(relative).read_bytes() == b'{"schema_version": 1}'
    (tmp_path / "safe.txt").write_text("deliverable", encoding="utf-8")
    with pytest.raises(InvalidResultFiles, match="framework_private"):
        _declared_artifacts(tmp_path, ["safe.txt", relative])
    assert _declared_artifacts(tmp_path, ["safe.txt"]) == ["safe.txt"]
    link = tmp_path / "readable-alias"
    link_directory(link, tmp_path / ".evaluation")
    try:
        alias = "readable-alias/context/" + Path(relative).name
        assert jail.resolve(alias).read_bytes() == jail.resolve(relative).read_bytes()
        with pytest.raises(PermissionError, match="framework_private"):
            jail.resolve_artifact(alias)
    finally:
        remove_link(link)
