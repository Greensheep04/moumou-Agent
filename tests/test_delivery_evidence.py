"""Immutable delivery facts stay distinct from current workspaces and quality."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from moumou.contracts import MemoryRecord, RoleSpec, TaskNode, TaskResult, TraceEvent
from moumou.eval.session_evidence import (
    capture_artifact_snapshot,
    capture_failure_snapshot,
    capture_memory_snapshot,
    capture_scenario_state,
    sanitize_evidence,
    validate_artifact_snapshot,
)


def node(**overrides):
    return TaskNode(id="root", project_id="scenario_1", conversation_id="instructor-group",
                    goal="Deliver output", role="lead", **overrides)


def paths(tmp_path):
    scenario, workspace = tmp_path / "scenario_1", tmp_path / "workspace"
    scenario.mkdir()
    workspace.mkdir()
    return scenario, workspace


def test_configured_secret_scrubbing_includes_keys_without_mutating_original_facts():
    raw = {"credential-test-secret": ["a test-secret value"]}
    cleaned = sanitize_evidence(raw, ("test-secret",))
    assert cleaned == {"credential-[REDACTED]": ["a [REDACTED] value"]}
    assert raw == {"credential-test-secret": ["a test-secret value"]}
    with pytest.raises(ValueError, match="merge distinct"):
        sanitize_evidence({"key-one": 1, "key-two": 2}, ("one", "two"))


def test_delivered_snapshot_survives_later_workspace_edits_and_cannot_be_overwritten(tmp_path):
    scenario, workspace = paths(tmp_path)
    (workspace / "out.md").write_text("original delivered bytes", encoding="utf-8")
    task = node(result=TaskResult(files=["out.md"]))
    ref = capture_artifact_snapshot(scenario, workspace, task, "task_1")
    (workspace / "out.md").write_text("Task 2 has changed this file", encoding="utf-8")
    saved = validate_artifact_snapshot(scenario, ref,
                                      task={"root_task_id": "root", "phase": "task_1"})
    assert saved["status"] == "complete" and saved["captured_count"] == 1
    assert (scenario / saved["entries"][0]["file"]).read_text(encoding="utf-8") == (
        "original delivered bytes"
    )
    with pytest.raises(FileExistsError):
        capture_artifact_snapshot(scenario, workspace, task, "task_1")
    with pytest.raises(ValueError, match="identity"):
        validate_artifact_snapshot(scenario, ref,
                                   task={"root_task_id": "other", "phase": "task_1"})


def test_partial_capture_preserves_missing_paths_and_excludes_secrets_and_framework_files(tmp_path):
    scenario, workspace = paths(tmp_path)
    secret = "classroom-secret-for-offline-regression-only"
    (workspace / ".evaluation").mkdir()
    files = {"safe.txt": "deliverable", ".env": "sensitive env", "leak.txt": secret,
             ".evaluation/history.json": "private context", ".env.example": "API_KEY="}
    for name, text in files.items():
        (workspace / name).write_text(text, encoding="utf-8")
    names = [*files, "deleted.txt", "../outside.txt", "sub/../safe.txt"]
    task = node(artifacts=names)
    ref = capture_artifact_snapshot(scenario, workspace, task, "task_1", secrets=(secret,))
    saved = validate_artifact_snapshot(scenario, ref)
    by_path = {entry["path"]: entry for entry in saved["entries"]}
    assert saved["status"] == "partial" and saved["selection"] == "tracked_artifacts"
    assert by_path["safe.txt"]["state"] == by_path[".env.example"]["state"] == "captured"
    assert by_path[".env"]["state"] == "excluded_secret_path"
    assert by_path["leak.txt"]["state"] == "excluded_secret_content"
    assert by_path[".evaluation/history.json"]["state"] == "excluded_framework_private"
    assert by_path["deleted.txt"]["state"] == "missing_or_deleted"
    assert by_path["../outside.txt"]["state"] == by_path["sub/../safe.txt"]["state"] == "invalid"
    assert all(secret.encode() not in path.read_bytes()
               for path in (scenario / "task_1-artifacts").iterdir())


def test_secret_across_copy_chunk_boundary_never_enters_a_saved_blob(tmp_path):
    scenario, workspace = paths(tmp_path)
    secret = "sensitive-copy-boundary"
    (workspace / "out.txt").write_bytes(b"x" * 65530 + secret.encode() + b"tail")
    ref = capture_artifact_snapshot(scenario, workspace,
                                    node(result=TaskResult(files=["out.txt"])),
                                    "task_1", secrets=(secret,))
    saved = validate_artifact_snapshot(scenario, ref)
    assert saved["entries"][0]["state"] == "excluded_secret_content"
    assert saved["captured_count"] == 0
    assert [path.name for path in (scenario / "task_1-artifacts").iterdir()] == ["manifest.json"]


@pytest.mark.parametrize("private", [".env", ".evaluation/prior-dialogue.txt"])
def test_private_file_alias_does_not_bypass_snapshot_exclusions(tmp_path, private):
    scenario, workspace = paths(tmp_path)
    target = workspace / private
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("private values without a configured secret", encoding="utf-8")
    try:
        (workspace / "public.txt").symlink_to(target)
    except OSError:
        pytest.skip("host does not permit creating file symlinks")
    ref = capture_artifact_snapshot(scenario, workspace,
                                    node(result=TaskResult(files=["public.txt"])), "task_1")
    saved = validate_artifact_snapshot(scenario, ref)
    assert saved["captured_count"] == 0
    assert saved["entries"][0]["state"] == (
        "excluded_secret_path" if private == ".env" else "excluded_framework_private"
    )
    assert [path.name for path in (scenario / "task_1-artifacts").iterdir()] == ["manifest.json"]


@pytest.mark.parametrize("damage", ["manifest", "blob", "count_type", "unsafe_reference"])
def test_snapshot_reader_rejects_changed_or_invalid_evidence(tmp_path, damage):
    scenario, workspace = paths(tmp_path)
    (workspace / "out.txt").write_text("original", encoding="utf-8")
    ref = capture_artifact_snapshot(scenario, workspace,
                                    node(result=TaskResult(files=["out.txt"])), "task_1")
    manifest_path = scenario / ref["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "manifest":
        manifest_path.write_text("{}", encoding="utf-8")
    elif damage == "blob":
        (scenario / manifest["entries"][0]["file"]).write_text("altered", encoding="utf-8")
    elif damage == "count_type":
        ref["selected_count"] = True
    else:
        ref["path"] = "../external.json"
    with pytest.raises(ValueError):
        validate_artifact_snapshot(scenario, ref)


def engine_with(task, *, records=(), events=(), memory_enabled=True):
    return SimpleNamespace(
        settings=SimpleNamespace(memory_enabled=memory_enabled, project_id="scenario_1",
                                 conversation_id="instructor-group"),
        roles=SimpleNamespace(list=lambda project: [RoleSpec(
            name="lead", project_id="scenario_1", title="Lead",
            description="Lead the team", reason="coordinate")]),
        memory=SimpleNamespace(all=lambda scope: list(records)),
        trace=SimpleNamespace(query=lambda task_id: list(events)),
        repo=SimpleNamespace(all_tasks=lambda: [task]),
    )


def test_memory_and_failure_snapshots_preserve_observations_without_claiming_success(tmp_path):
    task = node(status="WAITING_HUMAN")
    record = MemoryRecord(project_id="scenario_1", role="lead", task_id="earlier",
                          situation="earlier task", action="check history", outcome="success",
                          lesson="Retain the original history", uses=2, score=1.1)
    events = [TraceEvent(task_id=task.id, kind="memory", name="memory_retrieved",
                         payload={"retrieved_ids": [record.id]}),
              TraceEvent(task_id=task.id, kind="tool", name="python", status="ok",
                         payload={"ok": False}),
              TraceEvent(task_id=task.id, kind="approval", name="git", status="ok",
                         payload={"decision": "reject"})]
    engine = engine_with(task, records=[record], events=events)
    memory = capture_memory_snapshot(engine, [task])
    assert memory["status"] == "recorded" and memory["identity_policy"] == "project_role"
    saved = memory["scopes"][0]["records"][0]
    assert saved["uses"] == 2 and saved["lesson_sha256"] == hashlib.sha256(
        record.lesson.encode()).hexdigest()
    assert "lesson" not in saved and len(memory["observations"]) == 1
    assert "causal_benefit" not in memory and "verified_goal_success" not in memory
    failures = capture_failure_snapshot(engine, [task])
    assert failures["tool_failure_count"] == 1 and len(failures["events"]) == 2
    scenario = tmp_path / "scenario_1"
    scenario.mkdir()
    ref = capture_scenario_state(scenario, engine)
    state_path = scenario / ref["path"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert ref["sha256"] == hashlib.sha256(state_path.read_bytes()).hexdigest()
    assert state["nodes"][0]["status"] == "WAITING_HUMAN"
    assert state["kind"] == "observed_state_not_delivery"
    assert "verified_goal_success" not in state and "tasks" not in state
    with pytest.raises(ValueError, match="already exists"):
        capture_scenario_state(scenario, engine)


def test_incomplete_observation_and_disabled_memory_stay_unknown():
    task = node()
    engine = engine_with(task, memory_enabled=False)
    assert capture_memory_snapshot(engine, [task])["status"] == "disabled"

    def unreadable(*args):
        raise OSError("synthetic offline store failure")

    engine.settings.memory_enabled = True
    engine.memory.all = unreadable
    engine.trace.query = unreadable
    memory = capture_memory_snapshot(engine, [task])
    failure = capture_failure_snapshot(engine, [task])
    assert memory["status"] == "partial" and memory["scopes"][0]["records"] is None
    assert failure["status"] == "partial" and failure["tool_failure_count"] is None
    assert failure["observed_tool_failures"] == 0
