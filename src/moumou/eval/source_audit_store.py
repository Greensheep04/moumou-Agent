"""Enforced read-only projection of an already-created public evidence store."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from moumou.eval.source_evidence import (
    SourceEvidenceStore,
    _hash_key,
    _unalias,
    digest,
    public_url,
    safe_text,
)


def _identifier(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 200
            and safe_text(value, 200) == value)


class SourceAuditStore:
    """Never initialize/migrate a store or reopen it through writable getters.

    Missing databases/tables remain unavailable. SQL deadlines bound old-history
    scans. A read-only SQLite connection may still use SQLite's existing WAL/SHM
    coordination; this is a no-database-writes guarantee, not filesystem immutability.
    """

    def __init__(self, store):
        self.root, self.database = Path(store.root), Path(store.database)
        if (not self.root.is_absolute() or self.root.resolve() != self.root
                or self.database != self.root / "source-evidence.sqlite3"):
            raise ValueError("audit requires a canonical scenario-owned store")

    @contextmanager
    def _read(self):
        for path in (self.root, self.database, *(Path(str(self.database) + suffix)
                     for suffix in ("-wal", "-shm", "-journal"))):
            _unalias(path)
        db = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.create_function("audit_identifier", 1, lambda value: int(_identifier(value)),
                           deterministic=True)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def audit_event_page(self, *, agent_ids, task_id=None, limit=10, before_id=None):
        """Filter actor/task before LIMIT, with stable backwards event cursors."""
        if (not isinstance(agent_ids, (list, tuple)) or not 1 <= len(agent_ids) <= 64
                or any(not _identifier(agent) for agent in agent_ids)
                or (task_id is not None and not _identifier(task_id))
                or type(limit) is not int or not 1 <= limit <= 100
                or (before_id is not None and (type(before_id) is not int
                                              or not 1 <= before_id < 2**63))):
            raise ValueError("invalid public evidence page selection")
        projection = """
            WITH eligible AS (
                SELECT id,kind,payload FROM events
                WHERE kind IN ('read','search') AND id < ?
            ), checked AS (
                SELECT id,kind,CASE
                    WHEN typeof(payload)='text'
                         AND length(CAST(payload AS BLOB)) <= 262144 AND json_valid(payload)
                    THEN CASE WHEN json_type(payload)='object' THEN payload END
                    END AS payload FROM eligible
            ), projected AS (
                SELECT id,kind,payload,
                    json_extract(payload,'$.agent_id') AS agent_id,
                    json_extract(payload,'$.task_id') AS task_id,
                    json_type(payload,'$.agent_id') AS agent_type,
                    json_type(payload,'$.task_id') AS task_type
                FROM checked
            )
        """
        boundary = before_id if before_id is not None else 2**63 - 1
        placeholders = ",".join("?" for _ in agent_ids)
        selection = ("agent_type='text' AND task_type='text' "
                     "AND audit_identifier(agent_id) AND audit_identifier(task_id) "
                     "AND agent_id IN (" + placeholders + ")")
        parameters = [boundary, *agent_ids]
        if task_id is not None:
            selection += " AND task_id=?"
            parameters.append(task_id)
        with self._read() as db:
            rows = db.execute(projection + "SELECT id,kind,payload FROM projected WHERE "
                              + selection + " ORDER BY id DESC LIMIT ?",
                              [*parameters, limit + 1]).fetchall()
            unattributable = db.execute(projection + """
                SELECT COUNT(*) FROM projected WHERE COALESCE(agent_type,'')!='text'
                    OR COALESCE(task_type,'')!='text' OR NOT audit_identifier(agent_id)
                    OR NOT audit_identifier(task_id)
                """, (boundary,)).fetchone()[0]
        selected = rows[:limit]
        return {"events": [{"event_id": row["id"], "kind": row["kind"],
                            "payload": json.loads(row["payload"])} for row in selected],
                "has_more": len(rows) > limit,
                "next_before_id": selected[-1]["id"] if len(rows) > limit else None,
                "unattributable_events": unattributable}

    @staticmethod
    def _registered(db, url):
        variants = [url]
        parsed = urlsplit(url)
        match = re.fullmatch(
            r"/(abs|html)/((?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v[1-9]\d*)?)",
            parsed.path,
        )
        if parsed.hostname == "arxiv.org" and not parsed.query and match:
            variants.extend(scheme + "://arxiv.org/pdf/" + match[2] + suffix
                            for scheme in ("https", "http") for suffix in ("", ".pdf"))
        for candidate in variants:
            row = db.execute("SELECT payload FROM sources WHERE url=? AND "
                             "length(CAST(payload AS BLOB)) <= 262144", (candidate,)).fetchone()
            if row is not None:
                value = json.loads(row[0])
                if (isinstance(value, dict) and value.get("registered") is True
                        and value.get("url") == candidate):
                    return True
        return False

    def audit_document_version(self, metadata_sha256):
        """Validate registration and exact retained bytes in one read transaction."""
        key = _hash_key(metadata_sha256)
        with self._read() as db:
            row = db.execute("SELECT * FROM document_versions WHERE metadata_sha256=? AND "
                             "length(CAST(payload AS BLOB)) <= 262144", (key,)).fetchone()
            if row is None:
                return None
            payload = row["payload"]
            if not isinstance(payload, str) or digest(payload.encode()) != key:
                raise ValueError("retained metadata hash mismatch")
            metadata = json.loads(payload)
            if (not isinstance(metadata, dict) or any(metadata.get(field) != row[field]
                    for field in ("raw_sha256", "text_sha256", "retrieval_id", "url"))):
                raise ValueError("retained version index does not match metadata")
            if not self._registered(db, public_url(metadata["url"])):
                return None
            raw = db.execute("SELECT body,byte_count FROM raw_blobs WHERE sha256=? "
                             "AND length(body) <= 2097152", (row["raw_sha256"],)).fetchone()
            text = db.execute("SELECT text,byte_count FROM text_blobs WHERE sha256=? "
                              "AND length(CAST(text AS BLOB)) <= 8388608",
                              (row["text_sha256"],)).fetchone()
            if (raw is None or text is None or not isinstance(raw[0], bytes)
                    or not isinstance(text[0], str) or len(raw[0]) != raw[1]
                    or len(text[0].encode()) != text[1]):
                raise ValueError("retained blob unavailable or size mismatch")
            SourceEvidenceStore._validate_content(metadata, raw[0], text[0])
            return {**metadata, "metadata_sha256": key}, text[0]
