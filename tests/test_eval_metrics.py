"""Hand-computable metric definitions, including missing attempts and overlapping spans."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from moumou.contracts import TraceEvent
from moumou.eval.metrics import sample_stats, span_totals, summarize


def plan():
    return {
        "eval_id": "batch", "planned_runs": 4,
        "dataset": {"fixtures": [
            {"fixture_id": "first", "scenario": "debug"},
            {"fixture_id": "second", "scenario": "parallel"},
        ]},
        "runs": [{"fixture_id": "first", "repeat": i} for i in range(3)]
        + [{"fixture_id": "second", "repeat": 0}],
    }


def result(fixture_id="first", repeat=0, **changes):
    row = {
        "eval_id": "batch", "fixture_id": fixture_id, "repeat": repeat,
        "scenario": "debug" if fixture_id == "first" else "parallel",
        "status": "succeeded", "root_status": "DONE", "root_success": True,
        "root_machine_accepted": True, "elapsed_s": 2.0, "wall_s": 1.5,
        "total_tokens_in": 10, "total_tokens_out": 5, "usage_missing_calls": 0,
        "nodes": [{"id": "t1", "parent_id": None, "status": "DONE", "acceptance_count": 1}],
    }
    row.update(changes)
    return row


def test_root_failure_does_not_disappear_behind_successful_subtasks_or_missing_results():
    failed = result(
        status="failed", root_status="DEAD", root_success=False,
        nodes=[
            {"id": "t1", "parent_id": None, "status": "DEAD", "acceptance_count": 1},
            {"id": "t2", "parent_id": "t1", "status": "DONE", "acceptance_count": 1,
             "machine_accepted": True},
            {"id": "t3", "parent_id": "t1", "status": "DONE", "acceptance_count": 0},
        ],
    )
    rows = [failed, result(repeat=1), result("second", status="error", root_success=None)]
    report = summarize(plan(), rows)
    batch = report["batch"]
    assert batch["planned_runs"] == 4
    assert batch["persisted_runs"] == 3
    assert batch["missing_results"] == 1
    assert batch["root_completion_rate"] == 1 / 4
    assert batch["observed_subtask_completion_rate"] == 1
    assert batch["machine_verified_subtask_rate"] == 1 / 2
    assert batch["nodes_without_machine_criteria"] == 1
    assert batch["usage"]["total_tokens_in"] is None
    assert report["by_fixture"]["first"]["root_completion_rate"] == 1 / 3
    assert report["by_scenario"]["parallel"]["root_completion_rate"] == 0


def test_three_repeats_use_sample_std_and_missing_usage_stays_unknown():
    manifest = plan()
    manifest["runs"] = manifest["runs"][:3]
    manifest["planned_runs"] = 3
    rows = [result(repeat=i, elapsed_s=seconds, total_tokens_out=None)
            for i, seconds in enumerate((1, 2, 3))]
    batch = summarize(manifest, rows)["batch"]
    assert batch["timing"]["runner_elapsed_s"] == {"n": 3, "mean": 2, "sample_std": 1}
    assert batch["usage"]["total_tokens_in"] == 30
    assert batch["usage"]["total_tokens_out"] is None
    assert batch["root_outcome_stats"] == {"n": 3, "mean": 1, "sample_std": 0}
    assert batch["observed_subtask_completion_rate"] is None


@pytest.mark.parametrize("changes", [
    {"status": "unverified"}, {"root_machine_accepted": None}, {"root_success": None},
    {"root_status": "DEAD"}, {"status": "error"},
])
def test_success_requires_terminal_root_and_machine_evidence(changes):
    assert summarize(plan(), [result(**changes)])["batch"]["verified_root_successes"] == 0


def test_empty_result_list_retains_all_planned_attempts_without_inventing_durations():
    batch = summarize(plan(), [])["batch"]
    assert batch["missing_results"] == 4
    assert batch["root_completion_rate"] == 0
    assert batch["timing"]["runner_elapsed_s"] == {"n": 0, "mean": None, "sample_std": None}
    assert batch["usage"]["total_tokens_in"] is None


@pytest.mark.parametrize("mutation", ["duplicate", "unplanned", "batch", "scenario"])
def test_foreign_or_duplicate_results_are_rejected(mutation):
    rows = [result()]
    if mutation == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif mutation == "unplanned":
        rows[0]["repeat"] = 20
    elif mutation == "batch":
        rows[0]["eval_id"] = "another-batch"
    else:
        rows[0]["scenario"] = "research"
    with pytest.raises(ValueError):
        summarize(plan(), rows)


@pytest.mark.parametrize("mutation", ["duplicate", "count", "empty", "identity"])
def test_invalid_manifest_plan_does_not_create_misleading_denominators(mutation):
    manifest = plan()
    if mutation == "duplicate":
        manifest["runs"].append(deepcopy(manifest["runs"][0]))
    elif mutation == "count":
        manifest["planned_runs"] = 0
    elif mutation == "empty":
        manifest["runs"] = []
    else:
        manifest["runs"][0]["repeat"] = True
    with pytest.raises(ValueError):
        summarize(manifest, [])


def test_unknown_criteria_are_separate_from_declared_no_criteria():
    row = result(nodes=[{"id": "t1", "parent_id": None, "status": "DONE"}])
    batch = summarize(plan(), [row])["batch"]
    assert batch["nodes_without_machine_criteria"] == 0
    assert batch["nodes_with_unknown_criteria"] == 1


def test_parallel_spans_are_cumulative_and_overlap_is_exposed():
    start = datetime(2026, 11, 13, tzinfo=UTC)
    events = [
        TraceEvent(task_id="a", kind="llm", name="model", t0=start,
                   t1=start + timedelta(seconds=4)),
        TraceEvent(task_id="b", kind="tool", name="execute", t0=start + timedelta(seconds=1),
                   t1=start + timedelta(seconds=5)),
        TraceEvent(task_id="c", kind="approval", name="approve", t0=start + timedelta(seconds=6),
                   t1=start + timedelta(seconds=8)),
        TraceEvent(task_id="c", kind="question", name="ask", t0=start + timedelta(seconds=8),
                   t1=start + timedelta(seconds=9)),
    ]
    totals = span_totals(events)
    assert totals["observed_cumulative_s"] == 11
    assert totals["closed_span_union_s"] == 8
    assert totals["overlap_s"] == 3
    assert totals["human_wait"]["total_s"] == 3
    assert totals["is_task_wall_time"] is False


def test_incomplete_and_invalid_spans_leave_unknown_totals():
    start = datetime(2026, 11, 13, tzinfo=UTC)
    totals = span_totals([
        {"span_id": "one", "kind": "llm", "t0": start, "t1": None},
        {"span_id": "two", "kind": "tool", "t0": start.isoformat(),
         "t1": (start - timedelta(seconds=1)).isoformat()},
        {"span_id": "three", "kind": "llm", "t0": "2026-11-13T01:00:00",
         "t1": "2026-11-13T01:00:01"},
    ])
    assert totals["per_kind"]["llm"] == {
        "closed_spans": 0, "incomplete_spans": 1, "invalid_spans": 1,
        "observed_s": 0, "total_s": None,
    }
    assert totals["per_kind"]["tool"]["total_s"] is None


def test_duplicate_span_does_not_double_count():
    span = TraceEvent(task_id="a", kind="llm", name="model")
    with pytest.raises(ValueError, match="duplicate"):
        span_totals([span, span])


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "3"])
def test_nonfinite_or_nonnumeric_samples_are_rejected(value):
    with pytest.raises(ValueError):
        sample_stats([value])


def test_single_sample_std_is_unknown():
    assert sample_stats([None, 0]) == {"n": 1, "mean": 0, "sample_std": None}


def test_task_terminal_and_later_maintenance_times_are_not_combined_with_runner_time():
    batch = summarize(plan(), [result(task_elapsed_s=1, maintenance_s=0.5)])["batch"]
    assert batch["timing"]["task_terminal_s"] == {"n": 1, "mean": 1, "sample_std": None}
    assert batch["timing"]["maintenance_s"] == {"n": 1, "mean": 0.5, "sample_std": None}
    assert batch["timing"]["runner_elapsed_s"]["mean"] == 2


def test_missing_telemetry_is_not_zero_cost_in_phase_aggregates():
    snapshot = {
        "calls_total": 2, "known_tokens_in": 20, "known_tokens_out": 6,
        "total_tokens_in": 20, "total_tokens_out": 6,
        "by_phase": {"post_terminal": {
            "calls_total": 1, "total_tokens_in": 10, "total_tokens_out": 3,
            "known_tokens_in": 10, "known_tokens_out": 3,
        }},
    }
    batch = summarize(plan(), [result(metrics={"llm": snapshot})])["batch"]
    assert batch["llm"]["missing_run_telemetry"] == 3
    assert batch["llm"]["calls_total"] is None
    assert batch["llm"]["observed_calls_total"] == 2
    assert batch["llm"]["by_phase"]["post_terminal"]["known_tokens_in"] == 10
    assert batch["llm"]["by_phase"]["post_terminal"]["total_tokens_in"] is None
