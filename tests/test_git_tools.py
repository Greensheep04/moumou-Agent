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
