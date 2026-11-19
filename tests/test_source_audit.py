"""Read-only public-source access accounting; no live APIs or model calls."""

from __future__ import annotations

import json
import sqlite3

import pytest

from moumou.contracts import ToolContext, ToolResult
from moumou.eval.source_evidence import SourceEvidenceStore
from moumou.tools import source_audit
from moumou.tools.source_audit import SourceAccessAuditTool

URL = "https://example.test/article"


def setup(tmp_path):
    # Opaque paths deliberately do not imply any LocalDirs/Git provider layout.
    workspaces = {"collector": tmp_path / "opaque-7", "critic": tmp_path / "opaque-9"}
    store = SourceEvidenceStore(tmp_path / "public-evidence")
    contexts = {agent: ToolContext(agent_id=agent, task_id="t1", workspace=path)
                for agent, path in workspaces.items()}
    return store, contexts, SourceAccessAuditTool(store, workspaces=workspaces)


def save(store, ctx, text="甲😀abcdefgh", *, url=URL, origin="provider"):
    store.record_search("Fixture public query", ToolResult(ok=True, stdout=json.dumps([{
        "url": url, "title": "Public fixture", "snippet": "A search snippet only.",
    }])), ctx, origin=origin, request_id="original-request",
                        retrieved_at="2026-01-03T00:00:00+00:00")
    return store.record_document(url, url, text.encode(), text, "text/plain", "Fixture")


def decode(result):
    return json.loads(result.stdout)


async def test_partial_interval_union_counts_responses_without_claiming_full_read(tmp_path):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    metadata = save(store, ctx)
    store.record_read(URL, ctx, ok=True, metadata=metadata, offset=0, limit=4)
    store.record_read(URL, ctx, ok=True, cached=True, metadata=metadata, offset=3, limit=4)
    store.record_read(URL, ctx, ok=True, cached=True, metadata=metadata, offset=0, limit=4)
    store.record_read(URL, ctx, ok=False, error="HTTP 403; no bypass")
    before = store.history(limit=100)
    result = await tool.run({"limit": 50}, ctx)
    report = decode(result)
    assert result.ok and report["factual_grade"] is None
    counts = report["counts_for_this_page"]
    assert counts["read_successes"] == 3 and counts["read_failures"] == 1
    assert counts["fresh_read_successes"] == 1 and counts["cached_read_successes"] == 2
    assert counts["distinct_retained_versions"] == 1
    assert counts["distinct_fresh_versions"] == counts["distinct_cached_versions"] == 1
    version, = report["source_versions"]
    assert version["source"]["metadata_sha256"] == metadata["metadata_sha256"]
    assert version["retained_text_characters"] == 10
    assert version["served_intervals"] == [[0, 7]]
    assert version["known_served_characters"] == 7
    assert version["known_coverage_fraction"] == 0.7
    assert version["all_retained_characters_covered_in_this_page"] is False
    assert report["first_known_recorded_at_in_page"] <= report["last_known_recorded_at_in_page"]
    assert store.history(limit=100) == before
    assert "comprehension" in report["note"] and "not authoritative" in report["note"]


async def test_actor_task_filters_precede_limit_and_group_scope_is_explicit(tmp_path):
    store, contexts, tool = setup(tmp_path)
    collector, critic = contexts["collector"], contexts["critic"]
    metadata = save(store, collector)
    store.record_read(URL, collector, ok=True, metadata=metadata, offset=0, limit=3)
    for _ in range(20):
        store.record_read(URL, critic, ok=False, error="Bound actor's public read failure")
    selected = decode(await tool.run({"limit": 1}, collector))
    assert selected["events"][0]["agent_id"] == "collector"
    assert selected["counts_for_this_page"]["read_successes"] == 1
    assert selected["pagination"]["has_more"] is True
    assert selected["pagination"]["complete_selected_history"] is False
    later = critic.model_copy(update={"task_id": "t2"})
    own = decode(await tool.run({}, later))
    assert own["events"] == [] and own["scope"]["selected_task_id"] == "t2"
    shared = decode(await tool.run({"agent_id": "*", "all_tasks": True, "limit": 50}, later))
    assert shared["counts_for_this_page"]["read_successes"] == 1
    assert shared["counts_for_this_page"]["read_failures"] == 20
    assert shared["scope"]["selected_agent_ids"] == ["collector", "critic"]
    assert shared["scope"]["shared_public_evidence_only"] is True
    assert shared["scope"]["selected_task_id"] is None


