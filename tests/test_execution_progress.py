"""Repeat protection across the real task loop, approval gate, and recorder."""

from __future__ import annotations

import json
import os
from collections import deque

import pytest

from moumou.adapters import FakeLLM, LocalDirs, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import ToolResult
from moumou.eval import cli
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools

REPEAT = "duplicate_execution_no_progress"
OUTPUT = "observed room data"


class CommandProbe:
    action_class, timeout_s = "shell", 1
    description = "Record a local command invocation."
    params_schema = {"type": "object"}

    def __init__(self, name="run_python", previous=()):
        self.name = name
        self.previous = deque(previous)
        self.calls = []

    async def run(self, args, ctx):
        self.calls.append((dict(args), ctx))
        if self.previous:
            return self.previous.popleft()
        return ToolResult(ok=True, stdout=OUTPUT, exit_code=0)


def tool_call(identity, name="run_python", **args):
    if name == "run_python":
        args = {"code": "print('rooms')", **args}
    elif name == "shell":
        args = {"command": "echo rooms", **args}
    return LLMResponse(tool_calls=[ToolCall(id=identity, name=name, args=args)])


def engine_for(tmp_path, responses, probe, human, *, enabled=None, workspace=None):
    queue = deque(responses)
    observations = []

    def respond(messages, _tools):
        if messages[-1]["role"] == "tool":
            observations.append(messages[-1]["content"])
        item = queue.popleft()
        return item() if callable(item) else item

    registry = ToolRegistry()
    registry.register_all(*(probe if tool.name == probe.name else tool for tool in default_tools()))
    options = {} if enabled is None else {"execution_progress_enabled": enabled}
    settings = Settings(root=tmp_path / "run", memory_enabled=False,
                        role_review_enabled=False, **options)
    return Engine(FakeLLM(respond), tools=registry, human=human, settings=settings,
                  workspace=workspace), observations


@pytest.mark.parametrize("name", ["run_python", "shell"])
@pytest.mark.parametrize("enabled", [True, None], ids=["enabled", "default"])
async def test_exact_repeat_skips_new_approval_and_execution_only_when_enabled(
    tmp_path, name, enabled,
):
    probe, human = CommandProbe(name), ScriptedHuman()
    responses = [tool_call("first", name), tool_call("second", name),
                 LLMResponse(content="I can proceed with the previous result.")]
    engine, observations = engine_for(tmp_path, responses, probe, human, enabled=enabled)
    with engine:
        task = engine.submit("Inspect the rooms, then continue.")
        report = await engine.run()
        events = engine.trace.query(task.id)

    assert report.done == 1
    count = 1 if enabled else 2
    assert len(probe.calls) == len(human.requests) == count
    assert sum(event.kind == "tool" and event.name == name for event in events) == count
    assert OUTPUT in observations[0]
    markers = [event for event in events if event.name == "execution_repeat_skipped"]
    if enabled:
        assert REPEAT in observations[1]
        assert OUTPUT not in observations[1] and "human_approval_feedback" not in observations[1]
        marker, = markers
        assert marker.kind == "task" and marker.payload["previous_call_id"] == "first"
        assert marker.payload["tool"] == name
    else:
        assert OUTPUT in observations[1] and not markers


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_explicit_repeat_reason_requires_fresh_approval(tmp_path, decision):
    reason = "The upstream service may have changed since the first observation."
    probe = CommandProbe()
    def decide(_request):
        return decision if len(human.requests) == 2 else "approve"

    human = ScriptedHuman(on_ask=decide)
    responses = [tool_call("first"), tool_call("rerun", repeat_reason=reason),
                 LLMResponse(content="Reviewed the current decision.")]
    engine, observations = engine_for(tmp_path, responses, probe, human, enabled=True)
    with engine:
        engine.submit("Check whether a fresh observation can be taken.")
        report = await engine.run()

    assert report.done == 1
    first, second = human.requests
    assert first.id != second.id and second.payload["repeat_reason"] == reason
    assert REPEAT not in observations[1]
    if decision == "approve":
        assert len(probe.calls) == 2 and OUTPUT in observations[1]
    else:
        assert len(probe.calls) == 1 and "rejected_by_human" in observations[1]


