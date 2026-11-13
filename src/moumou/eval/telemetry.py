"""Evaluation-only LLM call observations without retaining prompts or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from moumou.adapters import FakeLLM
from moumou.context.builder import LESSONS_HEADER


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _count(value) -> bool:
    return type(value) is int and value >= 0


def summarize_calls(calls: list[dict]) -> dict:
    """Use one calculation for live writes and independently validated file reads."""
    def summarize_rows(rows):
        complete = [row for row in rows if row["status"] == "succeeded"]
        reported = [row for row in complete
                    if row["measurement_source"] == "provider_response"]
        measured = [row for row in reported if row["usage_present"] is True
                    and _count(row["tokens_in"]) and _count(row["tokens_out"])]

        def count_tokens(key):
            values = [row[key] for row in reported if _count(row[key])]
            known = sum(values)
            return known, known if len(values) == len(rows) else None

        known_in, total_in = count_tokens("tokens_in")
        known_out, total_out = count_tokens("tokens_out")
        _, total_cache = count_tokens("cache_hit_tokens")
        durations = [row["elapsed_s"] for row in rows if row["elapsed_s"] is not None]
        return {
            "calls_total": len(rows), "calls_succeeded": len(complete),
            "calls_failed": sum(row["status"] in {"error", "cancelled"} for row in rows),
            "calls_in_flight": sum(row["status"] == "running" for row in rows),
            "usage_measured_calls": len(measured),
            "usage_missing_calls": len(rows) - len(measured),
            "known_tokens_in": known_in, "known_tokens_out": known_out,
            "total_tokens_in": total_in, "total_tokens_out": total_out,
            "total_cache_hit_tokens": total_cache,
            "observed_client_elapsed_s": sum(durations),
            "total_client_elapsed_s": sum(durations) if len(durations) == len(rows) else None,
        }

    phases, kinds = {}, {}
    for row in calls:
        phases.setdefault(row["phase"], []).append(row)
        kinds.setdefault(row["kind"], []).append(row)
    return {
        **summarize_rows(calls),
        "by_phase_calls": {phase: len(rows) for phase, rows in phases.items()},
        "by_phase": {phase: summarize_rows(rows) for phase, rows in phases.items()},
        "by_kind": {kind: summarize_rows(rows) for kind, rows in kinds.items()},
        "known_memory_ids": sorted({item["id"] for row in calls
                                    for item in row["memory"]["known_memories"]}),
        "limits": [
            "Only wrapper-observed client calls are counted; internal HTTP retries are not.",
            "Client elapsed includes wrapper persistence overhead and may overlap other calls.",
            "Failed or interrupted requests may still incur unreported provider charges.",
            "Scripted clients are offline evidence, not measured provider usage.",
            "Memory evidence matches known lesson text in the system Lessons section only.",
            "No prompt, response, exception message, or API credential is retained here.",
        ],
    }


class EvalLLM:
    """Transparent client wrapper shared by execution and maintenance callers.

    ``path`` is an atomic metadata snapshot, written before and after each call.
    A killed worker can therefore leave a visible in-flight call. SDK-internal
    retries and provider billing for failed requests remain unobservable.
    """

    def __init__(self, client, path: Path | None = None, *, memory_records: Iterable = (),
                 phase: str = "execution"):
        self.client = client
        self.path = Path(path) if path is not None else None
        self.phase = phase
        self.calls: list[dict] = []
        self._memory: list[tuple[str, str, str]] = []
        self.set_memory_records(memory_records)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def set_phase(self, phase: str) -> None:
        if not isinstance(phase, str) or not phase:
            raise ValueError("phase must be a nonempty string")
        self.phase = phase

    def set_memory_records(self, records: Iterable) -> None:
        known = []
        seen = set()
        for record in records:
            if isinstance(record, Mapping):
                identity, lesson = record.get("id"), record.get("lesson")
            else:
                identity, lesson = getattr(record, "id", None), getattr(record, "lesson", None)
            if (not isinstance(identity, str) or not identity or identity in seen
                    or not isinstance(lesson, str) or not lesson):
                raise ValueError("memory observations require unique IDs and nonempty lessons")
            seen.add(identity)
            known.append((identity, lesson, hashlib.sha256(lesson.encode("utf-8")).hexdigest()))
        self._memory = known

    def _memory_observation(self, messages: list[dict]) -> dict:
        lesson_sections = []
        for message in messages:
            content = message.get("content")
            if message.get("role") != "system" or not isinstance(content, str):
                continue
            if LESSONS_HEADER not in content:
                continue
            # A later heading belongs to another context layer, not long-term lessons.
            section = content.split(LESSONS_HEADER, 1)[1].split("\n## ", 1)[0]
            lesson_sections.append(section)
        hits = [
            {"id": identity, "lesson_sha256": digest}
            for identity, lesson, digest in self._memory
            if any(f"\n- {lesson}\n" in f"\n{section}\n" for section in lesson_sections)
        ]
        return {"lessons_header_present": bool(lesson_sections), "known_memories": hits}

    def _persist(self) -> None:
        if self.path is None:
            return
        from moumou.eval.artifacts import atomic_json

        atomic_json(self.path, {
            "schema_version": 1, "calls": self.calls, "summary": self.summary(),
        })

    async def chat(self, messages: list[dict], tools: list[dict] | None = None):
        started = monotonic()
        names = {tool.get("function", {}).get("name") for tool in (tools or [])}
        maintenance_call = (
            len(messages) == 1 and messages[0].get("role") == "user" and len(tools or []) == 1
        )
        call_kind = "reflection" if maintenance_call and names == {"submit_lesson"} else (
            "role_review" if maintenance_call and names == {"submit_revision"} else "agent"
        )
        record = {
            "call_id": len(self.calls) + 1, "phase": self.phase, "kind": call_kind,
            "started_at": _now(), "finished_at": None, "elapsed_s": None,
            "status": "running", "error_type": None,
            "measurement_source": "offline_scripted" if isinstance(self.client, FakeLLM)
            else "provider_response",
            "usage_present": None, "tokens_in": None, "tokens_out": None,
            "cache_hit_tokens": None,
            "memory": self._memory_observation(messages),
        }
        self.calls.append(record)
        self._persist()
        try:
            response = await self.client.chat(messages, tools)
            record["status"] = "succeeded"
            present = getattr(response, "usage_present", None)
            record["usage_present"] = present if type(present) is bool else None
            if present is True:
                for source, target in (("tokens_in", "tokens_in"), ("tokens_out", "tokens_out")):
                    value = getattr(response, source, None)
                    record[target] = value if _count(value) else None
            raw = getattr(response, "usage_raw", {})
            if isinstance(raw, dict):
                for source, target in (
                    ("prompt_tokens", "tokens_in"), ("completion_tokens", "tokens_out"),
                    ("prompt_cache_hit_tokens", "cache_hit_tokens"),
                ):
                    if _count(raw.get(source)):
                        record[target] = raw[source]
            return response
        except BaseException as exc:
            record["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            # Messages may include URLs, keys, or model text; only store the exception class.
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["finished_at"] = _now()
            record["elapsed_s"] = round(monotonic() - started, 6)
            self._persist()

    def summary(self) -> dict:
        return summarize_calls(self.calls)


def _validate_timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("client-call timestamps must be time-zone-aware text")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("missing time zone")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("client-call timestamps must be time-zone-aware text") from exc
    return parsed


def _validate_calls(calls: list[dict]) -> None:
    fields = {
        "call_id", "phase", "kind", "started_at", "finished_at", "elapsed_s", "status",
        "error_type", "measurement_source", "usage_present", "tokens_in", "tokens_out",
        "cache_hit_tokens", "memory",
    }
    for identity, row in enumerate(calls, start=1):
        if (not isinstance(row, dict) or set(row) != fields
                or type(row["call_id"]) is not int or row["call_id"] != identity):
            raise ValueError("invalid or duplicate client-call identity/fields")
        if not isinstance(row["phase"], str) or not row["phase"]:
            raise ValueError("invalid client-call classification")
        for key, allowed in (
            ("kind", {"agent", "reflection", "role_review"}),
            ("status", {"running", "succeeded", "error", "cancelled"}),
            ("measurement_source", {"offline_scripted", "provider_response"}),
        ):
            if not isinstance(row[key], str) or row[key] not in allowed:
                raise ValueError("invalid client-call classification")
        if row["usage_present"] is not None and type(row["usage_present"]) is not bool:
            raise ValueError("invalid client-call usage provenance")
        for key in ("tokens_in", "tokens_out", "cache_hit_tokens"):
            if row[key] is not None and not _count(row[key]):
                raise ValueError("client-call usage must be nonnegative integers or null")
        start = _validate_timestamp(row["started_at"])
        if row["status"] == "running":
            if any(row[key] is not None for key in (
                "finished_at", "elapsed_s", "error_type", "usage_present", "tokens_in",
                "tokens_out", "cache_hit_tokens",
            )):
                raise ValueError("an in-flight client call cannot report finished usage")
        else:
            end = _validate_timestamp(row["finished_at"])
            elapsed = row["elapsed_s"]
            if (end < start or isinstance(elapsed, bool)
                    or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed)
                    or elapsed < 0):
                raise ValueError("invalid client-call duration")
            if row["status"] == "succeeded":
                if row["error_type"] is not None:
                    raise ValueError("a successful client call cannot report an error")
                if row["usage_present"] is True and not all(
                    _count(row[key]) for key in ("tokens_in", "tokens_out")
                ):
                    raise ValueError("complete client usage requires both input and output counts")
            elif not isinstance(row["error_type"], str) or not row["error_type"]:
                raise ValueError("a failed client call must identify its exception type")
        memory = row["memory"]
        if (not isinstance(memory, dict)
                or set(memory) != {"lessons_header_present", "known_memories"}
                or type(memory["lessons_header_present"]) is not bool
                or not isinstance(memory["known_memories"], list)):
            raise ValueError("invalid client-call memory observation")
        ids = set()
        for item in memory["known_memories"]:
            if (not isinstance(item, dict) or set(item) != {"id", "lesson_sha256"}
                    or not isinstance(item["id"], str) or not item["id"] or item["id"] in ids
                    or not isinstance(item["lesson_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["lesson_sha256"]) is None
                    or not memory["lessons_header_present"]):
                raise ValueError("invalid or duplicate client-call memory identity")
            ids.add(item["id"])


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate telemetry JSON field")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError(f"non-finite telemetry JSON number: {value}")


def read_telemetry(path: Path) -> dict:
    """Validate the latest snapshot and independently recalculate its usage coverage."""
    value = json.loads(
        Path(path).read_text(encoding="utf-8"), parse_constant=_invalid_constant,
        object_pairs_hook=_json_pairs,
    )
    if (not isinstance(value, dict) or set(value) != {"schema_version", "calls", "summary"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("calls"), list)
            or not isinstance(value.get("summary"), dict)):
        raise ValueError("unsupported or invalid telemetry snapshot")
    _validate_calls(value["calls"])
    expected = summarize_calls(value["calls"])
    # Canonical JSON also distinguishes booleans from integer counts (True == 1 in Python).
    options = {"sort_keys": True, "allow_nan": False, "separators": (",", ":")}
    if json.dumps(value["summary"], **options) != json.dumps(expected, **options):
        raise ValueError("client telemetry summary disagrees with its call observations")
    return value
