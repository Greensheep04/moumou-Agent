"""Public research reuse never becomes a fresh search, answer or quality approval."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from moumou.contracts import ToolResult
from moumou.eval.source_evidence import SourceEvidenceStore
from moumou.eval.source_seed import reuse_public_sources


def saved_source(tmp_path, status="agent_completed_quality_unreviewed"):
    root = tmp_path / "prior"
    folder = root / "scenario_2/search"
    folder.mkdir(parents=True)
    (root / "test.json").write_text(json.dumps({
        "schema_version": 1, "kind": "instructor_test", "status": status,
        "scenarios": [{"scenario_id": "scenario_2"}],
        "human_answers": "PRIVATE_ANSWER_MUST_NOT_BE_IMPORTED",
    }), encoding="utf-8")
    result = ToolResult(ok=True, stdout=json.dumps([{
        "title": "Public research", "url": "https://example.org/research",
        "snippet": "A public search preview, not an original-page verification.",
    }]))
    database = folder / "search-budget.sqlite3"
    retrieved = datetime(2025, 1, 3, tzinfo=UTC).timestamp()
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE cache(namespace,cache_key,payload,retrieved_at,request_id);
            CREATE TABLE events(namespace,kind,payload);
        """)
        connection.execute("INSERT INTO cache VALUES(?,?,?,?,?)", (
            "scenario_2", "a" * 64, result.model_dump_json(), retrieved, "old-request",
        ))
        connection.execute("INSERT INTO events VALUES(?,?,?)", (
            "scenario_2", "tool_call", json.dumps({"call_id": "old-request",
                                                      "task_id": "t1", "agent_id": "editor"}),
        ))
    return root, database


def test_public_reuse_keeps_original_time_and_identity_and_original_bytes(tmp_path):
    root, database = saved_source(tmp_path)
    old = database.read_bytes()
    store = SourceEvidenceStore(tmp_path / "new-public-evidence")
    report = reuse_public_sources(root, "scenario_2", store)
    assert database.read_bytes() == old
    assert report["provider_requests"] == report["new_run_cache_hits"] == 0
    assert report["search_records_imported"] == 1
    history = json.dumps(store.history())
    assert "2025-01-03" in history and "old-request" in history
    assert "PRIVATE_ANSWER_MUST_NOT_BE_IMPORTED" not in history
    assert '"origin": "imported"' in history
    assert "original query not retained" in history


@pytest.mark.parametrize("scenario", ["../scenario_2", "scenario_3"])
def test_source_reuse_rejects_escape_or_other_scenario(tmp_path, scenario):
    root, _ = saved_source(tmp_path)
    with pytest.raises(ValueError):
        reuse_public_sources(root, scenario, SourceEvidenceStore(tmp_path / "destination"))


def test_running_source_is_not_reused(tmp_path):
    root, _ = saved_source(tmp_path, status="running")
    with pytest.raises(ValueError, match="terminal"):
        reuse_public_sources(root, "scenario_2", SourceEvidenceStore(tmp_path / "destination"))


def test_source_reuse_rejects_nonempty_write_journal(tmp_path):
    root, database = saved_source(tmp_path)
    database.with_name(database.name + "-wal").write_bytes(b"unreviewed WAL")
    with pytest.raises(ValueError, match="journal"):
        reuse_public_sources(root, "scenario_2", SourceEvidenceStore(tmp_path / "destination"))


def test_source_reuse_bounds_manifest_before_parsing(tmp_path):
    root, _ = saved_source(tmp_path)
    (root / "test.json").write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds bounds"):
        reuse_public_sources(root, "scenario_2", SourceEvidenceStore(tmp_path / "destination"))


def test_source_reuse_bounds_actor_metadata_before_parsing(tmp_path):
    root, database = saved_source(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO events VALUES(?,?,?)", (
            "scenario_2", "tool_call", " " * 9000,
        ))
    with pytest.raises(ValueError, match="metadata exceeds bounds"):
        reuse_public_sources(root, "scenario_2", SourceEvidenceStore(tmp_path / "destination"))
