"""Actual artifact bytes, rather than caller-supplied prose, determine lexical checks."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from moumou.adapters import GitWorkspaces
from moumou.contracts import ToolContext, ToolResult
from moumou.eval.source_evidence import SourceEvidenceStore, digest
from moumou.tools import artifact_quote
from moumou.tools.artifact_quote import VerifyArtifactQuoteTool

URL = "https://example.test/retained"


def setup(tmp_path, source="An actual source quotation."):
    workspace = tmp_path / "opaque-provider/worktrees/current-483"
    workspace.mkdir(parents=True)
    store = SourceEvidenceStore(tmp_path / "evidence")
    ctx = ToolContext(agent_id="editor", task_id="t1.edit", workspace=workspace)
    store.record_search("Fixture", ToolResult(ok=True, stdout=json.dumps([
        {"url": URL, "title": "Fixture", "snippet": "Not the actual source body."},
    ])), ctx, request_id="fixture", retrieved_at="2030-01-02T00:00:00+00:00")
    metadata = store.record_document(URL, URL, source.encode(), source, "text/plain", "Fixture")
    tool = VerifyArtifactQuoteTool(store, workspaces=[workspace])
    return store, ctx, tool, metadata


def request(metadata, text, **overrides):
    return {"metadata_sha256": metadata["metadata_sha256"], "path": "article.md",
            "start": 0, "end": len(text), **overrides}


def decode(result):
    return json.loads(result.stdout)


async def test_actual_unicode_slice_receipt_has_file_and_byte_coordinate_identity(tmp_path):
    quote = "来源说：明年预计增长12.5%😀。"
    store, ctx, tool, metadata = setup(tmp_path, "前言\r\n" + quote + "\n后记")
    body = "# 标题😀\r\n" + quote + "\r\n编辑备注"
    raw = body.encode("utf-8")
    (ctx.workspace / "article.md").write_bytes(raw)
    start = body.index(quote)
    history = store.history(limit=30)
    result = await tool.run(request(metadata, quote, start=start, end=start + len(quote)), ctx)
    payload = decode(result)
    assert result.ok and payload["status"] == "matched" and payload["lexical_match"] is True
    receipt = payload["artifact"]
    assert receipt["sha256"] == digest(raw) and receipt["bytes"] == len(raw)
    assert receipt["path"] == "article.md" and receipt["characters"] == len(body)
    assert receipt["quote_sha256"] == payload["quote_sha256"] == digest(quote.encode())
    assert receipt["span"]["start"] == start
    assert receipt["span"]["end"] == start + len(quote)
    assert receipt["span"]["byte_start"] == len(body[:start].encode())
    assert raw[receipt["span"]["byte_start"]:receipt["span"]["byte_end"]] == quote.encode()
    assert receipt["stable_at_checks"] is True and result.artifacts == []
    assert payload["source"]["metadata_sha256"] == metadata["metadata_sha256"]
    assert (ctx.workspace / "article.md").read_bytes() == raw
    assert store.history(limit=30) == history
    assert quote not in result.stdout and "evidence_excerpt" not in payload


async def test_caller_cannot_substitute_correct_quote_for_different_disk_text(tmp_path):
    _, ctx, tool, metadata = setup(tmp_path, "will increase 12.5%")
    actual = "has decreased 92.5%"
    (ctx.workspace / "article.md").write_text(actual, encoding="utf-8")
    forged = await tool.run({**request(metadata, actual), "quote": "will increase 12.5%"}, ctx)
    assert not forged.ok and decode(forged)["status"] == "rejected"
    actual_check = await tool.run(request(metadata, actual), ctx)
    assert actual_check.ok and decode(actual_check)["status"] == "not_found"
    assert decode(actual_check)["lexical_match"] is False


async def test_whitespace_mode_keeps_real_artifact_hash_and_source_derived_coordinates(tmp_path):
    _, ctx, tool, metadata = setup(tmp_path, "Preface. A\tquotation\nwith whitespace.")
    actual = "A quotation\r\nwith  whitespace."
    raw = actual.encode()
    (ctx.workspace / "article.md").write_bytes(raw)
    exact = decode(await tool.run(request(metadata, actual), ctx))
    collapsed = decode(await tool.run(request(metadata, actual, mode="collapse_whitespace"), ctx))
    assert exact["status"] == "not_found" and collapsed["status"] == "matched"
    assert collapsed["artifact"]["sha256"] == digest(raw)
    assert collapsed["quote_sha256"] == digest(raw)
    assert collapsed["comparison_quote_sha256"] == digest(b"A quotation with whitespace.")
    assert collapsed["coordinates"]["space"] == "whitespace_normalized_retained_extractor_text"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path-stat/handle-stat distinction")
async def test_native_windows_stable_file_accepts_distinct_creation_and_modification_times(
    tmp_path,
):
    text = "An actual source quotation."
    _, ctx, tool, metadata = setup(tmp_path, text)
    path = ctx.workspace / "article.md"
    path.write_text(text, encoding="utf-8")
    # Deliberately separate mtime from creation time without timing-dependent sleeps.
    os.utime(path, ns=(1577836800000000000, 1577836800000000000))
    for _ in range(2):
        result = await tool.run(request(metadata, text), ctx)
        assert result.ok and decode(result)["status"] == "matched"
        assert decode(result)["artifact"]["stable_at_checks"] is True


async def test_later_rewrite_is_new_identity_and_stale_expected_hash_never_reaches_source(
    tmp_path, monkeypatch,
):
    store, ctx, tool, metadata = setup(tmp_path)
    path = ctx.workspace / "article.md"
    text = "An actual source quotation."
    path.write_text(text, encoding="utf-8")
    first = decode(await tool.run(request(metadata, text), ctx))
    path.write_text("Different wording of equal!", encoding="utf-8")

    def no_source(*args):
        raise AssertionError("A stale artifact hash must be rejected before source lookup")

    monkeypatch.setattr(store, "document_version", no_source)
    stale = await tool.run(request(metadata, text,
                                   expected_artifact_sha256=first["artifact"]["sha256"]), ctx)
    assert not stale.ok and decode(stale)["reason"] == "artifact_sha256_mismatch"
    assert decode(stale)["lexical_match"] is None
    saved_receipt = tool.audit_records()["records"][0]
    assert saved_receipt["artifact"]["sha256"] == first["artifact"]["sha256"]
    assert saved_receipt["artifact"]["sha256"] != digest(path.read_bytes())


@pytest.mark.parametrize("atomic", [False, True])
async def test_rewrite_during_source_check_is_unknown_even_with_same_size_or_same_bytes(
    tmp_path, monkeypatch, atomic,
):
    store, ctx, tool, metadata = setup(tmp_path)
    path = ctx.workspace / "article.md"
    original = b"An actual source quotation."
    path.write_bytes(original)
    read_source = store.document_version

    def change_after_artifact_read(key):
        saved = read_source(key)
        if atomic:
            replacement = ctx.workspace / "replacement.md"
            replacement.write_bytes(original)
            os.replace(replacement, path)
        else:
            path.write_bytes(b"X" * len(original))
        return saved

    monkeypatch.setattr(store, "document_version", change_after_artifact_read)
    result = await tool.run(request(metadata, original.decode()), ctx)
    payload = decode(result)
    assert not result.ok and payload["status"] == "unknown"
    assert payload["lexical_match"] is None
    assert payload["reason"] == "artifact_changed_during_verification"
    assert payload["artifact"]["stable_at_checks"] is False


@pytest.mark.parametrize("damage", ["metadata", "raw", "text", "missing"])
async def test_corrupt_exact_source_remains_unknown_with_actual_artifact_identity(tmp_path, damage):
    store, ctx, tool, metadata = setup(tmp_path)
    text = "An actual source quotation."
    (ctx.workspace / "article.md").write_text(text, encoding="utf-8")
    with sqlite3.connect(store.database) as db:
        if damage == "metadata":
            db.execute("UPDATE document_versions SET payload='{}' WHERE metadata_sha256=?",
                       (metadata["metadata_sha256"],))
        elif damage == "raw":
            db.execute("UPDATE raw_blobs SET body=? WHERE sha256=?",
                       (b"bad", metadata["raw_sha256"]))
        elif damage == "text":
            db.execute("UPDATE text_blobs SET text=? WHERE sha256=?",
                       ("bad", metadata["text_sha256"]))
        else:
            db.execute("DELETE FROM document_versions WHERE metadata_sha256=?",
                       (metadata["metadata_sha256"],))
    result = await tool.run(request(metadata, text), ctx)
    payload = decode(result)
    assert not result.ok and payload["status"] == "unknown" and payload["lexical_match"] is None
    assert payload["artifact"]["sha256"] == digest(text.encode())
    assert payload["artifact"]["stable_at_checks"] is True


@pytest.mark.parametrize("path", [
    "../other/article.md", "/absolute.md", "C:\\outside.md", "\\\\host\\share\\x",
    "article.md:stream", "article.md.", "article.md ", "CON", "nested/NUL.txt",
    ".git/config", ".GIT", ".evaluation/answers.json", "nested/.EVALUATION/x",
    ".env", ".ENV.local", ".env.example", "nested/.env.secret", "./article.md",
])
async def test_private_foreign_alias_and_windows_paths_rejected_before_source_lookup(
    tmp_path, monkeypatch, path,
):
    store, ctx, tool, metadata = setup(tmp_path)

    def no_source(*args):
        raise AssertionError("Private/foreign paths must not access the retained source store")

    monkeypatch.setattr(store, "document_version", no_source)
    result = await tool.run(request(metadata, "quote", path=path), ctx)
    assert not result.ok and decode(result)["status"] == "rejected"
    record = tool.audit_records()["records"][0]
    assert record["artifact"] is None and path not in result.stdout


@pytest.mark.parametrize("path", [
    ".aws/credentials", "nested/.SSH/config", ".codex/session.json", "id_rsa", "id_ed25519",
    "nested/credentials.json", "privatekey.pem", "nested/key.KEY", "client.p12", "bundle.PFX",
    ".envrc", "nested/.ENV_backup", ".env.example",
])
async def test_existing_credential_files_are_not_read_or_hashed_or_journalled(
    tmp_path, monkeypatch, path,
):
    store, ctx, tool, metadata = setup(tmp_path)
    private_file = ctx.workspace.joinpath(*path.split("/"))
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_text("DUMMY PRIVATE FIXTURE; never emit or hash this", encoding="utf-8")

    def no_read(*args):
        raise AssertionError("Credential paths must be rejected before artifact or source reads")

    monkeypatch.setattr(artifact_quote, "_snapshot", no_read)
    monkeypatch.setattr(store, "document_version", no_read)
    result = await tool.run(request(metadata, "DUMMY", path=path), ctx)
    assert not result.ok and decode(result)["reason"] == "private_artifact_path_refused"
    record = tool.audit_records()["records"][0]
    assert record["artifact"] is None
    assert "DUMMY PRIVATE" not in result.stdout + json.dumps(record)
    assert str(private_file) not in result.stdout + json.dumps(record)


async def test_legitimate_credentials_document_name_remains_readable(tmp_path):
    text = "An actual source quotation."
    _, ctx, tool, metadata = setup(tmp_path, text)
    (ctx.workspace / "credentials.md").write_text(text, encoding="utf-8")
    result = await tool.run(request(metadata, text, path="credentials.md"), ctx)
    assert result.ok and decode(result)["status"] == "matched"


@pytest.mark.parametrize("overrides", [
    {"start": True}, {"end": False}, {"start": -1}, {"start": 1, "end": 1},
    {"start": 0, "end": 4097}, {"end": 1.5}, {"mode": []}, {"mode": "fuzzy"},
    {"expected_artifact_sha256": None}, {"expected_artifact_sha256": "A" * 64},
    {"metadata_sha256": "0" * 63}, {"quote": "caller supplied"}, {"check_id": "forged"},
])
async def test_invalid_schema_and_forged_receipts_rejected(tmp_path, overrides):
    _, ctx, tool, metadata = setup(tmp_path)
    result = await tool.run(request(metadata, "quote", **overrides), ctx)
    assert not result.ok and decode(result)["status"] == "rejected"
    assert decode(result)["check_id"] != "forged"


async def test_bounds_invalid_utf8_missing_directory_and_empty_span_are_not_false_matches(tmp_path):
    _, ctx, tool, metadata = setup(tmp_path)
    path = ctx.workspace / "article.md"
    missing = decode(await tool.run(request(metadata, "x"), ctx))
    assert missing["status"] == "unknown" and missing["lexical_match"] is None
    directory = ctx.workspace / "directory"
    directory.mkdir()
    folder = decode(await tool.run(request(metadata, "x", path="directory"), ctx))
    assert folder["status"] == "rejected" and folder["lexical_match"] is None
    path.write_bytes(b"\xff")
    invalid = decode(await tool.run(request(metadata, "x"), ctx))
    assert invalid["status"] == "unknown" and invalid["lexical_match"] is None
    path.write_text("short", encoding="utf-8")
    outside = decode(await tool.run(request(metadata, "short", end=6), ctx))
    assert outside["status"] == "rejected" and outside["reason"] == "artifact_span_out_of_bounds"
    path.write_bytes(b" " * 10)
    blank = decode(await tool.run(request(metadata, " " * 10), ctx))
    assert blank["status"] == "rejected"
    path.write_bytes(b"x" * (artifact_quote._MAX_FILE_BYTES + 1))
    too_large = decode(await tool.run(request(metadata, "x"), ctx))
    assert too_large["status"] == "unknown" and too_large["lexical_match"] is None


async def test_file_changing_during_snapshot_is_rejected_before_source_read(tmp_path, monkeypatch):
    store, ctx, tool, metadata = setup(tmp_path)
    path = ctx.workspace / "article.md"
    original = b"An actual source quotation."
    path.write_bytes(original)
    real_fstat = os.fstat
    calls = 0

    def change_before_after_stat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"Y" * len(original))
        return real_fstat(fd)

    def no_source(*args):
        raise AssertionError("An unstable artifact must not reach source verification")

    monkeypatch.setattr(artifact_quote.os, "fstat", change_before_after_stat)
    monkeypatch.setattr(store, "document_version", no_source)
    result = await tool.run(request(metadata, original.decode()), ctx)
    assert not result.ok and decode(result)["status"] == "unknown"
    assert decode(result)["reason"] == "artifact_changed_during_read"


async def test_handle_ctime_change_remains_detected_even_if_common_identity_is_stable(
    tmp_path, monkeypatch,
):
    _, ctx, tool, metadata = setup(tmp_path)
    text = "An actual source quotation."
    (ctx.workspace / "article.md").write_text(text, encoding="utf-8")
    real_fstat, calls = os.fstat, 0

    def changed_ctime(fd):
        nonlocal calls
        calls += 1
        info = real_fstat(fd)
        if calls != 2:
            return info
        fields = {name: getattr(info, name) for name in (
            "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink",
        )}
        fields["st_birthtime_ns"] = getattr(info, "st_birthtime_ns", None)
        fields["st_ctime_ns"] += 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(artifact_quote.os, "fstat", changed_ctime)
    result = await tool.run(request(metadata, text), ctx)
    assert not result.ok and decode(result)["reason"] == "artifact_changed_during_read"


async def test_journal_is_tool_owned_bounded_content_free_and_deep_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_quote, "_MAX_AUDIT_RECORDS", 3)
    _, ctx, tool, metadata = setup(tmp_path)
    text = "An actual source quotation."
    (ctx.workspace / "article.md").write_text(text, encoding="utf-8")
    replies = [decode(await tool.run(request(metadata, text), ctx)) for _ in range(5)]
    journal = tool.audit_records()
    assert journal["dropped_records"] == tool.audit_dropped_records == 2
    assert len(journal["records"]) == 3
    assert len({reply["check_id"] for reply in replies}) == 5
    record = journal["records"][-1]
    assert record["check_id"] == replies[-1]["check_id"]
    assert record["agent_id"] == ctx.agent_id and record["task_id"] == ctx.task_id
    assert record["artifact"]["sha256"] == digest(text.encode())
    assert record["source_check"]["source"]["metadata_sha256"] == metadata["metadata_sha256"]
    assert record["source_check"]["status"] == "matched"
    assert text not in json.dumps(journal) and str(ctx.workspace) not in json.dumps(journal)
    record["artifact"]["sha256"] = "forged"
    journal["records"].clear()
    assert len(tool.audit_records()["records"]) == 3
    assert tool.audit_records()["records"][-1]["artifact"]["sha256"] == digest(text.encode())


async def test_unbound_and_foreign_contexts_fail_and_opaque_git_provider_paths_bind(tmp_path):
    store, ctx, _, metadata = setup(tmp_path)
    tool = VerifyArtifactQuoteTool(store)
    unbound = decode(await tool.run(request(metadata, "quote"), ctx))
    assert unbound["reason"] == "workspace_binding_unavailable"
    provider = GitWorkspaces(tmp_path / "managed")
    workspace = provider.allocate("project", "group", "editor")
    assert (workspace / ".git").is_file()
    tool.bind_workspaces([workspace])
    text = "An actual source quotation."
    (workspace / "article.md").write_text(text, encoding="utf-8")
    actual = ctx.model_copy(update={"workspace": workspace})
    assert decode(await tool.run(request(metadata, text), actual))["status"] == "matched"
    foreign = await tool.run(request(metadata, text), ctx)
    assert not foreign.ok and decode(foreign)["reason"] == "outside_bound_workspace_scope"
    with pytest.raises(ValueError, match="unbound collection"):
        tool.bind_workspaces([workspace])


async def test_hardlinks_are_refused_and_symlinks_checked_when_creation_available(tmp_path):
    _, ctx, tool, metadata = setup(tmp_path)
    outside = tmp_path / "foreign.md"
    outside.write_text("An actual source quotation.", encoding="utf-8")
    path = ctx.workspace / "article.md"
    os.link(outside, path)
    hardlink = decode(await tool.run(request(metadata, outside.read_text()), ctx))
    assert hardlink["status"] == "rejected" and hardlink["lexical_match"] is None
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        return  # Native Windows may require Developer Mode; hard-link refusal still ran.
    symlink = decode(await tool.run(request(metadata, outside.read_text()), ctx))
    assert symlink["status"] == "rejected" and symlink["lexical_match"] is None


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory junction")
async def test_real_windows_junction_cannot_import_another_workspace_directory(tmp_path):
    _, ctx, tool, metadata = setup(tmp_path)
    outside = tmp_path / "foreign-directory"
    outside.mkdir()
    (outside / "article.md").write_text("An actual source quotation.", encoding="utf-8")
    junction = ctx.workspace / "linked"
    assert junction.parent.resolve().is_relative_to(tmp_path.resolve())
    assert outside.resolve().is_relative_to(tmp_path.resolve())
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                   check=True, capture_output=True, text=True)
    try:
        result = await tool.run(request(metadata, "An actual source quotation.",
                                        path="linked/article.md"), ctx)
        assert not result.ok and decode(result)["status"] == "rejected"
        assert decode(result)["lexical_match"] is None
    finally:
        # Remove only this checked temporary junction, never recurse into its target.
        assert junction.absolute().parent == ctx.workspace.absolute()
        junction.rmdir()
