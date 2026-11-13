"""Reproducible evaluation summaries, with missing observations kept visible.

The manifest plan defines the denominator. A node's DONE status and an accepted
root goal are different measurements. Legacy Engine token/wall fields are not
silently upgraded to full cost or task delivery time.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from statistics import mean, stdev


def _mapping(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump()
    raise ValueError("measurement must be a mapping or a supported record")


def sample_stats(values: Iterable[float | None]) -> dict:
    """Use the sample standard deviation; a single observation has no spread estimate."""
    measured = []
    for value in values:
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError("measurements must be finite numbers or null")
        measured.append(value)
    return {
        "n": len(measured),
        "mean": mean(measured) if measured else None,
        "sample_std": stdev(measured) if len(measured) > 1 else None,
    }


def _timestamp(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("span timestamps must have a time zone")
    return value.astimezone(UTC)


def _union_seconds(intervals: Sequence[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    start, end = sorted(intervals)[0]
    total = 0.0
    for next_start, next_end in sorted(intervals)[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += (end - start).total_seconds()
            start, end = next_start, next_end
    return total + (end - start).total_seconds()


def span_totals(events: Iterable) -> dict:
    """Sum recorded spans and expose overlap; these sums are not task wall time."""
    kinds = {name: {"intervals": [], "incomplete_spans": 0, "invalid_spans": 0}
             for name in ("llm", "tool", "approval", "question")}
    seen = set()
    for record in events:
        event = _mapping(record)
        kind = event.get("kind")
        if kind not in kinds:
            continue
        span_id = event.get("span_id")
        if span_id is not None:
            if span_id in seen:
                raise ValueError("duplicate span identity would double-count elapsed time")
            seen.add(span_id)
        group = kinds[kind]
        if event.get("t1") is None:
            group["incomplete_spans"] += 1
            continue
        try:
            start, end = _timestamp(event.get("t0")), _timestamp(event["t1"])
            if end < start:
                raise ValueError("span ends before it starts")
        except (TypeError, ValueError, OverflowError):
            group["invalid_spans"] += 1
            continue
        group["intervals"].append((start, end))

    def summary(group):
        intervals = group["intervals"]
        observed = sum((end - start).total_seconds() for start, end in intervals)
        missing = group["incomplete_spans"] + group["invalid_spans"]
        return {
            "closed_spans": len(intervals),
            "incomplete_spans": group["incomplete_spans"],
            "invalid_spans": group["invalid_spans"],
            "observed_s": observed,
            "total_s": None if missing else observed,
        }

    per_kind = {kind: summary(group) for kind, group in kinds.items()}
    human = {
        key: kinds["approval"][key] + kinds["question"][key]
        for key in ("intervals", "incomplete_spans", "invalid_spans")
    }
    intervals = [interval for group in kinds.values() for interval in group["intervals"]]
    cumulative = sum(group["observed_s"] for group in per_kind.values())
    union = _union_seconds(intervals)
    return {
        "per_kind": per_kind,
        "human_wait": summary(human),
        "observed_cumulative_s": cumulative,
        "closed_span_union_s": union,
        "overlap_s": max(0.0, cumulative - union),
        "is_task_wall_time": False,
        "limits": [
            "Only recorded closed spans contribute to observed durations.",
            "Overlapping spans must not be added to infer task wall time.",
            "Uninstrumented reflection, review, and network retries may be absent.",
        ],
    }


def _identity(record: dict) -> tuple[str, int]:
    fixture_id, repeat = record.get("fixture_id"), record.get("repeat")
    if (not isinstance(fixture_id, str) or not fixture_id
            or type(repeat) is not int or repeat < 0):
        raise ValueError("invalid planned or observed run identity")
    return fixture_id, repeat


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _llm_group(records: list[dict], planned_count: int) -> dict:
    snapshots = [row["metrics"]["llm"] for row in records
                 if isinstance(row.get("metrics"), dict)
                 and isinstance(row["metrics"].get("llm"), dict)]

    def summarize_snapshots(rows):
        summary = {"runs_with_telemetry": len(rows),
                   "missing_run_telemetry": planned_count - len(rows)}
        for key in ("calls_total", "calls_succeeded", "calls_failed", "calls_in_flight",
                    "usage_measured_calls", "usage_missing_calls", "total_tokens_in",
                    "total_tokens_out", "total_cache_hit_tokens"):
            values = [row.get(key) for row in rows]
            summary[key] = sum(values) if len(rows) == planned_count and all(
                _count(value) for value in values
            ) else None
        summary["observed_calls_total"] = sum(row.get("calls_total", 0) for row in rows
                                               if _count(row.get("calls_total")))
        summary["known_tokens_in"] = sum(row.get("known_tokens_in", 0) for row in rows
                                         if _count(row.get("known_tokens_in")))
        summary["known_tokens_out"] = sum(row.get("known_tokens_out", 0) for row in rows
                                          if _count(row.get("known_tokens_out")))
        return summary

    phases = {phase for snapshot in snapshots for phase in snapshot.get("by_phase", {})}
    zero = {key: 0 for key in ("calls_total", "calls_succeeded", "calls_failed", "calls_in_flight",
                               "usage_measured_calls", "usage_missing_calls", "known_tokens_in",
                               "known_tokens_out", "total_tokens_in", "total_tokens_out",
                               "total_cache_hit_tokens")}
    return {
        **summarize_snapshots(snapshots),
        "by_phase": {
            phase: summarize_snapshots([
                snapshot.get("by_phase", {}).get(phase, zero) for snapshot in snapshots
            ]) for phase in sorted(phases)
        },
    }


def _group(planned: list[dict], observed: dict[tuple[str, int], dict]) -> dict:
    records = [observed[_identity(run)] for run in planned if _identity(run) in observed]
    outcomes = [
        int(bool((row := observed.get(_identity(run))) is not None
                 and row.get("status") == "succeeded"
                 and row.get("root_status") == "DONE"
                 and row.get("root_success") is True
                 and row.get("root_machine_accepted") is True))
        for run in planned
    ]
    nodes = [node for row in records for node in row.get("nodes", [])]
    subtasks = [node for node in nodes if node.get("parent_id") is not None]
    with_checks = [node for node in nodes
                   if _count(node.get("acceptance_count")) and node["acceptance_count"] > 0]
    without_checks = sum(type(node.get("acceptance_count")) is int
                         and node["acceptance_count"] == 0 for node in nodes)
    statuses = {}
    for row in records:
        status = str(row.get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    successes = sum(outcomes)
    subtask_done = sum(node.get("status") == "DONE" for node in subtasks)
    verified_subtasks = sum(node.get("status") == "DONE"
                            and node.get("machine_accepted") is True for node in subtasks)

    def token_sum(field):
        values = [row.get(field) for row in records]
        if len(records) != len(planned) or not all(_count(value) for value in values):
            return None
        return sum(values)

    return {
        "planned_runs": len(planned),
        "persisted_runs": len(records),
        "missing_results": len(planned) - len(records),
        "verified_root_successes": successes,
        "root_completion_rate": successes / len(planned) if planned else None,
        "root_outcome_stats": sample_stats(outcomes),
        "statuses": statuses,
        "unverified_runs": sum(row.get("status") == "unverified" for row in records),
        "observed_subtasks": len(subtasks),
        "observed_subtasks_done": subtask_done,
        "observed_subtask_completion_rate": subtask_done / len(subtasks) if subtasks else None,
        "machine_verified_subtasks_done": verified_subtasks,
        "machine_verified_subtask_rate": verified_subtasks / len(subtasks) if subtasks else None,
        "nodes_with_machine_criteria": len(with_checks),
        "nodes_without_machine_criteria": without_checks,
        "nodes_with_unknown_criteria": len(nodes) - len(with_checks) - without_checks,
        "timing": {
            "task_terminal_s": sample_stats(row.get("task_elapsed_s") for row in records),
            "maintenance_s": sample_stats(row.get("maintenance_s") for row in records),
            "runner_elapsed_s": sample_stats(row.get("elapsed_s") for row in records),
            "engine_wall_s": sample_stats(row.get("wall_s") for row in records),
        },
        "usage": {
            "total_tokens_in": token_sum("total_tokens_in"),
            "total_tokens_out": token_sum("total_tokens_out"),
            "usage_missing_calls": token_sum("usage_missing_calls"),
            "legacy_engine_tokens_are_full_cost": False,
        },
        "llm": _llm_group(records, len(planned)),
    }


def summarize(manifest: dict, results: Sequence) -> dict:
    """Summarize every planned attempt, rejecting duplicate or foreign results."""
    planned = manifest.get("runs")
    if not isinstance(planned, list) or not planned:
        raise ValueError("a nonempty manifest run plan is required")
    plans = {}
    for run in planned:
        identity = _identity(run)
        if identity in plans:
            raise ValueError("duplicate planned run")
        plans[identity] = run
    if manifest.get("planned_runs", len(planned)) != len(planned):
        raise ValueError("manifest planned_runs disagrees with its run plan")
    scenarios = {
        item["fixture_id"]: item["scenario"]
        for item in manifest.get("dataset", {}).get("fixtures", [])
    }
    observed = {}
    for value in results:
        row = _mapping(value)
        identity = _identity(row)
        if identity not in plans or identity in observed:
            raise ValueError("duplicate or unplanned evaluation result")
        if manifest.get("eval_id") is not None and row.get("eval_id") != manifest["eval_id"]:
            raise ValueError("result belongs to another evaluation batch")
        fixture_id = identity[0]
        scenario = scenarios.setdefault(fixture_id, row.get("scenario", "unknown"))
        if row.get("scenario", scenario) != scenario:
            raise ValueError("result scenario disagrees with the manifest")
        observed[identity] = row
    by_fixture, by_scenario = {}, {}
    for run in planned:
        fixture_id = run["fixture_id"]
        scenario = scenarios.get(fixture_id, "unknown")
        by_fixture.setdefault(fixture_id, []).append(run)
        by_scenario.setdefault(scenario, []).append(run)
    return {
        "schema_version": 1,
        "status": "partial",
        "batch": _group(planned, observed),
        "by_fixture": {key: _group(rows, observed) for key, rows in by_fixture.items()},
        "by_scenario": {key: _group(rows, observed) for key, rows in by_scenario.items()},
        "limits": [
            "Every manifest-planned attempt remains in the root-goal denominator.",
            "A missing result is not evidence of an attempted run or a measured duration.",
            "Subtask counts describe observed nodes; a crash may hide unpersisted nodes.",
            "Criteria presence is distinct from a recorded machine acceptance verdict.",
            "Task terminal time uses the declared per-run boundary; absent boundaries stay null.",
            "Runner elapsed, post-terminal maintenance and Engine wall time are separate measures.",
            "Complete token cost requires explicit usage for every call, including maintenance.",
        ],
    }
