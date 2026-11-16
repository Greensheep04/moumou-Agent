"""Capture delivery-time facts without replacing immutable run results."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile

from moumou.sandbox.local import LocalJail

MEMORY_OBSERVATIONS = frozenset({
    "memory_retrieved", "memory_context_selected", "memory_prompt_observed",
})


def _now():
    return datetime.now(UTC).isoformat()


def sanitize_evidence(value, secrets=()):
    """Scrub configured credentials without mutating the execution objects."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [sanitize_evidence(item, secrets) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            safe_key = sanitize_evidence(key, secrets) if isinstance(key, str) else key
            if safe_key in cleaned:
                raise ValueError("credential scrubbing would merge distinct evidence keys")
            cleaned[safe_key] = sanitize_evidence(item, secrets)
        return cleaned
    return value


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


def _inside(root, relative):
    if (not isinstance(relative, str) or not relative or "\0" in relative
            or Path(relative).is_absolute() or PureWindowsPath(relative).anchor
            or ".." in relative.replace("\\", "/").split("/")):
        raise ValueError("evidence path must be workspace-relative")
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("evidence path escapes its scenario")
    return path


def _private_path_state(parts):
    if any(part.casefold() in {".git", ".evaluation"} for part in parts):
        return "excluded_framework_private"
    if any(part.casefold() == ".env" or
           (part.casefold().startswith(".env.") and part.casefold() != ".env.example")
           for part in parts):
        return "excluded_secret_path"
    return None


