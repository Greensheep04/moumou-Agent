"""Assignment source availability and isolation, not model compliance or quality scores."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_eval_cases import write_cases

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.context import Compactor, ContextBuilder, TokenBudget
from moumou.context.lineage import (
    assignment_reference_records,
    collect_assignment_references,
)
from moumou.contracts import (
    Acceptance,
    Budget,
    ContextPolicy,
    HumanRequest,
    HumanResponse,
    MemoryRecord,
    RoleSpec,
    TaskNode,
    TaskResult,
    ToolResult,
)
from moumou.eval.cases import load_cases
from moumou.eval.team import TeamContextBuilder, configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings

ROOT_GOAL = (
    "SYNTHETIC editing fixture: retain the author's regional spelling. "
    "Whether an unpublished appendix exists is UNKNOWN; do not assert it is absent. "
    "SOURCE_TAIL_cafe\u0301"
)
MIDDLE_GOAL = "Check the spelling; an unpublished appendix is definitely absent."
LEAF_GOAL = "Report conflicts in the spelling review."
PRIVATE = "PARENT_PRIVATE_ANSWER_do_not_copy_or_treat_as_a_grant"
HEADER = "## Original assignment references (same task lineage)"


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=args)])


def settings(root):
    return Settings(root=root, project_id="p", conversation_id="group",
                    memory_enabled=False, role_review_enabled=False)


def record_private_answer(repo, node):
    request = HumanRequest(
        id="private-root-question", task_id=node.id, agent_id="root-agent",
        kind="question", summary="Private operator preference?",
        project_id=node.project_id, conversation_id=node.conversation_id, role=node.role,
    )
    repo.record_human_request(node, request)
    repo.record_human_response(node, HumanResponse(request_id=request.id, text=PRIVATE))


def chain():
    root = TaskNode(id="root", project_id="p", conversation_id="group",
                    role="lead", goal=ROOT_GOAL,
                    acceptance=[Acceptance(kind="file_exists", target="PRIVATE_ACCEPTANCE")],
                    artifacts=["PRIVATE_FILE"], error="PRIVATE_ERROR",
                    result=TaskResult(summary="PRIVATE_RESULT"))
    child = TaskNode(id="child", project_id="p", conversation_id="group",
                     parent_id=root.id, depth=1, role="reviewer", goal=MIDDLE_GOAL)
    nodes = {node.id: node for node in (root, child)}
    return root, child, SimpleNamespace(get=nodes.get)


async def test_actual_child_grandchild_requests_keep_exact_conflicting_goals_and_private_history(
    tmp_path,
):
    planned_root = planned_middle = False
    child_openings = []
    root_openings = []

    def script(messages, tools):
        nonlocal planned_root, planned_middle
        system, opening = messages[0]["content"], messages[1]["content"]
        if "LINEAGE_TEST_LEAF" in system:
            assert opening.count(ROOT_GOAL) == opening.count(MIDDLE_GOAL) == 1
            assert LEAF_GOAL in opening
            assert PRIVATE not in json.dumps(messages)
            child_openings.append(deepcopy(messages))
            return call("submit_result", summary="Both conflicting source goals were visible")
        if "LINEAGE_TEST_MIDDLE" in system:
            assert ROOT_GOAL in opening and MIDDLE_GOAL in opening
            assert PRIVATE not in json.dumps(messages)
            child_openings.append(deepcopy(messages))
            if not planned_middle:
                planned_middle = True
                return call("submit_plan", reasoning="Delegate a conflict check", roles=[],
                            subtasks=[{"key": "leaf", "role": "leaf", "goal": LEAF_GOAL,
                                       "deps": [], "acceptance": []}])
            return call("submit_result", summary="Reviewed the returned check")
        root_openings.append(deepcopy(messages))
        assert PRIVATE in opening
        assert HEADER not in opening
        if not planned_root:
            planned_root = True
            return call(
                "submit_plan", reasoning="Review in two levels",
                roles=[
                    {"name": "middle", "title": "Reviewer",
                     "description": "LINEAGE_TEST_MIDDLE", "reason": "review",
                     "tools": ["submit_plan", "submit_result"]},
                    {"name": "leaf", "title": "Checker",
                     "description": "LINEAGE_TEST_LEAF", "reason": "check",
                     "tools": ["submit_result"]},
                ],
                subtasks=[{"key": "middle", "role": "middle", "goal": MIDDLE_GOAL,
                           "deps": [], "acceptance": []}],
            )
        return call("submit_result", summary="Integrated")

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        root = run.submit(ROOT_GOAL, budget=Budget(max_retries=0))
        record_private_answer(run.repo, root)
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        grandchild = next(node for node in run.repo.all_tasks() if node.depth == 2)
        assert grandchild.goal == LEAF_GOAL and grandchild.acceptance == []
        assert run.repo.get(grandchild.parent_id).goal == MIDDLE_GOAL
        admitted = [event for event in run.trace.query(grandchild.id)
                    if event.name == "memory_context_selected"]
        assert admitted[0].payload["assignment_reference_ids"] == [root.id, grandchild.parent_id]
        assert len(root_openings) == 2 and len(child_openings) == 3


async def test_child_acceptance_retry_rebuilds_the_same_assignment_reference(tmp_path):
    planned = failed_once = False
    openings = []

    def script(messages, tools):
        nonlocal planned, failed_once
        if "LINEAGE_RETRY_WRITER" in messages[0]["content"]:
            assert ROOT_GOAL in messages[1]["content"]
            if len(messages) == 2:
                openings.append(messages[1]["content"])
            if not failed_once:
                failed_once = True
                return LLMResponse(content="First attempt deliberately lacks output")
            if messages[-1]["role"] != "tool":
                return call("write_file", path="answer.txt", content="checked")
            return call("submit_result", summary="Written", files=["answer.txt"])
        if not planned:
            planned = True
            return call(
                "submit_plan", reasoning="Write one output",
                roles=[{"name": "writer", "title": "Writer",
                        "description": "LINEAGE_RETRY_WRITER", "reason": "write",
                        "tools": ["write_file", "submit_result"]}],
                subtasks=[{"key": "write", "role": "writer", "goal": "Write answer.txt",
                           "deps": [], "acceptance": [
                               {"kind": "file_exists", "target": "answer.txt"},
                           ]}],
            )
        return call("submit_result", summary="Delivered", files=["answer.txt"])

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        root = run.submit(ROOT_GOAL, budget=Budget(max_retries=1))
        await run.run()
        child = run.repo.get(root.id + ".write")
        assert child.status == "DONE" and child.retries == 1
        assert run.repo.get(root.id).status == "DONE"
        assert len(openings) == 2 and openings[0] == openings[1]


@pytest.mark.parametrize("kind", ["generic", "teacher"])
def test_complete_references_are_pinned_through_compaction_and_replacement(tmp_path, kind):
    root, node, repo = chain()
    references = collect_assignment_references(repo, node)
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    role = RoleSpec(name=node.role, title="Reviewer", description="Review one document",
                    project_id=node.project_id, created_by="human")
    policy = ContextPolicy(
        context_window=20000, max_output_tokens=1000, opening_budget_tokens=12000,
        compact_trigger_tokens=3000, compact_target_tokens=2000,
    )
    budget = TokenBudget()
    builder = (ContextBuilder(policy, budget) if kind == "generic" else
               TeamContextBuilder(policy, budget, scenario=case,
                                  workspaces={role.name: tmp_path / "workspace"}))
    original = node.model_dump(mode="json")
    context = builder.build(node, role, assignment_references=references)
    pinned = deepcopy(context.messages()[:2])
    assert node.model_dump(mode="json") == original
    assert ROOT_GOAL in pinned[1]["content"] and MIDDLE_GOAL in pinned[1]["content"]
    assert all(value not in json.dumps(pinned) for value in (
        "PRIVATE_ACCEPTANCE", "PRIVATE_FILE", "PRIVATE_ERROR", "PRIVATE_RESULT",
    ))
    for number in range(8):
        tool_id = f"old-{number}"
        context.add_assistant("", [ToolCall(id=tool_id, name="echo", args={})])
        context.add_tool_result(tool_id, ToolResult(ok=True, stdout="old observation " * 2000))
    assert context.maybe_compact(Compactor(budget, policy)).applied
    assert context.messages()[:2] == pinned
    context.replace_opening(builder.build(node, role, assignment_references=references))
    assert context.messages()[:2] == pinned
    if kind == "teacher":
        archives = list((tmp_path / "workspace/.evaluation/context").glob("*.json"))
        assert len(archives) == 1, "lineage alone must trigger recoverable evidence"
        raw = archives[0].read_bytes()
        assert archives[0].stem == hashlib.sha256(raw).hexdigest()[:32]
        evidence = json.loads(raw)
        assert evidence["assignment_references"] == assignment_reference_records(node, references)
        assert evidence["assignment_references"][0]["goal"] == root.goal
        assert evidence["recorded_human_exchanges"] == []


@pytest.mark.parametrize("damage", ["missing", "project", "conversation", "cycle", "depth"])
async def test_invalid_lineage_fails_before_a_model_call(tmp_path, damage):
    calls = []

    def forbidden(messages, tools):
        calls.append(messages)
        raise AssertionError("No model call may follow corrupt lineage admission")

    with Engine(FakeLLM(forbidden), settings=settings(tmp_path / "run")) as run:
        bootstrap = run.submit("Unrelated root bootstrap")
        parent = TaskNode(id="parent", project_id="p", conversation_id="group",
                          role=bootstrap.role, goal="PRIVATE_OTHER_SCOPE_GOAL")
        child = TaskNode(id="child", project_id="p", conversation_id="group",
                         parent_id=parent.id, depth=1, role=bootstrap.role,
                         goal="Short child task", status="RUNNING")
        if damage == "project":
            parent = parent.model_copy(update={"project_id": "another-project"})
        elif damage == "conversation":
            parent = parent.model_copy(update={"conversation_id": "private-conversation"})
        elif damage == "cycle":
            parent = parent.model_copy(update={"parent_id": child.id, "depth": 1})
            child = child.model_copy(update={"depth": 2})
        elif damage == "depth":
            parent = parent.model_copy(update={"depth": 1})
        if damage != "missing":
            run.repo.add(parent)
        run.repo.add(child)
        await run.runner.run(child)
        saved = run.repo.get(child.id)
        assert saved.status == "FAILED" and "assignment lineage" in saved.error
        assert calls == []
        assert not [event for event in run.trace.query(child.id) if event.kind == "llm"]


@pytest.mark.parametrize("teacher", [False, True], ids=["generic", "teacher"])
async def test_oversized_reference_fails_before_model_instead_of_truncating(tmp_path, teacher):
    calls = []

    def forbidden(messages, tools):
        calls.append(messages)
        raise AssertionError("Mandatory reference cannot be trimmed into a model request")

    with Engine(FakeLLM(forbidden), settings=settings(tmp_path / "run")) as run:
        if teacher:
            case = load_cases(write_cases(tmp_path / "cases"))[0]
            state = configure_team(run, case)
            root = submit_teacher_task(
                run, case, ("exact original constraint " * 3000) + ROOT_GOAL, Budget(),
            )
            child_role = case.roles[1].name
            run.context = state.context
        else:
            root = run.submit(("exact original constraint " * 3000) + ROOT_GOAL)
            child_role = root.role
        child = TaskNode(id="short-child", project_id=root.project_id,
                         conversation_id=root.conversation_id, parent_id=root.id, depth=1,
                         role=child_role, goal="Short delegated task", status="RUNNING")
        run.repo.add(child)
        await run.runner.run(child)
        assert calls == []
        assert run.repo.get(child.id).status == "FAILED"
        assert "mandatory" in run.repo.get(child.id).error
        assert run.repo.get(root.id).goal.endswith(ROOT_GOAL)


async def test_real_teacher_entry_archives_root_goal_without_parent_private_answer(tmp_path):
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    planned = False
    seen = []
    goal = case.task1 + "\n\n" + ROOT_GOAL

    def script(messages, tools):
        nonlocal planned
        if case.roles[1].description in messages[0]["content"]:
            assert goal in messages[1]["content"]
            assert PRIVATE not in json.dumps(messages)
            seen.append(deepcopy(messages))
            return call("submit_result", summary="Read the exact labelled root assignment")
        if not planned:
            planned = True
            return call("submit_plan", reasoning="Use the existing team", roles=[],
                        subtasks=[{"key": "review", "role": case.roles[1].name,
                                   "goal": MIDDLE_GOAL, "deps": [], "acceptance": []}])
        return call("submit_result", summary="Integrated")

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        state = configure_team(run, case)
        descriptions = {role.name: role.description for role in case.roles}
        root = submit_teacher_task(run, case, goal, Budget(max_retries=0))
        record_private_answer(run.repo, root)
        await run.run()
        assert run.repo.get(root.id).status == "DONE" and len(seen) == 1
        assert {role.name: role.description for role in run.roles.list(root.project_id)} == (
            descriptions
        )
        folder = state.workspaces[case.roles[1].name] / ".evaluation/context"
        evidence = [json.loads(path.read_bytes()) for path in folder.glob("*.json")]
        assert len(evidence) == 1
        assert evidence[0]["assignment_references"][0]["goal"] == goal
        assert PRIVATE not in json.dumps(evidence)
        assert len(state.role_ids) == 4


async def test_teacher_previous_phase_dependency_does_not_become_an_ancestor(tmp_path):
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    openings = []

    def script(messages, tools):
        openings.append(messages[1]["content"])
        assert HEADER not in messages[1]["content"]
        return call("submit_result", summary="Phase complete")

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        configure_team(run, case)
        first = submit_teacher_task(run, case, case.task1, Budget(max_retries=0))
        await run.run()
        second = submit_teacher_task(run, case, case.task2, Budget(max_retries=0),
                                     previous_root=first)
        assert second.parent_id is None and second.deps == [first.id]
        assert collect_assignment_references(run.repo, second) == ()
        await run.run()
        assert run.repo.get(second.id).status == "DONE"
        assert len(openings) == 2


async def test_old_or_silently_ignoring_custom_builder_cannot_drop_required_references(tmp_path):
    calls = []

    def forbidden(messages, tools):
        calls.append(messages)
        raise AssertionError("Custom builder dropped a mandatory reference")

    for ignores_keyword in (False, True):
        with Engine(FakeLLM(forbidden),
                    settings=settings(tmp_path / str(ignores_keyword))) as run:
            root = run.submit(ROOT_GOAL)
            child = TaskNode(id="child", project_id=root.project_id,
                             conversation_id=root.conversation_id, parent_id=root.id, depth=1,
                             role=root.role, goal="Short child task", status="RUNNING")
            run.repo.add(child)
            original = run.runner.context

            class OldBuilder:
                def __init__(self, wrapped):
                    self.wrapped = wrapped

                def build(self, node, role, deps, memory_hits=None):
                    return self.wrapped.build(node, role, deps, memory_hits=memory_hits)

            class IgnoringBuilder(OldBuilder):
                def build(self, node, role, deps, memory_hits=None, **kwargs):
                    return self.wrapped.build(node, role, deps, memory_hits=memory_hits)

            run.runner.context = (
                IgnoringBuilder(original) if ignores_keyword else OldBuilder(original)
            )
            await run.runner.run(child)
            assert run.repo.get(child.id).status == "FAILED"
            assert "assignment" in run.repo.get(child.id).error
    assert calls == []


@pytest.mark.parametrize("kind", ["generic", "teacher"])
def test_fitting_reference_wins_over_large_optional_context(tmp_path, kind):
    _, node, repo = chain()
    references = collect_assignment_references(repo, node)
    role = RoleSpec(name=node.role, title="Reviewer", description="Review one document",
                    project_id=node.project_id, created_by="human")
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    budget = TokenBudget()

    def make(policy):
        if kind == "generic":
            return ContextBuilder(policy, budget)
        return TeamContextBuilder(policy, budget, scenario=case,
                                  workspaces={role.name: tmp_path / "workspace"})

    baseline = make(ContextPolicy()).build(node, role, assignment_references=references)
    limit = max(500, budget.estimate(baseline.messages()) + 200)
    builder = make(ContextPolicy(opening_budget_tokens=limit))
    memory = MemoryRecord(
        project_id=node.project_id, role=node.role, task_id="old-task",
        situation="previous work", action="review", outcome="success",
        lesson="OPTIONAL_MEMORY_CANARY " * 1000,
    )
    dependencies = [{
        "id": "optional-producer", "parent_id": "another-parent", "role": "writer",
        "status": "DONE", "goal": "Optional producer", "artifacts": [],
        "result": TaskResult(summary="OPTIONAL_REPORT_CANARY " * 1000), "error": None,
    }]
    context = builder.build(
        node, role, dep_reports=dependencies, memory_hits=[memory],
        board_notes=["OPTIONAL_BOARD_CANARY " * 1000], assignment_references=references,
    )
    messages = context.messages()
    assert budget.estimate(messages) <= limit
    assert ROOT_GOAL in messages[1]["content"] and MIDDLE_GOAL in messages[1]["content"]
    # A fitting partial preview is valid; only complete optional payloads must yield.
    assert memory.lesson not in messages[0]["content"]
    assert "OPTIONAL_BOARD_CANARY " * 1000 not in messages[0]["content"]
    assert set(context.memory_ids) <= {memory.id}


@pytest.mark.parametrize("teacher", [False, True], ids=["generic", "teacher"])
async def test_actual_child_clarification_refresh_keeps_lineage_and_answer_scope(tmp_path, teacher):
    planned = refreshed = False
    child_answer = "CHILD_PRIVATE_ANSWER_use_sentence_case"
    human = ScriptedHuman(answers={"child formatting": child_answer})
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    child_role = case.roles[1].name if teacher else "writer"
    child_description = case.roles[1].description if teacher else "LINEAGE_QUESTION_WRITER"

    def script(messages, tools):
        nonlocal planned, refreshed
        system, opening = messages[0]["content"], messages[1]["content"]
        if child_description in system:
            assert ROOT_GOAL in opening and PRIVATE not in json.dumps(messages)
            if child_answer not in opening:
                return call("ask_human", question="Which child formatting should I use?")
            refreshed = True
            assert "Recorded human-port exchanges" in opening
            assert MIDDLE_GOAL in opening and HEADER in opening
            return call("submit_result", summary="Checked the scoped formatting choice")
        assert PRIVATE in opening and child_answer not in opening
        if not planned:
            planned = True
            roles = [] if teacher else [{
                "name": child_role, "title": "Writer", "description": child_description,
                "reason": "review", "tools": ["ask_human", "submit_result"],
            }]
            return call("submit_plan", reasoning="Delegate formatting", roles=roles,
                        subtasks=[{"key": "review", "role": child_role, "goal": MIDDLE_GOAL,
                                   "deps": [], "acceptance": []}])
        return call("submit_result", summary="Integrated the checked result")

    with Engine(FakeLLM(script), human=human, settings=settings(tmp_path / "run")) as run:
        if teacher:
            configure_team(run, case)
            root = submit_teacher_task(run, case, ROOT_GOAL, Budget(max_retries=0))
        else:
            root = run.submit(ROOT_GOAL, budget=Budget(max_retries=0))
        record_private_answer(run.repo, root)
        await run.run()
        assert refreshed and run.repo.get(root.id).status == "DONE"
        assert len(human.requests) == 1 and human.requests[0].task_id == root.id + ".review"
        child = run.repo.get(root.id + ".review")
        assert run.repo.human_exchanges(child)[0]["response"]["text"] == child_answer
        assert run.repo.human_exchanges(root)[0]["response"]["text"] == PRIVATE
