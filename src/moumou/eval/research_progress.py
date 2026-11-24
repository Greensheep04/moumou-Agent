"""Opt-in formal-session evidence projections; native tools remain authoritative."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from pathlib import Path

from moumou.contracts import ToolResult

_SCOPES = 128
_IDENTITIES = 128
_INDEX_BYTES = 24000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _history_index(page):
    events = []
    for event in page["events"]:
        item = {}
        for key in ("kind", "request_id", "retrieved_at", "task_id", "agent_id",
                    "url", "metadata_sha256", "offset", "limit", "ok"):
            value = event.get(key)
            if isinstance(value, (str, int, bool)):
                item[key] = value if key == "url" or not isinstance(value, str) else value[:128]
        sources = event.get("sources") or []
        item["source_count"] = len(sources)
        item["sources"] = [{"url": source.get("url"),
                            "title": str(source.get("title") or "")[:160]}
                           for source in sources if isinstance(source, dict)]
        events.append(item)
    projected = {"representation": "compact_history_index", "events": events,
                 "total_events": page["total_events"], "offset": page["offset"],
                 "next_offset": page["next_offset"], "details_omitted": True,
                 "full_result_recovery": "Repeat this request with refresh=true for the "
                 "native bounded page, using the original store and cache policy.",
                 "note": "This is an index, not original-page verification. Follow the "
                 "actual next_offset; null ends this query. Do not guess further offsets."}
    # All event headers/cursors survive. Large source details are explicitly omitted,
    # never relabeled as a complete source list; native refresh remains available.
    while len(_json(projected).encode("utf-8")) > _INDEX_BYTES:
        candidate = next((item for item in reversed(events) if item["sources"]), None)
        if candidate is not None:
            candidate["sources"].pop()
            continue
        candidate = next((item for item in reversed(events) if "url" in item), None)
        if candidate is not None:
            candidate.pop("url")
            continue
        raise ValueError("history index headers exceed the projection bound")
    return projected


class TeacherResearchProgress:
    """A bounded, scenario-local ledger; no contents, credentials or model claims.

    Ledger eviction permits ordinary native output again; it never locks out a
    tool. Each native call still executes once. refresh is output recovery, not
    a network-cache bypass. No raw tool output or private filesystem is copied.
    """

    def __init__(self, project_id, conversation_id):
        self.project_id, self.conversation_id = project_id, conversation_id
        self._repo, self._workspaces, self._trace = None, None, None
        self._states = OrderedDict()

    def bind(self, repo, workspaces, trace):
        if self._repo is not None:
            raise ValueError("research progress is already bound")
        self.bind_workspaces(workspaces)
        self._repo, self._trace = repo, trace

    def bind_workspaces(self, workspaces):
        paths = {actor: Path(path).resolve() for actor, path in workspaces.items()}
        if not paths or len(set(paths.values())) != len(paths):
            raise ValueError("research progress needs distinct role workspaces")
        if self._workspaces is not None and paths.keys() != self._workspaces.keys():
            raise ValueError("research progress phase requires the same actors")
        self._workspaces = paths

    def _key(self, task_id, actor, workspace):
        if self._repo is None:
            raise ValueError("research progress is not bound")
        node = self._repo.get(task_id)
        if (node is None or node.project_id != self.project_id
                or node.conversation_id != self.conversation_id or node.role != actor
                or Path(workspace).resolve() != self._workspaces.get(actor)):
            raise ValueError("research progress caller is outside its bound task/role")
        return (self.project_id, self.conversation_id, task_id, actor,
                str(self._workspaces[actor]))

    def _state(self, key):
        if key not in self._states:
            self._states[key] = {"identities": OrderedDict(), "duplicates": 0,
                                 "last_history": None, "last_read": None}
            if len(self._states) > _SCOPES:
                self._states.popitem(last=False)
        self._states.move_to_end(key)
        return self._states[key]

    def checkpoint(self, node, role):
        key = self._key(node.id, role.name, self._workspaces.get(role.name))
        state = self._states.get(key)
        if not state:
            return ""
        return ("\n## This task's prepared evidence checkpoint\n" + _json({
            "duplicate_feedback_count": state["duplicates"],
            "last_history": state["last_history"], "last_read": state["last_read"],
        }) + "\nThis survives native retries for this task and actor only. These are tool "
                "preparations, not proof of model receipt, understanding or fact checking. "
                "If earlier text is absent after retry/compaction, refresh=true re-emits "
                "the native bounded result; it does not force a fresh network fetch. "
                "Do not restart unchanged exhausted pagination or infer completion.\n")

    def wrap(self, tool):
        if tool.name not in {"source_history", "read_source"}:
            raise ValueError("unsupported teacher evidence tool")
        return _ProjectedTool(self, tool)


class _ProjectedTool:
    def __init__(self, owner, native):
        self.owner, self.native = owner, native
        self.name, self.action_class, self.timeout_s = (
            native.name, native.action_class, native.timeout_s,
        )
        self.description = native.description + (
            " Formal-session progress mode: history normally returns a compact index; "
            "unchanged evidence may return no_new_evidence. refresh=true re-emits the "
            "current native bounded page/window, retaining native cache semantics. "
            "Stop pagination at next_offset=null; no_new_evidence is not a truth grade."
        )
        self.params_schema = copy.deepcopy(native.params_schema)
        self.params_schema["properties"]["refresh"] = {
            "type": "boolean", "default": False,
            "description": "Re-emit the native bounded result after retry/compaction; "
                           "does not bypass cache, access controls or source validation.",
        }

    async def run(self, args, ctx):
        try:
            if not isinstance(args, dict) or type(args.get("refresh", False)) is not bool:
                raise ValueError("refresh must be a boolean")
            key = self.owner._key(ctx.task_id, ctx.agent_id, ctx.workspace)
        except (ValueError, TypeError):
            return ToolResult(ok=False, error="invalid teacher research scope or refresh")
        native_args = {key: value for key, value in args.items() if key != "refresh"}
        # Never short-circuit native errors, cache/version checks or prepared-read audit.
        result = await self.native.run(native_args, ctx)
        if not result.ok:
            return result
        try:
            page = json.loads(result.stdout)
            if not isinstance(page, dict):
                return result
            if self.name == "source_history":
                if (not isinstance(page.get("events"), list) or len(page["events"]) > 30
                        or not all(isinstance(event, dict) for event in page["events"])
                        or type(page.get("total_events")) is not int
                        or type(page.get("offset")) is not int
                        or (page.get("next_offset") is not None
                            and type(page["next_offset"]) is not int)):
                    return result
                identity = _digest({"tool": self.name,
                                    "query": native_args.get("query", "").casefold(),
                                    "total_events": page["total_events"],
                                    "offset": page["offset"] if page["events"] else "empty",
                                    "events": page["events"], "next_offset": page["next_offset"]})
                projection = _history_index(page)
                latest = {"total_events": page["total_events"], "offset": page["offset"],
                          "next_offset": page["next_offset"], "page_identity": identity,
                          "refresh_request": {"query": native_args.get("query", ""),
                                              "limit": native_args.get("limit", 5),
                                              "offset": page["offset"], "refresh": True}}
            else:
                if (not isinstance(page.get("metadata_sha256"), str)
                        or len(page["metadata_sha256"]) != 64
                        or any(char not in "0123456789abcdef" for char in page["metadata_sha256"])
                        or not isinstance(page.get("url"), str) or len(page["url"]) > 4096
                        or not isinstance(page.get("text"), str)
                        or type(page.get("offset")) is not int):
                    return result
                identity = _digest({"tool": self.name,
                                    "url": page.get("url"), "version": page["metadata_sha256"],
                                    "offset": page["offset"],
                                    "next_offset": page.get("next_offset"),
                                    "text_sha256": _digest(page["text"])})
                projection = {k: v for k, v in page.items() if k != "text"}
                projection.update(text="", text_omitted=True,
                                  prepared_text_characters=len(page["text"]),
                                  emitted_text_characters=0,
                                  full_result_recovery="Repeat with refresh=true to re-emit "
                                  "the current native bounded window; cache policy is unchanged.")
                latest = {"metadata_sha256": page["metadata_sha256"],
                          "offset": page["offset"], "characters": len(page["text"]),
                          "next_offset": page.get("next_offset"), "window_identity": identity,
                          "refresh_request": {"url": page["url"],
                                              "offset": page["offset"],
                                              "limit": native_args.get("limit", 8000),
                                              "refresh": True}}
            state = self.owner._state(key)
            repeated = identity in state["identities"]
            refresh = args.get("refresh", False)
            if repeated and not refresh:
                if self.name == "source_history":
                    projection = {
                        "representation": "history_repeat_feedback",
                        "offset": page["offset"], "next_offset": page["next_offset"],
                        "total_events": page["total_events"],
                        "prepared_event_count": len(page["events"]), "events_omitted": True,
                        "note": "Repeated event details omitted, not an empty native page. "
                                "null next_offset ends the current query.",
                        "full_result_recovery": "Repeat with refresh=true to re-emit the "
                                                "current native bounded history page.",
                    }
                projection.update(status="no_new_evidence", evidence_identity=identity,
                                  progress_note="This task and actor already prepared the same "
                                  "evidence. Do not loop unchanged requests. Read a distinct "
                                  "relevant window, coordinate assigned work, or report a genuine "
                                  "blocker. Use refresh=true if earlier text is unavailable.")
            full = refresh or (self.name == "read_source" and not repeated)
            emitted = result if full else result.model_copy(update={"stdout": _json(projection)})
            # This is a zero-duration task preparation marker, not an extra tool
            # invocation or memory observation. Keep the existing trace contract.
            self.owner._trace.record(
                task_id=ctx.task_id, agent_id=ctx.agent_id, kind="task",
                name="research_progress_prepared", tool=self.name, evidence_identity=identity,
                repeated_evidence=repeated, explicit_refresh=refresh,
                native_stdout_characters=len(result.stdout),
                projected_stdout_characters=len(emitted.stdout),
                prepared_text_characters=(
                    len(page["text"]) if self.name == "read_source" else None
                ),
                projected_text_characters=(len(page["text"]) if self.name == "read_source"
                                           and full else 0 if self.name == "read_source" else None),
                interpretation="Prepared tool output only; not model receipt, understanding, "
                               "verification or quality approval.",
            )
            state["identities"][identity] = None
            if repeated and not refresh:
                state["duplicates"] += 1
            state["identities"].move_to_end(identity)
            if len(state["identities"]) > _IDENTITIES:
                state["identities"].popitem(last=False)
            state["last_history" if self.name == "source_history" else "last_read"] = latest
            return emitted
        except (ValueError, TypeError, KeyError, AttributeError):
            # Projection cannot turn a native success into a fabricated failure or
            # promise recovery from a private file that was never written.
            return result
