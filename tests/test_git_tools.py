"""Local Git operations and argument boundaries."""

import subprocess

import pytest

from moumou.contracts import ToolContext
from moumou.tools import default_tools


@pytest.fixture
def context(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    return ToolContext(agent_id="a", task_id="t", workspace=tmp_path)


async def test_git_add_commit_diff_log_and_branch(context):
    tools = {t.name: t for t in default_tools()}
    assert "git" in tools
    tool = tools["git"]
    (context.workspace / "a.txt").write_text("hello")
    added = await tool.run({"operation": "add", "paths": ["a.txt"]}, context)
    assert added.ok
    committed = await tool.run({"operation": "commit", "message": "add a"}, context)
    assert committed.ok, committed
    assert (await tool.run({"operation": "status"}, context)).ok
    assert "add a" in (await tool.run({"operation": "log"}, context)).stdout
    assert (await tool.run({"operation": "branch", "name": "feature"}, context)).ok
    assert (await tool.run({"operation": "merge", "branch": "feature"}, context)).ok
    assert (await tool.run({"operation": "diff"}, context)).ok


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "push"},
        {"operation": "add", "paths": ["../secret"]},
        {"operation": "branch", "name": "--bad"},
        {"operation": "merge", "branch": "main; echo injected"},
    ],
)
async def test_git_rejects_unapproved_operations_and_invalid_arguments(context, args):
    tools = {t.name: t for t in default_tools()}
    assert "git" in tools
    result = await tools["git"].run(args, context)
    assert not result.ok


@pytest.mark.parametrize("inherited", [False, True])
async def test_git_environment_cannot_redirect_operations_to_other_agent(
    context, tmp_path, monkeypatch, inherited
):
    tools = {t.name: t for t in default_tools()}
    outside = tmp_path.parent / (tmp_path.name + "-other")
    outside.mkdir()
    subprocess.run(["git", "init", str(outside)], check=True, capture_output=True)
    (outside / "secret.txt").write_text("private")
    overrides = {"GIT_DIR": str(outside / ".git"), "GIT_WORK_TREE": str(outside)}
    if inherited:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
    else:
        context.env = overrides
    result = await tools["git"].run({"operation": "status"}, context)
    assert result.ok
    assert "secret.txt" not in result.stdout


async def test_git_merge_reports_modified_files_as_artifacts(context):
    tools = {t.name: t for t in default_tools()}
    tool = tools["git"]
    (context.workspace / "base.txt").write_text("base")
    assert (await tool.run({"operation": "add", "paths": ["base.txt"]}, context)).ok
    assert (await tool.run({"operation": "commit", "message": "base"}, context)).ok
    parent = subprocess.check_output(
        ["git", "-C", str(context.workspace), "branch", "--show-current"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(context.workspace), "checkout", "-b", "feature"],
        check=True,
        capture_output=True,
    )
    (context.workspace / "feature.txt").write_text("feature")
    assert (await tool.run({"operation": "add", "paths": ["feature.txt"]}, context)).ok
    assert (await tool.run({"operation": "commit", "message": "feature"}, context)).ok
    subprocess.run(
        ["git", "-C", str(context.workspace), "checkout", parent], check=True, capture_output=True
    )
    result = await tool.run({"operation": "merge", "branch": "feature"}, context)
    assert result.ok
    assert result.artifacts == ["feature.txt"]
