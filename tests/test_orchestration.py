"""End-to-end orchestration against a deterministic fake model.

These tests are the real contract: if they pass, the four ports (Tool, Channel,
Store, HumanIO) are wired correctly and P1/P2/P3 can plug in without surprises.
"""

from __future__ import annotations

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance, Message
from moumou.orchestrator import Engine, Settings


def settings(tmp_path, **kw) -> Settings:
    return Settings(project_id="p", conversation_id="c", root=tmp_path / "run", **kw)


def steps(messages: list[dict]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def writer_llm(path: str, content: str = "hi") -> FakeLLM:
    """Writes one file, then reports done."""

    def responder(messages, tools):
        if steps(messages) == 0:
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="write_file", args={"path": path, "content": content})
                ]
            )
        return LLMResponse(content=f"wrote {path}")

    return FakeLLM(responder)


async def test_single_task_reaches_done(tmp_path):
    engine = Engine(writer_llm("out.txt"), settings=settings(tmp_path))
    try:
        engine.submit("write a file", acceptance=[Acceptance(kind="file_exists", target="out.txt")])
        report = await engine.run()
    finally:
        engine.close()

    assert report.done == 1
    assert report.dead == 0
    assert report.completion_rate == 1.0


async def test_failed_acceptance_retries_then_dies(tmp_path):
    # The model reports success but never produces the file.
    engine = Engine(
        FakeLLM(lambda m, t: LLMResponse(content="all done!")), settings=settings(tmp_path)
    )
    try:
        engine.submit(
            "lie about it", acceptance=[Acceptance(kind="file_exists", target="ghost.txt")]
        )
        report = await engine.run()
    finally:
        engine.close()

    assert report.done == 0
    assert report.dead == 1
    assert report.tasks[0]["retries"] == 2          # 3 attempts, then give up


async def test_risky_tool_parks_on_human_and_honours_rejection(tmp_path):
    human = ScriptedHuman(default="reject")

    def responder(messages, tools):
        if steps(messages) == 0:
            return LLMResponse(
                tool_calls=[ToolCall(id="s1", name="shell", args={"command": "echo hi"})]
            )
        return LLMResponse(content="could not run it")

    engine = Engine(FakeLLM(responder), human=human, settings=settings(tmp_path))
    try:
        engine.submit(
            "run a command", acceptance=[Acceptance(kind="file_exists", target="nope.txt")]
        )
        report = await engine.run()
    finally:
        engine.close()

    assert report.dead == 1
    assert human.requests, "the human should have been asked"
    assert all(r.action_class == "shell" for r in human.requests)


async def test_approval_transitions_are_audited(tmp_path):
    human = ScriptedHuman(default="reject")

    def responder(messages, tools):
        if steps(messages) == 0:
            return LLMResponse(
                tool_calls=[ToolCall(id="s1", name="shell", args={"command": "echo hi"})]
            )
        return LLMResponse(content="blocked")

    engine = Engine(FakeLLM(responder), human=human, settings=settings(tmp_path))
    try:
        task = engine.submit("run a command")
        await engine.run()
        log = engine.repo.event_log(task.id)
        approvals = [e for e in engine.trace.query(task.id) if e.kind == "approval"]
    finally:
        engine.close()

    reasons = [e["reason"] for e in log]
    assert any(r.startswith("approve:shell") for r in reasons)
    assert any(r == "human:reject" for r in reasons)
    assert approvals and approvals[0].payload["decision"] == "reject"


async def test_benign_tool_does_not_ask(tmp_path):
    human = ScriptedHuman(default="reject")      # would block if it were consulted
    engine = Engine(writer_llm("ok.txt"), human=human, settings=settings(tmp_path))
    try:
        engine.submit("write", acceptance=[Acceptance(kind="file_exists", target="ok.txt")])
        report = await engine.run()
    finally:
        engine.close()

    assert report.done == 1
    assert human.requests == []


PLAN = {
    "reasoning": "two independent deliverables",
    "roles": [
        {
            "name": "python_dev",
            "title": "Python developer",
            "description": "writes code",
            "tools": ["write_file"],
            "reason": "nothing existing writes code",
        },
        {
            "name": "tech_writer",
            "title": "Technical writer",
            "description": "writes docs",
            "tools": ["write_file"],
            "reason": "nothing existing writes docs",
        },
    ],
    "subtasks": [
        {
            "key": "code",
            "goal": "write hello.py",
            "role": "python_dev",
            "deps": [],
            "acceptance": [{"kind": "file_exists", "target": "hello.py"}],
        },
        {
            "key": "doc",
            "goal": "write README.md",
            "role": "tech_writer",
            "deps": [],
            "acceptance": [{"kind": "file_exists", "target": "README.md"}],
        },
    ],
}


