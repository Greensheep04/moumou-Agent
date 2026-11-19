"""Offline declared-input contracts and private-workspace boundaries."""

import json
import re

import pytest
from test_eval_team import engine, scenario

from moumou.adapters import FakeLLM
from moumou.contracts import Budget, TaskResult
from moumou.eval.input_handoff import DeclaredInputs, InputHandoffError
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator.delegation import DelegationError


def plan(*, path="report.md", deps=None, inputs=None):
    return {"subtasks": [
        {"key": "writer", "role": "dev", "goal": "Produce fixture report", "deps": [],
         "inputs": []},
        {"key": "review", "role": "qa", "goal": "Review the actual fixture report",
         "deps": ["writer"] if deps is None else deps,
         "inputs": ([{"producer": "writer", "path": path}] if inputs is None else inputs)},
    ]}


def running_parent(run, case):
    parent = submit_teacher_task(run, case, "Coordinate fixture review", Budget(max_retries=0))
    run.repo.promote()
    run.repo.transition(parent.id, "RUNNING", "fixture_dispatched")
    return run.repo.get(parent.id)


@pytest.mark.parametrize("path", ["../report.md", "C:/report.md", ".env", ".evaluation/a.md",
                                 ".git/config", "report.md:stream", "report.md "])
def test_bad_input_paths_never_create_children_or_metadata(tmp_path, path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        with pytest.raises(DelegationError):
            run.delegation.apply(parent, plan(path=path))
        assert len(run.repo.all_tasks()) == 1
        assert run.repo.get(parent.id).status == "RUNNING"
        assert run.db.conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'teacher_inputs:%'",
        ).fetchone()[0] == 0


def test_missing_edge_is_rejected_before_any_child_is_created(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        with pytest.raises(DelegationError, match="sibling key in deps"):
            run.delegation.apply(parent, plan(deps=[]))
        assert len(run.repo.all_tasks()) == 1
        assert not [e for e in run.trace.query(parent.id) if e.name == "plan_applied"]


@pytest.mark.parametrize("second_path", ["report.md", "REPORT.md"])
def test_multiple_producers_for_one_input_path_are_rejected_before_any_changes(
    tmp_path, second_path,
):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        payload = plan(deps=["writer", "checker"], inputs=[
            {"producer": "writer", "path": "report.md"},
            {"producer": "checker", "path": second_path},
        ])
        payload["subtasks"].insert(1, {
            "key": "checker", "role": "qa", "goal": "Produce another fixture version",
            "deps": [], "inputs": [],
        })
        recipient = state.workspaces["qa"] / "report.md"
        recipient.write_bytes(b"Existing recipient bytes")
        with pytest.raises(DelegationError, match="exactly one producer"):
            run.delegation.apply(parent, payload)
        assert len(run.repo.all_tasks()) == 1
        assert run.repo.get(parent.id).status == "RUNNING"
        assert recipient.read_bytes() == b"Existing recipient bytes"
        assert run.db.conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'teacher_inputs:%'",
        ).fetchone()[0] == 0
        assert not [e for e in run.trace.query(parent.id) if e.name == "plan_applied"]


