"""Teacher teams retain four real role identities and prior scenario state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sys

import pytest
from test_eval_cases import write_cases

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance, Budget, TaskNode
from moumou.eval.cases import load_cases
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.orchestrator.delegation import DelegationError


def scenario(tmp_path, index=0):
    return load_cases(write_cases(tmp_path / "cases"))[index]


def response(name, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=arguments)])


def engine(tmp_path, client):
    return Engine(client, settings=Settings(
        root=tmp_path / "run", project_id="teacher-project", conversation_id="same-group",
        memory_enabled=False, role_review_enabled=True,
    ))


def test_team_has_exactly_four_original_roles_without_a_fifth_planner(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        state = configure_team(run, case, previous_dialogue_path=tmp_path / "not-created-yet.jsonl")
        node = submit_teacher_task(run, case, case.task1, Budget())
        assert node.role == case.leader == state.leader == "lead"
        assert state.role_ids == ("lead", "dev", "qa", "release")
        assert len(set(state.workspaces.values())) == 4
        assert {role.name for role in run.roles.list("teacher-project")} == set(state.role_ids)
        assert run.roles.get("teacher-project", "planner") is None
        assert run.settings.role_review_enabled is False
        for original in case.roles:
            registered = run.roles.get("teacher-project", original.name)
            assert registered.description == original.description
        first = run.runner._instance(node, run.roles.get(node.project_id, node.role))
        second = run.runner._instance(node.model_copy(update={"id": "t2"}),
                                      run.roles.get(node.project_id, node.role))
        assert first.id == second.id == "lead"
        assert first.workspace == second.workspace


def test_nonsoftware_role_prompt_and_factual_context_enter_model_messages(tmp_path):
    case = scenario(tmp_path, 1)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case)
        node = submit_teacher_task(run, case, case.task1, Budget())
        role = run.roles.get(node.project_id, node.role)
        messages = run.context.build(node, role).messages()
        assert role.description in messages[0]["content"]
        assert all(member.name in messages[0]["content"] for member in case.roles)
        assert "multi-agent software team" not in messages[0]["content"]
        assert case.task1 in messages[1]["content"]
        assert "self-report" in messages[1]["content"]
        assert platform.system() in messages[0]["content"]
        assert sys.executable in messages[0]["content"]
        assert "subprocess argument lists" in messages[0]["content"]
        if os.name == "nt":
            assert "cmd.exe syntax" in messages[0]["content"]
            assert "run_python" in messages[0]["content"]


def test_unknown_fifth_role_is_rejected_before_creating_tasks_or_rewriting_prompts(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case)
        parent = submit_teacher_task(run, case, case.task1, Budget())
        with pytest.raises(DelegationError, match="exactly"):
            run.delegation.apply(parent, {
                "reasoning": "invent another person", "roles": [{
                    "name": "extra_agent", "title": "Extra", "description": "New identity",
                    "tools": [], "reason": "extra",
                }], "subtasks": [{"key": "extra", "role": "extra_agent", "goal": "Extra"}],
            })
        assert len(run.repo.all_tasks()) == 1
        assert run.roles.count(parent.project_id) == 4


async def test_real_engine_two_runs_preserve_role_files_actual_result_and_dialogue(tmp_path):
    case = scenario(tmp_path)
    calls = []

    def script(messages, tools):
        calls.append(messages)
        user = messages[1]["content"]
        if "Second actual request" in user:
            assert "Actual first delivery" in user
            assert "Asia/Hong_Kong" in user
            assert "HUGE_REQUEST_SENTINEL" not in user
            if messages[-1]["role"] != "tool":
                return response("read_file", path="booking.txt")
            if messages[-1].get("tool_call_id") == "read_file":
                assert "original bookings" in messages[-1]["content"]
                return response("write_file", path="booking.txt", content="bookings plus dashboard")
            return response("submit_result", summary="Actual second delivery",
                            files=["booking.txt"])
        if messages[-1]["role"] != "tool":
            return response("write_file", path="booking.txt", content="original bookings")
        return response("submit_result", summary="Actual first delivery", files=["booking.txt"])

    transcript = tmp_path / "dialogue.jsonl"
    with engine(tmp_path, FakeLLM(script)) as run:
        state = configure_team(run, case, previous_dialogue_path=transcript)
        original_memory = run.memory
        first = submit_teacher_task(run, case, "First actual request", Budget(max_retries=0))
        await run.run()
        first = run.repo.get(first.id)
        assert first.status == "DONE"
        transcript.write_text("\n".join(json.dumps(row) for row in [
            {"event": "llm_request", "messages": [{"content": "HUGE_REQUEST_SENTINEL" * 100}]},
            {"event": "human_request", "request": {
                "id": "actual-question", "agent_id": "lead", "kind": "question",
                "summary": "Which timezone?", "context": "booking times",
            }},
            {"event": "human_response", "response": {
                "request_id": "actual-question", "text": "Asia/Hong_Kong", "decision": None,
            }},
        ]) + "\n", encoding="utf-8")
        archived = transcript.read_bytes()
        second = submit_teacher_task(
            run, case, "Second actual request", Budget(max_retries=0), previous_root=first.id,
        )
        assert second.deps == [first.id]
        await run.run()
        assert run.repo.get(second.id).status == "DONE"
        assert run.memory is original_memory
        assert (state.workspaces["lead"] / "booking.txt").read_text(
            encoding="utf-8") == "bookings plus dashboard"
        for workspace in state.workspaces.values():
            assert (workspace / ".evaluation/prior-dialogue.txt").read_bytes() == archived
        assert state.prior_dialogue_sha256 == hashlib.sha256(archived).hexdigest()
        assert state.previous_root_id == first.id
        assert len(calls) == 5
        assert {event.agent_id for event in run.trace.all_events()
                if event.kind == "llm"} == {"lead"}
        assert run.roles.count("teacher-project") == 4


async def test_existing_fixed_member_delegation_integrates_declared_files_only(tmp_path):
    case = scenario(tmp_path)
    lead_calls = 0

    def script(messages, tools):
        nonlocal lead_calls
        if case.roles[1].description in messages[0]["content"]:
            if messages[-1]["role"] != "tool":
                return response("write_file", path="answer.txt", content="verified")
            return response("submit_result", summary="Developer wrote answer", files=["answer.txt"])
        lead_calls += 1
        if lead_calls == 1:
            return response("submit_plan", reasoning="Reuse the provided developer", roles=[],
                            subtasks=[{"key": "build", "role": "dev", "goal": "Write answer.txt",
                                       "acceptance": [{"kind": "file_contains",
                                                       "target": "answer.txt",
                                                       "expect": "verified"}]}])
        return response("submit_result", summary="Actual integrated answer", files=["answer.txt"])

    with engine(tmp_path, FakeLLM(script)) as run:
        state = configure_team(run, case)
        (state.workspaces["dev"] / "private.txt").write_text("private", encoding="utf-8")
        task = submit_teacher_task(run, case, case.task1, Budget(max_retries=0), acceptance=[
            Acceptance(kind="file_contains", target="answer.txt", expect="verified"),
        ])
        await run.run()
        assert run.repo.get(task.id).status == "DONE"
        assert (state.workspaces["lead"] / "answer.txt").read_text(encoding="utf-8") == "verified"
        assert not (state.workspaces["lead"] / "private.txt").exists()
        assert {node.role for node in run.repo.all_tasks()} == {"lead", "dev"}
        assert run.roles.count(task.project_id) == 4


async def test_same_role_tasks_are_serialized_while_different_members_can_run(tmp_path):
    case = scenario(tmp_path)
    active, maximum, cross_role = {}, {}, 0
    with engine(tmp_path, FakeLLM([])) as run:
        async def original(node):
            nonlocal cross_role
            active[node.role] = active.get(node.role, 0) + 1
            maximum[node.role] = max(maximum.get(node.role, 0), active[node.role])
            cross_role = max(cross_role, sum(active.values()))
            await asyncio.sleep(0.01)
            active[node.role] -= 1

        run.runner.run = original
        configure_team(run, case)
        nodes = [TaskNode(id=f"t{n}", project_id="teacher-project", conversation_id="same-group",
                          goal="Work", role=role) for n, role in enumerate(["dev", "dev", "qa"])]
        await asyncio.gather(*(run.runner.run(node) for node in nodes))
        assert maximum == {"dev": 1, "qa": 1}
        assert cross_role == 2


def test_reused_role_handoff_does_not_copy_a_file_onto_itself(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        state = configure_team(run, case)
        target = state.workspaces["lead"]
        (target / "keep.txt").write_text("unchanged", encoding="utf-8")
        assert run.workspace.integrate(target, target, ["keep.txt"]).ok
        assert (target / "keep.txt").read_text(encoding="utf-8") == "unchanged"
        assert not run.workspace.integrate(target, target, ["../escape.txt"]).ok


def test_prior_root_must_be_an_actual_terminal_member_of_the_same_group(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case)
        pending = submit_teacher_task(run, case, case.task1, Budget())
        with pytest.raises(ValueError, match="terminal"):
            submit_teacher_task(run, case, case.task2, Budget(), previous_root=pending)
        with pytest.raises(ValueError, match="terminal"):
            submit_teacher_task(run, case, case.task2, Budget(), previous_root="missing")


def test_cannot_attach_a_teacher_team_after_default_planner_creation(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        run.submit("Existing normal task")
        with pytest.raises(ValueError, match="before creating"):
            configure_team(run, case)
