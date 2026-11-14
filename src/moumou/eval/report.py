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
import sys
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
    rows_by_group = {name: [] for name in _GROUPS}
    sources, missing_by_group = [], {name: [] for name in _GROUPS}
    common_model = common_purpose = common_configuration = common_environment = None
    for key, planned in plans.items():
        group, fixture_id, repeat = key
        directory = planned["directory"]
        if not (directory / "manifest.json").exists():
            missing_by_group[group].append({"fixture_id": fixture_id, "repeat": repeat,
                                            "result": str(directory / "manifest.json")})
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


def build_report(root: Path | str) -> dict:
    """Read and validate one saved batch or experiment without changing any input."""
    root = Path(root).resolve()
    batch_file, ablation_file = root / "manifest.json", root / "ablation.json"
    if batch_file.is_file() and ablation_file.is_file():
        raise ValueError("report root is ambiguous: both manifest and ablation files exist")
    if ablation_file.is_file():
        return _ablation(root)
    if not batch_file.is_file():
        raise FileNotFoundError("report root has neither manifest.json nor ablation.json")
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
