"""Real local remote pushes, including the existing framework approval gate."""

import subprocess

import pytest

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import AgentInstance, ToolContext
from moumou.orchestrator import Engine, Settings
from moumou.tools import default_tools


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8"
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_gate_controls_real_push_to_bare_remote(tmp_path, decision):
    tools = {t.name: t for t in default_tools()}
    assert "git_push" in tools
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    engine = Engine(
        FakeLLM(lambda m, t: LLMResponse(content="done")),
        human=ScriptedHuman(default=decision),
        settings=Settings(root=tmp_path / "run", memory_enabled=False),
    )
    try:
        node = engine.submit("push a change")
        engine.repo.transition(node.id, "RUNNING", "test")
        path = engine.workspace.allocate(node.project_id, node.conversation_id, "agent")
        assert git(path, "init", "--initial-branch=main").returncode == 0
        assert (
            git(
                path,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "base",
            ).returncode
            == 0
        )
        assert git(path, "remote", "add", "origin", str(remote)).returncode == 0
        agent = AgentInstance(
            id="agent",
            role=node.role,
            project_id=node.project_id,
            conversation_id=node.conversation_id,
            workspace=str(path),
        )
        result = await engine.gate.execute(
            ToolCall(id="push", name="git_push", args={"remote": "origin", "branch": "main"}),
            engine.repo.get(node.id),
            agent,
            path,
        )
        exists = git(remote, "show-ref", "--verify", "refs/heads/main").returncode == 0
        assert exists == (decision == "approve")
        assert result.ok == (decision == "approve"), result
        assert len([e for e in engine.trace.query(node.id) if e.kind == "approval"]) == 1
        if decision == "reject":
            assert result.error == "rejected_by_human"
    finally:
        engine.close()


async def test_push_rejects_unsafe_options_and_missing_remote(tmp_path):
    tools = {t.name: t for t in default_tools()}
    assert "git_push" in tools
    git(tmp_path, "init")
    ctx = ToolContext(agent_id="a", task_id="t", workspace=tmp_path)
    for args in (
        {"remote": "--force", "branch": "main"},
        {"remote": "origin", "branch": "-main"},
        {"remote": "missing", "branch": "main"},
    ):
        assert not (await tools["git_push"].run(args, ctx)).ok
