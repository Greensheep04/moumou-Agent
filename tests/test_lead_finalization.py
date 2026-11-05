"""Framework evidence helps the lead finish; parent checks remain authoritative."""

from copy import deepcopy

import pytest

from moumou.adapters import FakeLLM, GitWorkspaces, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.context import ContextBuilder, estimate_messages
from moumou.contracts import Acceptance, Budget, ContextPolicy, RoleSpec, TaskNode, TaskResult
from moumou.orchestrator import Engine, Settings

MARKER = "Framework closing handoff"
PLAN = {
    "roles": [
        {
            "name": "writer",
            "title": "Writer",
            "description": "write out.txt",
            "reason": "need a file writer",
            "tools": ["write_file", "submit_result"],
        }
    ],
    "subtasks": [
        {
            "key": "write",
            "role": "writer",
            "goal": "write out.txt",
            "deps": [],
            "acceptance": [{"kind": "file_exists", "target": "out.txt"}],
        }
    ],
}


def dep():
    return {
        "id": "t1.write",
        "parent_id": "t1",
        "role": "writer",
        "status": "DONE",
        "goal": "write out.txt",
        "artifacts": ["out.txt"],
        "result": TaskResult(summary="wrote file", files=["out.txt"]),
        "error": None,
        "acceptance": {
            "ok": True,
            "checks": [{"kind": "file_exists", "target": "out.txt", "ok": True}],
        },
        "delivery": {"ok": True, "files": ["out.txt"], "conflicts": [], "error": None},
    }


def context(deps, **kwargs):
    kwargs.setdefault("acceptance", [Acceptance(kind="file_exists", target="out.txt")])
    node = TaskNode(
        id="t1",
        project_id="p",
        conversation_id="c",
        role="planner",
        goal="deliver",
        deps=[d["id"] for d in deps],
        **kwargs,
    )
    role = RoleSpec(
        name="planner", title="Lead", description="lead", project_id="p", created_by="human"
    )
    return ContextBuilder().build(node, role, dep_reports=deps)


def test_parent_receives_trusted_checks_and_completion_guidance():
    brief = context([dep()]).messages()[1]["content"]
    assert "framework acceptance: passed" in brief
    assert "file_exists" in brief and "out.txt" in brief
    assert MARKER in brief and "submit_result" in brief
    assert "parent acceptance" in brief


def test_acceptance_expectations_are_visible_to_the_model():
    ctx = context(
        [],
        acceptance=[
            Acceptance(kind="json_key", target="proof.json", expect="passed=true"),
            Acceptance(kind="command", target="python3 app.py", expect="0"),
        ],
    )
    brief = ctx.messages()[1]["content"]
    assert "passed=true" in brief and 'expect="0"' in brief


@pytest.mark.parametrize(
    "problem",
    [
        "dead",
        "unchecked",
        "self_report",
        "failed_check",
        "delivery_conflict",
        "delivery_failed",
        "blocker",
        "not_child",
    ],
)
def test_incomplete_or_untrusted_handoffs_do_not_get_fast_finish_guidance(problem):
    item = dep()
    if problem == "dead":
        item["status"] = "DEAD"
    elif problem == "unchecked":
        item["acceptance"] = None
    elif problem == "self_report":
        item["acceptance"]["checks"] = [{"kind": "self_report", "ok": True}]
    elif problem == "failed_check":
        item["acceptance"] = {"ok": False, "checks": [{"kind": "file_exists", "ok": False}]}
    elif problem == "delivery_conflict":
        item["delivery"]["conflicts"] = ["out.txt"]
    elif problem == "delivery_failed":
        item["delivery"]["ok"] = False
    elif problem == "blocker":
        item["result"].blockers = ["manual decision needed"]
    elif problem == "not_child":
        item["parent_id"] = "other"
    assert MARKER not in context([item]).messages()[1]["content"]


