"""Bind trusted tool-owned lexical receipts to immutable delivered artifact bytes."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from moumou.eval.session_evidence import sanitize_evidence, validate_artifact_snapshot
from moumou.eval.source_evidence import digest, public_url, safe_text, timestamp
from moumou.tools.artifact_quote import _PRIVATE_NAMES, _PRIVATE_PARTS, _PRIVATE_SUFFIXES

_HASH = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[\w.:-]{1,256}")
_MAX_RECORDS = 256
_MAX_BLOB_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_NOTE = (
    "Operator-supplied tool-owned receipts, scoped to the supplied current task tree. "
    "Only final whole-file content identity and the recorded literal slice are rebound here. "
    "An identical child copy is not a manager read or verification. Source receipt structure "
    "is checked; source occurrence is the trusted original tool's observation, not rechecked "
    "against source bytes here. No quotation coverage, truth, completeness, task success or "
    "human-quality judgment is inferred. Later edits invalidate the whole-file binding."
)


def _require(condition):
    if not condition:
        raise ValueError("invalid bounded quote evidence")


def _identifier(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and safe_text(value, 256) == value


def _hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _canonical(path):
    path = Path(path).absolute()
    _require(not str(path).startswith(("\\\\", "//")))
    for part in (path, *path.parents):
        info = part.lstat()
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & 0x400)
    _require(path.resolve(strict=True) == path)
    return path


def _relative(value):
    _require(isinstance(value, str) and bool(value) and len(value.encode()) <= 2048)
    _require(not Path(value).is_absolute() and not PureWindowsPath(value).anchor)
    parts = value.replace("\\", "/").split("/")
    for part in parts:
        _require(part not in {"", ".", ".."} and ":" not in part and "\0" not in part
                 and not part.endswith((".", " ")) and not PureWindowsPath(part).is_reserved())
        name = part.casefold()
        _require(name not in _PRIVATE_PARTS and name not in _PRIVATE_NAMES
                 and not name.startswith(".env") and Path(name).suffix not in _PRIVATE_SUFFIXES)
    _require(safe_text(value, 2048) == value)
    return "/".join(parts)


def _read_blob(scenario, entry):
    relative = _relative(entry["file"])
    path = _canonical(scenario / relative)
    _require(path.is_relative_to(scenario))
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
             and type(entry["bytes"]) is int and 0 <= entry["bytes"] <= _MAX_BLOB_BYTES)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_BLOB_BYTES + 1)
    after = _canonical(path).stat()
    _require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
             == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
    _require(len(raw) == entry["bytes"] and digest(raw) == entry["sha256"])
    return raw


def _origin(record):
    _require(isinstance(record, dict) and set(record) == {
        "check_id", "checked_at", "agent_id", "task_id", "artifact", "source_check",
    })
    _require(isinstance(record["check_id"], str)
             and re.fullmatch(r"[0-9a-f]{32}", record["check_id"]) is not None)
    _require(_identifier(record["agent_id"]) and _identifier(record["task_id"]))
    _require(isinstance(record["checked_at"], str))
    timestamp(record["checked_at"])
    return {key: record[key] for key in ("check_id", "checked_at", "agent_id", "task_id")}


def _artifact(value):
    _require(isinstance(value, dict) and set(value) == {
        "path", "sha256", "bytes", "characters", "quote_sha256", "span", "stable_at_checks",
    })
    path = _relative(value["path"])
    _require(_hash(value["sha256"]) and _hash(value["quote_sha256"])
             and type(value["bytes"]) is int and 0 <= value["bytes"] <= _MAX_BLOB_BYTES
             and type(value["characters"]) is int and 0 <= value["characters"] <= value["bytes"])
    span = value["span"]
    _require(isinstance(span, dict) and set(span) == {
        "space", "unit", "interval", "start", "end", "byte_start", "byte_end",
    })
    _require(span["space"] == "UTF-8 decoded artifact" and span["unit"] == "Unicode code points"
             and span["interval"] == "zero-based [start,end)")
    _require(all(type(span[key]) is int for key in ("start", "end", "byte_start", "byte_end")))
    _require(0 <= span["start"] < span["end"] <= value["characters"]
             and span["end"] - span["start"] <= 4096
             and 0 <= span["byte_start"] < span["byte_end"] <= value["bytes"]
             and span["byte_end"] - span["byte_start"] <= 16384)
    _require(type(value["stable_at_checks"]) is bool)
    return {**value, "path": path}


def _source_proof(proof, quote):
    _require(isinstance(proof, dict))
    if proof.get("status") in {"unknown", "rejected"}:
        _require(proof.get("lexical_match") is None)
        return None
    _require(set(proof) == {"status", "lexical_match", "source", "mode", "normalization",
                           "coordinates", "stored_text_characters", "comparison_text_sha256",
                           "quote_sha256", "comparison_quote_sha256"})
    matched = proof["status"] == "matched"
    _require(proof["status"] in {"matched", "not_found"}
             and proof["lexical_match"] is matched)
    mode = proof["mode"]
    _require(mode in {"exact", "collapse_whitespace"})
    normalized = quote if mode == "exact" else re.sub(r"\s+", " ", quote).strip(" ")
    _require(bool(normalized) and proof["quote_sha256"] == digest(quote.encode())
             and proof["comparison_quote_sha256"] == digest(normalized.encode())
             and _hash(proof["comparison_text_sha256"]))
    source = proof["source"]
    allowed = {"url", "final_url", "retrieved_at", "retrieval_id", "metadata_sha256",
               "raw_sha256", "text_sha256", "retention_origin", "extractor_version", "retained_at"}
    required = {"url", "final_url", "retrieved_at", "retrieval_id", "metadata_sha256",
                "raw_sha256", "text_sha256"}
    _require(isinstance(source, dict) and required <= set(source) <= allowed)
    _require(all(isinstance(value, str) and len(value.encode()) <= 4096
                 for value in source.values()))
    public_url(source["url"])
    public_url(source["final_url"])
    timestamp(source["retrieved_at"])
    _require(all(_hash(source[key]) for key in ("metadata_sha256", "raw_sha256", "text_sha256")))
    if mode == "exact":
        _require(proof["comparison_text_sha256"] == source["text_sha256"]
                 and proof["normalization"] == "none")
    else:
        _require(proof["normalization"] == "Unicode whitespace runs -> U+0020; trim outer U+0020; "
                 "all non-whitespace code points unchanged")
    coordinates = proof["coordinates"]
    _require(isinstance(coordinates, dict) and set(coordinates) == {
        "space", "unit", "interval", "start", "end",
    })
    _require(coordinates["space"] == ("retained_extractor_text" if mode == "exact" else
                                     "whitespace_normalized_retained_extractor_text")
             and coordinates["unit"] == "Unicode code points"
             and coordinates["interval"] == (
                 "zero-based [start,end); first occurrence only; not a count")
             and type(proof["stored_text_characters"]) is int
             and 0 <= proof["stored_text_characters"] <= 8 * 1024 * 1024)
    if matched:
        _require(type(coordinates["start"]) is int and type(coordinates["end"]) is int
                 and 0 <= coordinates["start"] < coordinates["end"]
                 <= proof["stored_text_characters"]
                 and coordinates["end"] - coordinates["start"] == len(normalized))
    else:
        _require(coordinates["start"] is None and coordinates["end"] is None)
    # No source prose or potentially large URL is copied into delivery evidence.
    return {key: value for key, value in proof.items() if key != "source"} | {
        "source": {key: source[key] for key in (
            "metadata_sha256", "raw_sha256", "text_sha256", "retrieved_at",
        )}, "source_url_sha256": digest(source["url"].encode()),
        "source_final_url_sha256": digest(source["final_url"].encode()),
    }


def _bind_record(record, task_ids, entries, scenario, snapshot_available):
    origin = _origin(record)
    if record["task_id"] not in task_ids:
        return {**origin, "classification": "unapplied", "reason": "outside_current_task_tree"}
    if not snapshot_available:
        return {**origin, "classification": "unknown", "reason": "snapshot_unavailable"}
    if record["artifact"] is None:
        return {**origin, "classification": "unknown", "reason": "no_checked_artifact_identity"}
    artifact = _artifact(record["artifact"])
    origin["artifact"] = artifact
    if artifact["stable_at_checks"] is not True:
        return {**origin, "classification": "unknown", "reason": "artifact_unstable_at_check"}
    candidates = [entry for entry in entries if entry.get("state") == "captured"
                  and entry.get("sha256") == artifact["sha256"]
                  and entry.get("bytes") == artifact["bytes"]]
    if not candidates:
        return {**origin, "classification": "unapplied",
                "reason": "final_whole_file_identity_absent"}
    delivered = []
    raw = None
    for entry in candidates:
        path = _relative(entry["path"])
        candidate_raw = _read_blob(scenario, entry)
        _require(raw is None or candidate_raw == raw)
        raw = candidate_raw
        if len(delivered) < 8:
            delivered.append(path)
    text = raw.decode("utf-8")
    span = artifact["span"]
    quote = text[span["start"]:span["end"]]
    _require(len(text) == artifact["characters"] and bool(quote.strip())
             and len(text[:span["start"]].encode()) == span["byte_start"]
             and len(text[:span["end"]].encode()) == span["byte_end"]
             and digest(quote.encode()) == artifact["quote_sha256"]
             and raw[span["byte_start"]:span["byte_end"]] == quote.encode())
    proof = _source_proof(record["source_check"], quote)
    return {**origin, "classification": proof["status"] if proof else "unknown",
            "reason": None if proof else "source_lexical_evidence_unavailable",
            "delivered_artifact_paths": delivered, "matching_delivery_path_count": len(candidates),
            "delivery_paths_omitted": max(0, len(candidates) - len(delivered)),
            "binding": "whole_file_content_identity_only", "manager_read_or_verification": None,
            "source_check": proof}


def _publish_once(directory, name, raw):
    """O_EXCL temp + flushed bytes + atomic non-replacing hard-link publication."""
    temporary = directory / ("." + name + "." + uuid4().hex + ".tmp")
    target = directory / name
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _canonical(directory)
        _require(_canonical(temporary).read_bytes() == raw)
        # Unlike replace/rename-overwrite, link fails atomically if any target exists.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def capture_artifact_quote_evidence(scenario_dir, snapshot_ref, task, journal, *, task_ids,
                                    secrets=()):
    """Publish write-once evidence from actual tool.audit_records(), never model receipts."""
    scenario = _canonical(scenario_dir)
    _require(scenario.is_dir() and isinstance(task, dict)
             and task.get("phase") in {"task_1", "task_2"}
             and _identifier(task.get("root_task_id")))
    _require(isinstance(task_ids, (set, frozenset, list, tuple)) and len(task_ids) <= 1024
             and all(_identifier(value) for value in task_ids)
             and task["root_task_id"] in task_ids)
    current_ids = frozenset(task_ids)
    name = task["phase"] + "-artifact-quote-evidence.json"
    # Refuse reuse even when new evidence would be byte-identical.
    if os.path.lexists(scenario / name):
        raise FileExistsError("artifact quote evidence already exists")
    manifest, snapshot_status = None, "unavailable"
    try:
        _require(isinstance(snapshot_ref, dict)
                 and snapshot_ref.get("path") == task["phase"] + "-artifacts/manifest.json")
        _canonical(scenario / snapshot_ref["path"])
        manifest = validate_artifact_snapshot(scenario, snapshot_ref, task=task)
        _require(manifest is not None and len(manifest["entries"]) <= 512)
        snapshot_status = "validated"
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        manifest = None
    records, dropped, malformed_journal = [], None, False
    try:
        _require(isinstance(journal, dict) and set(journal) == {
            "schema_version", "records", "dropped_records",
        })
        _require(type(journal["schema_version"]) is int and journal["schema_version"] == 1
                 and isinstance(journal["records"], list)
                 and len(journal["records"]) <= _MAX_RECORDS
                 and type(journal["dropped_records"]) is int
                 and 0 <= journal["dropped_records"] < 2**63)
        dropped, seen = journal["dropped_records"], set()
        for record in journal["records"]:
            try:
                _require(isinstance(record, dict) and record.get("check_id") not in seen)
                item = _bind_record(record, current_ids, manifest["entries"] if manifest else [],
                                    scenario, manifest is not None)
                seen.add(record["check_id"])
            except (ValueError, TypeError, KeyError, OSError, RuntimeError, UnicodeError):
                item = {"classification": "unknown", "reason": "malformed_or_unavailable_receipt"}
            records.append(item)
    except (ValueError, TypeError, KeyError):
        malformed_journal = True
    if manifest is not None:
        try:
            _require(validate_artifact_snapshot(scenario, snapshot_ref, task=task) == manifest)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            manifest, snapshot_status = None, "unavailable"
            for record in records:
                if record["classification"] in {"matched", "not_found"}:
                    record.update(classification="unknown",
                                  reason="snapshot_changed_during_capture", source_check=None)
    counts = {kind: (None if malformed_journal else
                     sum(record["classification"] == kind for record in records))
              for kind in ("matched", "not_found", "unknown", "unapplied")}
    complete = (manifest is not None and not malformed_journal and dropped == 0
                and counts["unknown"] == 0)
    payload = {"schema_version": 1, "phase": task["phase"],
               "root_task_id": task["root_task_id"], "captured_at": timestamp(),
               "status": "recorded" if complete else "incomplete" if manifest else "unavailable",
               "snapshot_status": snapshot_status,
               "snapshot_capture_status": manifest["status"] if manifest else None,
               "snapshot_manifest_sha256": snapshot_ref.get("sha256") if manifest else None,
               "journal_status": "malformed" if malformed_journal else "recorded",
               "observed_record_count": None if malformed_journal else len(records),
               "journal_dropped_records": dropped, "counts": counts, "records": records,
               "quotation_coverage": None, "factually_correct": None, "quality_pass": None,
               "note": _NOTE}
    payload = sanitize_evidence(payload, secrets)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(raw) > _MAX_OUTPUT_BYTES:
        payload.update(status="incomplete", records=[], counts={
            "matched": 0, "not_found": 0, "unknown": len(records), "unapplied": 0,
        }, bounded_records_unavailable=True)
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
               + "\n").encode()
    path = _publish_once(scenario, name, raw)
    _require(_canonical(path).read_bytes() == raw)
    return {"schema_version": 1, "path": path.name, "sha256": digest(raw),
            "status": payload["status"], "counts": payload["counts"],
            "journal_dropped_records": dropped}
