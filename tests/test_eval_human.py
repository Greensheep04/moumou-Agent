"""Instructor sessions wait for actual human replies and preserve real dialogue."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import HumanRequest, Message
from moumou.eval.human import ConsoleHuman, FileHuman, RecordingLLM, TranscriptRecorder


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def request(identity="question_1", **changes):
    return HumanRequest(
        id=identity, task_id="t1", agent_id="lead@t1", kind="question",
        summary="Which requirements should the team follow?", **changes,
    )


async def pending(human, req):
    task = asyncio.create_task(human.ask(req))
    path = human.root / "pending" / f"{req.id}.json"

    async def wait():
        while not path.exists():
            if task.done():
                return await task
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 3)
    return task, json.loads(path.read_text(encoding="utf-8"))


def reply(human, req, **values):
    (human.root / "responses" / f"{req.id}.json").write_text(
        json.dumps({"request_id": req.id, **values}), encoding="utf-8",
    )


async def test_file_human_waits_then_consumes_only_an_actual_answer(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("moumou.eval.human.monotonic", lambda: clock[0])
    shared = tmp_path / "transcript.jsonl"
    observed = []
    human = FileHuman(tmp_path, transcript_path=shared, poll_interval_s=0.001,
                      on_response=lambda req, answer: observed.append((req.id, answer.text)))
    human.set_phase("task_1")
    req = request()
    task, record = await pending(human, req)
    try:
        assert record["phase"] == "task_1"
        assert record["request"] == req.model_dump(mode="json")
        assert not task.done() and observed == []
        await asyncio.sleep(0.01)
        assert not task.done(), "a pending question must never receive an automatic answer"
        clock[0] = 13.5
        reply(human, req, text="Use Hong Kong time and keep cancellation records.")
        answer = await asyncio.wait_for(task, 3)
        assert answer.text == "Use Hong Kong time and keep cancellation records."
        assert observed == [(req.id, answer.text)]
        assert not (tmp_path / "pending" / f"{req.id}.json").exists()
        assert not (tmp_path / "responses" / f"{req.id}.json").exists()
        assert (tmp_path / "requests" / f"{req.id}.json").is_file()
        assert (tmp_path / "consumed" / f"{req.id}.json").is_file()
        assert [row["event"] for row in rows(shared)] == ["human_request", "human_response"]
        assert human.summary()["answered_requests"] == 1
        assert human.summary()["by_phase"]["task_1"]["cumulative_wait_s"] == 3.5
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["approval", "review"])
async def test_decisions_require_an_explicit_human_reply(tmp_path, kind):
    human = FileHuman(tmp_path, poll_interval_s=0.001)
    req = HumanRequest(id="approval_1", task_id="t1", agent_id="lead@t1", kind=kind,
                       summary="May I perform this action?", options=["approve", "reject"])
    task, _ = await pending(human, req)
    assert not task.done()
    reply(human, req, decision="reject", text="Keep the original file.")
    answer = await asyncio.wait_for(task, 3)
    assert answer.decision == "reject"
    assert rows(tmp_path / "human-events.jsonl")[-1]["response"]["decision"] == "reject"


@pytest.mark.parametrize(("kind", "options", "payload"), [
    ("question", [], {"request_id": "another", "text": "not the pending answer"}),
    ("question", [], {}),
    ("question", ["A", "B"], {"text": "C"}),
    ("approval", ["approve", "reject"], {"text": "possibly"}),
    ("review", ["approve", "reject"], {"decision": "maybe"}),
    ("approval", ["reject"], {"decision": "approve"}),
])
async def test_invalid_response_never_reaches_the_response_hook(tmp_path, kind, options, payload):
    observed = []
    human = FileHuman(tmp_path, poll_interval_s=0.001,
                      on_response=lambda *args: observed.append(args))
    req = HumanRequest(id="invalid", task_id="t1", agent_id="lead", kind=kind,
                       summary="Actual question", options=options)
    task, _ = await pending(human, req)
    reply(human, req, **payload)
    with pytest.raises(ValueError):
        await asyncio.wait_for(task, 3)
    assert observed == []
    assert not (tmp_path / "consumed" / "invalid.json").exists()


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_question_answer_is_rejected_before_consumption(tmp_path, text):
    observed = []
    human = FileHuman(tmp_path, poll_interval_s=0.001,
                      on_response=lambda *args: observed.append(args))
    req = request()
    task, _ = await pending(human, req)
    reply(human, req, text=text)
    with pytest.raises(ValueError, match="non-empty"):
        await asyncio.wait_for(task, 3)
    assert observed == []
    assert (tmp_path / "pending" / f"{req.id}.json").is_file()
    assert not (tmp_path / "consumed" / f"{req.id}.json").exists()
    assert human.summary()["answered_requests"] == 0
    assert [row["event"] for row in rows(tmp_path / "human-events.jsonl")] == ["human_request"]


async def test_question_answer_phase_is_captured_at_original_request(tmp_path):
    human = FileHuman(tmp_path, poll_interval_s=0.001)
    human.set_phase("task_1")
    req = request()
    task, _ = await pending(human, req)
    human.set_phase("task_2")
    reply(human, req, text="Keep the original booking history.")
    answer = await asyncio.wait_for(task, 3)
    assert answer.text == "Keep the original booking history."
    assert rows(tmp_path / "human-events.jsonl")[-1]["phase"] == "task_1"
    assert set(human.summary()["by_phase"]) == {"task_1"}


async def test_preexisting_reply_and_previously_consumed_id_are_not_replayed(tmp_path):
    human = FileHuman(tmp_path, poll_interval_s=0.001)
    old = request("preexisting")
    reply(human, old, text="Old reply")
    with pytest.raises(ValueError, match="preexisting|reused"):
        await human.ask(old)
    req = request("once")
    task, _ = await pending(human, req)
    reply(human, req, text="A fresh answer")
    await asyncio.wait_for(task, 3)
    restarted = FileHuman(tmp_path, poll_interval_s=0.001)
    with pytest.raises(ValueError, match="reused"):
        await restarted.ask(req)


@pytest.mark.parametrize("identity", ["../outside", "", "a/b", "C:drive", "white space"])
async def test_request_identifiers_cannot_escape_the_human_queue(tmp_path, identity):
    human = FileHuman(tmp_path)
    with pytest.raises(ValueError, match="unsafe"):
        await human.ask(request(identity))
    assert not list((tmp_path / "requests").glob("*.json"))


async def test_async_response_hook_receives_explicit_command_without_automatic_grant(tmp_path):
    observed = []

    async def hook(req, response):
        observed.append((req.id, response.text))

    human = FileHuman(tmp_path, poll_interval_s=0.001, on_response=hook)
    req = request()
    task, _ = await pending(human, req)
    assert observed == []
    reply(human, req, text="/grant-search 2")
    await asyncio.wait_for(task, 3)
    assert observed == [(req.id, "/grant-search 2")]


async def test_concurrent_file_questions_keep_response_identities_separate(tmp_path):
    human = FileHuman(tmp_path, poll_interval_s=0.001)
    first, second = request("first"), request("second")
    one, _ = await pending(human, first)
    two, _ = await pending(human, second)
    reply(human, second, text="Second actual answer")
    assert (await asyncio.wait_for(two, 3)).text == "Second actual answer"
    assert not one.done()
    reply(human, first, text="First actual answer")
    assert (await asyncio.wait_for(one, 3)).text == "First actual answer"
    assert human.summary()["answered_requests"] == 2
    assert human.summary()["pending_ids"] == []


async def test_notifications_and_pending_question_are_redacted_in_both_logs(tmp_path):
    secret = "private-classroom-secret"
    human = FileHuman(tmp_path, secrets=[secret], transcript_path=tmp_path / "all.jsonl",
                      poll_interval_s=0.001)
    message = Message(project_id="teacher", conversation_id="scenario_1", sender="lead",
                      content=f"A full notification {secret}")
    await human.notify(message)
    req = request(context=f"Question context {secret}")
    task, _ = await pending(human, req)
    reply(human, req, text="Actual human reply")
    await asyncio.wait_for(task, 3)
    for path in tmp_path.rglob("*.json*"):
        assert secret not in path.read_text(encoding="utf-8")
    assert rows(tmp_path / "all.jsonl")[0]["event"] == "human_notification"


async def test_recording_llm_preserves_complete_real_messages_and_tool_calls(tmp_path):
    secret = "private-classroom-secret"
    long_text = "teacher dialogue " * 5000
    messages = [{"role": "user", "content": long_text + secret}]
    tools = [{"type": "function", "function": {"name": "ask_human", "parameters": {}}}]
    response = LLMResponse(content=long_text + secret,
                           tool_calls=[ToolCall(id="call_1", name="ask_human",
                                                args={"question": long_text})],
                           tokens_in=17, tokens_out=5, usage_present=True, model="teacher-model")

    class Delegate:
        model = "teacher-model"
        base_url = "https://api.hkgai.net/v1"

        async def chat(self, received, schemas):
            assert received == messages and schemas == tools
            assert received[0]["content"].endswith(secret), "logging cannot change the real prompt"
            return response

    recorded = RecordingLLM(Delegate(), tmp_path / "dialogue.jsonl", secrets=[secret])
    recorded.set_phase("task_1")
    assert await recorded.chat(messages, tools) is response
    actual = rows(tmp_path / "dialogue.jsonl")
    assert actual[0]["messages"][0]["content"] == long_text + "[REDACTED]"
    assert actual[0]["tools"] == tools
    assert actual[1]["response"]["content"] == long_text + "[REDACTED]"
    assert actual[1]["response"]["tool_calls"][0]["args"]["question"] == long_text
    assert actual[0]["call_id"] == actual[1]["call_id"]
    assert actual[0]["phase"] == actual[1]["phase"] == "task_1"
    assert recorded.model == "teacher-model" and recorded.base_url.endswith("/v1")
    assert recorded.summary()["total"]["tokens_in"] == 17


async def test_recording_llm_keeps_unknown_usage_and_delegate_errors_distinct(tmp_path):
    secret = "private-classroom-secret"

    class Delegate:
        closed = False
        attempts = 0

        async def chat(self, messages, tools):
            self.attempts += 1
            if self.attempts == 1:
                return LLMResponse(content="Old-style response", tokens_in=99, usage_present=None)
            raise RuntimeError(f"provider failure {secret}")

        async def aclose(self):
            self.closed = True

    delegate = Delegate()
    recording = RecordingLLM(delegate, tmp_path / "dialogue.jsonl", secrets=[secret])
    recording.set_phase("task_1")
    await recording.chat([])
    recording.set_phase("task_2")
    with pytest.raises(RuntimeError, match="provider failure"):
        await recording.chat([])
    await recording.aclose()
    assert delegate.closed
    summary = recording.summary()
    assert summary["total"]["calls"] == 2 and summary["total"]["error_calls"] == 1
    assert summary["total"]["tokens_in"] is None
    assert summary["total"]["usage_missing_calls"] == 2
    assert summary["by_phase"]["task_1"]["known_tokens_in"] == 0
    assert summary["by_phase"]["task_2"]["cumulative_call_s"] >= 0
    assert rows(tmp_path / "dialogue.jsonl")[-1]["event"] == "llm_error"
    assert secret not in (tmp_path / "dialogue.jsonl").read_text(encoding="utf-8")


async def test_cancelled_model_call_preserves_its_real_request_and_error(tmp_path):
    entered = asyncio.Event()

    class Delegate:
        async def chat(self, messages, tools):
            entered.set()
            await asyncio.Event().wait()

    recording = RecordingLLM(Delegate(), tmp_path / "dialogue.jsonl")
    task = asyncio.create_task(recording.chat([{"role": "user", "content": "Actual task"}]))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    actual = rows(tmp_path / "dialogue.jsonl")
    assert actual[0]["event"] == "llm_request"
    assert actual[-1]["event"] == "llm_error"
    assert actual[-1]["error_type"] == "CancelledError"
    assert recording.summary()["total"]["error_calls"] == 1


def test_multiple_recorders_share_a_lock_and_write_complete_json_lines(tmp_path):
    path = tmp_path / "shared.jsonl"
    recorders = [TranscriptRecorder(path) for _ in range(6)]
    assert all(recorder.lock is recorders[0].lock for recorder in recorders)

    def worker(index):
        for step in range(20):
            recorders[index].write("thread_probe", "task_1", index=index, step=step)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))
    actual = rows(path)
    assert len(actual) == 120
    assert {(row["index"], row["step"]) for row in actual} == {
        (index, step) for index in range(6) for step in range(20)
    }


async def test_console_human_has_no_blank_approval_default(tmp_path, monkeypatch, capsys):
    inputs = iter(["", "approve"])
    prompts = []

    def actual_input(prompt):
        prompts.append(prompt)
        return next(inputs)

    monkeypatch.setattr("builtins.input", actual_input)
    human = ConsoleHuman(tmp_path)
    req = HumanRequest(id="console", task_id="t1", agent_id="lead", kind="approval",
                       summary="May I delete this file?", options=["approve", "reject"])
    response = await asyncio.wait_for(human.ask(req), 3)
    assert response.decision == "approve" and len(prompts) == 2
    assert "Enter approve or reject explicitly" in capsys.readouterr().out


async def test_console_question_reprompts_until_an_actual_nonempty_answer(
    tmp_path, monkeypatch, capsys,
):
    inputs = iter(["", "   ", "Retain cancellation history."])
    prompts = []

    def actual_input(prompt):
        prompts.append(prompt)
        return next(inputs)

    monkeypatch.setattr("builtins.input", actual_input)
    human = ConsoleHuman(tmp_path)
    response = await asyncio.wait_for(human.ask(request()), 3)
    assert response.text == "Retain cancellation history." and response.decision is None
    assert len(prompts) == 3
    assert capsys.readouterr().out.count("Enter a non-empty answer") == 2
    assert human.summary()["answered_requests"] == 1
    assert [row["event"] for row in rows(tmp_path / "human-events.jsonl")] == [
        "human_request", "human_response",
    ]


async def test_console_review_note_is_the_actual_human_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "reject Fix cancellation reporting.")
    human = ConsoleHuman(tmp_path)
    req = HumanRequest(id="quality_review", task_id="t1", agent_id="lead", kind="review",
                       summary="Do these delivered artifacts meet your requirements?",
                       options=["approve", "reject"])
    response = await asyncio.wait_for(human.ask(req), 3)
    assert response.decision == "reject" and response.text == "Fix cancellation reporting."
    saved = rows(tmp_path / "human-events.jsonl")[-1]["response"]
    assert saved == response.model_dump(mode="json")


async def test_concurrent_console_questions_serialize_actual_input(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    prompts = []

    def actual_input(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            entered.set()
            assert release.wait(3), "the test did not release its own input probe"
            return "First reply"
        return "Second reply"

    monkeypatch.setattr("builtins.input", actual_input)
    human = ConsoleHuman(tmp_path)
    first = asyncio.create_task(human.ask(request("first_console")))
    second = None

    async def input_started():
        while not entered.is_set():
            if first.done():
                await first
                raise AssertionError("the console request ended without requesting input")
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(input_started(), 3)
        second = asyncio.create_task(human.ask(request("second_console")))
        await asyncio.sleep(0.01)
        assert len(prompts) == 1
        release.set()
        answers = await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert [answer.text for answer in answers] == ["First reply", "Second reply"]
    finally:
        release.set()
        for task in [first, second]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in [first, second] if task is not None],
                             return_exceptions=True)