def delegating_llm() -> FakeLLM:
    def responder(messages, tools):
        system, brief = messages[0]["content"], messages[1]["content"]

        if "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" in brief:
                return LLMResponse(tool_calls=[ToolCall(id="p", name="submit_plan", args=PLAN)])
            return LLMResponse(content="both deliverables are ready")

        name = "hello.py" if "role: python_dev" in system else "README.md"
        if steps(messages) == 0:
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="w", name="write_file", args={"path": name, "content": "hello"})
                ]
            )
        return LLMResponse(content=f"wrote {name}")

    return FakeLLM(responder)


async def test_planner_invents_roles_and_a_dag(tmp_path):
    engine = Engine(delegating_llm(), settings=settings(tmp_path))
    try:
        root = engine.submit("build a hello project")
        report = await engine.run()
        roles = [r.name for r in engine.roles.list("p")]
        children = engine.repo.children(root.id)
    finally:
        engine.close()

    assert report.total == 3
    assert report.done == 3
    assert {"python_dev", "tech_writer"} <= set(roles)
    assert {c.id for c in children} == {"t1.code", "t1.doc"}
    assert root.id == "t1"


async def test_parent_waits_for_children_then_reviews(tmp_path):
    engine = Engine(delegating_llm(), settings=settings(tmp_path))
    try:
        root = engine.submit("build a hello project")
        await engine.run()
        parent = engine.repo.get(root.id)
        log = [e["reason"] for e in engine.repo.event_log(root.id)]
    finally:
        engine.close()

    assert parent.status == "DONE"
    assert "waiting_for_children" in log
    assert parent.result and "deliverables are ready" in parent.result.summary


async def test_children_run_in_separate_workspaces(tmp_path):
    engine = Engine(delegating_llm(), settings=settings(tmp_path))
    try:
        root = engine.submit("build a hello project")
        await engine.run()
        children = engine.repo.children(root.id)
    finally:
        engine.close()

    ws_root = tmp_path / "run" / "workspaces" / "p" / "c"
    dirs = {p.name for p in ws_root.iterdir() if p.is_dir()}
    # planner owns one, each specialist owns its own; none is shared
    assert len(dirs) == 3, f"expected one directory per agent instance, got {dirs}"
    assert {f"{c.role}@{c.id}" for c in children} <= dirs


# -- steering ---------------------------------------------------------------


async def test_steering_subscription_and_injection(tmp_path):
    engine = Engine(writer_llm("x.txt"), settings=settings(tmp_path))
    try:
        from moumou.context import ContextBuilder

        runner = engine.runner
        runner._subscribe("python_dev@t1")

        await engine.bus.publish(
            Message(
                project_id="p",
                conversation_id="c",
                sender="human",
                recipients=[],
                kind="steer",
                content="focus on the tests first",
            )
        )
        drained = runner._drain("python_dev@t1")
        assert [m.content for m in drained] == ["focus on the tests first"]

        ctx = ContextBuilder().build(_node(), _role())
        ctx.inject_steering([drained[0].content])
        assert any("[Human steering]" in str(m.get("content")) for m in ctx.messages())
    finally:
        engine.close()


def _node():
    from moumou.contracts import TaskNode

    return TaskNode(id="t1", project_id="p", conversation_id="c", goal="g", role="writer_role")


def _role():
    from moumou.contracts import RoleSpec

    return RoleSpec(
        name="writer_role", title="T", description="D", project_id="p", created_by="human"
    )


async def test_child_artifacts_are_integrated_into_the_parent(tmp_path):
    """The parent can only verify the combined result if the files reach it."""
    engine = Engine(delegating_llm(), settings=settings(tmp_path))
    try:
        engine.submit(
            "build a hello project",
            acceptance=[
                Acceptance(kind="file_exists", target="hello.py"),
                Acceptance(kind="file_contains", target="README.md", expect="hello"),
            ],
        )
        report = await engine.run()
    finally:
        engine.close()

    assert report.done == 3
    planner_ws = tmp_path / "run" / "workspaces" / "p" / "c" / "planner@t1"
    assert (planner_ws / "hello.py").is_file()
    assert (planner_ws / "README.md").is_file()