def test_missing_inputs_and_historical_ids_are_not_silently_rewritten(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        payload = plan()
        del payload["subtasks"][0]["inputs"]
        with pytest.raises(DelegationError, match="explicitly declare"):
            run.delegation.apply(parent, payload)
        with pytest.raises(DelegationError, match="unknown"):
            run.delegation.apply(parent, plan(deps=[parent.id + ".old_writer"]))
        assert len(run.repo.all_tasks()) == 1


def test_metadata_survives_policy_reconstruction_and_own_edits_are_not_old_review(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        writer, reviewer = run.delegation.apply(parent, plan())
        source = state.workspaces["dev"] / "report.md"
        source.write_bytes(b"Original fixture bytes\r\n")
        run.repo.transition(writer.id, "READY", "fixture_ready")
        run.repo.transition(writer.id, "RUNNING", "fixture_dispatched")
        run.repo.set_result(writer.id, TaskResult(summary="Fixture", files=["report.md"]))
        run.repo.add_artifacts(writer.id, ["report.md"])
        run.repo.transition(writer.id, "DONE", "fixture_accepted")
        rebuilt = DeclaredInputs(run.repo, state.workspaces, parent.project_id,
                                 parent.conversation_id, run.trace)
        rebuilt.preflight(reviewer, state.workspaces["qa"])
        delivery = run.runner._materialise_dependencies(reviewer, state.workspaces["qa"])
        assert delivery[writer.id]["ok"]
        rebuilt.verify_received(reviewer, run.roles.get(parent.project_id, "qa"))
        recipient = state.workspaces["qa"] / "report.md"
        assert recipient.read_bytes() == source.read_bytes()
        recipient.write_text("An allowed consumer revision.", encoding="utf-8")
        # Reopening a direct consumer reports the conflict before native copy.
        with pytest.raises(InputHandoffError, match="conflicts with existing owned bytes"):
            rebuilt.preflight(reviewer, state.workspaces["qa"])
        # If this task has delegated review children, its original input is not
        # automatically restored on that later parent resume.
        run.repo.set_deps(reviewer.id, [])
        resumed = run.repo.get(reviewer.id)
        rebuilt.preflight(resumed, state.workspaces["qa"])
        rebuilt.verify_received(resumed, run.roles.get(parent.project_id, "qa"))
        assert recipient.read_text(encoding="utf-8") == "An allowed consumer revision."
        event = next(e for e in run.trace.query(reviewer.id)
                     if e.name == "declared_inputs_received")
        assert event.payload["final_file_review"] is None
        assert event.payload["human_quality"] is None


def test_corrupted_received_record_never_bypasses_first_admission(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        _writer, reviewer = run.delegation.apply(parent, plan())
        policy = state.context.input_policy
        run.db.set_meta(policy._key("received", reviewer.id), json.dumps(False))
        with pytest.raises(InputHandoffError, match="receipt"):
            policy.preflight(reviewer, state.workspaces["qa"])


def test_task_index_is_scoped_bounded_and_omits_private_history(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        state = configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        writer, _reviewer = run.delegation.apply(parent, plan())
        run.repo.transition(writer.id, "READY", "fixture_ready")
        run.repo.transition(writer.id, "RUNNING", "fixture_dispatched")
        run.repo.set_result(writer.id, TaskResult(
            summary="PRIVATE_SUMMARY", files=["report.md", ".evaluation/private.md"],
            notes="PRIVATE_NOTES", blockers=["PRIVATE_BLOCKER"],
        ))
        run.repo.add_artifacts(writer.id, ["report.md", ".evaluation/private.md"])
        run.repo.transition(writer.id, "DONE", "fixture_accepted")
        foreign = run.repo.get(writer.id).model_copy(update={
            "id": "foreign", "conversation_id": "other", "artifacts": ["FOREIGN.md"],
            "result": TaskResult(files=["FOREIGN.md"]),
        })
        run.repo.add(foreign)
        text = state.context.input_policy.task_index(parent)
        assert "report.md" in text and "not valid sibling keys" in text
        assert '"id": "foreign"' not in text
        assert all(value not in text for value in (
            "PRIVATE_SUMMARY", "PRIVATE_NOTES", "PRIVATE_BLOCKER", ".evaluation", "FOREIGN.md",
            str(state.workspaces["dev"]),
        ))
        assert len(text) < 4500


def test_input_contract_keeps_native_key_boundary(tmp_path):
    case = scenario(tmp_path)
    key = "writer_long_name"
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case, require_declared_inputs=True)
        item = run.tools.get("submit_plan").params_schema["properties"]["subtasks"]["items"]
        producer = item["properties"]["inputs"]["items"]["properties"]["producer"]
        # This is the actual advertised model schema, not a duplicate validator.
        if "pattern" in producer:
            assert re.fullmatch(producer["pattern"], key)
        assert len(key) >= producer.get("minLength", 0)
        assert len(key) <= producer.get("maxLength", len(key))
        payload = plan()
        payload["subtasks"][0]["key"] = key
        payload["subtasks"][1]["deps"] = [key]
        payload["subtasks"][1]["inputs"][0]["producer"] = key
        parent = running_parent(run, case)
        children = run.delegation.apply(parent, payload)
        assert children[1].deps == [parent.id + "." + key]


@pytest.mark.parametrize("key", ["writer.phase2", "writer_name_longer_than_sixteen"])
def test_invalid_native_keys_create_no_partial_input_contracts(tmp_path, key):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case, require_declared_inputs=True)
        parent = running_parent(run, case)
        payload = plan()
        payload["subtasks"][0]["key"] = key
        payload["subtasks"][1]["deps"] = [key]
        payload["subtasks"][1]["inputs"][0]["producer"] = key
        with pytest.raises(DelegationError):
            run.delegation.apply(parent, payload)
        assert len(run.repo.all_tasks()) == 1
        assert run.db.conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'teacher_inputs:%'",
        ).fetchone()[0] == 0
