"""P3 memory namespaces and scripted human decisions at their public boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import (
    Acceptance,
    HumanIO,
    HumanRequest,
    HumanResponse,
    MemoryRecord,
    MemoryStore,
    Message,
)
from moumou.memory import SqliteMemoryStore, memory_scope
from moumou.orchestrator import Engine, Settings
from moumou.tools.builtin import python_command

BASE = datetime(2026, 11, 8, tzinfo=UTC)


def lesson(record_id: str, **changes) -> MemoryRecord:
    fields = {
        "id": record_id,
        "project_id": "project-a",
        "role": "coder",
        "task_id": "source-task",
        "situation": "dependency installation",
        "action": "use the dependency mirror",
        "outcome": "success",
        "lesson": "dependency recovery: 使用镜像源后重试",
        "created_at": BASE,
    }
    return MemoryRecord(**{**fields, **changes})


def request(**changes) -> HumanRequest:
    fields = {
        "task_id": "task-1",
        "agent_id": "coder@task-1",
        "summary": "Confirm the next action",
        "action": "delete_file",
        "action_class": "fs_delete",
    }
    return HumanRequest(**{**fields, **changes})


def test_memory_queries_remain_project_and_role_scoped_after_reopen(tmp_path):
    database = tmp_path / "memory.db"
    records = [
        lesson("a-coder", score=0.1),
        lesson("b-coder", project_id="project-b", score=0.1),
        lesson("a-reviewer", role="reviewer", score=0.1),
    ]
    store = SqliteMemoryStore(database)
    try:
        assert isinstance(store, MemoryStore)
        for item in records:
            assert store.write(item) == item.id
    finally:
        store.close()

    store = SqliteMemoryStore(database)
    try:
        for item in records:
            scope = memory_scope(item.project_id, item.role)
            assert [hit.id for hit in store.all(scope)] == [item.id]
            assert [hit.id for hit in store.search(scope, "dependency")] == [item.id]
            assert store.evict_candidates(scope) == [item.id]
        missing_scope = memory_scope("project-c", "coder")
        assert store.all(missing_scope) == []
        assert store.search(missing_scope, "dependency") == []
        assert store.evict_candidates(missing_scope) == []
    finally:
        store.close()


def test_memory_preserves_all_fields_and_the_written_snapshot(tmp_path):
    database = tmp_path / "memory.db"
    item = lesson(
        "full-record",
        outcome="failure",
        score=1.25,
        uses=4,
        last_used_at=BASE + timedelta(seconds=1),
    )
    saved = item.model_copy(deep=True)
    store = SqliteMemoryStore(database)
    try:
        store.write(item)
        item.lesson = "a later in-memory edit"
        item.score = 0.0
    finally:
        store.close()

    store = SqliteMemoryStore(database)
    try:
        assert store.all(memory_scope(saved.project_id, saved.role)) == [saved]
    finally:
        store.close()


def test_score_decay_without_use_does_not_rewrite_usage_metadata(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    item = lesson("used-lesson", score=1.0, uses=3, last_used_at=BASE - timedelta(days=1))
    scope = memory_scope(item.project_id, item.role)
    try:
        store.write(item)
        store.update_score(item.id, -0.2, used=False)
        decayed = store.all(scope)[0]
        assert decayed.score == pytest.approx(0.8)
        assert decayed.uses == 3
        assert decayed.last_used_at == item.last_used_at

        store.update_score(item.id, 0.1)
        reused = store.all(scope)[0]
        assert reused.score == pytest.approx(0.9)
        assert reused.uses == 4
        assert reused.last_used_at is not None
        assert reused.last_used_at != item.last_used_at
    finally:
        store.close()


def test_eviction_ignores_deleted_records_and_excludes_the_exact_threshold(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    scope = memory_scope("project-a", "coder")
    below, exact, deleted = (
        lesson("below", score=0.29),
        lesson("exact", score=0.3),
        lesson("deleted", score=0.1),
    )
    try:
        for item in (below, exact, deleted):
            store.write(item)
        store.mark_deleted([deleted.id])
        store.mark_deleted([deleted.id])
        assert store.evict_candidates(scope, threshold=0.3) == [below.id]
        assert {hit.id for hit in store.all(scope)} == {below.id, exact.id}
        assert {hit.id for hit in store.search(scope, "dependency")} == {below.id, exact.id}
    finally:
        store.close()


async def test_scripted_decisions_match_action_class_with_correlated_responses():
    human = ScriptedHuman(decisions={"fs_delete": "approve"}, default="reject")
    assert isinstance(human, HumanIO)
    requests = [
        request(),
        request(action="shell", action_class="shell"),
        request(kind="review", action="revise_role", action_class=""),
    ]
    replies = [await human.ask(item) for item in requests]

    assert [reply.decision for reply in replies] == ["approve", "reject", "reject"]
    assert [reply.request_id for reply in replies] == [item.id for item in requests]
    assert human.requests == requests


async def test_scripted_questions_use_the_first_matching_answer_and_default():
    human = ScriptedHuman(
        answers={"filename": "out.txt", "name": "less specific"},
        default_answer="Please provide more context",
        default="reject",
    )
    await human.notify(Message(
        project_id="project-a", conversation_id="c", sender="coder@task-1", content="Working"
    ))
    matched = request(kind="question", summary="Which filename should I use?")
    unmatched = request(kind="question", summary="Which output format should I use?")

    first, second = await human.ask(matched), await human.ask(unmatched)

    assert first.text == "out.txt" and first.decision is None
    assert second.text == "Please provide more context" and second.decision is None
    assert [first.request_id, second.request_id] == [matched.id, unmatched.id]
    assert human.requests == [matched, unmatched]


@pytest.mark.parametrize("async_callback", [False, True], ids=["sync", "async"])
async def test_scripted_callback_response_takes_precedence_over_static_decision(async_callback):
    seen = []

    def answer(item):
        seen.append(item.id)
        return HumanResponse(request_id=item.id, decision="approve", text="approved for this task")

    async def async_answer(item):
        return answer(item)

    human = ScriptedHuman(
        decisions={"fs_delete": "reject"}, default="reject",
        on_ask=async_answer if async_callback else answer,
    )
    item = request()

    reply = await human.ask(item)

    assert reply == HumanResponse(
        request_id=item.id, decision="approve", text="approved for this task"
    )
    assert seen == [item.id]
    assert human.requests == [item]


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_scripted_shell_decision_controls_actual_file_side_effect(tmp_path, decision):
    human = ScriptedHuman(decisions={"shell": decision}, default="reject")
    settings = Settings(
        project_id="project-a", conversation_id="c", root=tmp_path / "run",
        memory_enabled=False, role_review_enabled=False,
    )
    script = None

    def respond(messages, tools):
        if not any(message["role"] == "assistant" for message in messages):
            return LLMResponse(tool_calls=[ToolCall(
                id="run-side-effect", name="shell", args={"command": python_command(str(script))}
            )])
        return LLMResponse(content="Handled the requested action")

    with Engine(FakeLLM(respond), human=human, settings=settings) as engine:
        task = engine.submit(
            "Run the requested action only after approval",
            acceptance=[Acceptance(
                kind="file_exists" if decision == "approve" else "file_not_exists",
                target="side-effect.txt",
            )],
        )
        workspace = engine.workspace.allocate("project-a", "c", f"{task.role}@{task.id}")
        script = workspace / "side effect.py"
        script.write_text(
            "from pathlib import Path\n"
            "Path('side-effect.txt').write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )

        report = await engine.run()

        assert report.done == 1 and report.dead == 0
        assert len(human.requests) == 1
        assert human.requests[0].kind == "approval"
        assert human.requests[0].action_class == "shell"
        marker = workspace / "side-effect.txt"
        assert marker.exists() is (decision == "approve")
        if decision == "approve":
            assert marker.read_text(encoding="utf-8") == "executed"
        approvals = [event for event in engine.trace.query(task.id) if event.kind == "approval"]
        assert len(approvals) == 1 and approvals[0].payload["decision"] == decision
