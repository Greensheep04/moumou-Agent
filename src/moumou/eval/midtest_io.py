"""Real human ports and complete, redacted instructor-session dialogue."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from moumou.contracts import HumanResponse
from moumou.eval.artifacts import redact

_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


def _now():
    return datetime.now(UTC).isoformat()


class TranscriptRecorder:
    def __init__(self, path, *, secrets=()):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets = tuple(value for value in secrets if value)
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.Lock())

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = redact(text, None)
        text = re.sub(r"tvly-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
        return json.loads(text)

    def write(self, event, phase, **fields):
        row = self.clean({"event": event, "phase": phase, "ts": _now(), **fields})
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        return row


class RecordingLLM:
    def __init__(self, delegate, transcript_path, *, secrets=()):
        self.delegate = delegate
        self.recorder = TranscriptRecorder(transcript_path, secrets=secrets)
        self.phase = "initialization"
        self.calls = []

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "delegate"), name)

    def set_phase(self, phase):
        self.phase = str(phase)

    async def chat(self, messages, tools=None):
        phase, call_id, started = self.phase, uuid4().hex, monotonic()
        self.recorder.write("llm_request", phase, call_id=call_id, messages=messages, tools=tools)
        try:
            response = await self.delegate.chat(messages, tools)
        except BaseException as exc:
            elapsed = monotonic() - started
            self.calls.append(
                {
                    "phase": phase,
                    "status": "error",
                    "elapsed_s": elapsed,
                    "tokens_in": None,
                    "tokens_out": None,
                }
            )
            self.recorder.write(
                "llm_error",
                phase,
                call_id=call_id,
                elapsed_s=elapsed,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        elapsed = monotonic() - started
        measured = getattr(response, "usage_present", None) is True
        self.calls.append(
            {
                "phase": phase,
                "status": "completed",
                "elapsed_s": elapsed,
                "tokens_in": response.tokens_in if measured else None,
                "tokens_out": response.tokens_out if measured else None,
            }
        )
        self.recorder.write(
            "llm_response",
            phase,
            call_id=call_id,
            elapsed_s=elapsed,
            response=response.model_dump(mode="json"),
        )
        return response

    def summary(self):
        def count(rows):
            missing = sum(row["tokens_in"] is None or row["tokens_out"] is None for row in rows)
            return {
                "calls": len(rows),
                "error_calls": sum(row["status"] == "error" for row in rows),
                "usage_missing_calls": missing,
                "known_tokens_in": sum(row["tokens_in"] or 0 for row in rows),
                "known_tokens_out": sum(row["tokens_out"] or 0 for row in rows),
                "tokens_in": None if missing else sum(row["tokens_in"] for row in rows),
                "tokens_out": None if missing else sum(row["tokens_out"] for row in rows),
                "cumulative_call_s": sum(row["elapsed_s"] for row in rows),
            }

        return {
            "total": count(self.calls),
            "by_phase": {
                phase: count([row for row in self.calls if row["phase"] == phase])
                for phase in sorted({row["phase"] for row in self.calls})
            },
            "price_assumed": False,
            "transcript": str(self.recorder.path),
        }

    async def aclose(self):
        close = getattr(self.delegate, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


class FileHuman:
    def __init__(
        self, root, *, secrets=(), on_response=None, transcript_path=None, poll_interval_s=0.2
    ):
        if poll_interval_s <= 0:
            raise ValueError("human polling interval must be positive")
        self.root = Path(root).resolve()
        for name in ("pending", "requests", "responses", "consumed"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.recorder = TranscriptRecorder(self.root / "human-events.jsonl", secrets=secrets)
        self.shared = (
            TranscriptRecorder(transcript_path, secrets=secrets)
            if transcript_path is not None
            else None
        )
        self.phase = "initialization"
        self.on_response = on_response
        self.poll_interval_s = poll_interval_s
        self.exchanges = []
        self._begin_lock = asyncio.Lock()

    def set_phase(self, phase):
        self.phase = str(phase)

    def _event(self, event, phase, **fields):
        row = self.recorder.write(event, phase, **fields)
        if self.shared is not None:
            self.shared.write(event, phase, **fields)
        return row

    async def _begin(self, req):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", req.id):
            raise ValueError("unsafe human request id")
        async with self._begin_lock:
            request_file = self.root / "requests" / f"{req.id}.json"
            response_file = self.root / "responses" / f"{req.id}.json"
            if request_file.exists() or response_file.exists():
                raise ValueError("human request ids and preexisting responses cannot be reused")
            phase = self.phase
            record = self.recorder.clean(
                {"phase": phase, "created_at": _now(), "request": req.model_dump(mode="json")}
            )
            content = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
            with request_file.open("x", encoding="utf-8") as stream:
                stream.write(content)
            (self.root / "pending" / f"{req.id}.json").write_text(content, encoding="utf-8")
            self._event("human_request", phase, request=record["request"])
        return phase, monotonic()

    async def _finish(self, req, payload, phase, started):
        response = HumanResponse.model_validate(payload)
        if response.request_id != req.id:
            raise ValueError("human response id does not match the pending request")
        if req.kind in {"approval", "review"} and response.decision not in {"approve", "reject"}:
            raise ValueError("human approval or review requires an explicit decision")
        if req.kind == "question" and response.text is None:
            raise ValueError("a real answer is required for a human question")
        if req.kind == "question" and req.options and response.text not in req.options:
            raise ValueError("human response is outside the offered options")
        if (
            req.kind in {"approval", "review"}
            and req.options
            and response.decision not in req.options
        ):
            raise ValueError("human decision is outside the offered options")
        response = HumanResponse.model_validate(
            self.recorder.clean(response.model_dump(mode="json"))
        )
        wait_s = monotonic() - started
        if self.on_response is not None:
            value = self.on_response(req, response)
            if inspect.isawaitable(value):
                await value
        content = json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        (self.root / "consumed" / f"{req.id}.json").write_text(content, encoding="utf-8")
        (self.root / "pending" / f"{req.id}.json").unlink()
        (self.root / "responses" / f"{req.id}.json").unlink(missing_ok=True)
        self.exchanges.append({"phase": phase, "kind": req.kind, "wait_s": wait_s})
        self._event(
            "human_response",
            phase,
            request_id=req.id,
            response=response.model_dump(mode="json"),
            wait_s=wait_s,
        )
        return response

    async def ask(self, req):
        phase, started = await self._begin(req)
        path = self.root / "responses" / f"{req.id}.json"
        while not path.exists():
            await asyncio.sleep(self.poll_interval_s)
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("invalid human response file")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return await self._finish(req, payload, phase, started)

    async def notify(self, message):
        self._event("human_notification", self.phase, message=message.model_dump(mode="json"))

    def summary(self):
        return {
            "answered_requests": len(self.exchanges),
            "cumulative_wait_s": sum(row["wait_s"] for row in self.exchanges),
            "by_phase": {
                phase: {
                    "answered_requests": len(rows),
                    "cumulative_wait_s": sum(row["wait_s"] for row in rows),
                }
                for phase in sorted({row["phase"] for row in self.exchanges})
                if (rows := [row for row in self.exchanges if row["phase"] == phase])
            },
            "pending_ids": sorted(path.stem for path in (self.root / "pending").glob("*.json")),
            "wait_spans_may_overlap": True,
        }


class ConsoleHuman(FileHuman):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._console_lock = asyncio.Lock()

    async def ask(self, req):
        async with self._console_lock:
            phase, started = await self._begin(req)
            safe = self.recorder.clean(req.model_dump(mode="json"))
            print(json.dumps(safe, ensure_ascii=False, indent=2), flush=True)
            while True:
                text = await asyncio.to_thread(input, "Your answer: ")
                if req.kind in {"approval", "review"}:
                    decision, _, note = text.strip().partition(" ")
                    if decision.lower() not in {"approve", "reject"}:
                        print(
                            "Enter approve or reject explicitly, with an optional note.", flush=True
                        )
                        continue
                    payload = {
                        "request_id": req.id,
                        "decision": decision.lower(),
                        "text": note or None,
                    }
                else:
                    if req.options and text not in req.options:
                        print("Choose one of the displayed options.", flush=True)
                        continue
                    payload = {"request_id": req.id, "text": text}
                return await self._finish(req, payload, phase, started)

    async def notify(self, message):
        await super().notify(message)
        print(
            json.dumps(self.recorder.clean(message.model_dump(mode="json")), ensure_ascii=False),
            flush=True,
        )
