"""Shared public-source provenance; search snippets are never full-text verification."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from uuid import uuid4

from moumou.contracts import ToolResult


def timestamp(value=None):
    if value is None:
        value = time.time()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("retrieval timestamp must include timezone")
        return parsed.astimezone(UTC).isoformat()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid retrieval timestamp")
    return datetime.fromtimestamp(value, UTC).isoformat()


def safe_text(value, limit=4000):
    text = str(value or "")[:limit]
    text = re.sub(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    return re.sub(r"(?i)(Bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", text)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _hash_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("content lookup requires a full lowercase SHA256")
    return value


def _unalias(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("source evidence cannot follow path aliases")


def public_ip(value):
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    if not address.is_global or (mapped is not None and not mapped.is_global):
        raise ValueError("only globally routable public addresses are allowed")
    return str(address)


def public_url(value):
    """Syntactic policy only; every live fetch additionally resolves and pins DNS."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or len(value.encode("utf-8")) > 4096):
        raise ValueError("invalid public URL")
    if any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("invalid URL characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only HTTP(S) public sources are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credential URLs are forbidden")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("nonstandard source ports are forbidden")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if (host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".onion"))
            or ("." not in host and ":" not in host)):
        raise ValueError("local/private hostnames are forbidden")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        public_ip(host)
    engines = ("google.com", "bing.com", "duckduckgo.com", "baidu.com", "search.yahoo.com")
    if (any(host == engine or host.endswith("." + engine) for engine in engines)
            or any(part in {"google", "bing", "duckduckgo", "baidu", "yandex", "searx",
                            "searxng"} for part in host.split("."))
            or re.search(r"/(?:search|searchresults)(?:/|$)", parsed.path, re.I)):
        raise ValueError("search-engine retrieval cannot bypass the search allowance")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if re.search(r"api.?key|token|secret|password|credential|auth|signature", key, re.I):
            raise ValueError("credential-bearing URL parameters are forbidden")
    if re.search(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{8,}", value):
        raise ValueError("credential-bearing URLs are forbidden")
    authority = "[" + host + "]" if ":" in host else host
    if parsed.port is not None and parsed.port != (443 if parsed.scheme == "https" else 80):
        authority += ":" + str(parsed.port)
    return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))


