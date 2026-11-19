"""An audit never creates, migrates or writes an existing evidence database."""

import sqlite3

import pytest

from moumou.eval.source_audit_store import SourceAuditStore
from moumou.eval.source_evidence import SourceEvidenceStore


def test_read_connection_enforces_query_only_and_missing_database_stays_missing(tmp_path):
    original = SourceEvidenceStore(tmp_path / "public")
    reader = SourceAuditStore(original)
    before = original.database.read_bytes()
    with reader._read() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO events(kind,payload) VALUES('read','{}')")
    assert original.database.read_bytes() == before
    original.database.unlink()
    with pytest.raises(sqlite3.OperationalError):
        reader.audit_event_page(agent_ids=["actor"])
    assert not original.database.exists()


def test_projection_rejects_malformed_payload_and_unicode_blank_identity(tmp_path):
    original = SourceEvidenceStore(tmp_path / "public")
    reader = SourceAuditStore(original)
    with original._db() as db:
        db.executemany("INSERT INTO events(kind,payload) VALUES('read',?)", [
            ("{",), ("[]",), ('{"agent_id": "a", "task_id": "\\u2003"}',),
            ('{"agent_id": "a", "task_id": "t"}',),
            ('{"agent_id": "a", "task_id": {"x": 1}}',),
            ('{"agent_id": "a", "task_id": ["t"]}',),
        ])
    page = reader.audit_event_page(agent_ids=["a"], task_id="t")
    assert len(page["events"]) == 1
    assert page["events"][0]["event_id"] == 4
    assert page["unattributable_events"] == 5
    assert reader.audit_event_page(agent_ids=["a"])["events"] == page["events"]
