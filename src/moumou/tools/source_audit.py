"""Report recorded public-source access, never factual correctness or comprehension."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from moumou.contracts import ToolResult
from moumou.eval.source_audit_store import SourceAuditStore
from moumou.eval.source_evidence import digest, public_url, safe_text, timestamp

_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_OUTPUT_BYTES = 32768
_NOTE = (
    "Counts and interval unions cover only the returned event page. A successful read event "
    "records a prepared read_source response, not model receipt, attention, comprehension, "
    "fact verification or human approval. Full fetched bytes are not full characters served. "
    "Intervals are inferred from recorded pagination in exact retained extractor text, using "
    "zero-based half-open Unicode code-point coordinates. Unknown legacy evidence is not zero. "
    "Search origins are provenance, not authoritative provider HTTP billing."
)


def _workspace(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("workspace must be absolute")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("workspace aliases are forbidden")
    if path.resolve() != path:
        raise ValueError("workspace must be canonical")
    return path


def _identifier(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 200
            and safe_text(value, 200) == value)


def _reply(payload, *, ok=True, truncated=False):
    return ToolResult(ok=ok, stdout=json.dumps(payload, ensure_ascii=False),
                      error=None if ok else "source access audit unavailable: " + payload["reason"],
                      truncated=truncated)


def _unavailable(reason, *, rejected=False):
    return _reply({"status": "rejected" if rejected else "unknown", "reason": reason,
                   "factual_grade": None, "note": _NOTE}, ok=False)


def _time(value):
    try:
        return timestamp(value) if isinstance(value, str) else None
    except (ValueError, OverflowError):
        return None


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


class SourceAccessAuditTool:
    name = "source_access_audit"
    description = (
        "Audit recorded page-read successes/failures, exact source versions and characters "
        "served to an actor/task. Use agent_id='*' for this bound group's public evidence; "
        "all_tasks=true includes prior tasks. No fetch/search, truth grade or comprehension claim."
    )
    action_class = "fs_read"
    timeout_s = 10.0
    params_schema = {"type": "object", "additionalProperties": False, "properties": {
        "agent_id": {"type": "string", "maxLength": 200},
        "task_id": {"type": "string", "maxLength": 200},
        "all_tasks": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "before_event_id": {"type": "integer", "minimum": 1},
    }}

    def __init__(self, store, *, workspaces=None):
        self.store, self._workspaces = SourceAuditStore(store), None
        if workspaces is not None:
            self.bind_workspaces(workspaces)

    def bind_workspaces(self, workspaces, *, phase_change=False):
        """Bind trusted provider paths for the same agents across teacher tasks."""
        if self._workspaces is not None and not phase_change:
            raise ValueError("source audit workspaces are already bound")
        if (not isinstance(workspaces, Mapping) or not workspaces or len(workspaces) > 64
                or any(not _identifier(agent) or agent == "*" for agent in workspaces)):
            raise ValueError("source audit requires an agent-to-workspace mapping")
        bound = {agent: _workspace(path) for agent, path in workspaces.items()}
        if len(set(bound.values())) != len(bound):
            raise ValueError("source audit requires independent actor workspaces")
        if self._workspaces is not None and bound.keys() != self._workspaces.keys():
            raise ValueError("source audit phase requires the same actors")
        self._workspaces = bound

    def _selection(self, args, ctx):
        allowed = {"agent_id", "task_id", "all_tasks", "limit", "before_event_id"}
        if not isinstance(args, dict) or set(args) - allowed:
            raise ValueError("invalid arguments")
        if self._workspaces is None:
            raise ValueError("workspace binding unavailable")
        if (_workspace(ctx.workspace) != self._workspaces.get(ctx.agent_id)
                or not _identifier(ctx.task_id)):
            raise ValueError("caller outside bound actor/workspace scope")
        agent, all_tasks = args.get("agent_id", ctx.agent_id), args.get("all_tasks", False)
        limit, before = args.get("limit", 10), args.get("before_event_id")
        task = args.get("task_id", ctx.task_id)
        if (not _identifier(agent) or (agent != "*" and agent not in self._workspaces)
                or type(all_tasks) is not bool or (all_tasks and "task_id" in args)
                or not _identifier(task) or type(limit) is not int or not 1 <= limit <= 50
                or (before is not None and (type(before) is not int
                                           or not 1 <= before < 2**63))):
            raise ValueError("invalid selection")
        return (sorted(self._workspaces) if agent == "*" else [agent],
                None if all_tasks else task, limit, before)

    def _version(self, metadata, url):
        key = metadata.get("metadata_sha256") if isinstance(metadata, dict) else None
        if not isinstance(key, str) or not _HASH.fullmatch(key):
            return None, {"status": "unknown", "reason": "legacy_or_missing_version_identity"}
        try:
            # A single read-only connection validates registration and all version blobs.
            # Never route an audit through the store's legacy read/write _db helpers.
            saved = self.store.audit_document_version(key)
            if saved is None:
                return None, {"status": "unknown",
                              "reason": "exact_version_unavailable_in_registered_store",
                              "metadata_sha256": key}
            retained, text = saved
            if metadata != retained or public_url(url) != retained["url"]:
                raise ValueError("read metadata differs from its pinned version")
            identity = {field: retained[field] for field in (
                "url", "final_url", "metadata_sha256", "raw_sha256", "text_sha256",
                "retrieved_at", "retrieval_id", "extractor_version", "retention_origin",
                "source_request_id", "source_search_retrieved_at", "variant_of",
                "derived_format", "retained_at",
            ) if field in retained}
            public_url(identity["url"])
            public_url(identity["final_url"])
            if (any(not isinstance(value, str) or len(value.encode()) > 4096
                    for value in identity.values()) or _time(identity["retrieved_at"]) is None):
                raise ValueError("invalid bounded source identity")
            return len(text), {"status": "retained_exact_version", **identity}
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, UnicodeError):
            return None, {"status": "unknown", "reason": "exact_version_invalid_or_unavailable",
                          "metadata_sha256": key}

    def _report(self, rows, scope, page, before, *, clipped=False):
        counts = {"read_successes": 0, "read_failures": 0, "read_unknown_outcomes": 0,
                  "fresh_read_successes": 0, "cached_read_successes": 0,
                  "unknown_cache_successes": 0, "search_successes": 0, "search_failures": 0,
                  "search_unknown_outcomes": 0, "imported_search_events": 0}
        events, versions, times = [], {}, []
        for row in rows:
            payload, kind = row["payload"], row["kind"]
            moment = _time(payload.get("recorded_at"))
            if moment is not None:
                times.append(moment)
            outcome = payload.get("ok")
            outcome_name = "successes" if outcome is True else (
                "failures" if outcome is False else "unknown_outcomes")
            counts[kind + "_" + outcome_name] += 1
            event = {"event_id": row["event_id"], "kind": kind,
                     "agent_id": payload["agent_id"], "task_id": payload["task_id"],
                     "recorded_at": moment, "ok": outcome if type(outcome) is bool else None}
            if kind == "read":
                try:
                    event["url"] = public_url(payload.get("url"))
                except (ValueError, UnicodeError):
                    event["url"] = None
            if kind == "search":
                origin = safe_text(payload.get("origin"), 100)
                event.update({"origin": origin, "retrieved_at": _time(payload.get("retrieved_at")),
                              "request_id": safe_text(payload.get("request_id"), 200),
                              "access": "search_snippets_only; not a page read"})
                counts["imported_search_events"] += origin == "imported"
            elif outcome is False:
                event["error"] = safe_text(payload.get("error"), 300)
            elif outcome is True:
                cached = payload.get("cached")
                counter = ("cached_read_successes" if cached is True else
                           "fresh_read_successes" if cached is False else "unknown_cache_successes")
                counts[counter] += 1
                event["cached"] = cached if type(cached) is bool else None
                length, identity = self._version(payload.get("metadata"), payload.get("url"))
                key = (identity["metadata_sha256"] if length is not None else
                       "unknown-event-" + str(row["event_id"]))
                group = versions.setdefault(key, {
                    "source": identity, "successful_read_responses": 0,
                    "fresh_responses": 0, "cached_responses": 0,
                    "unknown_cache_responses": 0, "retained_text_characters": length,
                    "unknown_interval_responses": 0, "served_intervals": [], "times": [],
                })
                group["successful_read_responses"] += 1
                if moment is not None:
                    group["times"].append(moment)
                group[("cached" if cached is True else "fresh" if cached is False else
                       "unknown_cache") + "_responses"] += 1
                start, limit = payload.get("offset"), payload.get("limit")
                interval = None
                if (length is not None and type(start) is int and 0 <= start <= length
                        and type(limit) is int and 1 <= limit <= 12000):
                    interval = [start, min(length, start + limit)]
                    group["served_intervals"].append(interval)
                else:
                    group["unknown_interval_responses"] += 1
                event.update({"metadata_sha256": identity.get("metadata_sha256"),
                              "version_status": identity["status"], "served_interval": interval})
            events.append(event)
        for group in versions.values():
            version_times = group.pop("times")
            group["first_known_response_recorded_at_in_page"] = (
                min(version_times) if version_times else None)
            group["last_known_response_recorded_at_in_page"] = (
                max(version_times) if version_times else None)
            group["served_intervals"] = _union(group["served_intervals"])
            length = group["retained_text_characters"]
            known = sum(end - start for start, end in group["served_intervals"])
            group["known_served_characters"] = known if length is not None else None
            group["known_coverage_fraction"] = known / length if length else None
            group["all_retained_characters_covered_in_this_page"] = (
                True if length is not None and known == length else
                False if length is not None and not group["unknown_interval_responses"] else None)
        exact = [group for group in versions.values()
                 if group["source"]["status"] == "retained_exact_version"]
        counts.update({"distinct_retained_versions": len(exact),
                       "distinct_fresh_versions": sum(
                           group["fresh_responses"] > 0 for group in exact),
                       "distinct_cached_versions": sum(
                           group["cached_responses"] > 0 for group in exact)})
        has_more = clipped or page["has_more"]
        return {"status": "recorded_access_only", "factual_grade": None, "scope": scope,
                "pagination": {"returned_events": len(rows), "has_more": has_more,
                               "next_before_event_id": rows[-1]["event_id"] if has_more else None,
                               "output_limited": clipped,
                               "complete_selected_history": (
                                   before is None and not has_more
                                   and page["unattributable_events"] == 0)},
                "counts_for_this_page": counts,
                "first_known_recorded_at_in_page": min(times) if times else None,
                "last_known_recorded_at_in_page": max(times) if times else None,
                "unattributable_events_in_bound_store_window": page["unattributable_events"],
                "attribution_complete_for_bound_store_window": page["unattributable_events"] == 0,
                "source_versions": list(versions.values()), "events": events, "note": _NOTE}

    @staticmethod
    def _bounded_report(report):
        """Account for an omitted event and retain its continuation, never silently skip it."""
        events = [{key: event[key] for key in (
            "event_id", "kind", "agent_id", "task_id", "recorded_at", "ok",
        )} for event in report["events"]]
        for event in events:
            event["details_omitted"] = True
        result = {
            "status": "unknown", "reason": "single_event_details_exceed_output_bound",
            "factual_grade": None, "scope": report["scope"],
            "pagination": {**report["pagination"], "output_limited": True,
                           "complete_selected_history": False},
            "counts_for_this_page": report["counts_for_this_page"],
            "events": events, "source_versions": [],
            "source_version_details_omitted": len(report["source_versions"]),
            "coverage_unavailable_due_to_output_bound": True,
            "unattributable_events_in_bound_store_window":
                report["unattributable_events_in_bound_store_window"],
            "attribution_complete_for_bound_store_window":
                report["attribution_complete_for_bound_store_window"],
            "note": _NOTE,
        }
        if len(json.dumps(result, ensure_ascii=False).encode()) > _MAX_OUTPUT_BYTES:
            scope = report["scope"]
            result["scope"] = {
                "requester_agent_id": scope["requester_agent_id"],
                "requester_task_id": scope["requester_task_id"],
                "selected_agent_count": len(scope["selected_agent_ids"]),
                "selected_agent_ids_sha256": digest(json.dumps(
                    scope["selected_agent_ids"], ensure_ascii=False,
                ).encode()),
                "selected_agent_ids_omitted": True,
                "selected_task_id": scope["selected_task_id"],
                "shared_public_evidence_only": True,
            }
        return result

    async def run(self, args, ctx):
        try:
            agents, task, limit, before = self._selection(args, ctx)
        except (ValueError, TypeError, OSError, AttributeError):
            return _unavailable("invalid_arguments_or_bound_actor_scope", rejected=True)
        try:
            page = self.store.audit_event_page(agent_ids=agents, task_id=task,
                                               limit=limit, before_id=before)
            rows = page["events"]
            scope = {"requester_agent_id": ctx.agent_id, "requester_task_id": ctx.task_id,
                     "selected_agent_ids": agents, "selected_task_id": task,
                     "shared_public_evidence_only": True,
                     "binding": "trusted actual actor/workspace mapping; current source store only"}
            clipped = False
            while True:
                report = self._report(rows, scope, page, before, clipped=clipped)
                if len(json.dumps(report, ensure_ascii=False).encode()) <= _MAX_OUTPUT_BYTES:
                    return _reply(report, truncated=clipped)
                if len(rows) <= 1:
                    bounded = self._bounded_report(report)
                    if len(json.dumps(bounded, ensure_ascii=False).encode()) <= _MAX_OUTPUT_BYTES:
                        return _reply(bounded, ok=False, truncated=True)
                    return _unavailable("bounded_report_unavailable")
                rows, clipped = rows[:-1], True
        except (ValueError, TypeError, KeyError, OSError, AttributeError, UnicodeError,
                sqlite3.Error):
            return _unavailable("read_only_public_evidence_unavailable_or_invalid")
