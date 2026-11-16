"""Authored offline report-evidence regressions; this development turn runs no tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from test_eval_review import answer_review, saved_case
from test_eval_session_report import session

from moumou.contracts import TaskNode, TaskResult, TraceEvent
from moumou.eval.report import build_report, render_report
from moumou.eval.session_evidence import capture_artifact_snapshot


def test_missing_observations_remain_unknown_instead_of_zero(tmp_path):
    session(tmp_path)
    model = build_report(tmp_path)
    evidence = model["records"][0]["evidence"]
    assert evidence["llm"] is None and evidence["search_delta"] is None
    assert evidence["memory_snapshot"] is None and evidence["memory_status"] == "unknown"
    assert evidence["failure"]["tool_failure_count"] is None
    assert evidence["artifact_status"] == "unsupported_historical_artifact_snapshot"
    text = render_report(model)
    assert "cumulative LLM call seconds" in text
    assert "Live memory databases are not sampled" in text
    assert "Cumulative call/wait spans can overlap and are not wall time" in text


def test_result_semantic_hash_tracks_loaded_quality_and_capture_content(tmp_path):
    task, path = session(tmp_path)
    first = build_report(tmp_path)["records"][0]
    expected = hashlib.sha256(json.dumps(
        task, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    assert first["result_content_sha256"] == expected
    # Formatting is not a semantic change; review binding retains a separate raw-byte hash.
    path.write_text(json.dumps(task, indent=4), encoding="utf-8")
    assert build_report(tmp_path)["records"][0]["result_content_sha256"] == expected
    task["quality_status"] = "different_saved_unknown_quality"
    task["llm"] = {"calls": 1}
    path.write_text(json.dumps(task), encoding="utf-8")
    changed = build_report(tmp_path)["records"][0]
    assert changed["root_status"] == first["root_status"]
    assert changed["result_content_sha256"] != expected


def test_saved_phase_usage_and_scenario_totals_are_presented_separately(tmp_path):
    task, path = session(tmp_path)
    task.update(
        llm={"calls": 3, "error_calls": 1, "usage_missing_calls": 1,
             "known_tokens_in": 20, "known_tokens_out": 5, "tokens_in": None,
             "tokens_out": None, "cumulative_call_s": 8.5},
        human={"answered_requests": 2, "cumulative_wait_s": 12.5},
        search_delta={"provider_requests": 2, "cache_hits": 1, "budget_denials": 0},
        search_scenario_cumulative={"provider_requests": 12, "used": 12, "limit": 12},
    )
    path.write_text(json.dumps(task), encoding="utf-8")
    model = build_report(tmp_path)
    evidence = model["records"][0]["evidence"]
    assert evidence["llm"]["values"]["tokens_in"] is None
    assert evidence["llm"]["values"]["known_tokens_in"] == 20
    assert evidence["llm"]["values"]["cumulative_call_s"] == 8.5
    assert evidence["human"]["values"]["answered_requests"] == 2
    assert evidence["human"]["values"]["cumulative_wait_s"] == 12.5
    assert evidence["search_delta"]["values"]["provider_requests"] == 2
    assert evidence["search_scenario_cumulative"]["values"]["provider_requests"] == 12
    assert "not task-only usage" in evidence["search_scenario_cumulative"]["scope"]
    assert model["metrics"]["delivery_time_s"]["mean"] == 12
    assert model["metrics"]["suite_wall_time_s"] is None


@pytest.mark.parametrize("field,values", [
    ("llm", {"calls": True}),
    ("llm", {"cumulative_call_s": -1}),
    ("search_delta", {"cache_hits": -1}),
])
def test_invalid_saved_observation_values_are_not_used_as_metrics(tmp_path, field, values):
    task, path = session(tmp_path)
    task[field] = values
    path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid saved"):
        build_report(tmp_path)


def test_memory_uses_saved_state_and_hash_checked_candidates_without_live_database(
    tmp_path, monkeypatch,
):
    task, path = session(tmp_path)
    task["memory_snapshot"] = {
        "schema_version": 1, "captured_at": "2026-11-16T02:00:00+00:00",
        "stage": "post_engine_run", "status": "recorded", "identity_policy": "project_role",
        "scopes": [{"scope": "scenario_1:lead", "project_id": "scenario_1", "role": "lead",
                    "records": [{"id": "old-memory", "task_id": "previous-root",
                                 "outcome": "success", "score": 1.0, "uses": 0,
                                 "created_at": "2026-11-15T02:00:00+00:00",
                                 "last_used_at": None, "lesson_sha256": "a" * 64}]}],
        "observations": [TraceEvent(task_id="t1", kind="memory", name="memory_prompt_observed",
                                    payload={"memory_ids": ["old-memory"]}).model_dump(mode="json")],
    }
    path.write_text(json.dumps(task), encoding="utf-8")
    snapshot = {"schema_version": 1, "task_id": "t1", "role": "lead",
                "memory_hits": [{"id": "old-memory", "role": "lead"}]}
    raw = json.dumps(snapshot, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    folder = tmp_path / "scenario_1/workspaces/lead/.evaluation/context"
    folder.mkdir(parents=True)
    archive = folder / (digest[:32] + ".json")
    archive.write_bytes(raw)

    def forbidden(*args, **kwargs):
        raise AssertionError("report must not sample mutable live memory databases")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    row = build_report(tmp_path)["records"][0]
    assert row["evidence"]["memory_snapshot"]["values"] == task["memory_snapshot"]
    saved, = row["evidence"]["memory_context_snapshots"]
    assert saved["sha256"] == digest and saved["candidate_ids"] == ["old-memory"]
    assert saved["node_id"] == "t1" and "admission/use unverified" in saved["scope"]
    assert row["verified_goal_success"] is None
    archive.write_bytes(b"changed context bytes")
    with pytest.raises(ValueError, match="content hash"):
        build_report(tmp_path)


async def test_review_binds_saved_artifact_snapshot_and_detects_changed_blob(tmp_path):
    task, path, _ = saved_case(tmp_path)
    directory = tmp_path / "scenario_1"
    workspace = directory / "workspaces/lead"
    workspace.mkdir(parents=True)
    (workspace / "output.txt").write_text("reviewed output", encoding="utf-8")
    node = TaskNode(id="t1", project_id="scenario_1", conversation_id="group", role="lead",
                    status="DONE",
                    goal="saved fixture goal", result=TaskResult(files=["output.txt"]))
    task["artifact_snapshot"] = capture_artifact_snapshot(directory, workspace, node, "task_1")
    path.write_text(json.dumps(task), encoding="utf-8")
    _, review = await answer_review(tmp_path, "approve")
    assert review["binding"]["artifact_snapshot"] == task["artifact_snapshot"]
    assert review["binding"]["artifact_boundary"] == "saved_immutable_snapshot"
    (workspace / "output.txt").write_text("later mutable workspace bytes", encoding="utf-8")
    model = build_report(tmp_path)
    snapshot = model["records"][0]["evidence"]["artifact_snapshot"]
    entry, = [entry for entry in snapshot["entries"] if entry["state"] == "captured"]
    blob = directory / entry["file"]
    blob.write_bytes(b"tampered snapshot bytes")
    with pytest.raises(ValueError, match="(snapshot|hash|digest)"):
        build_report(tmp_path)


def test_partial_memory_and_failure_snapshots_keep_unknown_coverage(tmp_path):
    task, path = session(tmp_path)
    event = TraceEvent(task_id="t1", kind="tool", name="python", status="error",
                       payload={"ok": False, "error": "fixture failure"}).model_dump(mode="json")
    task["memory_snapshot"] = {
        "schema_version": 1, "stage": "post_engine_run", "status": "partial",
        "scopes": [{"scope": "scenario_1:lead", "records": None}], "observations": [],
    }
    task["failure_snapshot"] = {
        "schema_version": 1, "status": "partial", "observed_tool_failures": 1,
        "tool_failure_count": None, "events": [event],
        "errors": [{"task_id": "t1.child", "error_type": "OSError"}],
    }
    path.write_text(json.dumps(task), encoding="utf-8")
    row = build_report(tmp_path)["records"][0]
    assert row["evidence"]["memory_status"] == "partial"
    assert row["evidence"]["memory_snapshot"]["values"]["scopes"][0]["records"] is None
    assert row["evidence"]["failure"]["tool_failure_count"] is None
    assert row["evidence"]["failure"]["snapshot"]["observed_tool_failures"] == 1
    assert row["verified_goal_success"] is None


@pytest.mark.parametrize("change", ["tree", "count", "machine"])
def test_saved_failure_identity_counts_and_machine_decision_are_checked(tmp_path, change):
    task, path = session(tmp_path)
    event = TraceEvent(task_id="t1", kind="tool", name="python", status="error",
                       payload={"ok": False}).model_dump(mode="json")
    task["failure_snapshot"] = {
        "schema_version": 1, "status": "recorded", "observed_tool_failures": 1,
        "tool_failure_count": 1, "events": [event], "errors": [],
    }
    if change == "tree":
        event["task_id"] = "other-root"
    elif change == "count":
        task["failure_snapshot"]["tool_failure_count"] = 7
    else:
        task["root_machine_accepted"] = "false"
    path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="(failure|root_machine_accepted)"):
        build_report(tmp_path)