# -- sequential dependencies and clarification --------------------------------

PLAN_SEQ = {
    "reasoning": "write the script, then verify it actually runs",
    "roles": [
        {
            "name": "python_dev",
            "title": "Python developer",
            "description": "writes and runs small programs",
            "tools": ["write_file", "run_python"],
            "reason": "nothing existing writes code",
        }
    ],
    "subtasks": [
        {
            "key": "code",
            "goal": "write hello.py that prints hello, moumou",
            "role": "python_dev",
            "deps": [],
            "acceptance": [{"kind": "file_exists", "target": "hello.py"}],
        },
        {
            "key": "verify",
            "goal": "verify that hello.py runs cleanly",
            "role": "python_dev",
            "deps": ["code"],
            "acceptance": [{"kind": "command", "target": "python3 hello.py", "expect": "0"}],
        },
    ],
}


def sequential_llm() -> FakeLLM:
    def responder(messages, tools):
        system, brief = messages[0]["content"], messages[1]["content"]
        first_turn = "(this task has no upstream dependencies)" in brief

        if "You are the lead agent" in system:
            if first_turn:
                return LLMResponse(tool_calls=[ToolCall(id="p", name="submit_plan", args=PLAN_SEQ)])
            return LLMResponse(content="verified")

        if first_turn and steps(messages) == 0:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        args={"path": "hello.py", "content": "print('hello, moumou')\n"},
                    )
                ]
            )
        return LLMResponse(content="done")

    return FakeLLM(responder)


async def test_dependent_task_receives_upstream_artifacts(tmp_path):
    """task B depends on task A -> B's workspace must contain A's files."""
    engine = Engine(sequential_llm(), settings=settings(tmp_path))
    try:
        engine.submit("build and verify a hello script")
        report = await engine.run()
        verify = engine.repo.get("t1.verify")
    finally:
        engine.close()

    assert report.done == 3, "the verify task can only pass if hello.py reached it"
    assert verify.status == "DONE"

    ws = tmp_path / "run" / "workspaces" / "p" / "c" / "python_dev@t1.verify"
    assert (ws / "hello.py").is_file()


async def test_agent_can_ask_the_human_a_question(tmp_path):
    human = ScriptedHuman(answers={"filename": "out.txt"})
    llm = FakeLLM(lambda m, t: LLMResponse(content="ok") if steps(m) else LLMResponse(
        tool_calls=[ToolCall(id="q", name="ask_human", args={"question": "which filename?"})]
    ))

    engine = Engine(llm, human=human, settings=settings(tmp_path))
    try:
        engine.submit("create a file, but the name is ambiguous")
        report = await engine.run()
    finally:
        engine.close()

    assert report.done == 1
    assert human.requests and human.requests[0].kind == "question"
    # the answer must come back to the model as a tool result
    assert any("out.txt" in str(m.get("content")) for m in llm.calls[-1])


async def test_finished_tasks_land_on_the_conclusion_board(tmp_path):
    engine = Engine(writer_llm("out.txt"), settings=settings(tmp_path))
    try:
        task = engine.submit(
            "write out.txt", acceptance=[Acceptance(kind="file_exists", target="out.txt")]
        )
        await engine.run()
        scope = engine.board.scope("p", "c", "planner")
        notes = engine.board.recent(scope)
    finally:
        engine.close()

    assert notes, "every terminal task must leave a hand-off note"
    assert task.id in notes[0]
    assert "DONE" in notes[0]


async def test_an_agent_hands_off_a_structured_result(tmp_path):
    def responder(messages, tools):
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="r1",
                    name="submit_result",
                    args={
                        "summary": "created out.txt",
                        "files": ["out.txt"],
                        "notes": "contents are a placeholder",
                        "blockers": ["no tests yet"],
                    },
                )
            ]
        )

    engine = Engine(FakeLLM(responder), settings=settings(tmp_path))
    try:
        task = engine.submit("write a file")
        await engine.run()
        node = engine.repo.get(task.id)
    finally:
        engine.close()

    assert node.status == "DONE"
    assert node.result is not None
    assert node.result.summary == "created out.txt"
    assert node.result.files == ["out.txt"]
    assert node.result.blockers == ["no tests yet"]
