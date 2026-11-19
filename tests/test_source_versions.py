"""Immutable source retrieval evidence, using isolated stores and offline transports."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from moumou.contracts import ToolContext, ToolResult
from moumou.eval.source_evidence import SourceEvidenceStore, digest
from moumou.tools.source import ReadSourceTool

URL = "https://example.test/article"


def save(store, body=b"first", text="first", **kwargs):
    return store.record_document(URL, URL, body, text, "text/plain", "Title", **kwargs)


def counts(store):
    with sqlite3.connect(store.database) as db:
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("raw_blobs", "text_blobs", "document_versions", "document_cache")}


def test_changed_url_retains_exact_earlier_bytes_text_and_hashed_metadata(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    first = save(store)
    second = save(store, b"second", "second")
    assert store.document(URL) == (second, "second")
    assert store.document_version(first["metadata_sha256"]) == (first, "first")
    assert store.raw_blob(first["raw_sha256"]) == b"first"
    assert store.text_blob(first["text_sha256"]) == "first"
    assert first["retrieval_id"] != second["retrieval_id"]
    with sqlite3.connect(store.database) as db:
        raw_metadata = db.execute("SELECT payload FROM document_versions WHERE metadata_sha256=?",
                                  (first["metadata_sha256"],)).fetchone()[0]
    assert digest(raw_metadata.encode()) == first["metadata_sha256"]
    assert "metadata_sha256" not in json.loads(raw_metadata)  # no self-referential hash


def test_changed_html_same_text_and_repeated_gets_deduplicate_only_blobs(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    first = save(store, b"<p>Text</p>", "Text")
    second = save(store, b"<p class='new'>Text</p>", "Text")
    third = save(store, b"<p class='new'>Text</p>", "Text")
    assert len({item["retrieval_id"] for item in (first, second, third)}) == 3
    assert counts(store) == {"raw_blobs": 2, "text_blobs": 1,
                             "document_versions": 3, "document_cache": 1}
    assert all(store.document_version(item["metadata_sha256"])[1] == "Text"
               for item in (first, second, third))


def test_transaction_failure_does_not_lose_previous_version_or_publish_partial_one(tmp_path,
                                                                                  monkeypatch):
    store = SourceEvidenceStore(tmp_path)
    first = save(store)
    old_counts = counts(store)
    real_retain = store._retain

    def fail_after_pointer(*args):
        real_retain(*args)
        raise ValueError("injected before commit")

    monkeypatch.setattr(store, "_retain", fail_after_pointer)
    with pytest.raises(ValueError, match="before commit"):
        save(store, b"second", "second")
    assert counts(store) == old_counts and store.document(URL) == (first, "first")
    assert store.raw_blob(digest(b"second")) is None


def test_concurrent_refreshes_retain_every_successful_observation(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(lambda i: save(store, str(i).encode(), str(i)), range(8)))
    assert counts(store)["document_versions"] == 8
    assert all(store.document_version(item["metadata_sha256"])[0] == item for item in versions)
    assert store.document(URL)[0] in versions


def test_corrupt_existing_blob_is_rejected_without_overwrite(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    with sqlite3.connect(store.database) as db:
        db.execute("INSERT INTO raw_blobs VALUES(?,?,?)", (digest(b"first"), b"wrong", 5))
    with pytest.raises(ValueError, match="immutable source blob"):
        save(store)
    assert counts(store)["document_versions"] == 0
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT body FROM raw_blobs").fetchone()[0] == b"wrong"


def test_legacy_backfill_retains_only_real_current_row_and_is_idempotent(tmp_path):
    legacy = {"url": URL, "final_url": URL, "retrieved_at": "2026-01-03T00:00:00+00:00",
              "raw_sha256": digest(b"current"), "text_sha256": digest(b"current"),
              "raw_bytes": 7, "text_characters": 7, "content_type": "text/plain", "title": "Old"}
    database = tmp_path / "source-evidence.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE documents(url TEXT PRIMARY KEY,payload TEXT,body BLOB,text TEXT)")
        db.execute("INSERT INTO documents VALUES(?,?,?,?)",
                   (URL, json.dumps(legacy), b"current", "current"))
    store = SourceEvidenceStore(tmp_path)
    retained, text = store.document(URL)
    assert text == "current" and retained["retrieved_at"] == legacy["retrieved_at"]
    assert retained["retention_origin"] == "legacy_current_row_retained"
    assert retained["extractor_version"] == "legacy_unknown"
    assert retained["retained_at"] != retained["retrieved_at"]
    assert store.raw_blob(digest(b"lost older version")) is None
    assert "unknown" in store.retention_status()["before_start_completeness"]
    assert SourceEvidenceStore(tmp_path).document(URL) == (retained, text)
    assert counts(store)["document_versions"] == 1


@pytest.mark.parametrize("key", ["../file", "A" * 64, "0" * 63, "' OR 1=1--"])
def test_hash_getters_do_not_fall_back_to_latest_or_accept_path_sql_keys(tmp_path, key):
    store = SourceEvidenceStore(tmp_path)
    save(store)
    for getter in (store.raw_blob, store.text_blob, store.document_version):
        with pytest.raises(ValueError, match="SHA256"):
            getter(key)
    assert store.document_version("0" * 64) is None


def test_metadata_or_blob_corruption_is_detected_on_read(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    version = save(store)
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE document_versions SET payload='{}' WHERE metadata_sha256=?",
                   (version["metadata_sha256"],))
    with pytest.raises(ValueError, match="metadata hash"):
        store.document(URL)


async def test_ttl_refresh_cache_pagination_and_variant_bind_to_immutable_metadata(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    ctx = ToolContext(agent_id="research", task_id="t1", workspace=tmp_path)
    original = "https://arxiv.org/pdf/2501.12345v2"
    store.record_search("Query", ToolResult(ok=True, stdout=json.dumps([
        {"url": original, "title": "Paper", "snippet": "Search snippet"},
    ])), ctx, retrieved_at="2026-01-03T00:00:00+00:00", request_id="original-request")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"},
                              text="First paper" if len(calls) == 1 else "New paper")

    tool = ReadSourceTool(store, resolver=lambda *args: ["93.184.216.34"],
                          transport=httpx.MockTransport(handle))
    url = "https://arxiv.org/html/2501.12345v2"
    first = json.loads((await tool.run({"url": url, "limit": 5}, ctx)).stdout)
    cached = json.loads((await tool.run({"url": url, "offset": 5}, ctx)).stdout)
    assert len(calls) == 1 and cached["cached"] and counts(store)["document_versions"] == 1
    assert first["metadata_sha256"] == cached["metadata_sha256"]
    assert first["retrieved_at"] == cached["retrieved_at"]
    version, _ = store.document_version(first["metadata_sha256"])
    assert version["variant_of"] == original and version["source_request_id"] == "original-request"
    assert version["source_search_retrieved_at"].startswith("2026-01-03")
    tool.cache_ttl_s = 0
    refreshed = json.loads((await tool.run({"url": url}, ctx)).stdout)
    assert len(calls) == 2 and not refreshed["cached"]
    assert store.document_version(first["metadata_sha256"])[1] == "First paper"
    assert store.document_version(refreshed["metadata_sha256"])[1] == "New paper"
    reads = [event for event in store.history(limit=30)["events"] if event["kind"] == "read"]
    assert all(store.document_version(event["metadata"]["metadata_sha256"]) for event in reads)
    assert any(event["offset"] == 5 for event in reads)


async def test_failed_refresh_retains_old_version_without_serving_stale_as_fresh(tmp_path):
    store = SourceEvidenceStore(tmp_path)
    ctx = ToolContext(agent_id="research", task_id="t1", workspace=tmp_path)
    store.record_search("Query", ToolResult(ok=True, stdout=json.dumps([
        {"url": URL, "title": "Source", "snippet": "Excerpt"},
    ])), ctx)
    old = save(store)
    tool = ReadSourceTool(store, resolver=lambda *args: ["93.184.216.34"], cache_ttl_s=0,
                          transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    result = await tool.run({"url": URL}, ctx)
    assert not result.ok and "403" in result.error
    assert store.document(URL) == (old, "first")
    assert counts(store)["document_versions"] == 1
    assert store.history()["events"][0]["ok"] is False


def test_store_bounds_and_aliases_are_not_bypassed_by_retention(tmp_path):
    store = SourceEvidenceStore(tmp_path / "real")
    with pytest.raises(ValueError, match="size"):
        save(store, b"x" * (2 * 1024 * 1024 + 1), "Text")
    assert counts(store)["document_versions"] == 0
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(store.root, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a Windows symlink requires a privilege unavailable here")
    with pytest.raises(ValueError, match="aliases"):
        SourceEvidenceStore(alias)
