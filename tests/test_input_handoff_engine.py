"""Real Engine/FakeLLM handoff scenarios, never a teacher answer or API run."""

import copy
import json
import os

import pytest
from test_eval_team import engine, response, scenario
from test_input_handoff_policy import plan, running_parent

from moumou.adapters import FakeLLM
from moumou.contracts import Budget, TaskResult
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools


async def test_actual_producer_file_reaches_reviewer_before_its_model_call(tmp_path):
    case = scenario(tmp_path)
    replies = [
        response("submit_plan", **plan()),
        response("write_file", path="report.md", content="Actual fixture source bytes."),
        response("submit_result", summary="Fixture report", files=["report.md"]),
        response("read_file", path="report.md"),
        response("write_file", path="review.md", content="Observed the actual fixture file."),
        response("submit_result", summary="Fixture review", files=["review.md"]),
        response("submit_result", summary="Fixture handoff", files=["report.md", "review.md"]),
    ]
    seen_review = False
    holder = {}

    def script(messages, tools):
        nonlocal seen_review
        if case.roles[2].description in messages[0]["content"]:
            seen_review = True
            state = holder["state"]
            assert (state.workspaces["qa"] / "report.md").read_text(
                encoding="utf-8",
            ) == "Actual fixture source bytes."
            assert case.roles[2].description in messages[0]["content"]
        assert replies, "Unexpected extra model call in fixture workflow"
        return replies.pop(0)

    client = FakeLLM(script)
    with Engine(client, settings=Settings(root=tmp_path / "run", max_slots=1,
                                         memory_enabled=False, role_review_enabled=False)) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        holder["state"] = state
        root = submit_teacher_task(run, case, "Coordinate fixture review", Budget(max_retries=0))
        await run.run()
        assert seen_review and not replies
        assert run.repo.get(root.id).status == "DONE"
        reviewer = run.repo.get(root.id + ".review")
        event = next(e for e in run.trace.query(reviewer.id)
                     if e.name == "declared_inputs_received")
        assert event.payload["observed"][0]["path"] == "report.md"
        assert event.payload["final_file_review"] is None
        assert {r.name: r.description for r in run.roles.list(root.project_id)} == {
            r.name: r.description for r in case.roles
        }
        assert case.task1 not in root.goal


@pytest.mark.parametrize("problem", ["disappeared", "private_source", "foreign_scope", "hardlink"])
async def test_missing_or_invalid_source_blocks_before_copy_and_model(tmp_path, problem):
    case = scenario(tmp_path)
    client = FakeLLM([])
    with engine(tmp_path, client) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        writer, reviewer = run.delegation.apply(parent, plan())
        source = state.workspaces["dev"] / "report.md"
        source.write_text("Fixture input", encoding="utf-8")
        run.repo.transition(writer.id, "READY", "fixture_ready")
        run.repo.transition(writer.id, "RUNNING", "fixture_dispatched")
        run.repo.set_result(writer.id, TaskResult(files=["report.md"]))
        run.repo.add_artifacts(writer.id, ["report.md"])
        run.repo.transition(writer.id, "DONE", "fixture_accepted")
        if problem == "disappeared":
            source.unlink()
        elif problem == "private_source":
            run.repo.set_result(writer.id, TaskResult(files=[]))
        elif problem == "foreign_scope":
            run.db.conn.execute("UPDATE tasks SET conversation_id=? WHERE id=?",
                                ("unrelated", writer.id))
        else:
            os.link(source, state.workspaces["dev"] / "alias.md")
        run.repo.transition(reviewer.id, "READY", "fixture_ready")
        run.repo.transition(reviewer.id, "RUNNING", "fixture_dispatched")
        await run.runner.run(run.repo.get(reviewer.id))
        failed = run.repo.get(reviewer.id)
        assert failed.status == "FAILED" and "InputHandoffError" in failed.error
        assert not client.calls
        assert not (state.workspaces["qa"] / "report.md").exists()


async def test_copy_conflict_blocks_consumer_without_overwriting_existing_file(tmp_path):
    case = scenario(tmp_path)
    client = FakeLLM([])
    with engine(tmp_path, client) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        writer, reviewer = run.delegation.apply(parent, plan())
        (state.workspaces["dev"] / "report.md").write_text("producer", encoding="utf-8")
        (state.workspaces["qa"] / "report.md").write_text("existing own edit", encoding="utf-8")
        run.repo.transition(writer.id, "READY", "fixture_ready")
        run.repo.transition(writer.id, "RUNNING", "fixture_dispatched")
        run.repo.set_result(writer.id, TaskResult(files=["report.md"]))
        run.repo.add_artifacts(writer.id, ["report.md"])
        run.repo.transition(writer.id, "DONE", "fixture_accepted")
        run.repo.transition(reviewer.id, "READY", "fixture_ready")
        run.repo.transition(reviewer.id, "RUNNING", "fixture_dispatched")
        await run.runner.run(run.repo.get(reviewer.id))
        assert not client.calls
        assert run.repo.get(reviewer.id).status == "FAILED"
        assert (state.workspaces["qa"] / "report.md").read_text() == "existing own edit"