def capture_artifact_snapshot(scenario_dir, workspace, node, phase, *, secrets=()):
    """Copy selected delivered bytes once; never read later workspaces as history.

    A complete capture covers the selected paths, not the correctness of a goal.
    Tracked deletion markers remain visible and never become fabricated files.
    """
    from moumou.eval.artifacts import atomic_json

    scenario = Path(scenario_dir).resolve()
    if not re.fullmatch(r"task_[12]", phase):
        raise ValueError("invalid delivery phase")
    directory = _inside(scenario, phase + "-artifacts")
    directory.mkdir(parents=False, exist_ok=False)
    declared = list(node.result.files) if node.result is not None else []
    selected = list(dict.fromkeys(declared or node.artifacts))
    selection = "result_files" if declared else "tracked_artifacts"
    jail = LocalJail(Path(workspace))
    keys = tuple(key.encode("utf-8") for key in secrets if isinstance(key, str) and key)
    overlap = max((len(key) for key in keys), default=1) - 1
    entries = []
    for relative in selected:
        entry = {"path": relative, "state": "invalid"}
        entries.append(entry)
        temporary = None
        try:
            if not isinstance(relative, str) or Path(relative).is_absolute() or PureWindowsPath(relative).anchor:
                raise ValueError("delivery path must be relative")
            parts = relative.replace("\\", "/").split("/")
            if ".." in parts:
                raise ValueError("delivery path must not contain parent traversal")
            private = _private_path_state(parts)
            if private is not None:
                entry["state"] = private
                continue
            source = jail.resolve(relative)
            private = _private_path_state(source.relative_to(jail.root).parts)
            if private is not None:
                entry["state"] = private
                continue
            if not source.exists():
                entry["state"] = "missing_or_deleted"
                continue
            if not source.is_file():
                raise ValueError("delivery path is not a regular file")
            before = source.stat()
            digest, size, tail, secret_found = hashlib.sha256(), 0, b"", False
            with NamedTemporaryFile(dir=directory, prefix=".capture-", delete=False) as target:
                temporary = Path(target.name)
                with source.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        block = tail + chunk
                        if any(key in block for key in keys):
                            secret_found = True
                            break
                        tail = block[-overlap:] if overlap else b""
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if secret_found:
                entry["state"] = "excluded_secret_content"
                continue
            after = source.stat()
            if ((before.st_size, before.st_mtime_ns, before.st_ino)
                    != (after.st_size, after.st_mtime_ns, after.st_ino) or size != after.st_size):
                entry["state"] = "changed"
                continue
            full_hash = digest.hexdigest()
            suffix = source.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", source.suffix) else ".data"
            destination = directory / (full_hash[:32] + suffix)
            if destination.exists():
                if destination.is_symlink() or _digest(destination) != full_hash:
                    raise ValueError("artifact snapshot short-key conflict")
            else:
                os.replace(temporary, destination)
                temporary = None
            entry.update(state="captured", file=destination.relative_to(scenario).as_posix(),
                         sha256=full_hash, bytes=size)
        except (OSError, ValueError, RuntimeError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    captured = sum(entry["state"] == "captured" for entry in entries)
    status = "complete" if captured == len(selected) else "partial"
    manifest = {"schema_version": 1, "scenario_id": node.project_id,
                "root_task_id": node.id, "phase": phase, "captured_at": _now(),
                "selection": selection, "selected_count": len(selected),
                "captured_count": captured, "status": status, "entries": entries,
                "note": "Selected file bytes only; no automatic goal-quality judgment."}

    manifest_path = directory / "manifest.json"
    atomic_json(manifest_path, sanitize_evidence(manifest, secrets))
    return {key: manifest[key] for key in (
        "schema_version", "status", "captured_at", "selection", "selected_count", "captured_count",
    )} | {"path": manifest_path.relative_to(scenario).as_posix(),
         "sha256": _digest(manifest_path)}


def validate_artifact_snapshot(scenario_dir, reference, *, task=None):
    """Read and authenticate saved copies, without repairing or creating evidence."""
    if reference is None:
        return None
    if not isinstance(reference, dict) or reference.get("schema_version") != 1:
        raise ValueError("unsupported artifact snapshot reference")
    if (type(reference.get("schema_version")) is not int
            or type(reference.get("selected_count")) is not int
            or type(reference.get("captured_count")) is not int
            or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))):
        raise ValueError("invalid artifact snapshot reference counts or digest")
    scenario = Path(scenario_dir).resolve()
    path = _inside(scenario, reference.get("path"))
    if _digest(path) != reference.get("sha256"):
        raise ValueError("artifact snapshot manifest hash mismatch")
    from moumou.eval.report import _read

    manifest = _read(path)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or type(manifest.get("schema_version")) is not int
            or manifest.get("scenario_id") != scenario.name):
        raise ValueError("artifact snapshot scenario identity mismatch")
    if (not re.fullmatch(r"task_[12]", str(manifest.get("phase", "")))
            or reference.get("path") != manifest["phase"] + "-artifacts/manifest.json"
            or manifest.get("selection") not in {"result_files", "tracked_artifacts"}):
        raise ValueError("invalid artifact snapshot location or selection")
    if task is not None and (manifest.get("root_task_id") != task.get("root_task_id")
                             or manifest.get("phase") != task.get("phase")):
        raise ValueError("artifact snapshot task identity mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("artifact snapshot entries must be a list")
    seen, captured = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("invalid artifact snapshot entry")
        if entry["path"] in seen:
            raise ValueError("duplicate artifact snapshot path")
        seen.add(entry["path"])
        if entry.get("state") == "captured":
            if (Path(entry["path"]).is_absolute() or PureWindowsPath(entry["path"]).anchor
                    or ".." in entry["path"].replace("\\", "/").split("/")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))):
                raise ValueError("captured artifact has an unsafe original path")
            copy = _inside(scenario, entry.get("file"))
            if copy.parent.resolve() != path.parent.resolve():
                raise ValueError("artifact blob is outside its snapshot")
            if (type(entry.get("bytes")) is not int or copy.stat().st_size != entry["bytes"]
                    or _digest(copy) != entry.get("sha256")):
                raise ValueError("artifact snapshot bytes or hash mismatch")
            if not copy.name.startswith(entry["sha256"][:32] + "."):
                raise ValueError("artifact blob name disagrees with its digest")
            captured += 1
        elif entry.get("state") not in {
            "invalid", "excluded_framework_private", "excluded_secret_path",
            "excluded_secret_content", "missing_or_deleted", "changed",
        }:
            raise ValueError("unknown artifact snapshot state")
    expected_status = "complete" if captured == len(entries) else "partial"
    if (type(manifest.get("selected_count")) is not int
            or type(manifest.get("captured_count")) is not int
            or manifest.get("selected_count") != len(entries)
            or manifest.get("captured_count") != captured or manifest.get("status") != expected_status):
        raise ValueError("artifact snapshot counts disagree with entries")
    for key in ("status", "selection", "selected_count", "captured_count", "captured_at"):
        if reference.get(key) != manifest.get(key):
            raise ValueError("artifact snapshot reference disagrees with its manifest")
    return manifest


