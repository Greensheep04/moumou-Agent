"""Reuse only authentic public search results, with their original provenance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path

from moumou.contracts import ToolContext, ToolResult


def _unalias(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("public-source reuse cannot follow path aliases")


def _bounded_read(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("public-source reuse input exceeds bounds")
    return raw


def reuse_public_sources(root, scenario_id, store):
    """Import public provider cache rows; never import answers, deliverables or memories.

    This is explicit research reuse, not a provider/cache-hit event in the new run.
    A terminal manifest describes saved evidence, not a claim about process liveness.
    The main cache must be stable without a nonempty WAL/journal during this read.
    """
    if not re.fullmatch(r"scenario_[0-9]+", scenario_id):
        raise ValueError("invalid scenario source identity")
    root = Path(root).absolute()
    _unalias(root)
    manifest_path = root / "test.json"
    _unalias(manifest_path)
    manifest_raw = _bounded_read(manifest_path, 8 * 1024 * 1024)
    manifest = json.loads(manifest_raw.decode("utf-8-sig"))
    if (manifest.get("schema_version") != 1 or manifest.get("kind") != "instructor_test"
            or manifest.get("status") not in {
                "human_accepted", "agent_completed_quality_unreviewed", "incomplete",
                "quality_rejected", "error", "completed",
                "agent_incomplete", "partially_completed",
            }):
        raise ValueError("source reuse requires a saved terminal teacher session")
    scenarios = [row for row in manifest.get("scenarios", [])
                 if isinstance(row, dict) and row.get("scenario_id") == scenario_id]
    if len(scenarios) != 1:
        raise ValueError("source session has no unique matching scenario")
    database = root / scenario_id / "search/search-budget.sqlite3"
    _unalias(database)
    for ending in ("-wal", "-journal"):
        sidecar = Path(str(database) + ending)
        _unalias(sidecar)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("public-source cache has an active/nonempty journal")
    raw = _bounded_read(database, 64 * 1024 * 1024)
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("invalid bounded public-source cache")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute(
            "SELECT cache_key,payload,retrieved_at,request_id FROM cache "
            "WHERE namespace=? ORDER BY retrieved_at,cache_key LIMIT 1001", (scenario_id,),
        ).fetchall()
        events = connection.execute(
            "SELECT substr(payload,1,8193) FROM events WHERE namespace=? AND kind='tool_call' "
            "LIMIT 10001", (scenario_id,),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) > 1000:
        raise ValueError("public-source cache row bound exceeded")
    if len(events) > 10000 or any(len(row[0]) > 8192 for row in events):
        raise ValueError("public-source actor metadata exceeds bounds")
    actors = {}
    for (payload,) in events:
        event = json.loads(payload)
        actors[event.get("call_id")] = event
    # Recheck before importing anything; no original run is resumed or written.
    if (_bounded_read(database, 64 * 1024 * 1024) != raw
            or _bounded_read(manifest_path, 8 * 1024 * 1024) != manifest_raw):
        raise ValueError("source evidence changed during read-only reuse")
    prepared = []
    for key, payload, retrieved, request_id in rows:
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not isinstance(request_id, str) or not request_id
                or isinstance(retrieved, bool) or not isinstance(retrieved, (int, float))
                or not math.isfinite(retrieved) or not 0 < retrieved <= time.time()):
            raise ValueError("cached source identity or original retrieval time is invalid")
        result = ToolResult.model_validate_json(payload)
        if not result.ok:
            raise ValueError("provider cache contains an unsuccessful result")
        actor = actors.get(request_id, {})
        prepared.append((key, result, retrieved, request_id, ToolContext(
            agent_id=actor.get("agent_id") or "unknown-source-agent",
            task_id=actor.get("task_id") or "unknown-source-task", workspace=store.root,
        )))
    for _, result, retrieved, request_id, ctx in prepared:
        store.record_search(
            "Imported public search results; original query not retained in this cache",
            result, ctx, retrieved_at=retrieved, request_id=request_id, origin="imported",
        )
    return {
        "kind": "explicit_public_source_reuse", "source_run": str(root),
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "source_cache_sha256": hashlib.sha256(raw).hexdigest(), "scenario_id": scenario_id,
        "search_records_imported": len(prepared), "provider_requests": 0,
        "new_run_cache_hits": 0, "source_files_modified": False,
        "note": "Original timestamps and request IDs retained. No human answers, task outputs "
                "or grading decisions imported. Original query text is unknown in this cache; "
                "inspect the registered titles/snippets. Pages still need source verification.",
    }
