"""Exercise the real teacher-session loop with externally answered human files."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from test_midtest_cases import write_cases

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.config import LLMSettings
from moumou.eval import midtest
from moumou.eval.midtest_cases import load_midtest_cases


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def response(name, **arguments):
    return LLMResponse(
        tool_calls=[ToolCall(id=name, name=name, args=arguments)],
        usage_present=True, tokens_in=17, tokens_out=5,
    )


async def exercise_session(tmp_path, monkeypatch, *, first_decision):
    source = write_cases(tmp_path / "teacher-cases")
    case = load_midtest_cases(source)[0]
    destination = tmp_path / "teacher-session"
    feedback = "Actual reviewer feedback: retain booking records in Task 2."

    def script(messages, tools):
        if {tool.get("function", {}).get("name") for tool in (tools or [])} == {"submit_lesson"}:
            return response("submit_lesson", lessons=[])
        brief = messages[1]["content"]
        if brief.startswith("## Task\n" + case.task2):
            assert first_decision == "approve", "Task 2 was posted despite rejected quality"
            assert feedback in brief
            assert "Actual first delivery" in brief
            if messages[-1]["role"] != "tool":
                return response("read_file", path="booking.txt")
            if messages[-1].get("tool_call_id") == "read_file":
                assert "original booking records" in messages[-1]["content"]
                return response("write_file", path="booking.txt", content="bookings plus dashboard")
            return response("submit_result", summary="Actual second delivery",
                            files=["booking.txt"])
        assert brief.startswith("## Task\n" + case.task1)
        if messages[-1]["role"] != "tool":
            return response("write_file", path="booking.txt", content="original booking records")
        return response("submit_result", summary="Actual first delivery", files=["booking.txt"])

    settings = LLMSettings(
        api_key="offline-controller-key", model="offline-course-model",
        base_url="https://offline.invalid", timeout_s=1,
    )
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr("moumou.adapters.DeepSeekClient", lambda **kwargs: FakeLLM(script))
    monkeypatch.setattr("moumou.eval.artifacts._code_version", lambda: {
        "git_commit": "a" * 40, "source_sha256": "b" * 64, "git_dirty": False,
    })
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("teacher-session regression attempted a real HTTP client")

    monkeypatch.setattr("httpx.AsyncClient", network_forbidden)
    args = midtest.parse_args([
        "--cases", str(source), "--scenario", case.id, "--out", str(destination),
        "--human", "files",
    ])
    suite = asyncio.create_task(midtest.run_suite(args, [case]))
    observed = []

    async def controller():
        answered = set()
        pending = destination / case.id / "human/pending"
        while not suite.done():
            for path in sorted(pending.glob("*.json")):
                if path.stem in answered:
                    continue
                request = read(path)
                req, phase = request["request"], request["phase"]
                assert req["kind"] == "review"
                task_path = destination / case.id / f"{phase}-result.json"
                assert task_path.is_file(), "review arrived before inspectable results were saved"
                task = read(task_path)
                assert task["root_status"] == "DONE"
                assert task["quality_status"].startswith("awaiting")
                assert task["verified_goal_success"] is None
                assert task["root_machine_accepted"] is None
                if phase == "task_1":
                    assert not (destination / case.id / "task_2-result.json").exists()
                    assert len(read(destination / "midtest.json")["scenarios"][0]["tasks"]) == 1
                    decision = first_decision
                    note = feedback
                else:
                    assert phase == "task_2"
                    assert read(destination / case.id / "task_1-result.json")[
                        "quality_status"] == "human_accepted"
                    decision, note = "approve", "Actual reviewer accepts the second delivery."
                observed.append({"phase": phase, "request_id": req["id"],
                                 "inspectable_before_answer": True})
                response_path = destination / case.id / "human/responses" / f"{req['id']}.json"
                temporary = response_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({
                    "request_id": req["id"], "decision": decision, "text": note,
                }), encoding="utf-8")
                os.replace(temporary, response_path)
                answered.add(path.stem)
            await asyncio.sleep(0.005)

    control = asyncio.create_task(controller())
    try:
        exit_code, _ = await asyncio.wait_for(asyncio.gather(suite, control), timeout=15)
    finally:
        for task in (suite, control):
            if not task.done():
                task.cancel()
        await asyncio.gather(suite, control, return_exceptions=True)
    manifest = read(destination / "midtest.json")
    transcript = [json.loads(line) for line in (
        destination / case.id / "dialogue.jsonl").read_text(encoding="utf-8").splitlines()]
    return exit_code, manifest, transcript, observed, destination, feedback


async def test_actual_file_reviews_gate_both_tasks_and_preserve_feedback_and_phase_metrics(
        tmp_path, monkeypatch):
    code, manifest, transcript, observed, destination, feedback = await exercise_session(
        tmp_path, monkeypatch, first_decision="approve",
    )
    assert code == 0
    assert manifest["status"] == "human_accepted"
    assert manifest["goal_metrics"]["planned_root_goals"] == 2
    assert manifest["goal_metrics"]["verified_successes"] == 2
    assert manifest["goal_metrics"]["verified_goal_coverage"] == 1
    entry = manifest["scenarios"][0]
    first, second = entry["tasks"]
    assert [row["phase"] for row in observed] == ["task_1", "task_2"]
    assert all(task["root_status"] == "DONE" and task["verified_goal_success"] is True
               and task["root_machine_accepted"] is None for task in entry["tasks"])
    assert {node["id"] for node in second["nodes"]} == {second["root_task_id"]}
    assert second["root_task_id"] != first["root_task_id"]
    assert entry["llm"]["total"]["calls"] == first["llm"]["calls"] + second["llm"]["calls"]
    assert second["llm"]["calls"] < entry["llm"]["total"]["calls"]
    phase2_responses = [row["response"] for row in transcript
                        if row["event"] == "llm_response" and row["phase"] == "task_2"]
    assert second["llm"]["calls"] == len(phase2_responses)
    assert second["llm"]["tokens_in"] == sum(row["tokens_in"] for row in phase2_responses)
    assert first["human"]["answered_requests"] == second["human"]["answered_requests"] == 1
    assert entry["human"]["answered_requests"] == 2
    assert entry["human"]["pending_ids"] == []
    assert manifest["search_requests_per_scenario"] == entry["search"]["limit"] == 6
    assert entry["search"]["used"] == entry["search"]["provider_requests"] == 0
    assert all(task["search_delta"]["provider_requests"] == 0 for task in entry["tasks"])
    assert all(task["total_to_review_s"] >= task["delivery_elapsed_s"]
               and task["quality_review_wait_s"] > 0 for task in entry["tasks"])
    for workspace in entry["workspaces"].values():
        archive = Path(workspace) / ".midtest/prior-dialogue.txt"
        text = archive.read_text(encoding="utf-8")
        assert feedback in text
        assert all(json.loads(line)["phase"] == "task_1" for line in text.splitlines())
    phase2_inputs = [row["messages"] for row in transcript
                     if row["event"] == "llm_request" and row["phase"] == "task_2"]
    assert any(feedback in json.dumps(messages) for messages in phase2_inputs)
    assert (destination / "report.md").is_file()


async def test_rejected_actual_review_keeps_agent_done_and_stops_before_task_two(
        tmp_path, monkeypatch):
    code, manifest, transcript, observed, destination, _ = await exercise_session(
        tmp_path, monkeypatch, first_decision="reject",
    )
    assert code == 1
    assert manifest["status"] == "quality_rejected"
    assert "error_type" not in manifest and "error_message" not in manifest
    assert manifest["goal_metrics"] == {
        "planned_root_goals": 2, "persisted_task_deliveries": 1, "agent_declared_complete": 1,
        "reviewed_goals": 1, "verified_successes": 0, "verified_goal_coverage": 0.0,
        "quality_review_method": "human",
    }
    entry = manifest["scenarios"][0]
    assert entry["status"] == "quality_rejected"
    assert len(entry["tasks"]) == len(observed) == 1
    task = entry["tasks"][0]
    assert task["root_status"] == "DONE" and task["agent_declared_complete"] is True
    assert task["quality_status"] == "human_rejected"
    assert task["verified_goal_success"] is False
    assert task["root_machine_accepted"] is None
    assert task["human"]["answered_requests"] == 1
    assert entry["human"]["pending_ids"] == []
    assert entry["search"]["used"] == entry["search"]["provider_requests"] == 0
    assert entry["search"]["remaining"] == 6
    assert not (destination / entry["scenario_id"] / "task_2-result.json").exists()
    assert not any(row.get("phase") == "task_2" for row in transcript)
