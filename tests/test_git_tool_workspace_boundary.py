"""Git actions stay in the actual private repo root, never an enclosing repo."""

import os
import subprocess

import pytest

from moumou.adapters.workspace import LocalDirs
from moumou.contracts import ToolContext, ToolResult
from moumou.tools import git as git_module
from moumou.tools.git import GitPushTool, GitTool
from moumou.workspace.git import GitWorkspaces


def git(path, *args):
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, encoding="utf-8", env=environment, check=True)


def context(path, env=None):
    return ToolContext(agent_id="dev", task_id="boundary", workspace=path, env=env or {})


@pytest.fixture
def parent_and_localdirs(tmp_path):
    parent = tmp_path / "parent-private-repository"
    parent.mkdir()
    git(parent, "init")
    (parent / "private.txt").write_text("PARENT SECRET", encoding="utf-8")
    git(parent, "add", "private.txt")
    child = LocalDirs(parent / "runs").allocate("project", "group", "dev")
    (child / "output.txt").write_text("Child output", encoding="utf-8")
    return parent, child


@pytest.mark.parametrize("args", [
    {"operation": "status"}, {"operation": "log"}, {"operation": "diff"},
    {"operation": "add", "paths": ["output.txt"]},
    {"operation": "commit", "message": "Do not touch parent"},
    {"operation": "merge", "branch": "main"}, {"operation": "branch", "name": "feature"},
])
async def test_plain_localdirs_never_even_probes_parent_repository(parent_and_localdirs,
                                                                  monkeypatch, args):
    parent, child = parent_and_localdirs
    before = (parent / ".git/index").read_bytes()
    calls = []

    async def forbidden(*arguments, **kwargs):
        calls.append(arguments)
        raise AssertionError("No Git process is allowed without an own marker")

    monkeypatch.setattr(git_module, "run_command", forbidden)
    result = await GitTool().run(args, context(child))
    assert not result.ok and "own Git repository" in result.error
    assert not calls and result.stdout == result.stderr == ""
    assert str(parent) not in result.error and "PARENT SECRET" not in result.error
    assert (parent / ".git/index").read_bytes() == before


async def test_push_also_refuses_parent_before_dispatch(parent_and_localdirs, monkeypatch):
    parent, child = parent_and_localdirs
    calls = []

    async def forbidden(*arguments, **kwargs):
        calls.append(arguments)
        raise AssertionError("Push must not discover the parent")

    monkeypatch.setattr(git_module, "run_command", forbidden)
    result = await GitPushTool().run({"branch": "main"}, context(child))
    assert not result.ok and not calls and not result.stdout and not result.stderr
    assert str(parent) not in result.error
    assert GitPushTool.action_class == "git_push" and GitTool.action_class == "git_commit"


@pytest.mark.parametrize("marker_kind", ["invalid_directory", "parent_gitfile"])
async def test_marker_alone_cannot_authorize_an_ancestor_root(parent_and_localdirs,
                                                            monkeypatch, marker_kind):
    parent, child = parent_and_localdirs
    marker = child / ".git"
    if marker_kind == "invalid_directory":
        marker.mkdir()
    else:
        git(parent, "config", "core.worktree", str(parent))
        marker.write_text("gitdir: " + (parent / ".git").as_posix() + "\n", encoding="utf-8")
    real = git_module.run_command
    calls = []

    async def observed(command, *arguments, **kwargs):
        calls.append(command)
        return await real(command, *arguments, **kwargs)

    monkeypatch.setattr(git_module, "run_command", observed)
    result = await GitTool().run({"operation": "status"}, context(child))
    assert not result.ok and len(calls) == 1 and "rev-parse" in calls[0]
    assert result.stdout == result.stderr == "" and str(parent) not in result.error


async def test_own_unborn_nested_repo_still_supports_status_diff_add(parent_and_localdirs):
    parent, child = parent_and_localdirs
    git(child, "init")
    before = (parent / ".git/index").read_bytes()
    tool = GitTool()
    status = await tool.run({"operation": "status"}, context(child))
    assert status.ok and "output.txt" in status.stdout and "private.txt" not in status.stdout
    assert (await tool.run({"operation": "diff"}, context(child))).ok
    assert (await tool.run({"operation": "add", "paths": ["output.txt"]}, context(child))).ok
    assert (parent / ".git/index").read_bytes() == before
    assert (child / ".git/index").is_file()


async def test_managed_linked_worktree_and_push_dispatch_remain_legal(tmp_path, monkeypatch):
    # Existing provider allocation creates its normal baseline; no manual test commit.
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("project", "group", "dev")
    assert (workspace / ".git").is_file()
    assert (await GitTool().run({"operation": "status"}, context(workspace))).ok
    real = git_module.run_command
    calls = []

    async def observed(command, *arguments, **kwargs):
        calls.append((command, kwargs))
        if "push" in command:
            return ToolResult(ok=True, stdout="test-only push dispatch")
        return await real(command, *arguments, **kwargs)

    monkeypatch.setattr(git_module, "run_command", observed)
    assert (await GitPushTool().run({"branch": "main"}, context(workspace))).ok
    assert len(calls) == 2 and "rev-parse" in calls[0][0] and "push" in calls[1][0]
    assert all(kwargs["strip_git_env"] for _, kwargs in calls)
    assert all(any(arg.startswith("core.hooksPath=") for arg in command)
               for command, _ in calls)


@pytest.mark.parametrize("inherited", [True, False])
async def test_guard_and_operation_both_strip_git_redirection(parent_and_localdirs, monkeypatch,
                                                             inherited):
    parent, child = parent_and_localdirs
    git(child, "init")
    overrides = {"GIT_DIR": str(parent / ".git"), "GIT_WORK_TREE": str(parent)}
    if inherited:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
        overrides = {}
    result = await GitTool().run({"operation": "status"}, context(child, overrides))
    assert result.ok and "output.txt" in result.stdout and "private.txt" not in result.stdout


async def test_symlink_git_marker_is_not_accepted(parent_and_localdirs):
    parent, child = parent_and_localdirs
    try:
        (child / ".git").symlink_to(parent / ".git", target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")
    result = await GitTool().run({"operation": "status"}, context(child))
    assert not result.ok and "own Git repository" in result.error


@pytest.mark.parametrize("probe_value", ["empty", "relative", "ancestor"])
async def test_untrusted_probe_output_never_dispatches_action(parent_and_localdirs,
                                                              monkeypatch, probe_value):
    parent, child = parent_and_localdirs
    git(child, "init")
    output = {"empty": "", "relative": ".", "ancestor": str(parent)}[probe_value]
    calls = []

    async def probe_only(command, *args, **kwargs):
        calls.append(command)
        assert "rev-parse" in command
        return ToolResult(ok=True, stdout=output)

    monkeypatch.setattr(git_module, "run_command", probe_only)
    result = await GitTool().run({"operation": "status"}, context(child))
    assert not result.ok and len(calls) == 1 and not result.stdout and not result.stderr
