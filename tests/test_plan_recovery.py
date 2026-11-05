"""Planning schema agrees with runtime validation; rejected plans can be corrected."""

from copy import deepcopy

import pytest

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance, Budget
from moumou.orchestrator import Engine, Settings
from moumou.tools import default_tools

PLAN = {
    "reasoning": "one specialist writes the requested file",
    "roles": [
        {
            "name": "writer",
            "title": "Writer",
            "description": "writes the deliverable",
            "reason": "no existing role writes files",
            "tools": ["write_file", "submit_result"],
        }
    ],
    "subtasks": [
        {
            "key": "write",
            "goal": "write out.txt containing done",
            "role": "writer",
            "deps": [],
            "acceptance": [{"kind": "file_exists", "target": "out.txt"}],
        }
    ],
}


def acceptance_schema():
    tool = next(t for t in default_tools() if t.name == "submit_plan")
    return tool.params_schema["properties"]["subtasks"]["items"]["properties"]["acceptance"][
        "items"
    ]


def test_published_schema_requires_non_null_expect_only_for_relevant_kinds():
    schema = acceptance_schema()
    branches = schema.get("anyOf", [])
    for kind in ("command", "file_contains", "json_key"):
        matching = [
            b for b in branches if kind in b.get("properties", {}).get("kind", {}).get("enum", [])
        ]
        assert matching, f"schema has no conditional constraint for {kind}"
        assert all("expect" in b.get("required", []) for b in matching)
        assert all(b["properties"]["expect"]["type"] == "string" for b in matching)
    for kind in ("pytest", "file_exists", "file_not_exists"):
        matching = [
            b for b in branches if kind in b.get("properties", {}).get("kind", {}).get("enum", [])
        ]
        assert matching and any("expect" not in b.get("required", []) for b in matching)
    assert "$ref" not in str(schema)


@pytest.mark.parametrize("kind", ["command", "file_contains", "json_key"])
def test_runtime_keeps_missing_and_null_expect_invalid(kind):
    for values in ({"kind": kind, "target": "x"}, {"kind": kind, "target": "x", "expect": None}):
        with pytest.raises(ValueError, match="requires `expect`"):
            Acceptance(**values)
    assert Acceptance(kind=kind, target="x", expect="0").expect == "0"


def invalid_plan(failure):
    plan = deepcopy(PLAN)
    if failure == "missing_expect":
        plan["subtasks"][0]["acceptance"] = [{"kind": "command", "target": "python3 x.py"}]
    elif failure == "missing_acceptance":
        plan["subtasks"][0].pop("acceptance")
    elif failure == "cycle":
        plan["subtasks"][0]["deps"] = ["other"]
        plan["subtasks"].append(dict(plan["subtasks"][0], key="other", deps=["write"]))
    elif failure == "unknown_tool":
        plan["roles"][0]["tools"] = ["unregistered_tool"]
    return plan


@pytest.mark.parametrize(
    "failure,fragment",
    [
        ("missing_expect", "expect"),
        ("missing_acceptance", "acceptance"),
        ("cycle", "cycle"),
        ("unknown_tool", "unknown tools"),
    ],
)
async def test_invalid_plan_feedback_allows_correction_without_task_retry(
    tmp_path, failure, fragment
):
    rejected = invalid_plan(failure)
    seen = []

    def respond(messages, tools):
        system = messages[0]["content"]
        brief = messages[1]["content"]
        if "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" not in brief:
                return LLMResponse(content="finished")
            feedback = [m for m in messages if m["role"] == "tool"]
            if not feedback:
                return LLMResponse(
                    tool_calls=[ToolCall(id="bad-plan", name="submit_plan", args=rejected)]
                )
            seen.append(feedback[-1])
            assert feedback[-1]["tool_call_id"] == "bad-plan"
            assert "ok=false" in feedback[-1]["content"]
            assert "invalid_plan" in feedback[-1]["content"]
            assert fragment in feedback[-1]["content"]
            assert engine.repo.get("t1").status == "RUNNING"
            assert engine.repo.children("t1") == []
            assert [r.name for r in engine.roles.list("p")] == ["planner"]
            return LLMResponse(
                tool_calls=[ToolCall(id="fixed-plan", name="submit_plan", args=PLAN)]
            )
        if not any(m["role"] == "assistant" for m in messages):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write", name="write_file", args={"path": "out.txt", "content": "done"}
                    )
                ]
            )
        return LLMResponse(content="file written")

    with Engine(
        FakeLLM(respond),
        human=ScriptedHuman(),
        settings=Settings(
            root=tmp_path / "run",
            project_id="p",
            require_subtask_acceptance=(failure == "missing_acceptance"),
            memory_enabled=False,
            role_review_enabled=False,
        ),
    ) as engine:
        engine.submit(
            "delegate writing out.txt",
            budget=Budget(max_steps=5, max_retries=0),
            acceptance=[Acceptance(kind="file_contains", target="out.txt", expect="done")],
        )
        result = await engine.run()
        assert result.done == 2 and result.dead == 0
        assert seen and len(seen) == 1
        events = engine.trace.all_events()
        assert sum(e.name == "plan_rejected" for e in events) == 1
        assert sum(e.name == "plan_applied" for e in events) == 1
        assert all(task["retries"] == 0 for task in result.tasks)


