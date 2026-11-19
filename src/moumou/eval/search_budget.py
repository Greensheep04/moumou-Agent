"""Scenario search cache and a persisted, human-extendable request allowance.

Only public search results are shared. A cache hit is never a new provider
request, and a failed HTTP attempt still consumes its reserved allowance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from moumou.contracts import ToolResult
from moumou.eval.artifacts import atomic_text, redact
from moumou.tools.search import WebSearchTool


def _timestamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, UTC).isoformat()


class _MeteredTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, started):
        self.inner, self.started = inner, started

    async def handle_async_request(self, request):
        self.started()
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


class SearchBudgetTool:
    name = WebSearchTool.name
    description = WebSearchTool.description
    action_class = WebSearchTool.action_class
    timeout_s = WebSearchTool.timeout_s
    params_schema = WebSearchTool.params_schema

    def __init__(self, cache_dir: Path, max_requests: int = 6, delegate=None,
                 namespace: str = "scenario", ttl_s: float = 900.0, source_store=None):
        if type(max_requests) is not int or max_requests < 0:
            raise ValueError("max_requests must be a nonnegative integer")
        if (isinstance(ttl_s, bool) or not isinstance(ttl_s, (float, int))
                or not math.isfinite(ttl_s) or ttl_s < 0):
            raise ValueError("ttl_s must be finite and nonnegative")
        if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 200:
            raise ValueError("namespace must be a nonempty scenario identifier")
        self.delegate = delegate or WebSearchTool()
        self.source_store = source_store
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.cache_dir / "search-budget.sqlite3"
        self.events_path = self.cache_dir / "search-events.jsonl"
        self.namespace, self.ttl_s = namespace, float(ttl_s)
        self._lock, self._flights = asyncio.Lock(), {}
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quotas (
                    namespace TEXT PRIMARY KEY, initial_limit INTEGER NOT NULL,
                    request_limit INTEGER NOT NULL, used INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (
                    namespace TEXT NOT NULL, cache_key TEXT NOT NULL, payload TEXT NOT NULL,
                    retrieved_at REAL NOT NULL, request_id TEXT NOT NULL,
                    PRIMARY KEY(namespace, cache_key));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending (
                    namespace TEXT NOT NULL, cache_key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(namespace, cache_key));
            """)
        with self._db(write=True) as db:
            old = db.execute("SELECT * FROM quotas WHERE namespace=?", (namespace,)).fetchone()
            if old is None:
                db.execute("INSERT INTO quotas VALUES(?,?,?,0)",
                           (namespace, max_requests, max_requests))
                self._event(db, "budget_initialized", {"limit": max_requests})
            elif old["initial_limit"] != max_requests:
                raise ValueError("existing scenario budget differs; use an approved grant")
        self._flush_events()

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _safe(self, text):
        text = redact(str(text), self.delegate)
        text = re.sub(r"\btvly-[A-Za-z0-9_-]+", "[REDACTED]", text)
        return re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s\"'<>]+", r"\1[REDACTED]", text)

    def _event(self, db, kind, fields):
        event = {"at": _timestamp(), "namespace": self.namespace, "event": kind, **fields}
        payload = self._safe(json.dumps(event, ensure_ascii=False, allow_nan=False))
        db.execute("INSERT INTO events(namespace,kind,payload) VALUES(?,?,?)",
                   (self.namespace, kind, payload))

    def _record(self, kind, fields):
        with self._db(write=True) as db:
            self._event(db, kind, fields)
        self._flush_events()

    def _flush_events(self):
        # The JSONL is an atomic projection of committed SQLite events.
        with self._db(write=True) as db:
            rows = db.execute("SELECT payload FROM events ORDER BY id").fetchall()
            atomic_text(self.events_path, "".join(row[0] + "\n" for row in rows))

    def _finish(self, result, *, call_id, origin, key=None, retrieved_at=None,
                request_id=None, provider_requests=0):
        meta = {"call_id": call_id, "cache_key": key, "status": origin,
                "cache_hit": origin == "cache_hit", "provider_requests": provider_requests,
                "original_retrieved_at": _timestamp(retrieved_at) if retrieved_at else None,
                "served_at": _timestamp(), "source_request_id": request_id,
                "ok": result.ok}
        self._record("tool_result", meta)
        note = "search_budget=" + json.dumps(meta, ensure_ascii=True, sort_keys=True)
        return result.model_copy(update={"stderr": (result.stderr + "\n" + note).strip()})

    def _sanitize_result(self, result):
        if not isinstance(result, ToolResult):
            raise TypeError("search delegate must return ToolResult")
        values = result.model_dump()
        for field in ("stdout", "stderr", "error"):
            if values[field] is not None:
                values[field] = self._safe(values[field])
        # A shared search cache must not distribute another agent's artifacts.
        values["artifacts"] = []
        if result.ok:
            if len(values["stdout"].encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("search output exceeds cache bounds")
            if not isinstance(json.loads(values["stdout"]), list):
                raise ValueError("search output must retain the provider result list")
        return ToolResult.model_validate(values)

    async def run(self, args, ctx):
        result = await self._run(args, ctx)
        if self.source_store is not None:
            try:
                metadata = json.loads(result.stderr.rsplit("search_budget=", 1)[1])
                self.source_store.record_search(
                    args.get("query", "") if isinstance(args, dict) else "", result, ctx,
                    retrieved_at=metadata["original_retrieved_at"],
                    request_id=metadata["source_request_id"] or metadata["call_id"],
                    origin=metadata["status"],
                )
            except Exception:
                # Do not alter search quota/cache semantics if the optional evidence sink
                # fails, and never claim a snippet or missing provenance was verified.
                result = result.model_copy(update={
                    "stderr": result.stderr + "\nsource_evidence_error: provenance was not saved",
                })
        return result

    async def _run(self, args, ctx):
        call_id = uuid4().hex
        self._record("tool_call", {"call_id": call_id, "task_id": ctx.task_id,
                                   "agent_id": ctx.agent_id})
        try:
            query, count = args.get("query"), args.get("max_results", 5)
            if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                raise ValueError("query must contain 1-2000 characters")
            if type(count) is not int or not 1 <= count <= 5:
                raise ValueError("max_results must be an integer from 1 to 5")
            normalized = {"query": query.strip(), "max_results": count}
        except (AttributeError, ValueError):
            return self._finish(ToolResult(ok=False, error="invalid search arguments"),
                                call_id=call_id, origin="invalid_arguments")
        key = hashlib.sha256(json.dumps(normalized, sort_keys=True,
                                       ensure_ascii=False).encode("utf-8")).hexdigest()
        async with self._lock:
            with self._db() as db:
                cache = db.execute("SELECT * FROM cache WHERE namespace=? AND cache_key=?",
                                   (self.namespace, key)).fetchone()
            if cache is not None and 0 <= time.time() - cache["retrieved_at"] <= self.ttl_s:
                return self._finish(ToolResult.model_validate_json(cache["payload"]),
                                    call_id=call_id, origin="cache_hit", key=key,
                                    retrieved_at=cache["retrieved_at"],
                                    request_id=cache["request_id"])
            future = self._flights.get(key)
            if future is None:
                with self._db(write=True) as db:
                    quota = db.execute("SELECT * FROM quotas WHERE namespace=?",
                                       (self.namespace,)).fetchone()
                    if quota["used"] >= quota["request_limit"]:
                        pending = {"call_id": call_id, "query": self._safe(normalized["query"]),
                                   "cache_key": key, "used": quota["used"],
                                   "limit": quota["request_limit"], "requires_human": True,
                                   "question": "Search allowance exhausted. Ask the human whether "
                                               "to grant additional provider requests."}
                        db.execute("INSERT OR REPLACE INTO pending VALUES(?,?,?)",
                                   (self.namespace, key, json.dumps(pending, ensure_ascii=False)))
                        self._event(db, "budget_pending", pending)
                        denied = True
                    else:
                        db.execute("UPDATE quotas SET used=used+1 WHERE namespace=?",
                                   (self.namespace,))
                        self._event(db, "request_reserved", {"call_id": call_id, "cache_key": key})
                        denied = False
                self._flush_events()
                if denied:
                    return self._finish(ToolResult(ok=False, error=(
                        "search_budget_exhausted: no new request was sent. Call ask_human to "
                        "request an additional allowance; only an explicit human approval can "
                        "grant more requests. Cached unexpired results remain available."
                    )), call_id=call_id, origin="budget_exhausted", key=key)
                future = asyncio.get_running_loop().create_future()
                self._flights[key] = future
                leader = True
            else:
                leader = False
        if not leader:
            try:
                payload, retrieved, source_id = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.timeout_s - 1,
                )
                return self._finish(ToolResult.model_validate(payload), call_id=call_id,
                                    origin="deduplicated", key=key, retrieved_at=retrieved,
                                    request_id=source_id)
            except TimeoutError:
                return self._finish(ToolResult(ok=False, error=(
                    "matching search is still pending; no duplicate provider request was sent"
                )), call_id=call_id, origin="deduplication_timeout", key=key)

        attempts = 0

        def started():
            nonlocal attempts
            with self._db(write=True) as db:
                if attempts:
                    quota = db.execute("SELECT * FROM quotas WHERE namespace=?",
                                       (self.namespace,)).fetchone()
                    if quota["used"] >= quota["request_limit"]:
                        raise RuntimeError("search transport attempted to exceed its allowance")
                    db.execute("UPDATE quotas SET used=used+1 WHERE namespace=?", (self.namespace,))
                attempts += 1
                metering = (
                    "http_transport" if isinstance(self.delegate, WebSearchTool) else "delegate"
                )
                self._event(db, "provider_request", {"call_id": call_id, "cache_key": key,
                                                     "attempt": attempts, "metering": metering})
            self._flush_events()

        result, retrieved = None, None
        cancelled = False
        try:
            if isinstance(self.delegate, WebSearchTool):
                inner = self.delegate.transport or httpx.AsyncHTTPTransport()
                backend = WebSearchTool(api_key=self.delegate.api_key,
                                        transport=_MeteredTransport(inner, started))
            else:
                backend = self.delegate
                # Explicit injected delegates model one provider attempt in tests.
                started()
            result = self._sanitize_result(await backend.run(normalized, ctx))
            if result.ok:
                retrieved = time.time()
                with self._db(write=True) as db:
                    db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?)", (
                        self.namespace, key, result.model_dump_json(), retrieved, call_id,
                    ))
        except asyncio.CancelledError:
            cancelled = True
            result = ToolResult(
                ok=False, error="search cancelled; an attempted request remains spent",
            )
            raise
        except Exception as exc:
            result = ToolResult(ok=False, error="search delegate failed: " + type(exc).__name__)
        finally:
            if attempts == 0:
                with self._db(write=True) as db:
                    db.execute("UPDATE quotas SET used=used-1 WHERE namespace=?", (self.namespace,))
                    self._event(db, "reservation_released", {"call_id": call_id,
                                                             "reason": "no HTTP attempt began"})
            if result is None:
                result = ToolResult(ok=False, error="search outcome unavailable")
            if not future.done():
                future.set_result((result.model_dump(), retrieved, call_id))
            async with self._lock:
                self._flights.pop(key, None)
            self._record("request_finished", {"call_id": call_id, "cache_key": key,
                                              "provider_requests": attempts, "ok": result.ok,
                                              "cancelled": cancelled})
        return self._finish(result, call_id=call_id,
                            origin="provider" if attempts else "no_request",
                            key=key, retrieved_at=retrieved, request_id=call_id,
                            provider_requests=attempts)

    def grant(self, n: int, *, approval_id: str | None = None) -> dict:
        """Apply one explicit approval once, including after a resumed callback."""
        if type(n) is not int or n <= 0:
            raise ValueError("grant must be a positive integer")
        if not isinstance(approval_id, str) or not approval_id.strip():
            raise ValueError("grant requires an explicit human approval_id")
        with self._db(write=True) as db:
            # The committed event and quota increment share this transaction.
            # Reading the existing journal also covers pre-idempotence databases.
            prior = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM events WHERE namespace=? AND kind='budget_granted'",
                (self.namespace,),
            )]
            matching = [event for event in prior if event.get("approval_id") == approval_id]
            if matching:
                if len(matching) != 1 or matching[0].get("amount") != n:
                    raise ValueError("approval_id conflicts with a previously recorded grant")
            else:
                db.execute("UPDATE quotas SET request_limit=request_limit+? WHERE namespace=?",
                           (n, self.namespace))
                db.execute("DELETE FROM pending WHERE namespace=?", (self.namespace,))
                self._event(db, "budget_granted", {"amount": n, "approval_id": approval_id})
        self._flush_events()
        return self.stats()

    def stats(self) -> dict:
        with self._db() as db:
            quota = db.execute("SELECT * FROM quotas WHERE namespace=?",
                               (self.namespace,)).fetchone()
            events = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM events WHERE namespace=? ORDER BY id", (self.namespace,),
            )]
            pending = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM pending WHERE namespace=?", (self.namespace,),
            )]
        outcomes = [event for event in events if event["event"] == "tool_result"]
        requests = [event for event in events if event["event"] == "request_finished"
                    and event["provider_requests"] > 0]
        return {"namespace": self.namespace, "limit": quota["request_limit"],
                "used": quota["used"], "remaining": quota["request_limit"] - quota["used"],
                "provider_requests": sum(event["event"] == "provider_request" for event in events),
                "provider_successes": sum(event["ok"] is True for event in requests),
                "provider_failures": sum(event["ok"] is False for event in requests),
                "tool_calls": sum(event["event"] == "tool_call" for event in events),
                "cache_hits": sum(event["status"] == "cache_hit" for event in outcomes),
                "deduplicated": sum(event["status"] == "deduplicated" for event in outcomes),
                "budget_denials": sum(event["status"] == "budget_exhausted" for event in outcomes),
                "pending_requests": pending, "ttl_s": self.ttl_s,
                "events": str(self.events_path), "database": str(self.database)}
