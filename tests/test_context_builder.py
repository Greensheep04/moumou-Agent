"""Admission rules for the four context layers, and the opening budget."""

from __future__ import annotations

from moumou.context import Compactor, ContextBuilder, TokenBudget, estimate_messages
from moumou.contracts import Acceptance, ContextPolicy, MemoryRecord, RoleSpec, TaskNode

NO_DEPS = "(this task has no upstream dependencies)"


def a_role() -> RoleSpec:
    return RoleSpec(
        name="python_dev",
        title="Python developer",
        description="writes small programs",
        project_id="p",
        created_by="human",
    )


def a_node(**kw) -> TaskNode:
    base = dict(
        id="t1",
        project_id="p",
        conversation_id="c",
        goal="build the thing",
        role="python_dev",
    )
    return TaskNode(**{**base, **kw})


def a_dep(dep_id="d1", status="DONE", result="", artifacts=None, error=None) -> dict:
    return {
        "id": dep_id,
        "role": "python_dev",
        "status": status,
        "goal": f"goal of {dep_id}",
        "artifacts": artifacts or [],
        "result": result,
        "error": error,
    }


def a_memory(lesson: str, score: float = 1.0) -> MemoryRecord:
    return MemoryRecord(
        project_id="p",
        role="python_dev",
        task_id="t0",
        situation="s",
        action="a",
        outcome="success",
        lesson=lesson,
        score=score,
    )


def builder(**policy_kw) -> ContextBuilder:
    return ContextBuilder(ContextPolicy(**policy_kw), TokenBudget())


def test_all_four_layers_reach_the_prompt():
    ctx = builder().build(
        a_node(acceptance=[Acceptance(kind="file_exists", target="out.txt")]),
        a_role(),
        dep_reports=[a_dep(result="did the first half")],
        memory_hits=[a_memory("check the mirror before installing")],
        board_notes=["coder#1 already scaffolded the package"],
    )
    system = ctx.messages()[0]["content"]
    brief = ctx.messages()[1]["content"]

    assert "python_dev" in system                      # role layer
    assert "check the mirror before installing" in system   # experience layer
    assert "coder#1 already scaffolded" in system      # shared board
    assert "build the thing" in brief                  # task layer
    assert "out.txt" in brief
    assert "did the first half" in brief


def test_a_dead_dependency_contributes_only_a_failure_line():
    long_report = "x" * 5_000
    ctx = builder().build(
        a_node(),
        a_role(),
        dep_reports=[a_dep(status="DEAD", result=long_report, error="tests kept failing")],
    )
    brief = ctx.messages()[1]["content"]

    assert "FAILED" in brief
    assert "tests kept failing" in brief
    assert long_report[:200] not in brief, "a dead dependency must not dump its whole report"


def test_upstream_artifacts_appear_as_paths_only():
    ctx = builder().build(
        a_node(),
        a_role(),
        dep_reports=[a_dep(artifacts=["src/app.py", "tests/test_app.py"])],
    )
    brief = ctx.messages()[1]["content"]

    assert "src/app.py" in brief
    assert "tests/test_app.py" in brief


def test_the_no_upstream_marker_is_stable():
    """Model prompts and the demo script both key off this exact sentence."""
    ctx = builder().build(a_node(), a_role(), dep_reports=[])
    assert NO_DEPS in ctx.messages()[1]["content"]


def test_without_memory_there_is_no_lessons_section():
    ctx = builder().build(a_node(), a_role(), memory_hits=[])
    assert "Lessons from previous runs" not in ctx.messages()[0]["content"]


def test_cited_memories_are_remembered_for_scoring():
    m = a_memory("retry with a mirror")
    ctx = builder().build(a_node(), a_role(), memory_hits=[m])
    assert ctx.memory_ids == [m.id]


def test_the_pinned_prefix_is_exactly_role_plus_brief():
    ctx = builder().build(a_node(), a_role())
    assert ctx.pinned == 2
    assert ctx.messages()[0]["role"] == "system"
    assert ctx.messages()[1]["role"] == "user"


def test_opening_budget_drops_lessons_before_it_touches_the_role():
    lessons = [a_memory(f"LESSON{i} " + "x" * 400) for i in range(5)]
    ctx = builder(opening_budget_tokens=600).build(a_node(), a_role(), memory_hits=lessons)

    system = ctx.messages()[0]["content"]
    assert "Python developer" in system, "the role layer is never traded away"
    assert system.count("LESSON") <= 2, "lessons are the first thing to go"


def test_opening_budget_keeps_the_whole_prompt_under_the_cap():
    lessons = [a_memory(f"LESSON{i} " + "x" * 400) for i in range(5)]
    deps = [a_dep(dep_id=f"d{i}", result="y" * 800) for i in range(6)]

    ctx = builder(opening_budget_tokens=900).build(
        a_node(), a_role(), dep_reports=deps, memory_hits=lessons
    )
    assert estimate_messages(ctx.messages()) <= 900


def test_maybe_compact_is_a_noop_below_the_trigger():
    ctx = builder().build(a_node(), a_role())
    compactor = Compactor(TokenBudget(), ContextPolicy())

    result = ctx.maybe_compact(compactor)

    assert not result.applied
    assert ctx.compactions == 0
    assert len(ctx.messages()) == 2


class FakeBoard:
    def __init__(self, notes):
        self.notes = notes

    def scope(self, project_id, conversation_id, role):
        return f"{project_id}:{conversation_id}:{role}"

    def recent(self, scope, limit=3):
        return self.notes[:limit]


def test_board_notes_are_pulled_in_automatically():
    board = FakeBoard(["t9 [DONE]: scaffolded the package | files: src/pkg/__init__.py"])
    ctx = ContextBuilder(ContextPolicy(), TokenBudget(), board=board).build(a_node(), a_role())

    assert "scaffolded the package" in ctx.messages()[0]["content"]
