"""Quality safeguards enter the real teacher adapter without changing task sources."""

import json

from test_eval_cases import write_cases

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Budget, ToolContext, ToolResult
from moumou.eval.cases import load_cases
from moumou.eval.source_evidence import SourceEvidenceStore
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools
from moumou.tools.source import ReadSourceTool, SourceHistoryTool


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=args)])


async def test_teacher_final_handoff_rejects_missing_declared_companion_then_recovers(tmp_path):
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    client = FakeLLM([
        call("write_file", path="support.md", content="The research and its limitations."),
        call("write_file", path="final.md", content="See [support](support.md)."),
        call("submit_result", summary="First submission", files=["final.md"]),
        call("submit_result", summary="Complete explicit handoff",
             files=["final.md", "support.md"]),
    ])
    with Engine(client, settings=Settings(root=tmp_path / "run", memory_enabled=False,
                                         role_review_enabled=False)) as run:
        state = configure_team(run, case)
        root = submit_teacher_task(run, case, "Deliver a report and its supporting research",
                                   Budget(max_retries=0))
        await run.run()
        final = run.repo.get(root.id)
        assert final.status == "DONE"
        assert final.result.files == ["final.md", "support.md"]
        rejected = [event for event in run.trace.query(root.id)
                    if event.kind == "task" and event.name == "result_rejected"]
        assert len(rejected) == 1
        assert "support.md" in rejected[0].payload["error"]
        assert (state.workspaces[case.leader] / "support.md").read_text() == (
            "The research and its limitations."
        )
        assert {r.name: r.description for r in run.roles.list(root.project_id)} == {
            r.name: r.description for r in case.roles
        }


async def test_different_fixed_role_can_inspect_real_search_provenance_without_new_search(tmp_path):
    case = load_cases(write_cases(tmp_path / "cases"))[1]
    store = SourceEvidenceStore(tmp_path / "public-evidence")
    registry = ToolRegistry()
    registry.register_all(*default_tools(), SourceHistoryTool(store), ReadSourceTool(store))
    with Engine(FakeLLM([]), tools=registry, settings=Settings(
            root=tmp_path / "run", memory_enabled=False, role_review_enabled=False)) as run:
        state = configure_team(run, case)
        leader_ctx = ToolContext(agent_id=case.leader, task_id="t2",
                                 workspace=state.workspaces[case.leader])
        store.record_search("publicfact research", ToolResult(ok=True, stdout=json.dumps([{
            "title": "Original publicfact record", "url": "https://example.org/research",
            "snippet": "The search summary is not independent fact verification.",
        }])), leader_ctx, retrieved_at="2026-01-03T10:00:00+00:00",
                            request_id="original-request", origin="imported")
        reviewer = case.roles[2]
        result = await registry.get("source_history").run(
            {"query": "publicfact"}, ToolContext(
                agent_id=reviewer.name, task_id="t2.verify",
                workspace=state.workspaces[reviewer.name],
            ),
        )
        assert result.ok
        assert "https://example.org/research" in result.stdout
        assert "2026-01-03" in result.stdout
        assert "original-request" in result.stdout
        root = submit_teacher_task(run, case, "Verify publicfact evidence", Budget())
        opening = run.context.build(root, run.roles.get(root.project_id, root.role)).messages()
        assert "source_history" in opening[0]["content"]
        assert "read_source" in opening[0]["content"]
        assert case.task1 not in opening[1]["content"]
        for role in run.roles.list(root.project_id):
            assert {"source_history", "read_source"} <= set(role.tools)
