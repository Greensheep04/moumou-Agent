"""Conversation reports use saved evidence and preserve unknown quality."""

import json
from pathlib import Path

import pytest

from moumou.eval.report import build_report, generate_report, render_report


def session(tmp_path, *, name="test.json", planned=6, source_kind="instructor"):
    folder = tmp_path / "scenario_1"
    folder.mkdir()
    task = {
        "phase": "task_1", "root_task_id": "t1", "root_status": "DONE",
        "elapsed_s": 12.0, "root_machine_accepted": None,
        "quality_status": "awaiting_human_or_independent_review",
        "verified_goal_success": None,
        "observed_subtasks": 999, "observed_subtasks_done": 999,
        "nodes": [{"id": "t1", "status": "DONE"},
                  {"id": "t1.child", "parent_id": "t1", "status": "DONE"}],
    }
    result = folder / "task_1-result.json"
    result.write_text(json.dumps(task), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "kind": "instructor_midtest" if name == "midtest.json" else "instructor_test",
        "dataset_kind": source_kind, "status": "running", "source": "selected bank",
        "planned_root_goals": planned,
        "goal_metrics": {"verified_successes": 999},
        "scenarios": [{"scenario_id": "scenario_1", "tasks": [
            {"phase": "task_1", "root_task_id": "t1", "verified_goal_success": True},
        ]}],
    }
    (tmp_path / name).write_text(json.dumps(manifest), encoding="utf-8")
    return task, result


def test_unreviewed_done_is_not_success_and_cached_counts_are_ignored(tmp_path):
    session(tmp_path)
    model = build_report(tmp_path)
    metrics = model["metrics"]
    assert metrics["planned_root_goals"] == 6
    assert metrics["verified_successes"] == 0
    assert metrics["agent_declared_complete"] == 1
    assert metrics["unreviewed_deliveries"] == 1
    assert metrics["not_delivered"] == 5
    assert metrics["observed_subtasks_done"] == metrics["observed_subtasks"] == 1
    assert metrics["delivery_time_s"]["sample_std"] is None
    report = render_report(model)
    assert "not a process-liveness check" in report
    assert "unreviewed: 1" in report and "12.000" in report


def test_review_evidence_required_for_claimed_success(tmp_path):
    task, path = session(tmp_path)
    task["verified_goal_success"] = True
    path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="quality-review evidence"):
        build_report(tmp_path)
    task.update(quality_status="human_accepted", quality_method="actual_human_review",
                human_quality_response={"decision": "approve", "request_id": "real-review"})
    path.write_text(json.dumps(task), encoding="utf-8")
    assert build_report(tmp_path)["metrics"]["provisional_verified_goal_rate"] == 1 / 6


def test_supplementary_denominator_and_identity_stay_separate(tmp_path):
    session(tmp_path, planned=15, source_kind="supplementary")
    model = build_report(tmp_path)
    assert model["metrics"]["not_delivered"] == 14
    assert "Dataset: supplementary" in render_report(model)


def test_report_reads_historical_session_without_rewriting_it(tmp_path):
    session(tmp_path, name="midtest.json")
    original = (tmp_path / "midtest.json").read_bytes()
    output = generate_report(tmp_path)
    assert output == tmp_path / "report.md"
    assert "Test session report" in output.read_text(encoding="utf-8")
    assert (tmp_path / "midtest.json").read_bytes() == original
    assert not (tmp_path / "test.json").exists()


def test_missing_task_evidence_and_ambiguous_roots_fail_closed(tmp_path):
    _, path = session(tmp_path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        build_report(tmp_path)
    (tmp_path / "midtest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        build_report(tmp_path)


def test_interruption_between_result_and_index_write_cannot_hide_a_delivery(tmp_path):
    session(tmp_path)
    path = tmp_path / "test.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenarios"][0]["tasks"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unindexed task result"):
        build_report(tmp_path)


def test_report_cannot_overwrite_workspace_evidence(tmp_path):
    session(tmp_path)
    workspace = tmp_path / "scenario_1/workspaces"
    workspace.mkdir()
    output = workspace / "README.md"
    output.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="original attempt evidence"):
        generate_report(tmp_path, output)
    assert output.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("change", ["../escape", "scenario_1/../../escape", "scenario_1/alias"])
def test_session_identity_cannot_redirect_evidence(tmp_path, change):
    session(tmp_path)
    path = tmp_path / "test.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenarios"][0]["scenario_id"] = change
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="scenario"):
        build_report(tmp_path)


def saved_timestamps(tmp_path, *, start, finish, status="agent_completed_quality_unreviewed"):
    path = tmp_path / "test.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(started_at=start, finished_at=finish, status=status)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.mark.parametrize("status", ["agent_completed_quality_unreviewed", "interrupted"])
