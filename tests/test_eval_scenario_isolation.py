"""Independent teacher groups proceed after a saved failure or review rejection."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_eval_cases import write_cases

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.config import LLMSettings
from moumou.eval import cli
from moumou.eval.cases import load_cases
from moumou.eval.human import FileHuman
from moumou.orchestrator import Engine


def setup_suite(tmp_path, monkeypatch):
    source = write_cases(tmp_path / "cases")
    cases = load_cases(source)
    destination = tmp_path / "session"
    settings = LLMSettings(api_key="offline-test-key", model="offline-model",
                           base_url="https://offline.invalid", timeout_s=1)
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(lambda cls: settings))

    def script(messages, tools):
        names = {tool.get("function", {}).get("name") for tool in (tools or [])}
        name, args = (("submit_lesson", {"lessons": []}) if names == {"submit_lesson"}
                      else ("submit_result", {"summary": "Offline fake delivery"}))
        return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=args)])

    monkeypatch.setattr("moumou.adapters.DeepSeekClient", lambda **kwargs: FakeLLM(script))
    monkeypatch.setattr("moumou.eval.artifacts._code_version", lambda: {
        "git_commit": "a" * 40, "source_sha256": "b" * 64, "git_dirty": False,
    })
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline scenario regression attempted real HTTP")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    return source, cases, destination


@pytest.mark.parametrize("problem", ["dead", "rejected", "runtime_error"])
async def test_one_group_failure_never_silently_drops_other_planned_groups(
        tmp_path, monkeypatch, problem):
    source, cases, destination = setup_suite(tmp_path, monkeypatch)
    original_run = Engine.run

    async def run(engine):
        if engine.settings.project_id == "scenario_1":
            if problem == "runtime_error":
                raise RuntimeError("synthetic scenario-local failure")
            if problem == "dead":
                for task in engine.repo.all_tasks():
                    engine.repo.transition(task.id, "RUNNING", "offline_fixture")
                    engine.repo.transition(task.id, "DEAD", "offline_fixture")
                return
        await original_run(engine)

    class ScriptedReviewPort(FileHuman):
        async def ask(self, request):
            assert request.kind == "review"
            project = request.payload["task"]["nodes"][0]["project_id"]
            phase, began = await self._begin(request)
            return await self._finish(request, {
                "request_id": request.id,
                "decision": "reject" if project == "scenario_1" else "approve",
                "text": "Synthetic review fixture; not a real evaluation result.",
            }, phase, began)

    monkeypatch.setattr(Engine, "run", run)
    monkeypatch.setattr("moumou.eval.human.FileHuman", ScriptedReviewPort)
    args = cli.parse_args(["--tasks", str(source), "--out", str(destination),
                           "--human", "files", "--quality-review",
                           "human" if problem == "rejected" else "deferred"])
    assert await cli.run_suite(args, cases) == 1
    manifest = json.loads((destination / "test.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "partially_completed"
    assert manifest["planned_root_goals"] == manifest["goal_metrics"]["planned_root_goals"] == 6
    assert manifest["unsuccessful_scenarios"] == ["scenario_1"]
    first, second, third = manifest["scenarios"]
    assert first["status"] == {"dead": "incomplete", "rejected": "quality_rejected",
                               "runtime_error": "error"}[problem]
    assert len(first["tasks"]) == (0 if problem == "runtime_error" else 1)
    assert not (destination / "scenario_1/task_2-result.json").exists()
    assert [task["phase"] for task in second["tasks"]] == ["task_1", "task_2"]
    assert [task["phase"] for task in third["tasks"]] == ["task_1", "task_2"]
    assert first["state_snapshot"]["status"] == "recorded"
    state = json.loads((destination / "scenario_1/state-snapshot.json").read_text(encoding="utf-8"))
    assert len(state["nodes"]) == 1 and state["kind"] == "observed_state_not_delivery"
    assert manifest["goal_metrics"]["verified_successes"] == (4 if problem == "rejected" else 0)


async def test_explicit_global_cancellation_still_stops_the_suite(tmp_path, monkeypatch):
    source, cases, destination = setup_suite(tmp_path, monkeypatch)

    async def cancel(engine):
        raise asyncio.CancelledError

    monkeypatch.setattr(Engine, "run", cancel)
    args = cli.parse_args(["--tasks", str(source), "--out", str(destination),
                           "--human", "files", "--quality-review", "deferred"])
    with pytest.raises(asyncio.CancelledError):
        await cli.run_suite(args, cases)
    manifest = json.loads((destination / "test.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "interrupted" and len(manifest["scenarios"]) == 1
    assert manifest["planned_root_goals"] == 6
    assert manifest["goal_metrics"]["persisted_task_deliveries"] == 0
    assert not (destination / "scenario_2").exists()


@pytest.mark.parametrize("stage", ["team_setup", "resource_cleanup"])
async def test_infrastructure_failure_stops_before_starting_another_group(
        tmp_path, monkeypatch, stage):
    source, cases, destination = setup_suite(tmp_path, monkeypatch)
    if stage == "team_setup":
        def broken_setup(*args, **kwargs):
            raise OSError("synthetic team initialization failure")

        monkeypatch.setattr("moumou.eval.team.configure_team", broken_setup)
    else:
        original_close = Engine.close

        def broken_close(engine):
            original_close(engine)
            raise OSError("synthetic resource closure failure")

        monkeypatch.setattr(Engine, "close", broken_close)
    args = cli.parse_args(["--tasks", str(source), "--out", str(destination),
                           "--human", "files", "--quality-review", "deferred"])
    with pytest.raises(OSError, match="synthetic"):
        await cli.run_suite(args, cases)
    manifest = json.loads((destination / "test.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "error" and manifest["error_type"] == "OSError"
    assert manifest["planned_root_goals"] == 6
    assert len(manifest["scenarios"]) == 1
    assert not (destination / "scenario_2").exists()