async def test_external_workspace_content_change_allows_identical_command(tmp_path):
    probe, human = CommandProbe(), ScriptedHuman()

    def replace_input():
        path = probe.calls[0][1].workspace / "input.txt"
        original = path.stat()
        path.write_bytes(b"B")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        return tool_call("after-edit")

    responses = [tool_call("setup", "write_file", path="input.txt", content="A"),
                 tool_call("before-edit"), replace_input, LLMResponse(content="Compared both.")]
    engine, observations = engine_for(tmp_path, responses, probe, human, enabled=True)
    with engine:
        engine.submit("Observe the file before and after a real edit.")
        report = await engine.run()

    assert report.done == 1
    assert len(probe.calls) == len(human.requests) == 2
    assert OUTPUT in observations[-1] and REPEAT not in observations[-1]


@pytest.mark.parametrize("prior", [
    ToolResult(ok=False, error="temporary failure", exit_code=1),
    ToolResult(ok=True, stdout="partial", exit_code=0, truncated=True),
    ToolResult(ok=True, exit_code=0, artifacts=["generated.txt"]),
], ids=["failed", "truncated", "workspace-effect"])
async def test_inconclusive_or_mutating_results_do_not_suppress_reruns(tmp_path, prior):
    probe, human = CommandProbe(previous=[prior]), ScriptedHuman()
    responses = [tool_call("first"), tool_call("second"), LLMResponse(content="Observed both.")]
    engine, observations = engine_for(tmp_path, responses, probe, human, enabled=True)
    with engine:
        engine.submit("Try the observation again if needed.")
        report = await engine.run()

    assert report.done == 1
    assert len(probe.calls) == len(human.requests) == 2
    assert OUTPUT in observations[-1] and REPEAT not in observations[-1]


async def test_answered_clarification_allows_a_fresh_gated_observation(tmp_path):
    probe = CommandProbe()
    human = ScriptedHuman(default_answer="Use the new operational requirements.")
    responses = [
        tool_call("first"),
        tool_call("clarify", "ask_human", question="What changed?"),
        tool_call("after-answer"),
        LLMResponse(content="Used the clarification."),
    ]
    engine, observations = engine_for(tmp_path, responses, probe, human, enabled=True)
    with engine:
        engine.submit("Observe and clarify the requirements.")
        report = await engine.run()

    assert report.done == 1
    assert len(probe.calls) == 2
    assert [request.kind for request in human.requests] == ["approval", "question", "approval"]
    assert OUTPUT in observations[-1] and REPEAT not in observations[-1]


async def test_same_role_workspace_does_not_share_repeat_history_across_tasks(tmp_path):
    class StableWorkspace(LocalDirs):
        def allocate(self, project_id, conversation_id, agent_id):
            return super().allocate(project_id, conversation_id, "lead")

    probe, human = CommandProbe(), ScriptedHuman()
    responses = [tool_call("task-one"), LLMResponse(content="First task done."),
                 tool_call("task-two"), LLMResponse(content="Second task done.")]
    engine, observations = engine_for(tmp_path, responses, probe, human, enabled=True,
                                      workspace=StableWorkspace(tmp_path / "workspace"))
    with engine:
        first = engine.submit("Inspect rooms for the first task.")
        assert (await engine.run()).done == 1
        second = engine.submit("Inspect rooms for the second task.")
        assert (await engine.run()).done == 2

    assert [ctx.task_id for _, ctx in probe.calls] == [first.id, second.id]
    assert len({ctx.workspace for _, ctx in probe.calls}) == 1
    assert len(human.requests) == 2
    assert all(OUTPUT in observation for observation in observations)


@pytest.mark.parametrize("flags", [[], ["--execution-progress"]], ids=["default", "enabled"])
def test_formal_cli_plan_reports_execution_progress_without_runtime(flags, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("plan attempted runtime execution")

    monkeypatch.setattr(cli.asyncio, "run", forbidden)
    assert cli.main(["--plan", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["execution_progress"] is bool(flags)