async def test_versions_never_share_coverage_or_fall_back_to_latest(tmp_path, monkeypatch):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    first = save(store, ctx, "A" * 10)
    store.record_read(URL, ctx, ok=True, metadata=first, offset=0, limit=10)
    second = save(store, ctx, "B" * 20)
    store.record_read(URL, ctx, ok=True, metadata=second, offset=0, limit=3)

    def forbidden(*args, **kwargs):
        raise AssertionError("The audit must not fetch, write an event, or consult latest")

    monkeypatch.setattr(store, "document", forbidden)
    monkeypatch.setattr(store, "document_version", forbidden)
    monkeypatch.setattr(store, "registered", forbidden)
    monkeypatch.setattr(store, "_db", forbidden)
    monkeypatch.setattr(store, "_migrate_documents", forbidden)
    monkeypatch.setattr(store, "record_read", forbidden)
    report = decode(await tool.run({"limit": 50}, ctx))
    versions = {row["source"]["metadata_sha256"]: row for row in report["source_versions"]}
    assert versions[first["metadata_sha256"]]["known_served_characters"] == 10
    assert versions[first["metadata_sha256"]]["all_retained_characters_covered_in_this_page"]
    assert versions[second["metadata_sha256"]]["known_served_characters"] == 3
    assert not versions[second["metadata_sha256"]]["all_retained_characters_covered_in_this_page"]


async def test_unregistered_version_is_unknown_without_writable_lookup(tmp_path, monkeypatch):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    metadata = save(store, ctx)
    store.record_read(URL, ctx, ok=True, metadata=metadata, offset=0, limit=5)
    with store._db() as db:
        db.execute("DELETE FROM sources WHERE url=?", (URL,))
    before = store.database.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Audit lookup must use only URI read-only APIs")

    monkeypatch.setattr(store, "_db", forbidden)
    monkeypatch.setattr(store, "document_version", forbidden)
    monkeypatch.setattr(store, "registered", forbidden)
    result = await tool.run({}, ctx)
    version, = decode(result)["source_versions"]
    assert result.ok and version["source"]["status"] == "unknown"
    assert version["known_served_characters"] is None
    assert store.database.read_bytes() == before


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_unavailable_exact_version_is_unknown_not_zero_or_latest(tmp_path, damage):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    metadata = save(store, ctx)
    store.record_read(URL, ctx, ok=True, metadata=metadata, offset=0, limit=5)
    if damage == "missing":
        with store._db() as db:
            db.execute("DELETE FROM document_versions WHERE metadata_sha256=?",
                       (metadata["metadata_sha256"],))
    else:
        with store._db() as db:
            db.execute("UPDATE raw_blobs SET body=? WHERE sha256=?",
                       (b"corrupt", metadata["raw_sha256"]))
    report = decode(await tool.run({}, ctx))
    version, = report["source_versions"]
    assert report["counts_for_this_page"]["read_successes"] == 1
    assert version["source"]["status"] == "unknown"
    assert version["retained_text_characters"] is None
    assert version["known_served_characters"] is None
    assert version["all_retained_characters_covered_in_this_page"] is None


async def test_legacy_missing_pagination_and_malformed_records_remain_unknown(tmp_path):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    metadata = save(store, ctx)
    store.record_read(URL, ctx, ok=True, metadata=metadata)
    store.record_read(URL, ctx, ok=True, metadata={"url": URL})
    store._event("read", {"agent_id": ctx.agent_id, "task_id": ctx.task_id, "ok": "yes",
                          "recorded_at": "not a timestamp"})
    with store._db() as db:
        db.execute("INSERT INTO events(kind,payload) VALUES('read','{malformed')")
    report = decode(await tool.run({"limit": 50}, ctx))
    assert report["counts_for_this_page"]["read_successes"] == 2
    assert report["counts_for_this_page"]["read_unknown_outcomes"] == 1
    assert report["unattributable_events_in_bound_store_window"] == 1
    assert not report["attribution_complete_for_bound_store_window"]
    exact = next(row for row in report["source_versions"] if row["source"]["status"] != "unknown")
    assert exact["unknown_interval_responses"] == 1
    assert exact["all_retained_characters_covered_in_this_page"] is None
    legacy = next(row for row in report["source_versions"] if row["source"]["status"] == "unknown")
    assert legacy["known_served_characters"] is None


@pytest.mark.parametrize("bad_identifier", ["", " ", "\t", "\u2003", "x" * 201])
async def test_invalid_legacy_actor_or_task_ids_are_unattributable_not_silent_zero(
    tmp_path, bad_identifier,
):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    for actor, task in ((bad_identifier, ctx.task_id), (ctx.agent_id, bad_identifier)):
        store._event("read", {"agent_id": actor, "task_id": task, "ok": True})
    report = decode(await tool.run({"all_tasks": True}, ctx))
    assert report["events"] == []
    assert report["unattributable_events_in_bound_store_window"] == 2
    assert report["attribution_complete_for_bound_store_window"] is False
    assert report["pagination"]["complete_selected_history"] is False


async def test_audit_does_not_construct_store_or_migrate_a_legacy_database(tmp_path):
    # Deliberately bypass __init__: this fixture is a legacy database, not a new native store.
    folder = tmp_path / "legacy"
    folder.mkdir()
    store = object.__new__(SourceEvidenceStore)
    store.root, store.database = folder, folder / "source-evidence.sqlite3"
    payload = {"agent_id": "collector", "task_id": "t1", "ok": True, "url": URL,
               "metadata": {"metadata_sha256": "a" * 64}, "offset": 0, "limit": 5}
    with sqlite3.connect(store.database) as db:
        db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")
        db.execute("INSERT INTO events VALUES(1,'read',?)", (json.dumps(payload),))
    before = store.database.read_bytes()
    ctx = ToolContext(agent_id="collector", task_id="t1", workspace=tmp_path)
    tool = SourceAccessAuditTool(store, workspaces={"collector": tmp_path})
    result = await tool.run({}, ctx)
    assert result.ok
    version, = decode(result)["source_versions"]
    assert version["source"]["status"] == "unknown"
    assert version["known_served_characters"] is None
    assert store.database.read_bytes() == before
    with sqlite3.connect(store.database.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("events",),
        ]


