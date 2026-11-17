"""Offline, evidence-linked failure and paired-memory observations.

This module reads one already validated batch/ablation or instructor session. It neither reruns
acceptance nor labels a trace symptom as the cause of a task failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from moumou.eval.artifacts import atomic_text, redact
from moumou.eval.replay import _from_report as replay_from_report
from moumou.eval.report import build_report

_LIMIT = 32 * 1024**2
_INFRASTRUCTURE = {
    "worker_start_failed", "worker_crash", "invalid_worker_result", "cleanup_failed", "setup_error",
}
_RAW_NAMES = {
    "manifest.json", "ablation.json", "freeze.json", "result.json", "metrics.json",
    "llm-calls.json", "llm-telemetry.json", "memory-snapshot.json", "warmup.json",
    "test.json", "midtest.json", "active.json", "review.json", "intent.json",
    "replay.json", "task_1-result.json", "task_2-result.json",
}


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate analysis evidence field: {key}")
        value[key] = item
    return value


def _invalid(value):
    raise ValueError(f"non-finite analysis evidence value: {value}")


def _loads(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid)


def _owned(path, owner):
    path, owner = Path(path).resolve(), Path(owner).resolve()
    if not path.is_relative_to(owner) or path == owner:
        raise ValueError("analysis evidence escapes its saved directory")
    return path


def _read(path, owner):
    path = _owned(path, owner)
    if path.stat().st_size > _LIMIT:
        raise ValueError("analysis evidence exceeds 32 MiB")
    return _loads(path.read_text(encoding="utf-8-sig"))


def _trace(directory):
    path = _owned(directory / "trace.jsonl", directory)
    result = {"path": str(path), "state": "missing", "events": [], "partial_tail": False}
    if not path.exists():
        return result
    if path.stat().st_size > _LIMIT:
        result["state"] = "too_large"
        return result
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    try:
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = _loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                    result["partial_tail"] = True
                    break
                raise
            if not isinstance(row, dict) or not isinstance(row.get("payload", {}), dict):
                raise ValueError("trace record must have an object payload")
            result["events"].append(row)
    except (ValueError, TypeError):
        result["events"] = []
        result["state"] = "malformed"
    else:
        result["state"] = "partial" if result["partial_tail"] else "available"
    return result


def _inventory(directory):
    workspace = _owned(directory / "workspaces", directory)
    result = {"root": str(workspace), "available": workspace.is_dir(), "files": [],
              "truncated": False, "scope": "current saved files; correctness not re-executed"}
    if not result["available"]:
        return result
    for path in sorted(workspace.rglob("*")):
        if any(part in {".git", "__pycache__"} for part in path.relative_to(workspace).parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(workspace) or not path.is_file():
            continue
        if len(result["files"]) == 100:
            result["truncated"] = True
            break
        size = path.stat().st_size
        result["files"].append({
            "path": str(path), "bytes": size,
            "sha256": (hashlib.sha256(path.read_bytes()).hexdigest()
                       if size <= 4 * 1024**2 else None),
        })
    return result


def _symptoms(trace):
    observations = []
    for event in trace["events"]:
        kind, payload = event.get("kind"), event.get("payload", {})
        category = None
        if kind in {"tool", "llm"} and event.get("status") == "error":
            category = f"{kind}_error"
        elif kind == "approval" and payload.get("decision") == "reject":
            category = "human_rejection"
        if category:
            observations.append({"category": category, "span_id": event.get("span_id"),
                                 "task_id": event.get("task_id"),
                                 "name": redact(str(event.get("name", "")), None)[:160]})
    return observations


def _classification(row):
    error = row.get("error_type")
    if row.get("status") == "succeeded":
        category, basis = "success", "saved machine and behavior verdicts"
    elif error in {"timeout", "hard_timeout"}:
        category, basis = "timeout", "recorded supervision error_type"
    elif error in _INFRASTRUCTURE:
        category, basis = "infrastructure", "recorded harness error_type"
    elif error == "cancelled" or row.get("status") == "cancelled":
        category, basis = "cancelled", "recorded cancellation"
    elif any(isinstance(node.get("error"), str) and node["error"].startswith("budget:")
             for node in row.get("nodes", [])) or (
            isinstance(row.get("error_message"), str)
            and row["error_message"].startswith("budget:")):
        category, basis = "budget", "recorded worker budget error prefix"
    elif error in {"acceptance_failed", "behavior_failed"}:
        category = "acceptance" if error == "acceptance_failed" else "behavior"
        basis = "recorded verdict failure; model/tool causality is not established"
    elif row.get("status") == "unverified":
        category, basis = "unverified", "task completion lacks verified root success"
    else:
        category, basis = "unknown", "saved data does not establish the terminal cause"
    return {"category": category, "confidence": "unknown" if category == "unknown" else "recorded",
            "basis": basis, "error_type": error,
            "message": redact(str(row.get("error_message") or ""), None)[:2000],
            "source": row["evidence_result"]}


def _attempt(row, group):
    directory = Path(row["run_dir"]).resolve()
    trace = _trace(directory)
    checks = [
        {"kind": check.get("kind"), "target": redact(str(check.get("target", "")), None),
         "ok": check.get("ok")}
        for check in row.get("root_acceptance_checks", [])
    ]
    verdicts = [event for event in trace["events"] if event.get("kind") == "acceptance"
                and event.get("name") == "verdict"
                and event.get("task_id") == row.get("root_task_id")]
    saved_trace_verdict = verdicts[-1]["payload"].get("ok") if verdicts else None
    return {
        "group": group, "fixture_id": row["fixture_id"], "repeat": row["repeat"],
        "status": row["status"], "root_success": row.get("root_success"),
        "evidence_result": row["evidence_result"], "classification": _classification(row),
        "checks": checks, "behavior_checks": [
            {"target": redact(str(check.get("target", "")), None), "ok": check.get("ok")}
            for check in row.get("behavior_checks", [])
        ],
        "success_audit": {
            "saved_root_machine_accepted": row.get("root_machine_accepted"),
            "trace_verdict": saved_trace_verdict,
            "trace_verdict_matches_saved": (
                saved_trace_verdict == row.get("root_machine_accepted")
                if type(saved_trace_verdict) is bool else None
            ),
            "acceptance_reexecuted": False,
        },
        "trace": {key: value for key, value in trace.items() if key != "events"},
        "observed_symptoms": _symptoms(trace),
        "memory_score_events": [
            {"span_id": event.get("span_id"), **{
                key: event["payload"].get(key) for key in ("count", "success", "delta")
            }} for event in trace["events"]
            if event.get("kind") == "memory" and event.get("name") == "scored"
        ],
        "artifacts": _inventory(directory),
    }


def _snapshot_digest(records):
    if (not isinstance(records, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in records)
            or len({item["id"] for item in records}) != len(records)):
        raise ValueError("memory evidence must contain unique record identities")
    return hashlib.sha256(json.dumps(
        sorted(records, key=lambda row: row["id"]), sort_keys=True,
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _memory_chain(root, job, row, warmup):
    directory = Path(row["run_dir"])
    records = job["options"]["memory_records"]
    expected = {record["id"]: hashlib.sha256(record["lesson"].encode("utf-8")).hexdigest()
                for record in records}
    chain = {
        "warmup_path": warmup.get("path") if warmup else None,
        "seed_ids": sorted(expected), "injected_seed_ids": [], "foreign_injected_ids": [],
        "warmup_snapshot_matches_seed": None, "telemetry_matches_seed_lessons": None,
        "seed_observed_in_prompt": None, "state": "unverified",
        "attribution": "attempt; not an independent per-agent identity",
    }
    if warmup:
        snapshot = _owned(Path(warmup["path"]).parent / "memory-snapshot.json", root)
        if snapshot.exists():
            chain["warmup_snapshot_matches_seed"] = (
                _snapshot_digest(_read(snapshot, root)) == _snapshot_digest(records)
            )
    telemetry = _owned(directory / "llm-telemetry.json", directory)
    if telemetry.exists():
        from moumou.eval.telemetry import read_telemetry

        if telemetry.stat().st_size > 8 * 1024**2:
            raise ValueError("analysis telemetry exceeds 8 MiB")
        saved = read_telemetry(telemetry)
        observed = [item for call in saved["calls"] for item in call["memory"]["known_memories"]]
        seen = {item["id"] for item in observed}
        chain["injected_seed_ids"] = sorted(seen & expected.keys())
        chain["foreign_injected_ids"] = sorted(seen - expected.keys())
        chain["telemetry_matches_seed_lessons"] = all(
            expected.get(item["id"]) == item["lesson_sha256"] for item in observed
        )
        chain["seed_observed_in_prompt"] = bool(seen & expected.keys())
        enabled = job["memory_enabled"]
        checks = [chain["warmup_snapshot_matches_seed"], chain["telemetry_matches_seed_lessons"],
                  chain["seed_observed_in_prompt"] if enabled else not bool(seen)]
        chain["state"] = "consistent" if all(value is True for value in checks) else (
            "inconsistent" if any(value is False for value in checks) else "unverified"
        )
    return chain


def _memory_comparisons(model, rows):
    if model["kind"] != "ablation":
        return []
    manifest, root = model["manifest"], Path(model["root"])
    fixtures = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
    jobs = {(job["fixture_id"], job["repeat"], job["group"]): job for job in manifest["jobs"]}
    warmups = {(row["fixture_id"], row["repeat"]): row for row in model["warmups"]}
    output = []
    for fixture_id, fixture in fixtures.items():
        if fixture.get("warmup") is None:
            continue
        for repeat in range(manifest["repeats"]):
            for suffix, slots in (("on", 4), ("off", 1)):
                names = [f"memory_{memory}_parallel_{suffix}" for memory in ("on", "off")]
                pair = [rows.get((name, fixture_id, repeat)) for name in names]
                result = {"fixture_id": fixture_id, "repeat": repeat, "max_slots": slots,
                          "memory_on_success": None, "memory_off_success": None,
                          "success_relation": "not_comparable",
                          "elapsed_delta_on_minus_off_s": None,
                          "on_chain": None, "off_chain": None,
                          "conclusion": "paired observation; no causal/statistical gain claim"}
                for name, row, label in zip(names, pair, ("on", "off"), strict=True):
                    if row is not None:
                        result[f"memory_{label}_success"] = (
                            None if row["status"] == "unverified" else row["status"] == "succeeded"
                        )
                        result[f"{label}_chain"] = _memory_chain(
                            root, jobs[(fixture_id, repeat, name)], row,
                            warmups.get((fixture_id, repeat)),
                        )
                if all(row is not None for row in pair):
                    left, right = result["memory_on_success"], result["memory_off_success"]
                    if type(left) is bool and type(right) is bool:
                        result["success_relation"] = (
                            "unchanged_success" if left and right else "unchanged_failure"
                            if not left and not right else "memory_on_only_success" if left
                            else "memory_off_only_success"
                        )
                    durations = [row.get("task_elapsed_s") for row in pair]
                    if all(type(value) in (int, float) and math.isfinite(value)
                           for value in durations):
                        result["elapsed_delta_on_minus_off_s"] = durations[0] - durations[1]
                output.append(result)
    return output


def _session_classification(row, original):
    """Classify saved outcomes without treating a symptom as model causality."""
    source = row["path"]
    status = row["root_status"]
    root = next(node for node in original["nodes"] if node["id"] == row["root_task_id"])
    error = root.get("error")
    if row.get("verified_goal_success") is True:
        category, basis = "reviewed_success", "validated recorded quality acceptance"
        if isinstance(row.get("review"), dict):
            source = row["review"].get("path", source)
    elif row.get("root_machine_accepted") is False:
        category, basis = "acceptance", "saved root machine verdict is false"
    elif isinstance(error, str) and error.startswith("budget:"):
        category, basis = "budget", "saved root budget error prefix"
    elif isinstance(error, str) and error.startswith("crash:"):
        category, basis = "recorded_crash", "saved root crash prefix, origin not inferred"
    elif row.get("verified_goal_success") is False:
        category, basis = "recorded_unsuccessful", "validated saved quality outcome"
    elif status == "DONE":
        category, basis = "unreviewed", "agent completion has no verified quality outcome"
    else:
        category, basis = "unknown", "saved evidence does not establish the terminal cause"
    return {"category": category, "confidence": "unknown" if category == "unknown" else "recorded",
            "basis": basis, "source": source, "saved_root_error": error}


def _saved_capture(row, original, field):
    """Expose an existing capture reference; never manufacture one from live state."""
    evidence = row.get("evidence")
    report_evidence = evidence.get(field) if isinstance(evidence, dict) else None
    value = original.get(field)
    if value is None:
        value = report_evidence.get("values") if isinstance(report_evidence, dict) else None
    if value is not None and not isinstance(value, dict):
        raise ValueError("saved phase capture must be an object")
    return {
        "state": "unknown" if value is None else value.get("status", "unknown"),
        "source": row["path"], "capture": value,
        "report_evidence": report_evidence,
        "basis": "saved task capture only; live workspaces and memory.db are not sampled",
    }


def _memory_events(original, trace, scenario_id, source):
    capture = original.get("memory_snapshot")
    if isinstance(capture, dict) and isinstance(capture.get("observations"), list):
        result = []
        for index, event in enumerate(capture["observations"]):
            if not isinstance(event, dict):
                raise ValueError("saved memory observation must be an original event object")
            result.append({
                "record": event,
                "identity": {"scenario_id": scenario_id, "task_id": event.get("task_id")},
                "source": {**source, "json_pointer": f"/memory_snapshot/observations/{index}"},
            })
        return result, "saved_phase_capture"
    return [item for item in trace if item["record"].get("kind") == "memory"], \
        "saved_trace_without_phase_capture"


def _failure_events(original, trace, scenario_id, source):
    capture = original.get("failure_snapshot")
    if not isinstance(capture, dict) or not isinstance(capture.get("events"), list):
        return trace, "saved_trace_without_phase_capture"
    result = []
    for index, event in enumerate(capture["events"]):
        if not isinstance(event, dict) or not isinstance(event.get("payload", {}), dict):
            raise ValueError("saved failure observation must be an original event object")
        result.append({"record": event,
                       "identity": {"scenario_id": scenario_id, "task_id": event.get("task_id")},
                       "source": {**source, "json_pointer": f"/failure_snapshot/events/{index}"}})
    return result, "saved_phase_capture"


def _session_analysis(model):
    replay = replay_from_report(model)
    by_scenario = {item["scenario_id"]: item for item in replay["scenarios"]}
    attempts = []
    for row in model["records"]:
        scenario = by_scenario[row["scenario_id"]]
        saved = next(item for item in scenario["task_results"]
                     if item["record"]["phase"] == row["phase"])
        original = saved["record"]
        task_ids = {node["id"] for node in original["nodes"]}
        trace = [item for item in scenario["trace"]["events"]
                 if item["record"].get("task_id") in task_ids]
        verdicts = [item for item in trace if item["record"].get("kind") == "acceptance"
                    and item["record"].get("name") == "verdict"
                    and item["record"].get("task_id") == row["root_task_id"]]
        trace_verdict = verdicts[-1]["record"].get("payload", {}).get("ok") if verdicts else None
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        memory_events, memory_basis = _memory_events(
            original, trace, row["scenario_id"], saved["source"],
        )
        failure_events, failure_basis = _failure_events(
            original, trace, row["scenario_id"], saved["source"],
        )
        attempts.append({
            "identity": {"scenario_id": row["scenario_id"], "task_id": row["root_task_id"]},
            "phase": row["phase"], "root_status": row["root_status"],
            "quality_status": row.get("quality_status"),
            "verified_goal_success": row.get("verified_goal_success"),
            "quality_source": row.get("quality_source", "unknown"),
            "raw_quality": row.get("raw_quality"), "review": row.get("review"),
            "evidence_result": row["path"], "result_source": saved["source"],
            "classification": _session_classification(row, original),
            "nodes": original["nodes"],
            "observed_symptoms": _symptoms({
                "events": [item["record"] for item in failure_events],
            }),
            "failure_snapshot": _saved_capture(row, original, "failure_snapshot"),
            "failure_observation_basis": failure_basis,
            "report_failure_evidence": evidence.get("failure"),
            "trace": {"state": scenario["trace"]["state"],
                      "source": scenario["trace"]["source"], "events": trace},
            "success_audit": {
                "saved_root_machine_accepted": original.get("root_machine_accepted"),
                "trace_verdict": trace_verdict,
                "trace_verdict_matches_saved": (
                    trace_verdict == original.get("root_machine_accepted")
                    if type(trace_verdict) is bool else None),
                "acceptance_reexecuted": False,
            },
            "artifact_snapshot": saved["artifact_snapshot"],
            "memory": {
                "snapshot": _saved_capture(row, original, "memory_snapshot"),
                "context_candidates": evidence.get("memory_context_snapshots"),
                "observed_events": memory_events, "observation_basis": memory_basis,
                "observed_event_counts": dict(sorted(Counter(
                    item["record"].get("name", "unknown") for item in memory_events
                ).items())),
                "retrieval_call_count": None, "causal_benefit": "not_established",
                "limits": [
                    "A context archive contains candidates at build time, "
                    "not proof of admission/use.",
                    "No live memory database is treated as an earlier phase snapshot.",
                    "Absent telemetry is unknown, not zero retrieval calls.",
                    "Sequential teacher tasks are not independent on/off experiment pairs.",
                ],
            },
        })
    return {
        "schema_version": 1, "kind": "instructor_test",
        "analysis_kind": "saved_session_observations", "root": model["root"],
        "code": model["manifest"].get("code", {}),
        "model": model["manifest"].get("model", {}),
        "report_builder": model.get("report_builder", {}),
        "analysis_builder": {"module": "moumou.eval.analysis", "source_sha256":
                             hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "summary": model["metrics"], "attempts": attempts,
        "outcome_categories": dict(sorted(Counter(
            row["classification"]["category"] for row in attempts).items())),
        "failure_categories": dict(sorted(Counter(
            row["classification"]["category"] for row in attempts
            if row["root_status"] in {"FAILED", "DEAD"}
            or row["verified_goal_success"] is False
            or row["success_audit"]["saved_root_machine_accepted"] is False
        ).items())),
        "human": [{"scenario_id": item["scenario_id"], "evidence": item["human"]}
                  for item in replay["scenarios"]],
        "scenario_diagnostics": [{"scenario_id": item["scenario_id"],
                                  "evidence": item["state_snapshot"]}
                                 for item in replay["scenarios"]],
        "saved_suite_status": replay["saved_suite_status"],
        "protected_directories": model["protected_directories"],
        "limitations": [
            "Raw statuses and the original complete planned denominator remain unchanged.",
            "Recorded symptoms do not identify model, human or tool causality.",
            "No acceptance, runtime, network call or memory intervention is re-executed.",
            "Missing phase captures and unknown quality stay unknown; versions are not pooled.",
            "Delivery-duration variation is not independent-repeat or causal memory evidence.",
            "An offline view is not evidence of P2 consumer or teammate sign-off.",
        ],
    }


def build_analysis(root: Path | str) -> dict:
    """Analyze one validated saved experiment without executing tasks or clients."""
    model = build_report(root)
    if model["kind"] == "instructor_test":
        return _session_analysis(model)
    attempts, missing, rows = [], [], {}
    deferred = {
        (item["group"], item["fixture_id"], item["repeat"]): item.get("reason")
        for item in model["manifest"].get("deferred_jobs", [])
    }
    for group in model["groups"]:
        for row in group["records"]:
            rows[(group["name"], row["fixture_id"], row["repeat"])] = row
            attempts.append(_attempt(row, group["name"]))
        for row in group["missing"]:
            key = group["name"], row["fixture_id"], row["repeat"]
            missing.append({**row, "group": group["name"],
                            "state": "deferred" if key in deferred else "not_persisted",
                            "deferred_reason": deferred.get(key)})
    counts = Counter(row["status"] for row in attempts)
    planned = sum(group["metrics"]["batch"]["planned_runs"] for group in model["groups"])
    deferred_count = sum(row["state"] == "deferred" for row in missing)
    selected = model["manifest"].get("selected_runs", planned)
    return {
        "schema_version": 1, "kind": model["kind"], "root": model["root"],
        "purpose": model["purpose"], "model": model["model"],
        "code": model["manifest"].get("code", {}),
        "summary": {"planned": planned, "selected": selected, "persisted": len(attempts),
                    "succeeded": counts["succeeded"],
                    "failed": sum(counts[key] for key in (
                        "failed", "error", "incomplete", "cancelled")),
                    "unverified": counts["unverified"], "missing": len(missing),
                    "deferred": deferred_count,
                    "unexplained_missing": len(missing) - deferred_count,
                    "planned_goal_coverage": counts["succeeded"] / planned if planned else None,
                    "selected_success_rate": counts["succeeded"] / selected if selected else None,
                    "observed_success_rate": (
                        counts["succeeded"] / len(attempts) if attempts else None),
                    "statuses": dict(sorted(counts.items())),
                    "failure_categories": dict(sorted(Counter(
                        row["classification"]["category"] for row in attempts
                        if row["status"] != "succeeded"
                    ).items()))},
        "group_metrics": {group["name"]: group["metrics"] for group in model["groups"]},
        "attempts": attempts, "not_persisted": missing,
        "memory_comparisons": _memory_comparisons(model, rows),
        "protected_directories": model["protected_directories"],
        "limitations": [
            "Only this saved experiment is analyzed; versions/models/purposes are not pooled.",
            "Recorded failure categories describe the terminal evidence, not model or human blame.",
            "Tool/LLM errors and rejected approvals are observations, not proven failure causes.",
            "Saved checks and current artifacts are not independent acceptance re-execution.",
            "Memory injection establishes prompt exposure, not lesson use or causal gain.",
            "Memory scoring spans report counts/deltas; individual updated IDs are not identified.",
            "Single paired differences do not establish significance; missing pairs stay unknown.",
            "Deferred/missing attempts stay in the planned denominator and are not model failures.",
        ],
    }


def _cell(value):
    return redact(str(value), None).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _link(path, directory, label):
    return f"[{label}]({quote(os.path.relpath(path, directory).replace(os.sep, '/'), safe='/:')})"


def render_analysis(model: dict, directory: Path | str | None = None) -> str:
    """Render observed failures, audits and paired memory evidence as Markdown."""
    directory = Path(directory or model["root"])
    if model["kind"] == "instructor_test":
        return _render_session_analysis(model, directory)
    summary = model["summary"]
    lines = ["# Evaluation failure and memory analysis", "",
             f"Purpose: {_cell(model['purpose'])}; "
             f"model: {_cell(model['model'].get('model', 'unknown'))}.",
             f"Planned {summary['planned']}; selected {summary['selected']}; "
             f"persisted {summary['persisted']}; "
             f"verified successes {summary['succeeded']}; failures {summary['failed']}; "
             f"unverified {summary['unverified']}; missing {summary['missing']} "
             f"(deferred {summary['deferred']}).", "",
             "## Saved outcomes and evidence", "",
             "Saved checks/artifacts are inspected; acceptance is not re-executed.", "",
             "| Group | Fixture | Repeat | Status | Category | Checks | Files | Evidence |",
             "| --- | --- | ---: | --- | --- | ---: | ---: | --- |"]
    for row in model["attempts"]:
        checks = row["checks"]
        lines.append(f"| {_cell(row['group'])} | {_cell(row['fixture_id'])} | {row['repeat']} | "
                     f"{_cell(row['status'])} | {_cell(row['classification']['category'])} | "
                     f"{sum(check['ok'] is True for check in checks)}/{len(checks)} | "
                     f"{len(row['artifacts']['files'])} | "
                     f"{_link(row['evidence_result'], directory, 'result')} |")
    lines += ["", "## Failure observations", ""]
    for row in model["attempts"]:
        if row["status"] == "succeeded":
            continue
        cause = row["classification"]
        lines.append(f"- {_cell(row['fixture_id'])} / {_cell(row['group'])} / {row['repeat']}: "
                     f"{_cell(cause['category'])} ({_cell(cause['confidence'])}); "
                     f"{_cell(cause['basis'])}. {_cell(cause['message'])} "
                     f"{_link(row['evidence_result'], directory, 'result')}; "
                     f"trace state={_cell(row['trace']['state'])}; "
                     f"symptoms={_cell([item['category'] for item in row['observed_symptoms']])}.")
    lines += ["", "## Paired memory observations", "",
              "Compare the same fixture/repeat and parallel setting. Deltas are on minus off; "
              "they do not imply statistical significance or causal improvement.", "",
              "| Fixture | Repeat | Slots | On | Off | Relation | Delta (s) | On/off chain |",
              "| --- | ---: | ---: | --- | --- | --- | ---: | --- |"]
    for row in model["memory_comparisons"]:
        delta = row["elapsed_delta_on_minus_off_s"]
        chains = "/".join(row[key]["state"] if row[key] else "missing"
                          for key in ("on_chain", "off_chain"))
        lines.append(f"| {_cell(row['fixture_id'])} | {row['repeat']} | {row['max_slots']} | "
                     f"{row['memory_on_success']} | {row['memory_off_success']} | "
                     f"{row['success_relation']} | "
                     f"{f'{delta:.3f}' if delta is not None else 'N/A'} | "
                     f"{chains} |")
    lines += ["", "## Not persisted", ""]
    for row in model["not_persisted"]:
        lines.append(f"- {_cell(row['fixture_id'])} / {_cell(row['group'])} / {row['repeat']}: "
                     f"{row['state']}; {_cell(row.get('deferred_reason') or 'no saved result')}.")
    lines += ["", "## Limits", "", *[f"- {item}" for item in model["limitations"]], ""]
    return "\n".join(lines)


def _render_session_analysis(model, directory):
    metrics = model["summary"]
    lines = [
        "# Saved instructor failure and memory observations", "",
        f"Planned roots: {metrics['planned_root_goals']}; "
        f"saved deliveries: {metrics['persisted_task_deliveries']}; "
        f"verified successes: {metrics['verified_successes']}; "
        f"not delivered: {metrics['not_delivered']}.",
        f"Raw suite status: {_cell(model['saved_suite_status'])}. "
        f"Saved suite wall time: {_cell(metrics.get('suite_wall_time_s'))} seconds.",
        "", "No acceptance is re-executed and no current workspace or memory database "
        "is sampled as a historical snapshot.", "",
        "| Scenario | Phase | Root | Saved status | Quality | Observation | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in model["attempts"]:
        lines.append(
            f"| {_cell(row['identity']['scenario_id'])} | {_cell(row['phase'])} | "
            f"{_cell(row['identity']['task_id'])} | {_cell(row['root_status'])} | "
            f"{_cell(row['quality_status'])} | {_cell(row['classification']['category'])} | "
            f"{_link(row['evidence_result'], directory, 'saved task')} |"
        )
    lines += ["", "## Memory and immutable artifact captures", ""]
    for row in model["attempts"]:
        memory = row["memory"]
        lines.append(
            f"- {_cell(row['identity']['scenario_id'])}/{_cell(row['phase'])}: "
            f"artifact capture={_cell(row['artifact_snapshot']['state'])}; "
            f"memory capture={_cell(memory['snapshot']['state'])}; "
            f"observed memory events={_cell(memory['observed_event_counts'])}; "
            "retrieval call coverage unknown; causal benefit not established."
        )
    lines += ["", "## Human evidence", ""]
    for item in model["human"]:
        human = item["evidence"]
        lines.append(
            f"- {_cell(item['scenario_id'])}: {len(human['answered'])} recorded response pairs; "
            f"{len(human['pending_files'])} pending files; "
            f"{len(human['staged_response_files'])} staged response files. "
            "A staged file does not prove consumption or manual authorship."
        )
    lines += ["", "## Limits", "", *[f"- {item}" for item in model["limitations"]], ""]
    return "\n".join(lines)


def generate_analysis(root: Path | str, out: Path | str | None = None) -> Path:
    """Write a derived .md/.txt or machine-readable .json, preserving raw inputs."""
    model = build_analysis(root)
    target = Path(out) if out is not None else Path(model["root"]) / "analysis.md"
    if target.is_symlink():
        raise ValueError("analysis output cannot replace a symlink")
    target = target.resolve()
    if target.suffix.lower() not in {".md", ".txt", ".json"}:
        raise ValueError("analysis output must use .md, .txt or .json")
    if target.name in _RAW_NAMES or any(
            target.is_relative_to(Path(path)) for path in model["protected_directories"]):
        raise ValueError("analysis output would overwrite or enter original evidence")
    if target.is_relative_to(Path(model["root"]) / "reviews"):
        raise ValueError("analysis output would enter original review evidence")
    text = json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False) + "\n" \
        if target.suffix.lower() == ".json" else render_analysis(model, target.parent)
    atomic_text(target, text)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze saved evaluation evidence offline")
    parser.add_argument("directory", nargs="?", help="saved batch or ablation directory")
    parser.add_argument("--run", dest="run_directory", help="saved batch or ablation directory")
    parser.add_argument("--out", default=None,
                        help="derived .md/.txt/.json; default RUN/analysis.md")
    args = parser.parse_args(argv)
    if bool(args.directory) == bool(args.run_directory):
        parser.error("supply one saved directory, positionally or with --run")
    try:
        path = generate_analysis(args.directory or args.run_directory, args.out)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"cannot analyze saved run: {redact(str(exc), None)}", file=sys.stderr)
        return 2
    print(f"analysis rebuilt: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