def capture_memory_snapshot(engine, nodes):
    """Snapshot current metadata, not retrospective reads of a mutable database."""
    from moumou.memory.store import memory_scope

    snapshot = {"schema_version": 1, "captured_at": _now(), "stage": "post_engine_run",
                "identity_policy": "project_role", "status": "recorded",
                "scopes": [], "observations": [], "errors": [],
                "observation_policy": "Recorded stage events; absence does not establish no retrieval or use.",
                "record_view": "Active records returned by MemoryStore.all; deletion counts are unknown."}
    if not getattr(engine.settings, "memory_enabled", False):
        snapshot["status"] = "disabled"
        return snapshot
    store = getattr(engine, "memory", None)
    if store is None:
        snapshot["status"] = "unavailable"
        return snapshot
    roles = engine.roles.list(engine.settings.project_id)
    for role in roles:
        scope = memory_scope(engine.settings.project_id, role.name)
        entry = {"scope": scope, "project_id": engine.settings.project_id,
                 "role": role.name, "records": []}
        snapshot["scopes"].append(entry)
        try:
            for record in store.all(scope):
                if record.project_id != engine.settings.project_id or record.role != role.name:
                    raise ValueError("memory store returned a record outside its requested scope")
                value = record.model_dump(mode="json")
                entry["records"].append({key: value.get(key) for key in (
                    "id", "task_id", "outcome", "score", "uses", "created_at", "last_used_at",
                )} | {"lesson_sha256": hashlib.sha256(record.lesson.encode("utf-8")).hexdigest()})
        except Exception as exc:
            entry["records"] = None
            snapshot["status"] = "partial"
            snapshot["errors"].append({"scope": scope, "error_type": type(exc).__name__})
    for node in nodes:
        try:
            snapshot["observations"].extend(event.model_dump(mode="json")
                                            for event in engine.trace.query(node.id)
                                            if event.kind == "memory" and event.name in MEMORY_OBSERVATIONS)
        except Exception as exc:
            snapshot["status"] = "partial"
            snapshot["errors"].append({"task_id": node.id, "error_type": type(exc).__name__})
    return snapshot


def capture_failure_snapshot(engine, nodes):
    """Keep observed operational failures separate from final goal quality."""
    events, errors = [], []
    tool_failures = 0
    for node in nodes:
        try:
            for event in engine.trace.query(node.id):
                failed_tool = event.kind == "tool" and (
                    event.payload.get("ok") is False or event.status in {"error", "cancelled"}
                )
                denied = event.kind == "approval" and event.payload.get("decision") == "reject"
                failed_check = event.kind == "acceptance" and event.payload.get("ok") is False
                rejected_result = event.kind == "task" and event.name in {
                    "result_rejected", "runner_crashed", "integration_failed", "materialise_failed",
                }
                if failed_tool or denied or failed_check or rejected_result:
                    events.append(event.model_dump(mode="json"))
                tool_failures += int(failed_tool)
        except Exception as exc:
            errors.append({"task_id": node.id, "error_type": type(exc).__name__})
    return {"schema_version": 1, "captured_at": _now(),
            "status": "partial" if errors else "recorded",
            "coverage": "Recorded trace for this saved root and its observed descendants.",
            "observed_tool_failures": tool_failures,
            "tool_failure_count": None if errors else tool_failures,
            "events": events, "errors": errors,
            "note": "Rejected permissions and failed checks are not automatically failed goals."}


def capture_scenario_state(scenario_dir, engine, *, secrets=()):
    """Persist an as-observed node view, including unfinished work, before close.

    This diagnostic is not a task delivery and never expands the scored plan.
    Forced process termination can prevent it from being written.
    """
    from moumou.eval.artifacts import atomic_json

    scenario = Path(scenario_dir).resolve()
    nodes = engine.repo.all_tasks()
    value = {"schema_version": 1, "scenario_id": engine.settings.project_id,
             "conversation_id": engine.settings.conversation_id, "captured_at": _now(),
             "stage": "scenario_close", "kind": "observed_state_not_delivery",
             "configured_secret_scrubbing": True,
             "nodes": [node.model_dump(mode="json") for node in nodes],
             "failure_snapshot": capture_failure_snapshot(engine, nodes),
             "memory_snapshot": capture_memory_snapshot(engine, nodes)}
    path = scenario / "state-snapshot.json"
    if path.exists():
        raise ValueError("scenario state snapshot already exists")
    atomic_json(path, sanitize_evidence(value, secrets))
    return {"schema_version": 1, "status": "recorded", "path": path.name,
            "sha256": _digest(path), "captured_at": value["captured_at"],
            "node_count": len(nodes), "kind": value["kind"]}