@pytest.mark.parametrize("failure", ["missing_expect", "missing_acceptance"])
async def test_repeated_invalid_plans_stop_at_existing_step_budget(tmp_path, failure):
    llm = FakeLLM(
        lambda messages, tools: LLMResponse(
            tool_calls=[
                ToolCall(
                    id=f"bad-{len(messages)}",
                    name="submit_plan",
                    args=invalid_plan(failure),
                )
            ]
        )
    )
    with Engine(
        llm,
        settings=Settings(
            root=tmp_path / "run", memory_enabled=False, role_review_enabled=False,
            require_subtask_acceptance=True,
        ),
    ) as engine:
        engine.submit("delegate", budget=Budget(max_steps=3, max_retries=0))
        result = await engine.run()
        assert result.total == 1 and result.dead == 1
        assert len(llm.calls) == 3
        assert "max_steps" in engine.repo.get("t1").error
        assert sum(e.name == "plan_rejected" for e in engine.trace.all_events()) == 3
        assert engine.repo.children("t1") == []


def test_plan_schema_guides_machine_acceptance_without_breaking_default():
    tool = next(t for t in default_tools() if t.name == "submit_plan")
    field = tool.params_schema["properties"]["subtasks"]["items"]["properties"]["acceptance"]
    assert "machine" in field.get("description", "").lower()
    assert "self_report" in field["description"]
    assert Settings().require_subtask_acceptance is False


async def test_strict_plan_still_requires_the_child_check_to_pass(tmp_path):
    plan = deepcopy(PLAN)
    plan["subtasks"][0]["acceptance"] = [
        {"kind": "file_contains", "target": "out.txt", "expect": "required content"}
    ]

    def respond(messages, tools):
        if "You are the lead agent" in messages[0]["content"]:
            if "(this task has no upstream dependencies)" in messages[1]["content"]:
                return LLMResponse(tool_calls=[ToolCall(id="plan", name="submit_plan", args=plan)])
            return LLMResponse(content="child failed")
        if not any(m["role"] == "assistant" for m in messages):
            return LLMResponse(tool_calls=[ToolCall(
                id="write", name="write_file", args={"path": "out.txt", "content": "wrong"}
            )])
        return LLMResponse(tool_calls=[ToolCall(
            id="finish", name="submit_result", args={"summary": "written", "files": ["out.txt"]}
        )])

    with Engine(
        FakeLLM(respond),
        settings=Settings(
            root=tmp_path / "run", memory_enabled=False, role_review_enabled=False,
            require_subtask_acceptance=True,
        ),
    ) as engine:
        engine.submit(
            "delegate writing the required content",
            budget=Budget(max_steps=4, max_retries=0),
            acceptance=[
                Acceptance(kind="file_contains", target="out.txt", expect="required content")
            ],
        )
        report = await engine.run()
        assert report.done == 0 and report.dead == 2
        child = engine.repo.get("t1.write")
        assert child.status == "DEAD"
        verdicts = [
            e for e in engine.trace.all_events() if e.task_id == child.id and e.name == "verdict"
        ]
        assert len(verdicts) == 1 and verdicts[0].payload["ok"] is False
        assert verdicts[0].payload["checks"][0]["kind"] == "file_contains"