def test_whole_session_span_uses_saved_offsets_and_includes_undelivered_time(tmp_path, status):
    session(tmp_path)
    path = saved_timestamps(tmp_path, start="2026-11-16T09:00:00+08:00",
                            finish="2026-11-16T02:00:00Z", status=status)
    before = path.read_bytes()
    model = build_report(tmp_path)
    metrics = model["metrics"]
    assert metrics["suite_wall_time_s"] == 3600
    assert metrics["suite_wall_time_evidence"]["status"] == "known"
    assert metrics["suite_wall_time_evidence"]["missing_timestamps"] == []
    assert metrics["delivery_time_s"]["sum"] == metrics["delivery_time_s"]["mean"] == 12
    assert metrics["delivery_time_s"]["n"] == 1
    assert metrics["not_delivered"] == 5 and metrics["unreviewed_deliveries"] == 1
    assert metrics["known_unsuccessful"] == metrics["verified_successes"] == 0
    text = render_report(model)
    assert "Whole-session elapsed time (s): 3600.000" in text
    assert "waiting and time spent on unfinished tasks" in text
    assert "exclude unfinished phases" in text
    assert path.read_bytes() == before


@pytest.mark.parametrize("start,finish,missing", [
    (None, None, ["started_at", "finished_at"]),
    ("2026-11-16T01:00:00+00:00", None, ["finished_at"]),
    (None, "2026-11-16T02:00:00+00:00", ["started_at"]),
])
def test_missing_saved_timestamp_keeps_total_unknown(tmp_path, start, finish, missing):
    session(tmp_path)
    saved_timestamps(tmp_path, start=start, finish=finish, status="interrupted")
    metrics = build_report(tmp_path)["metrics"]
    assert metrics["suite_wall_time_s"] is None
    assert metrics["suite_wall_time_evidence"]["status"] == "unknown"
    assert metrics["suite_wall_time_evidence"]["missing_timestamps"] == missing
    assert metrics["delivery_time_s"]["sum"] == 12


def test_zero_delivery_samples_and_external_stop_ledger_cannot_establish_execution_time(tmp_path):
    _, result = session(tmp_path)
    result.unlink()
    path = saved_timestamps(tmp_path, start="2026-11-16T01:00:00+00:00", finish=None)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenarios"][0]["tasks"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    (tmp_path / "stop-ledger.json").write_text(
        json.dumps({"finished_at": "2026-11-16T02:00:00+00:00", "elapsed_s": 3600}),
        encoding="utf-8",
    )
    model = build_report(tmp_path)
    assert model["metrics"]["suite_wall_time_s"] is None
    assert model["metrics"]["delivery_time_s"]["n"] == 0
    assert model["metrics"]["delivery_time_s"]["sum"] == 0
    assert model["metrics"]["delivery_time_s"]["mean"] is None
    text = render_report(model)
    assert "Whole-session elapsed time (s): unknown (missing finished_at)" in text
    assert "zero-sample delivery sum of zero is not total execution time" in text
    assert "external stop ledger" in text
    assert path.read_bytes() == before


@pytest.mark.parametrize("start,finish", [
    ("not-a-date", "2026-11-16T02:00:00Z"),
    ("2026-11-16T01:00:00Z", "not-a-date"),
    ("2026-11-16T01:00:00", "2026-11-16T02:00:00Z"),
    ("2026-11-16T01:00:00Z", "2026-11-16T02:00:00"),
    ("2026-11-16T02:00:00Z", "2026-11-16T01:00:00Z"),
    (123, "2026-11-16T02:00:00Z"),
    ("2026-11-16T01:00:00Z", True),
    ("", "2026-11-16T02:00:00Z"),
])
def test_invalid_naive_or_reversed_session_timestamps_fail_closed(tmp_path, start, finish):
    session(tmp_path)
    saved_timestamps(tmp_path, start=start, finish=finish)
    with pytest.raises(ValueError, match="saved session"):
        build_report(tmp_path)


def test_report_builder_identity_is_separate_from_unchanged_run_code_snapshot(tmp_path):
    import hashlib

    from moumou.eval import report

    session(tmp_path)
    path = tmp_path / "test.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    run_code = {"source_sha256": "a" * 64, "git_commit": "run-start-snapshot"}
    manifest["code"] = run_code
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    model = build_report(tmp_path)
    assert model["manifest"]["code"] == run_code
    assert model["report_builder"]["kind"] == "offline_reconstruction"
    expected = hashlib.sha256(Path(report.__file__).read_bytes()).hexdigest()
    assert model["report_builder"]["source_sha256"] == expected
    output = generate_report(tmp_path)
    text = output.read_text(encoding="utf-8")
    assert expected in text and "does not replace the run-start" in text
    assert path.read_bytes() == before
