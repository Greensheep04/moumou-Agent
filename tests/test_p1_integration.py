"""P1 providers wired into the existing engine without changing shared contracts."""

from test_orchestration import sequential_llm

from moumou.adapters import FakeLLM, GitWorkspaces, ScriptedHuman
from moumou.contracts import Acceptance, ToolContext
from moumou.demo.skeleton import responder
from moumou.orchestrator import Engine, Settings
from moumou.tools import default_tools


async def test_parallel_agents_deliver_to_git_parent_and_script_runs(tmp_path):
    ws = GitWorkspaces(tmp_path / "workspaces")
    settings = Settings(
        project_id="p",
        conversation_id="c",
        root=tmp_path / "run",
        memory_enabled=False,
        role_review_enabled=False,
    )
    with Engine(
        FakeLLM(responder), workspace=ws, human=ScriptedHuman(), settings=settings
    ) as engine:
        engine.submit(
            "build hello",
            acceptance=[
                Acceptance(kind="file_exists", target="hello.py"),
                Acceptance(kind="file_exists", target="README.md"),
            ],
        )
        report = await engine.run()
        assert report.done == 3 and report.dead == 0
        parent = ws.allocate("p", "c", "planner@t1")
        assert (parent / "hello.py").exists() and (parent / "README.md").exists()
        tools = {t.name: t for t in default_tools()}
        result = await tools["run_python"].run(
            {"path": "hello.py"}, ToolContext(agent_id="planner@t1", task_id="t1", workspace=parent)
        )
        assert result.ok and result.stdout.strip() == "hello, moumou"
        graph = ws.git(parent, "log", "--oneline", "--graph").stdout
        assert "P1 declared artifact delivery" in graph
        assert any(e.name == "integrated_into_parent" for e in engine.trace.all_events())


async def test_dependency_artifacts_arrive_in_git_workspace(tmp_path):
    ws = GitWorkspaces(tmp_path / "workspaces")
    with Engine(
        sequential_llm(),
        workspace=ws,
        human=ScriptedHuman(),
        settings=Settings(
            project_id="p",
            conversation_id="c",
            root=tmp_path / "run",
            memory_enabled=False,
            role_review_enabled=False,
        ),
    ) as engine:
        engine.submit("build and verify")
        report = await engine.run()
        assert report.done == 3 and report.dead == 0
        verify = ws.allocate("p", "c", "python_dev@t1.verify")
        assert (verify / "hello.py").read_text().strip() == "print('hello, moumou')"
        assert any(e.name == "materialised_from_dep" for e in engine.trace.all_events())