async def test_recovery_relay_redeclares_own_files_then_reviewer_receives_them(tmp_path):
    case = scenario(tmp_path)
    relay_plan = {"subtasks": [
        {"key": "relay", "role": "dev", "goal": "Declare existing owned fixture file",
         "deps": [], "inputs": []},
        {"key": "review", "role": "qa", "goal": "Review recovered fixture input",
         "deps": ["relay"], "inputs": [{"producer": "relay", "path": "report.md"}]},
    ]}
    replies = [response("submit_result", summary="Existing owned bytes", files=["report.md"]),
               response("read_file", path="report.md"),
               response("submit_result", summary="Read the fixture bytes", files=[]),
               response("submit_result", summary="Fixture recovery", files=["report.md"])]
    client = FakeLLM(replies)
    with Engine(client, settings=Settings(root=tmp_path / "run", max_slots=1,
                                         memory_enabled=False, role_review_enabled=False)) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        run.repo.bump_wakeups(parent.id)
        parent = run.repo.get(parent.id)
        (state.workspaces["dev"] / "report.md").write_text("Owned prior fixture", encoding="utf-8")
        relay, reviewer = run.delegation.apply(parent, relay_plan)
        assert relay.id == parent.id + ".r1.relay"
        await run.run()
        assert run.repo.get(reviewer.id).status == "DONE" and not replies
        assert (state.workspaces["qa"] / "report.md").read_text() == "Owned prior fixture"
        assert run.repo.get(parent.id).status == "DONE"


def test_teacher_schema_copy_leaves_shared_default_marker_unchanged(tmp_path):
    registry = ToolRegistry()
    registry.register_all(*default_tools())
    marker = registry.get("submit_plan")
    before = copy.deepcopy(marker.params_schema)
    case = scenario(tmp_path)
    with Engine(FakeLLM([]), tools=registry, settings=Settings(root=tmp_path / "run")) as run:
        configure_team(run, case, require_declared_inputs=True)
        teacher = run.tools.get("submit_plan")
        assert teacher is not marker
        assert "inputs" in teacher.params_schema["properties"]["subtasks"]["items"]["required"]
        assert marker.params_schema == before
        assert run.runner.tools is run.gate.tools is run.tools
        assert registry.get("submit_plan").params_schema == before
        assert json.dumps(before, sort_keys=True) == json.dumps(
            marker.params_schema, sort_keys=True,
        )


async def test_continuous_new_producer_replaces_only_unchanged_prior_delivered_bytes(tmp_path):
    case = scenario(tmp_path)
    replies = []
    for text in ("First fixture version.", "Second declared producer version."):
        replies.extend([
            response("submit_plan", **plan()),
            response("write_file", path="report.md", content=text),
            response("submit_result", summary="Declared source", files=["report.md"]),
            response("read_file", path="report.md"),
            response("write_file", path="review.md", content="Review of " + text),
            response("submit_result", summary="Fixture review", files=["review.md"]),
            response("submit_result", summary="Fixture package", files=["report.md", "review.md"]),
        ])
    with Engine(FakeLLM(replies), settings=Settings(root=tmp_path / "run", max_slots=1,
                                                 memory_enabled=False,
                                                 role_review_enabled=False)) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        first = submit_teacher_task(run, case, "First fixture phase", Budget(max_retries=0))
        await run.run()
        assert run.repo.get(first.id).status == "DONE"
        assert (state.workspaces["qa"] / "report.md").read_text() == "First fixture version."
        second = submit_teacher_task(run, case, "Second fixture phase", Budget(max_retries=0),
                                     previous_root=first.id)
        await run.run()
        assert run.repo.get(second.id).status == "DONE" and not replies
        assert (state.workspaces["qa"] / "report.md").read_text() == (
            "Second declared producer version."
        )
        received = [event for event in run.trace.query(second.id + ".review")
                    if event.name == "declared_inputs_received"]
        assert len(received) == 1
        assert received[0].payload["observed"][0]["source_id"] == second.id + ".writer"
        assert received[0].payload["final_file_review"] is None
