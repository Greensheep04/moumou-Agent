"""Recorded clarification facts survive delegation, retries and process recovery."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.context import Compactor, ContextAdmissionError, ContextBuilder, TokenBudget
from moumou.context.builder import LESSONS_HEADER
from moumou.context.observation import memory_prompt_observation
from moumou.contracts import (
    Acceptance,
    Budget,
    ContextPolicy,
    HumanRequest,
    HumanResponse,
    MemoryRecord,
    RoleSpec,
    TaskNode,
    ToolResult,
)
from moumou.eval.human import FileHuman
from moumou.human_history import HumanExchangeError
from moumou.orchestrator import Engine, Settings
from moumou.orchestrator.state import Database, SqliteTaskRepo
from moumou.tools import ToolRegistry, default_tools

ANSWER = "DECISION-keep-original-booking-history; no installations or tool-permission expansion."


def call(name, *, call_id=None, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=call_id or name, name=name, args=arguments)])


def settings(root, **overrides):
    return Settings(root=root, project_id="p", conversation_id="group",
                    memory_enabled=False, role_review_enabled=False, **overrides)


def question(node, *, request_id="original-question", agent_id="old-instance"):
    return HumanRequest(
        id=request_id, task_id=node.id, agent_id=agent_id, kind="question",
        summary="Which original booking history must be retained?",
        context="Clarify before planning.",
        project_id=node.project_id, conversation_id=node.conversation_id, role=node.role,
    )


def record_answer(repo, node, *, text=ANSWER, agent_id="old-instance"):
    request = question(node, agent_id=agent_id)
    repo.record_human_request(node, request)
    repo.record_human_response(node, HumanResponse(request_id=request.id, text=text))
    return request


@pytest.fixture
def ledger(tmp_path):
    db = Database(tmp_path / "state.db")
    repo = SqliteTaskRepo(db)
    node = TaskNode(id="t1", project_id="p", conversation_id="group", role="worker",
                    goal="Retain original booking history and write the requested output.")
    repo.add(node)
    try:
        yield db, repo, node
    finally:
        db.close()


async def test_parent_receives_original_answer_after_actual_delegation(tmp_path):
    delegated, resumed = False, []
    human = ScriptedHuman(answers={"history": ANSWER})

    def script(messages, tools):
        nonlocal delegated
        system, opening = messages[0]["content"], messages[1]["content"]
        if "The dedicated file writer" in system:
            assert ANSWER not in opening, "parent-private history leaked to the child context"
            if messages[-1]["role"] != "tool":
                return call("write_file", path="out.txt", content="blue", call_id="child-write")
            return call("submit_result", summary="Wrote blue", files=["out.txt"],
                        call_id="child-result")
        if ANSWER not in opening:
            return call("ask_human", question="Which history should remain?", call_id="clarify")
        if not delegated:
            assert ANSWER in messages[1]["content"], "new answer was not pinned before planning"
            delegated = True
            return call("submit_plan", reasoning="One writer produces the output", roles=[{
                "name": "writer", "title": "Writer", "description": "The dedicated file writer",
                "reason": "write the output", "tools": ["write_file", "submit_result"],
            }], subtasks=[{
                "key": "write", "role": "writer", "goal": "Write out.txt containing blue",
                "acceptance": [{"kind": "file_contains", "target": "out.txt", "expect": "blue"}],
            }])
        resumed.append(deepcopy(messages))
        assert len(messages) == 2, "old tool-call history was incorrectly replayed"
        assert "Recorded human-port exchanges" in opening and ANSWER in opening
        return call("submit_result", summary="Delivered the agreed output", files=["out.txt"],
                    call_id="parent-result")

    with Engine(FakeLLM(script), human=human, settings=settings(tmp_path / "run")) as run:
        node = run.submit(
            "Clarify the history preference, delegate a writer, and deliver out.txt",
            acceptance=[Acceptance(kind="file_contains", target="out.txt", expect="blue")],
        )
        await run.run()
        assert run.repo.get(node.id).status == "DONE"
        records = run.repo.human_exchanges(node)
        assert len(records) == len(human.requests) == 1
        assert records[0]["request"]["task_id"] == node.id
        assert records[0]["response"]["text"] == ANSWER
        assert records[0]["role"] == node.role and records[0]["agent_id"]
        assert len(resumed) == 1


async def test_answer_survives_actual_acceptance_retry(tmp_path):
    failed_once = False
    human = ScriptedHuman(answers={"history": ANSWER})

    def script(messages, tools):
        nonlocal failed_once
        if ANSWER not in messages[1]["content"]:
            return call("ask_human", question="Which history should remain?")
        if not failed_once:
            failed_once = True
            return LLMResponse(content="First attempt did not write the required file")
        if messages[-1]["role"] != "tool":
            assert len(messages) == 2 and ANSWER in messages[1]["content"]
            return call("write_file", path="out.txt", content="retained")
        return call("submit_result", summary="Retained", files=["out.txt"])

    with Engine(FakeLLM(script), human=human, settings=settings(tmp_path / "run")) as run:
        node = run.submit("Clarify and retain history in out.txt", budget=Budget(max_retries=1),
                          acceptance=[Acceptance(kind="file_exists", target="out.txt")])
        await run.run()
        assert run.repo.get(node.id).status == "DONE"
        assert run.repo.get(node.id).retries == 1
        assert len(human.requests) == 1


async def test_answer_survives_process_reopen_and_agent_instance_change(tmp_path):
    root = tmp_path / "run"
    with Engine(FakeLLM(lambda m, t: LLMResponse(content="unused")),
                settings=settings(root)) as run:
        node = run.submit("Use the existing clarification")
        original_agent = f"{node.role}@old-process-instance"
        record_answer(run.repo, node, agent_id=original_agent)

    def script(messages, tools):
        assert ANSWER in messages[1]["content"]
        assert original_agent in messages[1]["content"]
        assert len(messages) == 2
        return LLMResponse(content="Used the persisted clarification")

    human = ScriptedHuman()
    with Engine(FakeLLM(script), human=human, settings=settings(root)) as reopened:
        await reopened.run()
        assert reopened.repo.get(node.id).status == "DONE"
        assert human.requests == []


@pytest.mark.parametrize("field,value", [
    ("project_id", "another-project"), ("conversation_id", "private-chat"),
    ("role", "another-role"), ("id", "another-task"),
])
def test_history_never_crosses_task_role_or_conversation_scope(ledger, field, value):
    _, repo, node = ledger
    record_answer(repo, node)
    with pytest.raises(HumanExchangeError, match="scope"):
        repo.human_exchanges(node.model_copy(update={field: value}))
    other = node.model_copy(update={"id": "t2"})
    repo.add(other)
    assert repo.human_exchanges(other) == []
    assert repo.human_exchanges(node)[0]["response"]["text"] == ANSWER


@pytest.mark.parametrize("damage", [
    "wrong-id", "empty", "missing-field", "approval", "empty-question", "timestamp",
])
def test_corrupt_or_incomplete_records_are_diagnosed_without_default_answers(ledger, damage):
    db, repo, node = ledger
    record_answer(repo, node)
    row = db.conn.execute("SELECT * FROM human_exchanges").fetchone()
    if damage == "timestamp":
        db.conn.execute("UPDATE human_exchanges SET answered_at = 'not-a-time'")
    elif damage in {"approval", "empty-question"}:
        payload = json.loads(row["request_json"])
        if damage == "approval":
            payload["kind"] = "approval"
        else:
            payload["summary"] = "   "
        db.conn.execute("UPDATE human_exchanges SET request_json = ?", (json.dumps(payload),))
    else:
        payload = json.loads(row["response_json"])
        if damage == "wrong-id":
            payload["request_id"] = "unrelated-request"
        elif damage == "empty":
            payload["text"] = "   "
        else:
            payload.pop("decision")
        db.conn.execute("UPDATE human_exchanges SET response_json = ?", (json.dumps(payload),))
    with pytest.raises(HumanExchangeError):
        repo.human_exchanges(node)


def test_old_question_events_without_ledger_are_not_treated_as_no_questions(ledger):
    _, repo, node = ledger
    repo.transition(node.id, "READY", "setup")
    repo.transition(node.id, "RUNNING", "setup")
    repo.transition(node.id, "WAITING_HUMAN", "question")
    with pytest.raises(HumanExchangeError, match="history_missing"):
        repo.human_exchanges(node)


async def test_consumed_port_response_recovers_crash_window_without_callback_replay(tmp_path):
    root, callbacks = tmp_path / "run", []
    port = FileHuman(tmp_path / "human", on_response=lambda q, a: callbacks.append(q.id))
    with Engine(FakeLLM(lambda m, t: LLMResponse(content="unused")), human=port,
                settings=settings(root)) as run:
        node = run.submit("Recover the recorded original response")
        req = question(node)
        run.repo.record_human_request(node, req)
        phase, started = await port._begin(req)
        await port._finish(req, {"request_id": req.id, "text": ANSWER}, phase, started)
        assert run.repo.human_exchanges(node, require_answered=False)[0]["response"] is None

    def script(messages, tools):
        assert ANSWER in messages[1]["content"] and len(messages) == 2
        return LLMResponse(content="Recovered answer")

    recovered_port = FileHuman(tmp_path / "human", on_response=lambda q, a: callbacks.append(q.id))
    with Engine(FakeLLM(script), human=recovered_port, settings=settings(root)) as reopened:
        await reopened.run()
        assert reopened.repo.get(node.id).status == "DONE"
        assert reopened.repo.human_exchanges(node)[0]["response"]["text"] == ANSWER
    assert callbacks == [req.id]
    events = [json.loads(line) for line in (tmp_path / "human/human-events.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert sum(event["event"] == "human_request" for event in events) == 1
    assert sum(event["event"] == "human_response_recovered" for event in events) == 1


async def test_pending_response_resumes_original_id_without_asking_again(tmp_path):
    root, human_root = tmp_path / "run", tmp_path / "human"
    port = FileHuman(human_root)
    with Engine(FakeLLM(lambda m, t: LLMResponse(content="unused")), human=port,
                settings=settings(root)) as run:
        node = run.submit("Resume the original unanswered question")
        req = question(node)
        run.repo.record_human_request(node, req)
        await port._begin(req)
        (human_root / "responses" / f"{req.id}.json").write_text(json.dumps({
            "request_id": req.id, "text": ANSWER,
        }), encoding="utf-8")

    def script(messages, tools):
        assert ANSWER in messages[1]["content"]
        return LLMResponse(content="Original pending response consumed")

    with Engine(FakeLLM(script), human=FileHuman(human_root), settings=settings(root)) as reopened:
        await reopened.run()
        assert reopened.repo.get(node.id).status == "DONE"
    assert list((human_root / "requests").glob("*.json")) == [
        human_root / "requests" / f"{req.id}.json",
    ]
    assert (human_root / "consumed" / f"{req.id}.json").is_file()


async def test_pending_question_on_nonresumable_port_fails_before_any_model_call(tmp_path):
    calls = []
    human = ScriptedHuman()

    def forbidden(messages, tools):
        calls.append(messages)
        raise AssertionError("LLM must not receive a fabricated answer")

    with Engine(FakeLLM(forbidden), human=human, settings=settings(tmp_path / "run")) as run:
        node = run.submit("Pending question", budget=Budget(max_retries=0))
        run.repo.record_human_request(node, question(node))
        await run.run()
        assert "pending_human_exchange_requires_resume" in run.repo.get(node.id).error
        assert run.repo.human_exchanges(node, require_answered=False)[0]["response"] is None
        assert calls == human.requests == []


@pytest.mark.parametrize("invalid", ["wrong-id", "empty"])
async def test_invalid_live_answer_is_not_saved_or_presented_as_agreement(tmp_path, invalid):
    human = ScriptedHuman(on_ask=lambda req: HumanResponse(
        request_id="unrelated" if invalid == "wrong-id" else req.id,
        text=ANSWER if invalid == "wrong-id" else "   ",
    ))
    with Engine(FakeLLM(lambda m, t: call("ask_human", question="Clarify history")), human=human,
                settings=settings(tmp_path / "run")) as run:
        node = run.submit("Clarify", budget=Budget(max_retries=0))
        await run.run()
        assert run.repo.get(node.id).status == "DEAD"
        assert "human_exchange" in run.repo.get(node.id).error
        record = run.repo.human_exchanges(node, require_answered=False)[0]
        assert record["response"] is None and record["answered_at"] is None


@pytest.mark.parametrize("change", ["summary", "conversation_id", "role", "kind"])
async def test_file_resume_requires_original_question_content_and_scope(tmp_path, ledger, change):
    _, _, node = ledger
    port = FileHuman(tmp_path / "human")
    req = question(node)
    await port._begin(req)
    modified = req.model_copy(update={change: "approval" if change == "kind" else "different"})
    with pytest.raises(ValueError, match="scope|original|question"):
        await port.resume(modified)
    assert (tmp_path / "human/pending" / f"{req.id}.json").is_file()


def test_full_current_goal_criteria_role_and_answers_are_mandatory_not_truncated(ledger):
    _, repo, node = ledger
    node = node.model_copy(update={
        "goal": "Full current requirement " * 90 + "MANDATORY-GOAL-TAIL",
        "acceptance": [Acceptance(kind="file_contains", target="out.txt", expect="kept",
                                  timeout_s=37, description="MANDATORY-CRITERION")],
    })
    record_answer(repo, node)
    role = RoleSpec(
        name=node.role, title="Original role", description="ORIGINAL-ROLE-INSTRUCTIONS",
        project_id=node.project_id, created_by="human",
    )
    builder = ContextBuilder(ContextPolicy(opening_budget_tokens=8000), TokenBudget())
    ctx = builder.build(node, role, human_exchanges=repo.human_exchanges(node),
                        board_notes=["optional old note " * 10000])
    assert node.goal in ctx.messages()[1]["content"]
    assert ANSWER in ctx.messages()[1]["content"]
    assert "MANDATORY-CRITERION" in ctx.messages()[1]["content"]
    assert '"timeout_s": 37' in ctx.messages()[1]["content"]
    assert role.description in ctx.messages()[0]["content"]
    assert builder.budget.estimate(ctx.messages()) <= builder.policy.opening_budget_tokens
    oversized = deepcopy(repo.human_exchanges(node))
    oversized[0]["response"]["text"] = "Full required answer " * 10000
    with pytest.raises(ContextAdmissionError, match="mandatory.*opening budget"):
        builder.build(node, role, human_exchanges=oversized)


def test_runtime_compaction_keeps_full_recorded_answer_in_the_pinned_opening(ledger):
    _, repo, node = ledger
    record_answer(repo, node)
    role = RoleSpec(
        name=node.role, title="Worker", description="Preserve actual constraints",
        project_id=node.project_id, created_by="human",
    )
    policy = ContextPolicy(
        context_window=10000, max_output_tokens=1000, opening_budget_tokens=8000,
        compact_trigger_tokens=2000, compact_target_tokens=1000,
    )
    budget = TokenBudget()
    ctx = ContextBuilder(policy, budget).build(
        node, role, human_exchanges=repo.human_exchanges(node),
    )
    original_opening = deepcopy(ctx.messages()[:2])
    for number in range(8):
        call_id = f"old-output-{number}"
        ctx.add_assistant("", [ToolCall(id=call_id, name="echo", args={"text": "old work"})])
        ctx.add_tool_result(call_id, ToolResult(ok=True, stdout="old output " * 2000))
    result = ctx.maybe_compact(Compactor(budget, policy))
    assert result.applied
    assert ctx.messages()[:2] == original_opening
    assert ANSWER in ctx.messages()[1]["content"]


async def test_rejected_tool_note_reaches_model_and_earlier_approval_is_not_reused(tmp_path):
    executions, requests, model_calls = [], [], 0

    class Probe:
        name, action_class, timeout_s = "probe", "shell", 1
        description = "Side-effect-free approval probe"
        params_schema = {"type": "object", "properties": {}}

        async def run(self, args, ctx):
            executions.append(ctx.task_id)
            return ToolResult(ok=True, stdout="probe executed")

    def decide(req):
        requests.append(req)
        return HumanResponse(
            request_id=req.id, decision="approve" if len(requests) == 1 else "reject",
            text="Only that one call was approved; keep the private boundary.",
        )

    def script(messages, tools):
        nonlocal model_calls
        model_calls += 1
        if model_calls <= 2:
            return call("probe", call_id=f"probe-{model_calls}")
        assert "rejected_by_human" in messages[-1]["content"]
        assert "keep the private boundary" in messages[-1]["content"]
        return LLMResponse(content="Respected the current rejection")

    tools = ToolRegistry()
    tools.register_all(*default_tools(), Probe())
    with Engine(FakeLLM(script), human=ScriptedHuman(on_ask=decide), tools=tools,
                settings=settings(tmp_path / "run", approval={"shell": True})) as run:
        node = run.submit("Request two independent approval checks")
        await run.run()
        assert run.repo.get(node.id).status == "DONE"
        assert executions == [node.id] and len(requests) == 2
        assert requests[0].id != requests[1].id
        assert all(
            req.project_id == "p" and req.conversation_id == "group" and req.role == node.role
            for req in requests
        )
        assert run.repo.human_exchanges(node) == []


def test_memory_prompt_observation_does_not_count_prefixes_or_duplicate_identities():
    def memory(record_id, lesson):
        return MemoryRecord(id=record_id, project_id="p", role="worker", task_id="old",
                            situation="s", action="a", outcome="success", lesson=lesson)

    hits = [memory("short", "Keep history"), memory("long", "Keep history and timestamps"),
            memory("unique", "Use explicit local paths"), memory("dup1", "Same text"),
            memory("dup2", "Same text")]
    messages = [{"role": "system", "content": LESSONS_HEADER
                 + "- Keep history and timestamps\n- Use explicit local paths\n- Same text"}]
    observed = memory_prompt_observation(messages, hits, LESSONS_HEADER)
    assert observed["matched_ids"] == ["long", "unique"]
    assert {row["id"] for row in observed["partial_or_unknown"]} == {"short", "dup1", "dup2"}
    assert all(len(row["sha256"]) == 64 for row in observed["full_matches"])
    assert "Keep history" not in json.dumps(observed)


async def test_materialisation_exception_marks_task_failed_without_calling_model(tmp_path):
    calls = []

    def forbidden(messages, tools):
        calls.append(messages)
        raise AssertionError("model must not run after materialisation failed")

    with Engine(FakeLLM(forbidden), settings=settings(tmp_path / "run")) as run:
        node = run.submit("Deliver dependency files", budget=Budget(max_retries=0))

        def broken_materialisation(current, workspace):
            raise RuntimeError("dependency materialisation failed")

        run.runner._materialise_dependencies = broken_materialisation
        await run.run()
        assert run.repo.get(node.id).status == "DEAD"
        assert "dependency materialisation failed" in run.repo.get(node.id).error
        attempts = [event for event in run.trace.query(node.id)
                    if event.kind == "task" and event.name == "execution"]
        assert len(attempts) == 1 and attempts[0].status == "error"
        assert attempts[0].payload["task_status"] == "FAILED"
        assert calls == []