async def test_imported_search_time_is_provenance_not_page_access_or_billing(tmp_path):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    save(store, ctx, origin="imported")
    report = decode(await tool.run({}, ctx))
    assert report["source_versions"] == []
    assert report["counts_for_this_page"]["read_successes"] == 0
    assert report["counts_for_this_page"]["imported_search_events"] == 1
    event, = report["events"]
    assert event["retrieved_at"].startswith("2026-01-03")
    assert event["request_id"] == "original-request"
    assert event["access"].startswith("search_snippets_only")


@pytest.mark.parametrize("args", [
    {"agent_id": "foreign"}, {"task_id": "t1", "all_tasks": True}, {"limit": True},
    {"limit": 51}, {"before_event_id": 0}, {"before_event_id": 2**63},
    {"path": "another-source-store"}, {"document": "model supplied text"},
])
async def test_model_cannot_select_private_paths_or_unbound_actors(tmp_path, args):
    _, contexts, tool = setup(tmp_path)
    result = await tool.run(args, contexts["collector"])
    assert not result.ok and decode(result)["status"] == "rejected"


async def test_binding_is_once_and_checks_actor_workspace_pair_not_only_path(tmp_path):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    assert not (await SourceAccessAuditTool(store).run({}, ctx)).ok
    forged = ctx.model_copy(update={"agent_id": "critic"})
    assert not (await tool.run({}, forged)).ok
    foreign = ctx.model_copy(update={"workspace": tmp_path / "foreign-group"})
    assert not (await tool.run({}, foreign)).ok
    with pytest.raises(ValueError, match="already bound"):
        tool.bind_workspaces({"collector": ctx.workspace})


async def test_output_budget_keeps_valid_json_and_resumable_stable_event_cursor(tmp_path):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    for number in range(8):
        url = "https://example.test/" + "x" * 3000 + str(number)
        metadata = save(store, ctx, "Saved fixture text", url=url)
        store.record_read(url, ctx, ok=True, metadata=metadata, offset=0, limit=10)
    result = await tool.run({"limit": 50}, ctx)
    report = decode(result)
    assert result.ok and result.truncated and len(result.stdout.encode()) <= 32768
    assert report["pagination"]["has_more"]
    assert not report["pagination"]["complete_selected_history"]
    cursor = report["pagination"]["next_before_event_id"]
    # Later appended events must not shift the continuation or repeat earlier events.
    store.record_read(URL, ctx, ok=False, error="Later public event")
    later = decode(await tool.run({"limit": 50, "before_event_id": cursor}, ctx))
    first_ids = {event["event_id"] for event in report["events"]}
    assert first_ids.isdisjoint(event["event_id"] for event in later["events"])
    assert all(event["event_id"] < cursor for event in later["events"])


async def test_single_oversized_event_is_explicit_unknown_with_lossless_older_cursor(
    tmp_path, monkeypatch,
):
    store, contexts, tool = setup(tmp_path)
    ctx = contexts["collector"]
    metadata = save(store, ctx)
    store.record_read(URL, ctx, ok=True, metadata=metadata, offset=0, limit=5)
    long_url = "https://example.test/" + "x" * 3000
    later = save(store, ctx, url=long_url)
    store.record_read(long_url, ctx, ok=True, metadata=later, offset=0, limit=5)
    with store._db() as db:
        expected_ids = {row[0] for row in db.execute("SELECT id FROM events")}
    monkeypatch.setattr(source_audit, "_MAX_OUTPUT_BYTES", 4096)
    first = await tool.run({"limit": 50}, ctx)
    report = decode(first)
    assert not first.ok and first.truncated and report["status"] == "unknown"
    assert report["reason"] == "single_event_details_exceed_output_bound"
    assert report["events"][0]["details_omitted"] is True
    assert report["coverage_unavailable_due_to_output_bound"] is True
    assert report["pagination"]["has_more"] is True
    assert report["pagination"]["complete_selected_history"] is False
    assert len(first.stdout.encode()) <= 4096
    seen = set()
    for _ in range(len(expected_ids) + 1):
        ids = {row["event_id"] for row in report["events"]}
        assert seen.isdisjoint(ids)
        seen.update(ids)
        cursor = report["pagination"]["next_before_event_id"]
        if cursor is None:
            break
        assert cursor == min(ids)
        following = await tool.run({"limit": 50, "before_event_id": cursor}, ctx)
        assert len(following.stdout.encode()) <= 4096
        report = decode(following)
    else:
        pytest.fail("bounded source audit did not terminate")
    assert seen == expected_ids