class SourceEvidenceStore:
    """One scenario-owned store shared across roles; no agent workspace artifacts."""

    def __init__(self, root):
        _unalias(Path(root).absolute())
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "source-evidence.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    url TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    url TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    body BLOB NOT NULL, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_blobs (
                    sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, byte_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS text_blobs (
                    sha256 TEXT PRIMARY KEY, text TEXT NOT NULL, byte_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS document_versions (
                    metadata_sha256 TEXT PRIMARY KEY, retrieval_id TEXT UNIQUE NOT NULL,
                    url TEXT NOT NULL, payload TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL, text_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS document_cache (
                    url TEXT PRIMARY KEY, metadata_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS retention_settings (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        self._migrate_documents()

    @contextmanager
    def _db(self, *, write=False):
        for path in (self.root, self.database, *(Path(str(self.database) + suffix)
                     for suffix in ("-wal", "-shm", "-journal"))):
            _unalias(path)
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

    def _event(self, kind, payload):
        with self._db() as db:
            db.execute("INSERT INTO events(kind,payload) VALUES(?,?)", (
                kind, json.dumps(payload, ensure_ascii=False, allow_nan=False),
            ))

    def record_search(self, query, result, ctx, retrieved_at=None, request_id=None,
                      *, origin="provider"):
        """Import real old results with origin='imported'; does not meter/search anything.

        Callers supply original timestamps/request IDs from the real provider cache.
        Provider snippets are retained as snippets, never promoted to verified pages.
        """
        if not isinstance(result, ToolResult):
            raise TypeError("search evidence requires a ToolResult")
        event = {"query": safe_text(query, 2000), "task_id": safe_text(ctx.task_id, 200),
                 "agent_id": safe_text(ctx.agent_id, 200), "origin": safe_text(origin, 100),
                 "request_id": safe_text(request_id or uuid4().hex, 200),
                 "retrieved_at": timestamp(retrieved_at) if result.ok else None,
                 "recorded_at": timestamp(), "ok": result.ok,
                 "error": safe_text(result.error), "sources": [],
                 "verification": "search_snippet_only; original page has not been verified"}
        if result.ok:
            payload = json.loads(result.stdout)
            if not isinstance(payload, list) or len(result.stdout.encode()) > 2 * 1024 * 1024:
                raise ValueError("invalid bounded search evidence payload")
            for row in payload[:5]:
                if not isinstance(row, dict):
                    continue
                try:
                    url = public_url(row.get("url"))
                except (ValueError, UnicodeError):
                    event["sources"].append({"registered": False, "reason": "URL policy rejected"})
                    continue
                snippet = safe_text(row.get("snippet"), 8000)
                source = {"url": url, "title": safe_text(row.get("title"), 1000),
                          "snippet": snippet, "snippet_sha256": digest(snippet.encode()),
                          "retrieved_at": event["retrieved_at"], "request_id": event["request_id"],
                          "task_id": event["task_id"], "agent_id": event["agent_id"],
                          "query": event["query"], "registered": True,
                          "verification": "search_snippet_only"}
                event["sources"].append(source)
                with self._db() as db:
                    db.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (
                        url, json.dumps(source, ensure_ascii=False),
                    ))
        event["result_sha256"] = digest(json.dumps(
            {"ok": event["ok"], "error": event["error"], "sources": event["sources"]},
            ensure_ascii=False, sort_keys=True,
        ).encode())
        self._event("search", event)
        return event

    def registered(self, url):
        url = public_url(url)
        with self._db() as db:
            row = db.execute("SELECT payload FROM sources WHERE url=?", (url,)).fetchone()
        if row:
            return json.loads(row[0])
        parsed = urlsplit(url)
        # An explicit same-document format adapter, not source discovery/crawling.
        # The complete paper/version string must match an actual registered PDF.
        match = re.fullmatch(
            r"/(abs|html)/((?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v[1-9]\d*)?)",
            parsed.path,
        )
        if parsed.hostname != "arxiv.org" or parsed.query or not match:
            return None
        paper = match[2]
        originals = [scheme + "://arxiv.org/pdf/" + paper + suffix
                     for scheme in ("https", "http") for suffix in ("", ".pdf")]
        with self._db() as db:
            for original in originals:
                row = db.execute("SELECT payload FROM sources WHERE url=?", (original,)).fetchone()
                if row:
                    return {**json.loads(row[0]), "url": url, "variant_of": original,
                            "derived_format": "arxiv_same_document_" + match[1]}
        return None

    def history(self, query="", limit=5, offset=0):
        with self._db() as db:
            rows = db.execute("SELECT kind,payload FROM events ORDER BY id DESC").fetchall()
        events = [{"kind": row[0], **json.loads(row[1])} for row in rows]
        needle = query.casefold()
        events = [event for event in events if not needle or needle in json.dumps(
            event, ensure_ascii=False,
        ).casefold()]
        return {"events": events[offset:offset + limit], "total_events": len(events),
                "offset": offset, "next_offset": offset + limit if offset + limit < len(events)
                else None, "note": "Shared public evidence. Search snippets are not original-page "
                "verification. A page read does not independently verify its claims."}

    def document(self, url):
        url = public_url(url)
        with self._db() as db:
            row = db.execute("SELECT metadata_sha256 FROM document_cache WHERE url=?",
                             (url,)).fetchone()
        if row is None:
            return None
        version = self.document_version(row[0])
        if version is None:
            raise ValueError("source cache points to a missing retained version")
        return version

    @staticmethod
    def _validate_content(metadata, body, text):
        if (not isinstance(body, bytes) or len(body) > 2 * 1024 * 1024
                or not isinstance(text, str) or len(text.encode()) > 8 * 1024 * 1024):
            raise ValueError("retained source content exceeds bounded size/type")
        if (metadata.get("raw_sha256") != digest(body)
                or metadata.get("text_sha256") != digest(text.encode())
                or metadata.get("raw_bytes") != len(body)
                or metadata.get("text_characters") != len(text)):
            raise ValueError("retained source content/hash mismatch")
        if public_url(metadata["url"]) != metadata["url"]:
            raise ValueError("retained source URL is not canonical")
        public_url(metadata["final_url"])
        timestamp(metadata["retrieved_at"])

    @staticmethod
    def _blob(db, table, key, value):
        column = "body" if table == "raw_blobs" else "text"
        size = len(value if isinstance(value, bytes) else value.encode())
        db.execute(f"INSERT OR IGNORE INTO {table} VALUES(?,?,?)", (key, value, size))
        row = db.execute(f"SELECT {column},byte_count FROM {table} WHERE sha256=?",
                         (key,)).fetchone()
        if row is None or row[0] != value or row[1] != size:
            raise ValueError("immutable source blob conflicts with its content hash")

    def _retain(self, db, metadata, body, text):
        self._validate_content(metadata, body, text)
        payload = _canonical(metadata)
        if len(payload.encode()) > 256 * 1024:
            raise ValueError("retained source metadata exceeds size bounds")
        metadata_hash = digest(payload.encode())
        self._blob(db, "raw_blobs", metadata["raw_sha256"], body)
        self._blob(db, "text_blobs", metadata["text_sha256"], text)
        values = (metadata_hash, metadata["retrieval_id"], metadata["url"], payload,
                  metadata["raw_sha256"], metadata["text_sha256"])
        db.execute("INSERT OR IGNORE INTO document_versions VALUES(?,?,?,?,?,?)", values)
        row = db.execute("SELECT * FROM document_versions WHERE metadata_sha256=?",
                         (metadata_hash,)).fetchone()
        if row is None or tuple(row) != values:
            raise ValueError("immutable source version conflicts with its metadata hash")
        db.execute("INSERT OR REPLACE INTO document_cache VALUES(?,?)",
                   (metadata["url"], metadata_hash))
        returned = {**metadata, "metadata_sha256": metadata_hash}
        # Compatibility projection for existing read-only observers/auditors.
        # Immutable blobs/versions, not this latest row, retain historical bytes.
        db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?)", (
            metadata["url"], json.dumps(returned, ensure_ascii=False), body, text,
        ))
        return returned

    def _migrate_documents(self):
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO retention_settings VALUES(?,?)",
                       ("append_only_start_at", timestamp()))
            rows = db.execute("SELECT d.* FROM documents d LEFT JOIN document_cache c "
                              "ON c.url=d.url WHERE c.url IS NULL").fetchall()
            for row in rows:
                metadata = json.loads(row["payload"])
                self._validate_content(metadata, row["body"], row["text"])
                # Preserve only the current verified row. No old event/body is invented.
                metadata.pop("metadata_sha256", None)
                metadata.update({"retrieval_id": uuid4().hex, "retained_at": timestamp(),
                                 "retention_origin": "legacy_current_row_retained",
                                 "extractor_version": metadata.get(
                                     "extractor_version", "legacy_unknown")})
                self._retain(db, metadata, row["body"], row["text"])

    def retention_status(self):
        with self._db() as db:
            start = db.execute("SELECT value FROM retention_settings WHERE name=?",
                               ("append_only_start_at",)).fetchone()
        return {"append_only_start_at": start[0],
                "before_start_completeness": "unknown; overwritten legacy content is not invented"}

    def raw_blob(self, raw_sha256):
        key = _hash_key(raw_sha256)
        with self._db() as db:
            row = db.execute("SELECT body,byte_count FROM raw_blobs WHERE sha256=?",
                             (key,)).fetchone()
        if row is None:
            return None
        if not isinstance(row[0], bytes) or len(row[0]) != row[1] or digest(row[0]) != key:
            raise ValueError("retained raw blob hash mismatch")
        return row[0]

    def text_blob(self, text_sha256):
        key = _hash_key(text_sha256)
        with self._db() as db:
            row = db.execute("SELECT text,byte_count FROM text_blobs WHERE sha256=?",
                             (key,)).fetchone()
        if row is None:
            return None
        if (not isinstance(row[0], str) or len(row[0].encode()) != row[1]
                or digest(row[0].encode()) != key):
            raise ValueError("retained text blob hash mismatch")
        return row[0]

    def document_version(self, metadata_sha256):
        key = _hash_key(metadata_sha256)
        with self._db() as db:
            row = db.execute("SELECT * FROM document_versions WHERE metadata_sha256=?",
                             (key,)).fetchone()
        if row is None:
            return None
        if digest(row["payload"].encode()) != key:
            raise ValueError("retained metadata hash mismatch")
        metadata = json.loads(row["payload"])
        if (metadata["raw_sha256"] != row["raw_sha256"]
                or metadata["text_sha256"] != row["text_sha256"]
                or metadata["retrieval_id"] != row["retrieval_id"]
                or metadata["url"] != row["url"]):
            raise ValueError("retained version index does not match metadata")
        body, text = self.raw_blob(row["raw_sha256"]), self.text_blob(row["text_sha256"])
        self._validate_content(metadata, body, text)
        return {**metadata, "metadata_sha256": key}, text

    def record_document(self, url, final_url, body, text, content_type, title,
                        network_provenance=None, source_provenance=None):
        metadata = {"url": public_url(url), "final_url": public_url(final_url),
                    "retrieved_at": timestamp(), "retrieval_id": uuid4().hex,
                    "raw_sha256": digest(body), "text_sha256": digest(text.encode()),
                    "raw_bytes": len(body), "text_characters": len(text),
                    "content_type": content_type, "title": title,
                    "network_provenance": network_provenance or [],
                    "retention_origin": "native_get",
                    "extractor_version": "moumou-source-reader-v1",
                    "verification": "original_page_retrieved; claims still require assessment"}
        if source_provenance:
            metadata["source_request_id"] = safe_text(source_provenance["source_request_id"], 200)
            metadata["source_search_retrieved_at"] = timestamp(
                source_provenance["source_search_retrieved_at"])
            if source_provenance.get("variant_of"):
                metadata["variant_of"] = public_url(source_provenance["variant_of"])
                metadata["derived_format"] = safe_text(source_provenance["derived_format"], 100)
        with self._db(write=True) as db:
            return self._retain(db, metadata, body, text)

    def record_read(self, url, ctx, *, ok, cached=False, metadata=None, error=None,
                    offset=None, limit=None):
        # URL policy is rechecked; rejected credential/private URLs are never persisted.
        try:
            allowed_url = public_url(url)
        except (ValueError, UnicodeError):
            allowed_url = None
        self._event("read", {"url": allowed_url, "task_id": safe_text(ctx.task_id, 200),
                            "agent_id": safe_text(ctx.agent_id, 200), "recorded_at": timestamp(),
                            "ok": ok, "cached": cached, "metadata": metadata,
                            "offset": offset, "limit": limit,
                            "error": safe_text(error)})
