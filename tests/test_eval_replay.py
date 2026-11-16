"""Saved-session replay and analysis checks; no model or live runtime is required."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from moumou.eval import replay as replay_module
from moumou.eval.analysis import build_analysis, generate_analysis, render_analysis
from moumou.eval.replay import build_replay, generate_replay


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def journal(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")


def session(root, *, scenarios=1):
    manifest = {
        "schema_version": 1, "kind": "instructor_test", "status": "running",
        "planned_root_goals": 6, "started_at": "2026-11-16T00:00:00+00:00",
        "finished_at": None, "code": {"source_sha256": "a" * 64},
        "model": {"model": "FakeLLM"}, "scenarios": [],
    }
    for number in range(1, scenarios + 1):
        name = f"scenario_{number}"
        task = {
            "phase": "task_1", "root_task_id": "t1", "root_status": "DONE",
            "elapsed_s": 4.0, "root_machine_accepted": None,
            "root_machine_criteria_present": False, "agent_declared_complete": True,
            "quality_status": "awaiting_human_or_independent_review",
            "verified_goal_success": None,
            "nodes": [{"id": "t1", "parent_id": None, "role": "lead", "status": "DONE",
                       "machine_criteria": 0,
                       "result": {"files": []}}],
        }
        write(root / name / "task_1-result.json", task)
        manifest["scenarios"].append({"scenario_id": name, "status": "running", "tasks": [
            {"phase": "task_1", "root_task_id": "t1"},
        ]})
    write(root / "test.json", manifest)
    return root


def event(**extra):
    return {"span_id": "span-1", "task_id": "t1", "kind": "tool", "name": "read_file",
            "agent_id": "lead", "t0": "2026-11-16T00:00:00+00:00",
            "t1": "2026-11-16T00:00:01+00:00", "status": "error",
            "payload": {"error": "observed tool symptom"}, **extra}


def test_replay_qualifies_same_task_ids_and_preserves_original_trace(tmp_path):
    root = session(tmp_path, scenarios=2)
    original = event()
    journal(root / "scenario_1/trace.jsonl", [original])
    before = (root / "scenario_1/trace.jsonl").read_bytes()
    model = build_replay(root)
    assert [item["identity"] for item in model["tasks"]] == [
        {"scenario_id": "scenario_1", "task_id": "t1"},
        {"scenario_id": "scenario_2", "task_id": "t1"},
    ]
    saved = model["scenarios"][0]["trace"]["events"][0]
    assert saved["record"] == original and "parent_span" not in saved["record"]
    assert saved["source"]["line_number"] == 1
    assert saved["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert (root / "scenario_1/trace.jsonl").read_bytes() == before
    assert model["scenarios"][1]["trace"]["state"] == "missing"


def test_staged_human_response_is_separate_from_consumed_event(tmp_path):
    root = session(tmp_path)
    request = {"id": "question-1", "task_id": "t1", "agent_id": "lead", "kind": "question",
               "summary": "Choose a scope"}
    response = {"request_id": request["id"], "text": "Actual answer", "decision": None}
    records = [{"event": "human_request", "phase": "task_1", "request": request}]
    journal(root / "scenario_1/human/human-events.jsonl", records)
    write(root / "scenario_1/human/pending/question-1.json", {"request": request})
    write(root / "scenario_1/human/responses/question-1.json", response)
    human = build_replay(root)["scenarios"][0]["human"]
    assert human["answered"] == [] and len(human["unanswered_requests"]) == 1
    assert len(human["pending_files"]) == len(human["staged_response_files"]) == 1
    assert human["staged_response_files"][0]["record"] == response

    records.append({"event": "human_response", "phase": "task_1", "response": response})
    journal(root / "scenario_1/human/human-events.jsonl", records)
    human = build_replay(root)["scenarios"][0]["human"]
    pair = human["answered"][0]
    assert pair["request_event"]["record"]["request"] == request
    assert pair["response_event"]["record"]["response"] == response
    assert pair["response_event"]["source"]["line_number"] == 2
    assert "not inferred" in pair["provenance"]


def test_console_human_uses_original_mixed_transcript_indices(tmp_path):
    root = session(tmp_path)
    request = {"id": "q", "task_id": "t1", "kind": "question"}
    journal(root / "scenario_1/dialogue.jsonl", [
        {"event": "llm_request", "messages": []},
        {"event": "human_request", "request": request},
        {"event": "human_response", "response": {"request_id": "q", "text": "yes"}},
    ])
    human = build_replay(root)["scenarios"][0]["human"]
    assert len(human["journal"]["events"]) == 2
    assert human["answered"][0]["request_event"]["source"]["line_number"] == 2
    assert human["answered"][0]["response_event"]["source"]["line_number"] == 3


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}\n', '{"event":NaN}\n', '{"broken":'])
def test_replay_rejects_corrupt_journal_without_changing_it(tmp_path, raw):
    root = session(tmp_path)
    path = root / "scenario_1/trace.jsonl"
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        build_replay(root)
    assert path.read_bytes() == before


def test_replay_output_is_fresh_and_cannot_replace_originals(tmp_path):
    root = session(tmp_path)
    before = (root / "test.json").read_bytes()
    with pytest.raises(ValueError):
        generate_replay(root, root / "test.json")
    with pytest.raises(ValueError):
        generate_replay(root, root / "scenario_1/new.json")
    path = generate_replay(root)
    assert json.loads(path.read_text(encoding="utf-8"))["read_only"] is True
    with pytest.raises(ValueError):
        generate_replay(root)
    assert (root / "test.json").read_bytes() == before


def test_instructor_analysis_keeps_denominator_and_tool_symptom_is_not_failure_cause(tmp_path):
    root = session(tmp_path)
    journal(root / "scenario_1/trace.jsonl", [event()])
    model = build_analysis(root)
    assert model["kind"] == "instructor_test"
    assert model["summary"]["planned_root_goals"] == 6
    assert model["summary"]["not_delivered"] == 5
    assert model["summary"]["verified_successes"] == 0
    assert model["summary"]["suite_wall_time_s"] is None
    assert model["failure_categories"] == {}
    task = model["attempts"][0]
    assert task["classification"]["category"] == "unreviewed"
    assert task["observed_symptoms"][0]["category"] == "tool_error"
    assert task["artifact_snapshot"]["state"] == "unknown"
    assert task["memory"]["snapshot"]["state"] == "unknown"
    assert task["memory"]["retrieval_call_count"] is None
    assert "causal benefit not established" in render_analysis(model)


def test_replay_exclusive_create_does_not_replace_a_racing_writer(tmp_path, monkeypatch):
    root = session(tmp_path)
    target = root / "replay.json"
    original_open = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "x":
            with original_open(path, "w", encoding="utf-8") as stream:
                stream.write("another writer's evidence")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError):
        generate_replay(root)
    assert target.read_text(encoding="utf-8") == "another writer's evidence"


@pytest.mark.parametrize("changes", [
    {"t0": "invalid date"}, {"t0": "2026-11-16T00:00:00"}, {"status": "invented"},
    {"tokens_in": False}, {"kind": "not_a_span_kind"}, {"parent_span": 123},
])
def test_replay_validates_original_trace_types_and_timestamps(tmp_path, changes):
    root = session(tmp_path)
    journal(root / "scenario_1/trace.jsonl", [event(**changes)])
    with pytest.raises(ValueError):
        build_replay(root)


def test_session_analysis_never_reads_live_memory_db_as_a_snapshot(tmp_path, monkeypatch):
    root = session(tmp_path)
    (root / "scenario_1/memory.db").write_bytes(b"current database, not historical evidence")

    def forbidden(*args, **kwargs):
        pytest.fail("offline instructor analysis must not query a live memory database")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    model = build_analysis(root)
    assert model["attempts"][0]["memory"]["snapshot"]["capture"] is None


def test_memory_observations_prefer_saved_phase_capture_over_later_trace(tmp_path):
    root = session(tmp_path)
    path = root / "scenario_1/task_1-result.json"
    task = json.loads(path.read_text(encoding="utf-8"))
    captured_event = event(kind="memory", name="memory_retrieved", status="ok")
    capture = {
        "schema_version": 1, "status": "recorded", "captured_at": "2026-11-16T00:00:01Z",
        "stage": "post_engine_run", "identity_policy": "project_role", "scopes": [],
        "observations": [captured_event],
    }
    task["memory_snapshot"] = capture
    write(path, task)
    journal(root / "scenario_1/trace.jsonl", [
        captured_event, event(span_id="later", kind="memory", name="later_event"),
    ])
    before = copy.deepcopy(capture)
    memory = build_analysis(root)["attempts"][0]["memory"]
    assert memory["snapshot"]["capture"] == before
    assert memory["observation_basis"] == "saved_phase_capture"
    assert memory["observed_event_counts"] == {"memory_retrieved": 1}
    assert memory["retrieval_call_count"] is None
    assert json.loads(path.read_text(encoding="utf-8"))["memory_snapshot"] == before


def test_saved_artifact_capture_wins_over_mutable_workspace_and_rejects_tampering(tmp_path):
    root = session(tmp_path)
    folder = root / "scenario_1"
    accepted = b"accepted first delivery\n"
    digest = hashlib.sha256(accepted).hexdigest()
    blob = folder / "task_1-artifacts" / (digest[:32] + ".txt")
    blob.parent.mkdir()
    blob.write_bytes(accepted)
    manifest = {
        "schema_version": 1, "scenario_id": "scenario_1", "root_task_id": "t1",
        "phase": "task_1", "captured_at": "2026-11-16T00:00:02Z",
        "selection": "result_files", "selected_count": 1, "captured_count": 1,
        "status": "complete", "entries": [{
            "path": "answer.txt", "state": "captured", "file": blob.relative_to(folder).as_posix(),
            "sha256": digest, "bytes": len(accepted),
        }],
    }
    manifest_path = blob.parent / "manifest.json"
    write(manifest_path, manifest)
    task_path = folder / "task_1-result.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["artifact_snapshot"] = {key: manifest[key] for key in (
        "schema_version", "status", "captured_at", "selection", "selected_count", "captured_count",
    )} | {"path": "task_1-artifacts/manifest.json",
         "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    task["nodes"][0]["result"]["files"] = ["answer.txt"]
    write(task_path, task)
    workspace = folder / "workspaces/lead"
    workspace.mkdir(parents=True)
    (workspace / "answer.txt").write_text("changed by a later task", encoding="utf-8")

    snapshot = build_analysis(root)["attempts"][0]["artifact_snapshot"]
    assert snapshot["state"] == "validated_saved_capture"
    assert snapshot["manifest"] == manifest
    assert blob.read_bytes() == accepted
    blob.write_bytes(b"tampered accepted copy")
    with pytest.raises(ValueError):
        build_replay(root)


@pytest.mark.parametrize(("invalid_field", "invalid_value"), [
    ("project_id", "scenario_2"), ("status", "invented"), ("status", []),
])
def test_scenario_state_is_separate_diagnostic_and_cannot_inflate_delivery_count(
    tmp_path, invalid_field, invalid_value,
):
    root = session(tmp_path)
    body = {
        "schema_version": 1, "scenario_id": "scenario_1", "conversation_id": "group-1",
        "captured_at": "2026-11-16T00:00:05Z", "stage": "scenario_close",
        "kind": "observed_state_not_delivery", "nodes": [
            {"id": "t1", "project_id": "scenario_1", "conversation_id": "group-1",
             "role": "lead", "status": "DONE"},
            {"id": "t2", "project_id": "scenario_1", "conversation_id": "group-1",
             "role": "lead", "status": "WAITING_HUMAN"},
        ],
    }
    path = root / "scenario_1/state-snapshot.json"
    write(path, body)
    manifest_path = root / "test.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = {"schema_version": 1, "status": "recorded", "path": path.name,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                 "captured_at": body["captured_at"], "node_count": 2,
                 "kind": "observed_state_not_delivery"}
    manifest["scenarios"][0]["state_snapshot"] = reference
    write(manifest_path, manifest)
    model = build_replay(root)
    diagnostic = model["scenarios"][0]["state_snapshot"]
    assert diagnostic["record"] == body
    assert model["metrics"]["persisted_task_deliveries"] == 1
    assert len(model["tasks"]) == 1
    analysis = build_analysis(root)
    assert analysis["scenario_diagnostics"][0]["evidence"]["record"]["nodes"][1] == body["nodes"][1]
    assert analysis["summary"]["not_delivered"] == 5

    body["nodes"][1][invalid_field] = invalid_value
    write(path, body)
    reference["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(manifest_path, manifest)
    with pytest.raises(ValueError):
        build_replay(root)


@pytest.mark.parametrize("field,value", [
    ("quality_status", "human_rejected"), ("memory_snapshot", {"status": "unavailable"}),
])
def test_replay_rejects_same_status_content_change_after_report_validation(
    tmp_path, monkeypatch, field, value,
):
    root = session(tmp_path)
    original_report = replay_module.build_report

    def changed_after_validation(path):
        model = original_report(path)
        task_path = root / "scenario_1/task_1-result.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task[field] = value
        write(task_path, task)
        return model

    monkeypatch.setattr(replay_module, "build_report", changed_after_validation)
    with pytest.raises(ValueError, match="content changed"):
        replay_module.build_replay(root)


@pytest.mark.parametrize("filename", ["test.json", "active.json", "replay.json"])
def test_instructor_analysis_cannot_overwrite_raw_suite_metadata(tmp_path, filename):
    root = session(tmp_path)
    with pytest.raises(ValueError):
        generate_analysis(root, root / filename)
