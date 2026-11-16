"""Rebuild evaluation reports offline from authoritative saved attempt files.

No model client, credentials, event loop or JSONL index is required. Ablation
reports use the outer schedule for denominators and nested per-run files for
observations; embedded ``jobs[*].results`` are only historical conveniences.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

from moumou.eval.artifacts import atomic_text, load_results, redact
from moumou.eval.metrics import summarize

_GROUPS = {
    "memory_on_parallel_on": (True, 4),
    "memory_on_parallel_off": (True, 1),
    "memory_off_parallel_on": (False, 4),
    "memory_off_parallel_off": (False, 1),
}
_STATUSES = {"succeeded", "unverified", "failed", "incomplete", "error", "cancelled"}
_NODE_STATUSES = {
    "PENDING", "READY", "RUNNING", "WAITING_HUMAN", "DONE", "FAILED", "DEAD", "CANCELLED",
}
_MODEL_FIELDS = ("provider", "model", "client", "endpoint_host", "timeout_s", "max_retries",
                 "client_factory")
_LIMIT = 32 * 1024 * 1024


def _canonical(value, *, compact=False):
    options = {"separators": (",", ":")} if compact else {}
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, **options)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _read(path):
    path = Path(path)
    if path.stat().st_size > _LIMIT:
        raise ValueError("saved report input exceeds 32 MiB")

    def invalid(value):
        raise ValueError(f"non-finite JSON value: {value}")

    value = json.loads(path.read_text(encoding="utf-8-sig"),
                       object_pairs_hook=_unique_object, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("a saved report input must be a JSON object")
    return value


def _inside(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError("a planned path must be nonempty text")
    windows = PureWindowsPath(relative)
    if (windows.drive or windows.root or ".." in windows.parts or ":" in relative
            or any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
                   for part in windows.parts)):
        raise ValueError("a planned path escapes or aliases its report root")
    path = (root / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("a planned path escapes its report root")
    return path


def _count(value, *, nullable=True):
    return (value is None and nullable) or (type(value) is int and value >= 0)


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _validate_result(row):
    if not isinstance(row.get("status"), str) or row["status"] not in _STATUSES:
        raise ValueError("unknown saved attempt status")
    for key in ("root_success", "root_machine_accepted"):
        if row.get(key) is not None and type(row[key]) is not bool:
            raise ValueError("saved verdict must be a boolean or null")
    for key in ("done", "dead", "total", "unfinished", "total_tokens_in", "total_tokens_out",
                "usage_missing_calls", "tokens_in", "tokens_out"):
        if not _count(row.get(key)):
            raise ValueError("saved attempt counts must be nonnegative integers or null")
    for key in ("elapsed_s", "task_elapsed_s", "maintenance_s", "wall_s", "completion_rate"):
        value = row.get(key)
        if value is not None and (type(value) not in (int, float)
                                  or not math.isfinite(value) or value < 0):
            raise ValueError("saved measurements must be finite nonnegative numbers or null")
    if row.get("completion_rate") is not None and row["completion_rate"] > 1:
        raise ValueError("saved completion rate exceeds one")
    nodes = row.get("nodes", [])
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ValueError("saved nodes must be objects")
    identities = [node.get("id") for node in nodes]
    if any(not isinstance(identity, str) or not identity for identity in identities) \
            or len(set(identities)) != len(identities):
        raise ValueError("saved node identities are invalid or duplicated")
    for node in nodes:
        if not isinstance(node.get("status"), str) or node["status"] not in _NODE_STATUSES:
            raise ValueError("unknown saved node status")
        if not _count(node.get("acceptance_count")):
            raise ValueError("invalid saved node acceptance count")
        if node.get("machine_accepted") is not None and type(node["machine_accepted"]) is not bool:
            raise ValueError("invalid saved node machine verdict")
        if node.get("parent_id") is not None and node["parent_id"] not in identities:
            raise ValueError("saved node parent is missing")
    node_map = {node["id"]: node for node in nodes}
    for node in nodes:
        seen = set()
        while node.get("parent_id") is not None:
            if node["id"] in seen:
                raise ValueError("saved node parent relationships contain a cycle")
            seen.add(node["id"])
            node = node_map[node["parent_id"]]
    for key, observed in (("total", len(nodes)),
                          ("done", sum(node["status"] == "DONE" for node in nodes)),
                          ("dead", sum(node["status"] == "DEAD" for node in nodes))):
        if row.get(key) is not None and row[key] != observed:
            raise ValueError("saved counts disagree with the observed nodes")
    if row.get("status") == "succeeded":
        roots = [node for node in nodes if node.get("parent_id") is None]
        checks = row.get("root_acceptance_checks", [])
        behavior = row.get("behavior_checks", [])
        if (row.get("root_status") != "DONE" or row.get("root_success") is not True
                or row.get("root_machine_accepted") is not True or len(roots) != 1
                or roots[0]["id"] != row.get("root_task_id") or roots[0]["status"] != "DONE"
                or not isinstance(checks, list) or not checks
                or any(not isinstance(check, dict) or not isinstance(check.get("kind"), str)
                       or check.get("ok") is not True
                       or check.get("kind") == "self_report" for check in checks)
                or not isinstance(behavior, list)
                or any(not isinstance(check, dict) or check.get("ok") is not True
                       for check in behavior)):
            raise ValueError("saved success lacks consistent machine and behavior evidence")


def _public_model(value):
    if not isinstance(value, dict):
        raise ValueError("model metadata must be an object")
    return {key: value.get(key) for key in _MODEL_FIELDS if key in value}


def _code_identity(value):
    if not isinstance(value, dict) or not isinstance(value.get("source_sha256"), str):
        raise ValueError("ablation input is missing its source version")
    return value.get("git_commit"), value["source_sha256"]


def _purpose(value):
    if not isinstance(value, str) or not value:
        raise ValueError("saved purpose must be nonempty text")
    return value


def _batch(root):
    if not (root / "manifest.json").resolve().is_relative_to(root):
        raise ValueError("batch manifest escapes its report root")
    manifest = _read(root / "manifest.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported evaluation manifest version")
    if not isinstance(manifest.get("runs"), list) or not manifest["runs"]:
        raise ValueError("a batch needs a nonempty saved run plan")
    dataset = _object(manifest.get("dataset", {}), "batch dataset")
    fixture_rows = dataset.get("fixtures", [])
    if not isinstance(fixture_rows, list) or any(not isinstance(row, dict) for row in fixture_rows):
        raise ValueError("batch dataset fixtures must be objects")
    records = load_results(root)
    directories = []
    for planned in manifest["runs"]:
        if not isinstance(planned, dict):
            raise ValueError("invalid saved run plan")
        directories.append(_inside(root, planned.get("run_dir")))
    for row in records:
        # Re-read strictly: the common reader verifies identity/path ownership,
        # while this pass also rejects duplicate keys and non-finite JSON values.
        strict = _read(Path(row["run_dir"]) / "result.json")
        if _canonical(strict) != _canonical(row):
            raise ValueError("saved result changed while rebuilding its report")
        _validate_result(row)
        row["evidence_result"] = str(Path(row["run_dir"]) / "result.json")
    metrics = summarize(manifest, records)
    observed = {(row["fixture_id"], row["repeat"]) for row in records}
    missing = [{"fixture_id": plan["fixture_id"], "repeat": plan["repeat"],
                "result": str(directory / "result.json")}
               for plan, directory in zip(manifest["runs"], directories, strict=True)
               if (plan["fixture_id"], plan["repeat"]) not in observed]
    return {"root": str(root), "manifest": manifest, "metrics": metrics,
            "records": records, "missing": missing,
            "protected_directories": [str(path) for path in directories]}


def _schedule_key(item):
    if (not isinstance(item, dict) or not isinstance(item.get("group"), str)
            or item["group"] not in _GROUPS
            or not isinstance(item.get("fixture_id"), str)
            or not _count(item.get("repeat"), nullable=False)):
        raise ValueError("invalid ablation schedule identity")
    return item["group"], item["fixture_id"], item["repeat"]


def _controls(item, group):
    memory, slots = _GROUPS[group]
    if (type(item.get("memory_enabled")) is not bool or item["memory_enabled"] is not memory
            or type(item.get("max_slots")) is not int or item["max_slots"] != slots
            or item.get("role_review_enabled") is not False):
        raise ValueError("ablation controls disagree with the declared group")


def _seed_hash(records):
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("invalid ablation seed records")
    ids = [record.get("id") for record in records]
    if any(not isinstance(identity, str) or not identity for identity in ids) \
            or len(set(ids)) != len(ids):
        raise ValueError("invalid ablation seed identities")
    return hashlib.sha256(_canonical(sorted(records, key=lambda row: row["id"]),
                                     compact=True).encode("utf-8")).hexdigest()


def _warmups(root, manifest, by_id, seeds):
    entries = manifest.get("warmups", [])
    if not isinstance(entries, list):
        raise ValueError("ablation warmups must be a list")
    output, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid warmup entry")
        key = entry.get("fixture_id"), entry.get("repeat")
        if (not isinstance(key[0], str) or key[0] not in by_id
                or not _count(key[1], nullable=False) or key in seen):
            raise ValueError("invalid or duplicate warmup identity")
        seen.add(key)
        directory = _inside(root, entry.get("relative_dir"))
        path = directory / "warmup.json"
        if not path.resolve().is_relative_to(directory):
            raise ValueError("warmup record escapes its saved directory")
        record = {"fixture_id": key[0], "repeat": key[1], "status": "not persisted",
                  "path": str(path), "elapsed_s": None, "memory_records": None,
                  "tokens_in": None, "tokens_out": None, "calls": None}
        if path.exists():
            saved = _read(path)
            if (saved.get("fixture_id") != key[0] or saved.get("repeat") != key[1]
                    or saved.get("source") != by_id[key[0]].get("warmup")):
                raise ValueError("warmup identity or source differs from the frozen fixture")
            if saved.get("status") == "completed" and key in seeds \
                    and saved.get("memory_sha256") != seeds[key]:
                raise ValueError("warmup and held-out seed snapshots disagree")
            usage = saved.get("llm_usage", {})
            _object(usage, "warmup usage")
            telemetry_path = directory / "llm-calls.json"
            if telemetry_path.exists():
                from moumou.eval.telemetry import read_telemetry

                observed_usage = read_telemetry(telemetry_path)["summary"]
                if usage and _canonical(usage) != _canonical(observed_usage):
                    raise ValueError("warmup usage disagrees with its recorded calls")
                usage = observed_usage
            for name in ("calls_total", "total_tokens_in", "total_tokens_out"):
                if not _count(usage.get(name)):
                    raise ValueError("invalid warmup usage count")
            elapsed = saved.get("elapsed_s")
            if elapsed is not None and (type(elapsed) not in (int, float)
                                        or not math.isfinite(elapsed) or elapsed < 0):
                raise ValueError("invalid warmup duration")
            if not _count(saved.get("memory_record_count")):
                raise ValueError("invalid warmup memory-record count")
            record.update({"status": saved.get("status", "unknown"), "elapsed_s": elapsed,
                           "memory_records": saved.get("memory_record_count"),
                           "tokens_in": usage.get("total_tokens_in"),
                           "tokens_out": usage.get("total_tokens_out"),
                           "calls": usage.get("calls_total")})
        output.append(record)
    return output


def _ablation(root):
    if not (root / "ablation.json").resolve().is_relative_to(root):
        raise ValueError("ablation manifest escapes its report root")
    manifest = _read(root / "ablation.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported ablation manifest version")
    source = _code_identity(manifest.get("code"))
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("ablation needs its saved fixture snapshot")
    by_id = {}
    for fixture in fixtures:
        if (not isinstance(fixture, dict) or not isinstance(fixture.get("id"), str)
                or not isinstance(fixture.get("scenario"), str) or fixture["id"] in by_id):
            raise ValueError("invalid or duplicate ablation fixture")
        by_id[fixture["id"]] = fixture
    repeats = manifest.get("repeats")
    if not _count(repeats, nullable=False) or repeats == 0:
        raise ValueError("ablation repeats must be positive")
    groups = manifest.get("groups")
    if (not isinstance(groups, list) or len(groups) != 4
            or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                   for row in groups)
            or {row["name"] for row in groups} != set(_GROUPS)):
        raise ValueError("ablation must declare exactly the four supported groups")
    for group in groups:
        _controls(group, group["name"])
    schedule, plans, paths = manifest.get("schedule"), {}, set()
    if not isinstance(schedule, list):
        raise ValueError("ablation schedule must be a list")
    for item in schedule:
        key = _schedule_key(item)
        _controls(item, key[0])
        directory = _inside(root, item.get("relative_dir"))
        if key in plans or str(directory).casefold() in paths:
            raise ValueError("duplicate or aliased ablation attempt")
        paths.add(str(directory).casefold())
        plans[key] = {**item, "directory": directory}
    expected = {(group, fixture, repeat) for group in _GROUPS for fixture in by_id
                for repeat in range(repeats)}
    if set(plans) != expected or manifest.get("planned_runs") != len(plans):
        raise ValueError("ablation schedule does not match its full declared denominator")
    jobs = manifest.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("ablation jobs must be a list")
    observed_jobs, seed_hashes, conversations = {}, {}, set()
    for job in jobs:
        key = _schedule_key(job)
        if key not in plans or key in observed_jobs:
            raise ValueError("duplicate or unplanned ablation job")
        if job.get("relative_dir") != plans[key]["relative_dir"]:
            raise ValueError("ablation job path disagrees with its plan")
        _controls(job, key[0])
        options = job.get("options")
        if not isinstance(options, dict):
            raise ValueError("ablation job has no settings snapshot")
        _controls(options, key[0])
        if options.get("project_id") != key[1]:
            raise ValueError("ablation job project differs from its fixture")
        conversation = options.get("conversation_id")
        if not isinstance(conversation, str) or not conversation \
                or (key[1], conversation) in conversations:
            raise ValueError("ablation conversations must be distinct per fixture attempt")
        conversations.add((key[1], conversation))
        seed = _seed_hash(options.get("memory_records"))
        if seed != job.get("memory_sha256"):
            raise ValueError("ablation seed snapshot hash disagrees with its records")
        seed_key = key[1:]
        if seed_key in seed_hashes and seed_hashes[seed_key] != seed:
            raise ValueError("ablation groups did not receive the same memory starting state")
        seed_hashes[seed_key] = seed
        observed_jobs[key] = job
    deferred = {}
    if "selection" in manifest or "deferred_jobs" in manifest:
        selection = _object(manifest.get("selection"), "ablation selection")
        selected_ids = selection.get("selected_fixture_ids")
        deferred_ids = selection.get("deferred_fixture_ids")
        if (not isinstance(selected_ids, list) or not isinstance(deferred_ids, list)
                or any(not isinstance(value, str) for value in selected_ids + deferred_ids)
                or len(selected_ids + deferred_ids) != len(by_id)
                or len(set(selected_ids + deferred_ids)) != len(by_id)
                or set(selected_ids + deferred_ids) != set(by_id)
                or selection.get("mode") not in {"all", "fixture_ids"}
                or (selection["mode"] == "all" and deferred_ids)):
            raise ValueError("ablation selection must partition its fixture identities")
        expected_deferred = {key for key in plans if key[1] in deferred_ids}
        entries = manifest.get("deferred_jobs")
        if not isinstance(entries, list):
            raise ValueError("ablation deferred jobs must be a list")
        for item in entries:
            key = _schedule_key(item)
            if key not in expected_deferred or key in deferred or key in observed_jobs:
                raise ValueError("duplicate, started or unplanned deferred ablation attempt")
            planned = {field: value for field, value in plans[key].items()
                       if field != "directory"}
            if (set(item) != set(planned) | {"status", "reason"}
                    or any(type(item.get(field)) is not type(value) or item[field] != value
                           for field, value in planned.items())
                    or item.get("status") != "deferred"
                    or not isinstance(item.get("reason"), str) or not item["reason"].strip()
                    or item["reason"] != selection.get("deferred_reason")):
                raise ValueError("deferred ablation attempt disagrees with its saved selection")
            if plans[key]["directory"].exists():
                raise ValueError("deferred ablation attempt contains execution evidence")
            deferred[key] = item
        if (set(deferred) != expected_deferred
                or type(manifest.get("deferred_runs")) is not int
                or manifest["deferred_runs"] != len(deferred)
                or type(manifest.get("selected_runs")) is not int
                or manifest["selected_runs"] != len(plans) - len(deferred)):
            raise ValueError("ablation selection counts disagree with its original full plan")
    rows_by_group = {name: [] for name in _GROUPS}
    sources, missing_by_group = [], {name: [] for name in _GROUPS}
    common_model = common_purpose = common_configuration = common_environment = None
    for key, planned in plans.items():
        group, fixture_id, repeat = key
        directory = planned["directory"]
        if not (directory / "manifest.json").exists():
            missing = {"fixture_id": fixture_id, "repeat": repeat,
                       "result": str(directory / "manifest.json")}
            if key in deferred:
                missing.update(status="deferred", deferred_reason=deferred[key]["reason"])
            missing_by_group[group].append(missing)
            continue
        job = observed_jobs.get(key)
        if job is None:
            raise ValueError("nested batch exists without its declared ablation job")
        batch = _batch(directory)
        nested = batch["manifest"]
        if _code_identity(nested.get("code")) != source:
            raise ValueError("cannot combine mixed source versions in one ablation report")
        model, purpose = _public_model(nested.get("model")), _purpose(nested.get("purpose"))
        if model != _public_model(job.get("model")) or purpose != job.get("purpose"):
            raise ValueError("nested model or purpose disagrees with its ablation job")
        if common_model is not None and (model != common_model or purpose != common_purpose):
            raise ValueError("cannot combine mixed models or offline/real purposes")
        common_model, common_purpose = model, purpose
        dataset = _object(nested.get("dataset"), "nested dataset").get("fixtures")
        expected_hash = hashlib.sha256(_canonical(by_id[fixture_id]).encode("utf-8")).hexdigest()
        if (not isinstance(dataset, list) or len(dataset) != 1 or not isinstance(dataset[0], dict)
                or dataset[0].get("fixture_id") != fixture_id
                or dataset[0].get("scenario") != by_id[fixture_id]["scenario"]
                or dataset[0].get("sha256") != expected_hash
                or len(nested["runs"]) != 1 or nested["runs"][0]["fixture_id"] != fixture_id
                or nested["runs"][0]["repeat"] != 0):
            raise ValueError("nested batch does not match the frozen fixture and one-attempt plan")
        settings = _object(nested.get("settings"), "nested settings")
        engine = _object(settings.get("engine"), "nested engine settings")
        _controls(engine, group)
        if (settings.get("initial_memory_sha256") != job["memory_sha256"]
                or settings.get("conversation_id") != job["options"]["conversation_id"]
                or settings.get("project_id") != fixture_id
                or settings.get("fresh_state_per_run") is not True
                or settings.get("harness_concurrency") != 1
                or settings.get("workspace_backend") != manifest.get("workspace_backend")):
            raise ValueError("nested state/settings disagree with the ablation plan")
        fixed_engine = {k: v for k, v in engine.items()
                        if k not in {"memory_enabled", "max_slots", "role_review_enabled"}}
        configuration = {"engine": fixed_engine, "worker": settings.get("worker"),
                         "workspace_backend": settings.get("workspace_backend")}
        environment = nested.get("environment")
        if common_configuration is not None and (configuration != common_configuration
                                                 or environment != common_environment):
            raise ValueError("cannot combine differing fixed settings or runtime environments")
        common_configuration, common_environment = configuration, environment
        for row in batch["records"]:
            rows_by_group[group].append({**row, "repeat": repeat, "source_repeat": row["repeat"]})
        for row in batch["missing"]:
            missing_by_group[group].append({**row, "repeat": repeat})
        sources.append({"path": str(directory / "manifest.json"),
                        "eval_id": nested["eval_id"], "group": group,
                        "fixture_id": fixture_id, "repeat": repeat})
    output_groups = []
    for group in _GROUPS:
        group_plan = {"runs": [{"fixture_id": key[1], "repeat": key[2]}
                               for key in plans if key[0] == group],
                      "dataset": {"fixtures": [{"fixture_id": key, "scenario": value["scenario"]}
                                                for key, value in by_id.items()]}}
        output_groups.append({"name": group, "metrics": summarize(group_plan, rows_by_group[group]),
                              "records": rows_by_group[group], "missing": missing_by_group[group]})
    return {"schema_version": 1, "kind": "ablation", "root": str(root), "manifest": manifest,
            "purpose": common_purpose or "not_started", "model": common_model or {},
            "groups": output_groups, "sources": sources,
            "warmups": _warmups(root, manifest, by_id, seed_hashes),
            "protected_directories": [str(item["directory"]) for item in plans.values()]
            + [str(root / "warmups")]}


def _session_wall_time(manifest: dict) -> dict:
    """Use saved aware timestamps only; an unfinished span has no total yet."""
    times, missing = {}, []
    for name in ("started_at", "finished_at"):
        raw = manifest.get(name)
        if raw is None:
            missing.append(name)
            continue
        if not isinstance(raw, str):
            raise ValueError(f"saved session {name} must be an ISO timestamp with timezone")
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timezone is missing")
            times[name] = value.astimezone(UTC)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"invalid saved session {name}: timezone-aware timestamp required") \
                from exc
    if missing:
        return {"seconds": None, "status": "unknown", "missing_timestamps": missing,
                "basis": "saved started_at and finished_at only"}
    seconds = (times["finished_at"] - times["started_at"]).total_seconds()
    if seconds < 0:
        raise ValueError("saved session finished_at precedes started_at")
    return {"seconds": seconds, "status": "known", "missing_timestamps": [],
            "basis": "saved started_at and finished_at only"}


def _saved_observation(task, field, counts, durations, source, *, scope="saved task phase"):
    values = task.get(field)
    if values is None or values == {}:
        return None
    if not isinstance(values, dict):
        raise ValueError(f"saved {field} observation must be an object")
    selected = {}
    for name in (*counts, *durations):
        value = values.get(name)
        if value is not None:
            valid = (_count(value, nullable=False) if name in counts else
                     type(value) in {int, float} and math.isfinite(value) and value >= 0)
            if not valid:
                raise ValueError(f"invalid saved {field}.{name} observation")
        selected[name] = value
    return {"values": selected, "source": str(source), "json_field": field, "scope": scope}


def _memory_context_snapshots(directory, nodes):
    """Read only immutable context inputs; never sample the live memory database."""
    owners = {node["id"]: node.get("role") for node in nodes}
    snapshots = []
    for path in sorted((directory / "workspaces").rglob(".evaluation/context/*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("memory context snapshot escapes the saved scenario")
        if path.stat().st_size > _LIMIT:
            raise ValueError("memory context snapshot exceeds its size bound")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if path.name not in {digest + ".json", digest[:32] + ".json"}:
            raise ValueError("memory context snapshot content hash is invalid")
        data = _read(path)
        node_id = data.get("task_id")
        if node_id not in owners:
            continue
        if data.get("schema_version") != 1:
            raise ValueError("unsupported memory context snapshot schema")
        hits = data.get("memory_hits")
        if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
            raise ValueError("saved context memory candidates must be a list of objects")
        ids = [hit.get("id") for hit in hits]
        if any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("saved context memory candidate identity is invalid")
        snapshots.append({
            "path": str(path), "sha256": digest, "node_id": node_id,
            "role": data.get("role"), "recorded_node_role": owners[node_id],
            "candidate_count": len(hits), "candidate_ids": ids,
            "scope": "retrieval candidates at saved context build; admission/use unverified",
        })
    return snapshots


def _session_evidence(task, path, directory, nodes):
    from moumou.contracts import TraceEvent
    from moumou.eval.session_evidence import validate_artifact_snapshot

    node_ids = {node["id"] for node in nodes}
    snapshots = _memory_context_snapshots(directory, nodes)
    artifacts = validate_artifact_snapshot(directory, task.get("artifact_snapshot"), task=task)
    memory = task.get("memory_snapshot")
    if memory is not None:
        if (not isinstance(memory, dict) or memory.get("schema_version") != 1
                or memory.get("stage") != "post_engine_run"
                or memory.get("status") not in {"recorded", "partial", "disabled", "unavailable"}):
            raise ValueError("invalid saved memory snapshot")
        scopes, observations = memory.get("scopes"), memory.get("observations")
        if ((scopes is not None and (not isinstance(scopes, list)
                or any(not isinstance(scope, dict) or (scope.get("records") is not None
                       and not isinstance(scope["records"], list))
                       for scope in scopes)))
                or (observations is not None and (not isinstance(observations, list)
                    or any(not isinstance(event, dict) for event in observations)))):
            raise ValueError("invalid saved memory snapshot evidence")
        if any(event.get("task_id") not in node_ids for event in observations or []):
            raise ValueError("saved memory observations include another task tree")
        for event in observations or []:
            parsed = TraceEvent.model_validate(event)
            if parsed.kind != "memory" or parsed.name not in {
                "memory_retrieved", "memory_context_selected", "memory_prompt_observed",
            }:
                raise ValueError("saved memory observation is not a recorded memory stage")
    failure = task.get("failure_snapshot")
    if failure is not None:
        if (not isinstance(failure, dict) or failure.get("schema_version") != 1
                or failure.get("status") not in {"recorded", "partial"}
                or not isinstance(failure.get("events"), list)
                or any(not isinstance(event, dict) or event.get("task_id") not in node_ids
                       for event in failure["events"])):
            raise ValueError("invalid saved failure snapshot")
        saved_tool_failures = 0
        for event in failure["events"]:
            parsed = TraceEvent.model_validate(event)
            saved_tool_failures += int(parsed.kind == "tool" and (
                parsed.payload.get("ok") is False or parsed.status in {"error", "cancelled"}
            ))
        observed = failure.get("observed_tool_failures")
        count = failure.get("tool_failure_count")
        if (not _count(observed, nullable=False) or not _count(count)
                or observed != saved_tool_failures
                or (failure["status"] == "partial" and count is not None)
                or (count is not None and count != observed)):
            raise ValueError("invalid saved tool-failure counts")
    return {
        "result_source": str(path),
        "artifact_snapshot": artifacts,
        "artifact_reference": task.get("artifact_snapshot"),
        "artifact_status": ("saved_immutable_snapshot" if artifacts is not None else
                            "unsupported_historical_artifact_snapshot"),
        "llm": _saved_observation(
            task, "llm", ("calls", "error_calls", "usage_missing_calls", "known_tokens_in",
                          "known_tokens_out", "tokens_in", "tokens_out"),
            ("cumulative_call_s",), path,
        ),
        "human": _saved_observation(task, "human", ("answered_requests",),
                                    ("cumulative_wait_s",), path),
        "search_delta": _saved_observation(
            task, "search_delta", ("tool_calls", "provider_requests", "cache_hits",
                                   "deduplicated", "budget_denials"), (), path,
        ),
        "search_scenario_cumulative": _saved_observation(
            task, "search_scenario_cumulative", ("limit", "used", "remaining",
                "provider_requests", "provider_successes", "provider_failures", "tool_calls",
                "cache_hits", "deduplicated", "budget_denials"), (), path,
            scope="saved scenario cumulative snapshot; not task-only usage",
        ),
        "memory_context_snapshots": snapshots,
        "memory_snapshot": ({"values": memory, "source": str(path),
                             "scope": "saved post-engine role-scope state; not task-only writes"}
                            if memory is not None else None),
        "memory_status": (memory["status"] if memory is not None else
                          "saved_candidates_only" if snapshots else "unknown"),
        "memory_note": "No live memory.db sampled; candidates do not prove use or causal benefit.",
        "failure": {
            "source": str(path),
            "unsuccessful_node_ids": [node["id"] for node in nodes
                                      if node["status"] in {"FAILED", "DEAD", "CANCELLED"}],
            "root_machine_rejected": task.get("root_machine_accepted") is False,
            "recorded_error_type": task.get("error_type"),
            "recorded_error_message": task.get("error_message"),
            "tool_failure_count": failure.get("tool_failure_count") if failure else None,
            "snapshot": failure,
            "note": "Only saved node/error/trace facts; missing failure measurements are unknown.",
        },
    }


def _session(root: Path, source: Path) -> dict:
    """Recompute conversation results from each saved task, never cached totals."""
    manifest = _read(source)
    if (manifest.get("schema_version") != 1
            or manifest.get("kind") not in {"instructor_test", "instructor_midtest"}):
        raise ValueError("unsupported saved test-session schema")
    wall_time = _session_wall_time(manifest)
    planned = manifest.get("planned_root_goals")
    if type(planned) is not int or planned < 1:
        raise ValueError("planned_root_goals must be a positive integer")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("session scenarios must be a list")
    records, protected, seen = [], [str(root / "reviews")], set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("session scenario must be an object")
        identity = scenario.get("scenario_id")
        if (not isinstance(identity, str) or not identity.startswith("scenario_")
                or not identity.removeprefix("scenario_").isdigit() or identity in seen):
            raise ValueError("invalid or duplicate session scenario")
        seen.add(identity)
        directory = _inside(root, identity)
        protected.append(str(directory))
        tasks = scenario.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) > 2:
            raise ValueError("a conversation group supports one or two sequential tasks")
        indexed = {f"task_{index}-result.json" for index in range(1, len(tasks) + 1)}
        if any(path.name not in indexed for path in directory.glob("task_*-result.json")):
            raise ValueError("unindexed task result; recover the session index before reporting")
        for index, cached in enumerate(tasks, 1):
            phase = f"task_{index}"
            if not isinstance(cached, dict) or cached.get("phase") != phase:
                raise ValueError("saved tasks are not in conversation order")
            path = _inside(root, f"{identity}/{phase}-result.json")
            task = _read(path)
            if (task.get("phase") != phase
                    or task.get("root_task_id") != cached.get("root_task_id")):
                raise ValueError("saved task identity disagrees with the session index")
            status = task.get("root_status")
            if status not in _NODE_STATUSES:
                raise ValueError("unknown saved task status")
            elapsed = task.get("delivery_elapsed_s", task.get("elapsed_s"))
            if (type(elapsed) not in {int, float} or not math.isfinite(elapsed)
                    or elapsed < 0):
                raise ValueError("saved delivery time must be finite and nonnegative")
            nodes = task.get("nodes")
            if (not isinstance(nodes, list) or not nodes
                    or any(not isinstance(node, dict) for node in nodes)):
                raise ValueError("saved task has no node evidence")
            node_ids = [node.get("id") for node in nodes]
            if (any(not isinstance(item, str) or not item for item in node_ids)
                    or len(set(node_ids)) != len(node_ids)
                    or task["root_task_id"] not in node_ids
                    or any(node.get("status") not in _NODE_STATUSES for node in nodes)):
                raise ValueError("invalid saved node identities or statuses")
            subtasks = [node for node in nodes if node["id"] != task["root_task_id"]]
            root_node = next(node for node in nodes if node["id"] == task["root_task_id"])
            if root_node["status"] != status:
                raise ValueError("root status disagrees with saved node evidence")
            success = task.get("verified_goal_success")
            if (task.get("root_machine_accepted") is not None
                    and type(task["root_machine_accepted"]) is not bool):
                raise ValueError("root_machine_accepted must be boolean or null")
            if success is not None and type(success) is not bool:
                raise ValueError("verified_goal_success must be boolean or null")
            if success is True and not (
                status == "DONE" and task.get("root_machine_accepted") is not False
                and task.get("quality_status") == "human_accepted"
                and task.get("quality_method") == "actual_human_review"
                and isinstance(task.get("human_quality_response"), dict)
                and task.get("human_quality_response", {}).get("decision") == "approve"
            ):
                raise ValueError("a successful goal lacks its actual quality-review evidence")
            raw_quality = {
                "quality_status": task.get("quality_status", "unreviewed"),
                "verified_goal_success": success, "quality_method": task.get("quality_method"),
            }
            from moumou.eval.review import resolve_review

            review = resolve_review(root, manifest, scenario, task, path)
            quality_status = raw_quality["quality_status"]
            quality_source = "raw_task_result" if success is not None else "unknown"
            if review is not None:
                if success is not None:
                    raise ValueError("offline review conflicts with an original quality outcome")
                approved = review["response"]["decision"] == "approve"
                quality_status = "human_accepted" if approved else "human_rejected"
                success = (approved and status == "DONE"
                           and task.get("root_machine_accepted") is not False)
                quality_source = "offline_human_review"
            records.append({
                "scenario_id": identity, "phase": phase, "root_status": status,
                "result_content_sha256": hashlib.sha256(
                    _canonical(task).encode("utf-8")
                ).hexdigest(),
                "root_task_id": task["root_task_id"], "elapsed_s": elapsed,
                "root_machine_accepted": task.get("root_machine_accepted"),
                "quality_status": quality_status, "quality_source": quality_source,
                "verified_goal_success": success,
                "raw_quality": raw_quality, "review": review, "nodes": nodes,
                "evidence": _session_evidence(task, path, directory, nodes),
                "observed_subtasks": len(subtasks),
                "observed_subtasks_done": sum(node["status"] == "DONE" for node in subtasks),
                "path": str(path),
            })
    if len(records) > planned:
        raise ValueError("saved task deliveries exceed the planned denominator")
    times = [record["elapsed_s"] for record in records]
    successes = sum(record["verified_goal_success"] is True for record in records)
    subtask_count = sum(record["observed_subtasks"] for record in records)
    subtask_done = sum(record["observed_subtasks_done"] for record in records)
    metrics = {
        "planned_root_goals": planned, "persisted_task_deliveries": len(records),
        "agent_declared_complete": sum(record["root_status"] == "DONE" for record in records),
        "verified_successes": successes,
        "unreviewed_deliveries": sum(record["verified_goal_success"] is None for record in records),
        "known_unsuccessful": sum(record["verified_goal_success"] is False for record in records),
        "not_delivered": planned - len(records),
        "provisional_verified_goal_rate": successes / planned,
        "observed_subtasks": subtask_count, "observed_subtasks_done": subtask_done,
        "observed_subtask_completion_rate": subtask_done / subtask_count if subtask_count else None,
        "suite_wall_time_s": wall_time["seconds"],
        "suite_wall_time_evidence": wall_time,
        "delivery_time_s": {"n": len(times), "sum": sum(times),
                            "mean": statistics.mean(times) if times else None,
                            "sample_std": statistics.stdev(times) if len(times) > 1 else None},
    }
    return {"schema_version": 1, "kind": "instructor_test", "root": str(root),
            "manifest": manifest, "records": records, "metrics": metrics,
            "report_builder": {
                "module": "moumou.eval.report", "kind": "offline_reconstruction",
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            },
            "sources": [{"path": str(source)}], "protected_directories": protected}


def build_report(root: Path | str) -> dict:
    """Read and validate one saved batch or experiment without changing any input."""
    root = Path(root).resolve()
    batch_file, ablation_file = root / "manifest.json", root / "ablation.json"
    session_files = [path for path in (root / "test.json", root / "midtest.json")
                     if path.is_file()]
    if session_files:
        if len(session_files) != 1 or batch_file.is_file() or ablation_file.is_file():
            raise ValueError("report root contains ambiguous session and batch manifests")
        return _session(root, session_files[0])
    if batch_file.is_file() and ablation_file.is_file():
        raise ValueError("report root is ambiguous: both manifest and ablation files exist")
    if ablation_file.is_file():
        return _ablation(root)
    if not batch_file.is_file():
        raise FileNotFoundError(
            "report root has neither a saved test session nor a historical batch")
    batch = _batch(root)
    manifest = batch["manifest"]
    return {"schema_version": 1, "kind": "batch", "root": str(root), "manifest": manifest,
            "purpose": _purpose(manifest.get("purpose", "unrecorded")),
            "model": _public_model(manifest.get("model", {})),
            "groups": [{"name": "batch", "metrics": batch["metrics"],
                        "records": batch["records"], "missing": batch["missing"]}],
            "warmups": [],
            "sources": [{"path": str(batch_file), "eval_id": manifest["eval_id"]}],
            "protected_directories": batch["protected_directories"]}


def _cell(value):
    text = redact(str(value), None)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"))


def _number(value):
    return "N/A" if value is None else f"{value:.3f}"


def _optional(value):
    return "N/A" if value is None else _cell(value)


def _stats(value):
    return f"{_number(value['mean'])} / {_number(value['sample_std'])} / {value['n']}"


def _link(model, path, label="evidence"):
    path = Path(path)
    try:
        target = os.path.relpath(path, model.get("report_directory", model["root"]))
    except ValueError:  # A user-selected output on another Windows drive.
        target = path.as_posix()
    target = quote(target.replace("\\", "/"), safe="/:")
    return f"[{label}]({target})"


def _summary_table(rows, label):
    lines = [f"| {label} | planned | saved | goals passed | goal rate | "
             "terminal mean / sample std / n (s) | attempt mean / sample std / n (s) |",
             "|---|---:|---:|---:|---:|---|---|"]
    for key, row in rows.items():
        lines.append(f"| {_cell(key)} | {row['planned_runs']} | {row['persisted_runs']} | "
                     f"{row['verified_root_successes']} | {_number(row['root_completion_rate'])} | "
                     f"{_stats(row['timing']['task_terminal_s'])} | "
                     f"{_stats(row['timing']['runner_elapsed_s'])} |")
    return lines


def render_report(model: dict) -> str:
    """Render a deterministic Markdown view of the validated report model."""
    if model["kind"] == "instructor_test":
        return _render_session(model)
    manifest, purpose = model["manifest"], model["purpose"]
    labels = {"offline_validation": "Offline validation; not real-model performance.",
              "custom_worker_validation": "Custom worker validation; not model performance.",
              "model_evaluation": "Real-model evaluation declaration; dataset source shown below.",
              "not_started": "Not started: no model-performance observations.",
              "unrecorded": "Purpose was not recorded; no real-model provenance is inferred."}
    if (purpose == "model_evaluation"
            and model["model"].get("provider") not in {"hkgai", "deepseek"}):
        labels[purpose] = ("Model-evaluation intent; provider provenance is unknown or unverified. "
                           "This report does not certify real-model performance.")
    dataset_source = manifest.get("dataset_source", manifest.get("dataset", {}).get(
        "source", "unspecified; instructor closed-set use is not declared"))
    lines = ["# Evaluation performance report", "",
             labels.get(purpose, f"Unclassified saved purpose: {_cell(purpose)}."), "",
             f"- Saved status: {_cell(manifest.get('status', 'unknown'))}",
             f"- Dataset source: {_cell(dataset_source)}",
             f"- Provider/model: {_optional(model['model'].get('provider'))} / "
             f"{_optional(model['model'].get('model'))}",
             f"- Git/source: {_cell(manifest.get('code', {}).get('git_commit'))} / "
             f"{_cell(manifest.get('code', {}).get('source_sha256'))}",
             "", "Every planned attempt remains in its group's goal denominator, including "
             "missing results. Missing results do not prove execution started. "
             "Subtasks exclude the root and report observed coverage only.",
             "Times use mean / sample standard deviation / measured n; n=1 has no spread estimate.",
             "Cumulative LLM/tool/human spans can overlap and are not added as wall time.",
             "Unknown usage is N/A. Scripted token counters are not measured provider cost. "
             "No API price, cache hit rate or statistical improvement is inferred.", ""]
    if model["kind"] == "ablation":
        lines += ["Long-term experience memory and task slots are ablated; the conclusion board "
                  "remains enabled and role review is fixed off. Warmup is separate and only "
                  "MemoryRecord values seed fresh held-out state.",
                  "One observation per fixture/group is a pilot, not evidence of repeated-trial "
                  "variance or a memory/parallel performance benefit.", ""]
        if "selection" in manifest:
            lines += [f"Selected attempts: {manifest['selected_runs']}/{manifest['planned_runs']}; "
                      f"explicitly deferred, not started: {manifest['deferred_runs']}. "
                      "The full declared plan remains the goal denominator.", ""]
    for group in model["groups"]:
        metrics, name = group["metrics"], group["name"]
        row = metrics["batch"]
        lines += [f"## {_cell(name)}", "",
                  f"Verified root goals: {row['verified_root_successes']}/{row['planned_runs']} "
                  f"({_number(row['root_completion_rate'])}); missing results: "
                  f"{row['missing_results']}; saved unverified outcomes: {row['unverified_runs']}.",
                  f"Observed subtasks DONE: {row['observed_subtasks_done']}/"
                  f"{row['observed_subtasks']} "
                  f"({_number(row['observed_subtask_completion_rate'])}); "
                  f"machine-verified DONE: {row['machine_verified_subtasks_done']}/"
                  f"{row['observed_subtasks']}.",
                  f"Node machine-criteria coverage: with={row['nodes_with_machine_criteria']}, "
                  f"without={row['nodes_without_machine_criteria']}, "
                  f"unknown={row['nodes_with_unknown_criteria']}.", "",
                  "### By fixture", "", *_summary_table(metrics["by_fixture"], "fixture"),
                  "", "### By scenario", "", *_summary_table(metrics["by_scenario"], "scenario"),
                  "", "### Call and timing coverage", ""]
        llm = row["llm"]
        lines += [f"- Calls observed: {_cell(llm['observed_calls_total'])}; runs with telemetry: "
                  f"{llm['runs_with_telemetry']}/{row['planned_runs']}; "
                  f"missing run telemetry: {llm['missing_run_telemetry']}.",
                  f"- Complete input/output tokens: {_optional(llm['total_tokens_in'])} / "
                  f"{_optional(llm['total_tokens_out'])}; "
                  f"known subtotals: {llm['known_tokens_in']} / {llm['known_tokens_out']}.",
                  f"- Missing usage calls: {_optional(llm['usage_missing_calls'])}; "
                  "complete cache tokens: "
                  f"{_optional(llm['total_cache_hit_tokens'])}.",
                  f"- Post-terminal maintenance mean / sample std / n (s): "
                  f"{_stats(row['timing']['maintenance_s'])}.", "",
                  "### Failures, unverified and missing outcomes", "",
                  "| fixture | repeat | status | error | detail | authoritative result |",
                  "|---|---:|---|---|---|---|"]
        failures = [record for record in group["records"] if record["status"] != "succeeded"]
        for record in failures:
            lines.append(f"| {_cell(record['fixture_id'])} | {record['repeat']} | "
                         f"{_cell(record['status'])} | {_cell(record.get('error_type') or '-')} | "
                         f"{_cell(str(record.get('error_message') or '-')[:1200])} | "
                         f"{_link(model, record['evidence_result'], 'result.json')} |")
        for missing in group["missing"]:
            if missing.get("status") == "deferred":
                lines.append(f"| {_cell(missing['fixture_id'])} | {missing['repeat']} | "
                             f"deferred / not started | required scope unavailable | "
                             f"{_cell(missing['deferred_reason'])} | No execution evidence |")
                continue
            lines.append(f"| {_cell(missing['fixture_id'])} | {missing['repeat']} | "
                         "not persisted | unknown/not started | No authoritative result saved | "
                         f"{_link(model, missing['result'], 'planned evidence')} |")
        if not failures and not group["missing"]:
            lines.append("| - | - | none | - | No observed failure in this group | - |")
        lines += ["", "### Attempt evidence", "",
                  "| fixture | repeat | root machine verdict | behavior | result |",
                  "|---|---:|---|---|---|"]
        for record in group["records"]:
            checks = record.get("behavior_checks", [])
            behavior = "; ".join(f"{check.get('target')}: {check.get('ok')}" for check in checks)
            lines.append(f"| {_cell(record['fixture_id'])} | {record['repeat']} | "
                         f"{_cell(record.get('root_machine_accepted'))} | "
                         f"{_cell(behavior or 'no declared behavioral checks')} | "
                         f"{_link(model, record['evidence_result'], 'result.json')} |")
        lines += [""]
    if model["warmups"]:
        lines += ["## Warmup, separate from held-out measurements", "",
                  "| fixture | repeat | status | elapsed (s) | lessons | calls | "
                  "input / output | record |",
                  "|---|---:|---|---:|---:|---:|---|---|"]
        for row in model["warmups"]:
            lines.append(f"| {_cell(row['fixture_id'])} | {row['repeat']} | "
                         f"{_cell(row['status'])} | "
                         f"{_number(row['elapsed_s'])} | {_optional(row['memory_records'])} | "
                         f"{_optional(row['calls'])} | {_optional(row['tokens_in'])} / "
                         f"{_optional(row['tokens_out'])} | "
                         f"{_link(model, row['path'], 'warmup.json')} |")
        lines += [""]
    lines += ["## Raw provenance", ""]
    for source in model["sources"]:
        lines.append(f"- {_link(model, source['path'], 'manifest.json')}: "
                     f"eval_id={_cell(source.get('eval_id'))}")
    lines += ["", "This report is rebuilt from saved per-run results, not embedded ablation "
              "result copies or the potentially truncated JSONL index. It does not resume work, "
              "call a model, certify undeclared provenance, or turn pending research into success.",
              ""]
    return "\n".join(lines)


def _render_session(model: dict) -> str:
    manifest, metrics = model["manifest"], model["metrics"]
    wall = metrics["suite_wall_time_evidence"]
    wall_text = (_number(wall["seconds"]) if wall["status"] == "known" else
                 "unknown (missing " + ", ".join(wall["missing_timestamps"]) + ")")
    lines = [
        "# Test session report", "",
        f"Dataset: {_cell(manifest.get('dataset_kind', 'instructor'))}; "
        f"source: {_cell(manifest.get('source', 'not recorded'))}.",
        f"Saved status: {_cell(manifest['status'])}. This is not a process-liveness check.",
        "",
        f"Planned goals: {metrics['planned_root_goals']}; saved deliveries: "
        f"{metrics['persisted_task_deliveries']}; agent DONE: "
        f"{metrics['agent_declared_complete']}; verified successes: "
        f"{metrics['verified_successes']}; unreviewed: {metrics['unreviewed_deliveries']}; "
        f"not delivered: {metrics['not_delivered']}.",
        f"Provisional verified goal coverage: {metrics['provisional_verified_goal_rate']:.2%}. "
        "Unreviewed or unstarted goals are not silently counted as successes or measured failures.",
        f"Observed subtasks DONE: {metrics['observed_subtasks_done']}/"
        f"{metrics['observed_subtasks']}.",
        f"Whole-session elapsed time (s): {wall_text}; derived only from saved start/end.",
        f"Saved delivery time mean / sample std / n (s): {_stats(metrics['delivery_time_s'])}.",
        "",
        "| scenario | phase | root | agent status | machine accepted | quality | source | "
        "delivery (s) | result |",
        "|---|---|---|---|---|---|---|---:|---|",
    ]
    for row in model["records"]:
        lines.append(f"| {_cell(row['scenario_id'])} | {row['phase']} | "
                     f"{_cell(row['root_task_id'])} | {row['root_status']} | "
                     f"{_cell(row['root_machine_accepted'])} | {_cell(row['quality_status'])} | "
                     f"{_cell(row['quality_source'])} | "
                     f"{row['elapsed_s']:.3f} | {_link(model, row['path'], 'result.json')} |")
    lines += [
        "", "Saved phase observations (N/A means no saved measurement):", "",
        "| scenario / phase | LLM calls | input / output tokens | missing usage calls | "
        "cumulative LLM call seconds | fresh search attempts / cache hits / quota denials | "
        "memory | unsuccessful saved nodes |",
        "|---|---:|---|---:|---:|---|---|---:|",
    ]
    evidence_notes = []
    for row in model["records"]:
        evidence = row["evidence"]
        llm = (evidence["llm"] or {}).get("values", {})
        search = (evidence["search_delta"] or {}).get("values", {})
        snapshots = evidence["memory_context_snapshots"]
        memory_text = evidence["memory_status"]
        if snapshots:
            memory_text += f"; {len(snapshots)} saved context candidate snapshots"
        lines.append(
            f"| {_cell(row['scenario_id'])} / {row['phase']} | {_optional(llm.get('calls'))} | "
            f"{_optional(llm.get('tokens_in'))} / {_optional(llm.get('tokens_out'))} | "
            f"{_optional(llm.get('usage_missing_calls'))} | "
            f"{_number(llm.get('cumulative_call_s'))} | "
            f"{_optional(search.get('provider_requests'))} / "
            f"{_optional(search.get('cache_hits'))} / "
            f"{_optional(search.get('budget_denials'))} | {_cell(memory_text)} | "
            f"{len(evidence['failure']['unsuccessful_node_ids'])} |"
        )
        if row["review"] is not None:
            review = row["review"]
            evidence_notes.append(
                f"\n{_cell(row['scenario_id'])}/{row['phase']}: original quality "
                f"{_cell(row['raw_quality']['quality_status'])}; effective quality "
                f"{_cell(row['quality_status'])}; {_link(model, review['path'], 'human review')}. "
                "The original execution state/result is unchanged."
            )
        if evidence["artifact_reference"] is not None:
            artifact_path = Path(row["path"]).parent / evidence["artifact_reference"]["path"]
            reference = evidence["artifact_reference"]
            evidence_notes.append(
                f"\n{_cell(row['scenario_id'])}/{row['phase']}: "
                + _link(model, artifact_path, "saved immutable artifact manifest")
                + f"; capture {_cell(reference['status'])}, "
                + f"{reference['captured_count']}/{reference['selected_count']} selected files."
            )
        human = (evidence["human"] or {}).get("values", {})
        failure = evidence["failure"]
        evidence_notes.append(
            f"\n{_cell(row['scenario_id'])}/{row['phase']}: saved answered human requests "
            f"{_optional(human.get('answered_requests'))}; cumulative human wait (s) "
            f"{_number(human.get('cumulative_wait_s'))}; known token subtotals in/out "
            f"{_optional(llm.get('known_tokens_in'))}/{_optional(llm.get('known_tokens_out'))}; "
            f"saved tool failures {_optional(failure['tool_failure_count'])}."
        )
    lines += evidence_notes
    lines += [
        "", "Recomputed offline from authoritative task result files; cached manifest totals "
        "and duplicate task summaries do not establish success. "
        "DONE alone is not quality acceptance.",
        "Offline human decisions require a matching append-only request, consumed response and "
        "HumanIO event log bound to the saved result/source hashes. Reconsideration retains "
        "the prior decision through an explicit supersession chain. This checks recorded "
        "port evidence, not cryptographic human identity. Raw quality and review provenance "
        "remain separate; a review cannot create a delivery or repair a failed agent state.",
        "LLM and search observations above come from saved per-phase result fields, not "
        "scenario cumulative totals. Missing provider usage stays unknown even when known "
        "token subtotals exist. Cumulative call/wait spans can overlap and are not wall time. "
        "Memory is limited to saved post-engine snapshots or hash-checked context inputs; "
        "retrieval candidates do not prove prompt admission, reuse or a causal score gain. "
        "Live memory databases are not sampled. Missing tool-failure counts remain unknown.",
        "An immutable artifact snapshot, when saved, is hash-validated independently. Older "
        "results without one retain the unsupported_historical_artifact_snapshot boundary; "
        "a current mutable workspace must not be called the original delivery bytes.",
        "Whole-session time includes waiting and time spent on unfinished tasks. "
        "Delivery samples cover only persisted task results and exclude unfinished phases; "
        "a zero-sample delivery sum of zero is not total execution time. "
        "A missing saved finish remains unknown; the current clock or an external stop ledger "
        "is not substituted. Human/tool/LLM spans may overlap; "
        "they are not subtracted blindly. Missing usage, search or memory evidence is unknown, "
        "not zero; no memory benefit is inferred from an unpaired conversation.",
        "Full dialogue, actual human exchanges, private workspaces and metered search records "
        "remain in each scenario directory. "
        "Supplementary results remain separate from instructor scores.",
        "Offline report builder: " + _cell(model["report_builder"]["module"])
        + "; source SHA-256: " + model["report_builder"]["source_sha256"] + ". "
        "This identifies the report reconstruction and does not replace the run-start "
        "code snapshot stored in the original manifest.",
        "",
    ]
    return "\n".join(lines)


def generate_report(root: Path | str, output: Path | str | None = None) -> Path:
    """Write one derived Markdown/plain-text report, protecting attempt evidence."""
    model = build_report(root)
    target = Path(output) if output is not None else Path(model["root"]) / "report.md"
    if target.is_symlink():
        raise ValueError("report output cannot replace a symlink")
    target = target.resolve()
    if target.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("report output must use .md or .txt")
    if any(target.is_relative_to(Path(path)) for path in model["protected_directories"]):
        raise ValueError("report output would overwrite or enter original attempt evidence")
    model["report_directory"] = str(target.parent)
    atomic_text(target, render_report(model))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild a saved evaluation report offline")
    parser.add_argument("directory", nargs="?", help="saved batch or ablation directory")
    parser.add_argument("--run", dest="run_directory", help="saved batch or ablation directory")
    parser.add_argument("--out", default=None,
                        help="derived .md/.txt report; default RUN/report.md")
    args = parser.parse_args(argv)
    if bool(args.directory) == bool(args.run_directory):
        parser.error("supply one saved directory, positionally or with --run")
    try:
        path = generate_report(args.directory or args.run_directory, args.out)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"cannot rebuild report: {redact(str(exc), None)}", file=sys.stderr)
        return 2
    print(f"report rebuilt: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