def test_handoff_evidence_respects_context_budget_and_truncation():
    deps = []
    for i in range(6):
        item = deepcopy(dep())
        item["id"] = f"t1.c{i}"
        item["result"].summary = "report " * 1000
        item["acceptance"]["checks"][0]["stdout"] = "x" * 10000
        deps.append(item)
    node = TaskNode(
        id="t1",
        project_id="p",
        conversation_id="c",
        role="planner",
        goal="deliver",
        deps=[d["id"] for d in deps],
        acceptance=[Acceptance(kind="file_exists", target="out.txt")],
    )
    role = RoleSpec(
        name="planner", title="Lead", description="lead", project_id="p", created_by="human"
    )
    ctx = ContextBuilder(ContextPolicy(opening_budget_tokens=4000)).build(node, role, deps)
    assert estimate_messages(ctx.messages()) <= 4000
    assert MARKER not in ctx.messages()[1]["content"]


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("content,expected_done,expected_dead", [("good", 2, 0), ("bad", 1, 1)])
async def test_direct_lead_submission_still_executes_parent_acceptance(
    tmp_path, content, expected_done, expected_dead, strict
):
    closing_briefs = []

    def respond(messages, tools):
        system, brief = messages[0]["content"], messages[1]["content"]
        if "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" in brief:
                return LLMResponse(tool_calls=[ToolCall(id="plan", name="submit_plan", args=PLAN)])
            closing_briefs.append(brief)
            assert MARKER in brief and "framework acceptance: passed" in brief
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="submit_result",
                        args={"summary": "deliver assembled artifacts", "files": ["out.txt"]},
                    )
                ]
            )
        if not any(m["role"] == "assistant" for m in messages):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write", name="write_file", args={"path": "out.txt", "content": content}
                    )
                ]
            )
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="child-done",
                    name="submit_result",
                    args={"summary": "wrote file", "files": ["out.txt"]},
                )
            ]
        )

    llm = FakeLLM(respond)
    settings = Settings(
        root=tmp_path / "run", project_id="p", memory_enabled=False, role_review_enabled=False,
        require_subtask_acceptance=strict,
    )
    with Engine(
        llm,
        workspace=GitWorkspaces(tmp_path / "workspaces"),
        human=ScriptedHuman(),
        settings=settings,
    ) as engine:
        engine.submit(
            "deliver a good file",
            budget=Budget(max_retries=0),
            acceptance=[Acceptance(kind="file_contains", target="out.txt", expect="good")],
        )
        report = await engine.run()
        assert report.done == expected_done and report.dead == expected_dead
        assert len(closing_briefs) == 1
        verdicts = [
            e for e in engine.trace.all_events() if e.task_id == "t1" and e.name == "verdict"
        ]
        assert len(verdicts) == 1
        assert verdicts[0].payload["ok"] == (content == "good")
        parent_tools = [
            e for e in engine.trace.all_events() if e.task_id == "t1" and e.kind == "tool"
        ]
        assert parent_tools == []


def test_no_parent_machine_criteria_does_not_get_fast_finish_guidance():
    assert MARKER not in context([dep()], acceptance=[]).messages()[1]["content"]


def test_delivery_error_is_visible_and_prevents_fast_finish():
    item = dep()
    item["delivery"]["error"] = "destination unavailable"
    brief = context([item]).messages()[1]["content"]
    assert "artifact delivery: failed" in brief and "destination unavailable" in brief
    assert MARKER not in brief


def test_delivery_conflict_is_visible_to_the_lead():
    item = dep()
    item["delivery"]["conflicts"] = ["out.txt"]
    brief = context([item]).messages()[1]["content"]
    assert "artifact delivery: failed" in brief and "out.txt" in brief
    assert MARKER not in brief


async def test_failed_provider_handoff_is_not_described_as_successful(tmp_path):
    from moumou.adapters import LocalDirs
    from moumou.contracts import IntegrateResult

    class FailedHandoff(LocalDirs):
        def integrate(self, src, dst, artifacts):
            return IntegrateResult(ok=False, error="destination unavailable")

    seen = []

    def respond(messages, tools):
        system, brief = messages[0]["content"], messages[1]["content"]
        if "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" in brief:
                return LLMResponse(tool_calls=[ToolCall(id="plan", name="submit_plan", args=PLAN)])
            seen.append(brief)
            return LLMResponse(content="delivery failed")
        if not any(m["role"] == "assistant" for m in messages):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write", name="write_file", args={"path": "out.txt", "content": "good"}
                    )
                ]
            )
        return LLMResponse(content="written")

    with Engine(
        FakeLLM(respond),
        workspace=FailedHandoff(tmp_path / "workspaces"),
        settings=Settings(root=tmp_path / "run", memory_enabled=False, role_review_enabled=False),
    ) as engine:
        engine.submit(
            "deliver",
            budget=Budget(max_retries=0),
            acceptance=[Acceptance(kind="file_exists", target="out.txt")],
        )
        result = await engine.run()
        assert result.done == 1 and result.dead == 1 and seen
        assert "artifact delivery: failed" in seen[0] and MARKER not in seen[0]
        assert not engine.repo.get("t1").error.startswith("crash:")
