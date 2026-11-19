"""Bind actual tool-owned quote receipts to sealed files, not mutable workspace prose."""

from __future__ import annotations

import copy
import json

import pytest

from moumou.contracts import TaskNode, TaskResult, ToolContext, ToolResult
from moumou.eval import quote_delivery
from moumou.eval.quote_delivery import capture_artifact_quote_evidence
from moumou.eval.session_evidence import capture_artifact_snapshot, validate_artifact_snapshot
from moumou.eval.source_evidence import SourceEvidenceStore, digest
from moumou.tools import artifact_quote
from moumou.tools.artifact_quote import VerifyArtifactQuoteTool

URL = "https://example.test/public-source"
TEXT = "预期增长12.5%😀，这是原文。"


async def prepare(tmp_path, *, text=TEXT, source=None, mode="exact", task_id="t1.edit",
                  missing_source=False, filename="article.md"):
    scenario, workspace = tmp_path / "scenario_1", tmp_path / "child-workspace"
    scenario.mkdir()
    workspace.mkdir()
    store = SourceEvidenceStore(tmp_path / "public-evidence")
    ctx = ToolContext(agent_id="editor", task_id=task_id, workspace=workspace)
    store.record_search("Fixture", ToolResult(ok=True, stdout=json.dumps([{
        "url": URL, "title": "Public fixture", "snippet": "Only a search snippet.",
    }])), ctx, request_id="fixture", retrieved_at="2026-01-03T00:00:00+00:00")
    source = text if source is None else source
    metadata = store.record_document(URL, URL, source.encode(), source, "text/plain", "Fixture")
    body = "# 标题😀\r\n" + text + "\r\n尾注"
    (workspace / filename).write_bytes(body.encode())
    tool = VerifyArtifactQuoteTool(store, workspaces=[workspace])
    start = body.index(text)
    args = {"metadata_sha256": metadata["metadata_sha256"], "path": filename,
            "start": start, "end": start + len(text), "mode": mode}
    if missing_source:
        with store._db() as db:
            db.execute("DELETE FROM document_versions WHERE metadata_sha256=?",
                       (metadata["metadata_sha256"],))
    await tool.run(args, ctx)
    return {"scenario": scenario, "workspace": workspace, "store": store, "ctx": ctx,
            "tool": tool, "args": args, "body": body,
            "task": {"phase": "task_1", "root_task_id": "t1"}}


def seal(case, *, workspace=None, files=None):
    node = TaskNode(id=case["task"]["root_task_id"], project_id="scenario_1",
                    conversation_id="group", goal="Fixture delivery", role="lead",
                    result=TaskResult(files=files or ["article.md"]))
    return capture_artifact_snapshot(case["scenario"], workspace or case["workspace"],
                                     node, case["task"]["phase"])


def capture(case, reference, *, journal=None, task_ids=None, secrets=()):
    result = capture_artifact_quote_evidence(
        case["scenario"], reference, case["task"],
        case["tool"].audit_records() if journal is None else journal,
        task_ids={"t1", "t1.edit"} if task_ids is None else task_ids, secrets=secrets,
    )
    path = case["scenario"] / result["path"]
    raw = path.read_bytes()
    assert digest(raw) == result["sha256"]
    return result, json.loads(raw)


async def test_actual_unicode_receipt_binds_full_blob_and_slice_without_prose_or_grading(tmp_path):
    case = await prepare(tmp_path)
    result, evidence = capture(case, seal(case))
    assert result["status"] == "recorded"
    assert evidence["counts"] == {"matched": 1, "not_found": 0, "unknown": 0, "unapplied": 0}
    record, = evidence["records"]
    assert record["artifact"]["sha256"] == digest(case["body"].encode())
    assert record["artifact"]["quote_sha256"] == digest(TEXT.encode())
    assert record["source_check"]["quote_sha256"] == record["artifact"]["quote_sha256"]
    assert record["source_check"]["coordinates"]["space"] == "retained_extractor_text"
    assert record["manager_read_or_verification"] is None
    assert evidence["quotation_coverage"] is evidence["factually_correct"] is None
    assert evidence["quality_pass"] is None
    encoded = json.dumps(evidence, ensure_ascii=False)
    assert TEXT not in encoded and str(case["workspace"]) not in encoded


async def test_stale_whole_file_with_same_quote_is_unapplied(tmp_path):
    case = await prepare(tmp_path)
    (case["workspace"] / "article.md").write_text(case["body"] + "\nLater edit", encoding="utf-8")
    _, evidence = capture(case, seal(case))
    assert evidence["counts"]["matched"] == 0 and evidence["counts"]["unapplied"] == 1
    assert evidence["records"][0]["reason"] == "final_whole_file_identity_absent"


async def test_edits_after_snapshot_do_not_replace_the_immutable_delivery(tmp_path):
    case = await prepare(tmp_path)
    reference = seal(case)
    (case["workspace"] / "article.md").write_text("Mutable workspace changed", encoding="utf-8")
    _, evidence = capture(case, reference)
    assert evidence["counts"]["matched"] == 1


async def test_byte_identical_child_copy_keeps_actual_actor_task_and_path_origin(tmp_path):
    case = await prepare(tmp_path)
    manager = tmp_path / "manager-workspace"
    manager.mkdir()
    (manager / "final-report.md").write_bytes((case["workspace"] / "article.md").read_bytes())
    _, evidence = capture(case, seal(case, workspace=manager, files=["final-report.md"]))
    record, = evidence["records"]
    assert record["classification"] == "matched"
    assert (record["agent_id"], record["task_id"], record["artifact"]["path"]) == (
        "editor", "t1.edit", "article.md",
    )
    assert record["delivered_artifact_paths"] == ["final-report.md"]
    assert record["binding"] == "whole_file_content_identity_only"
    assert record["manager_read_or_verification"] is None


async def test_whitespace_mode_checks_derived_quote_hash_without_rewriting_real_file_hash(tmp_path):
    case = await prepare(tmp_path, text="A quote\r\nwith  whitespace.",
                         source="Preface. A\tquote\nwith whitespace.", mode="collapse_whitespace")
    _, evidence = capture(case, seal(case))
    record, = evidence["records"]
    assert record["classification"] == "matched"
    assert record["source_check"]["comparison_quote_sha256"] == digest(
        b"A quote with whitespace.")
    assert record["artifact"]["sha256"] == digest(case["body"].encode())


async def test_valid_negative_is_not_found_without_becoming_a_factual_failure(tmp_path):
    case = await prepare(tmp_path, source="A different sentence.")
    _, negative = capture(case, seal(case))
    assert negative["counts"]["not_found"] == 1 and negative["counts"]["matched"] == 0
    assert negative["records"][0]["source_check"]["lexical_match"] is False


async def test_actual_unknown_source_remains_unknown_without_latest_version_fallback(tmp_path):
    case = await prepare(tmp_path, missing_source=True)
    _, evidence = capture(case, seal(case))
    assert evidence["counts"]["unknown"] == 1 and evidence["counts"]["matched"] == 0
    assert evidence["records"][0]["source_check"] is None


async def test_unavailable_journal_keeps_counts_unknown_instead_of_zero(tmp_path):
    case = await prepare(tmp_path)
    result = capture_artifact_quote_evidence(case["scenario"], seal(case), case["task"], None,
                                              task_ids={"t1", "t1.edit"})
    evidence = json.loads((case["scenario"] / result["path"]).read_bytes())
    assert evidence["status"] == "incomplete" and evidence["journal_status"] == "malformed"
    assert evidence["observed_record_count"] is None
    assert all(value is None for value in evidence["counts"].values())


@pytest.mark.parametrize("field", ["codepoints", "byte_span", "slice_hash", "quote_hash",
                                   "comparison_hash", "source_coordinate", "source_text_hash"])
async def test_malformed_range_or_source_receipt_cannot_be_credited(tmp_path, field):
    case = await prepare(tmp_path)
    journal = case["tool"].audit_records()
    record = journal["records"][0]
    if field == "codepoints":
        record["artifact"]["span"]["end"] += 1
    elif field == "byte_span":
        record["artifact"]["span"]["byte_start"] += 1
    elif field == "slice_hash":
        record["artifact"]["quote_sha256"] = "0" * 64
    elif field == "quote_hash":
        record["source_check"]["quote_sha256"] = "0" * 64
    elif field == "comparison_hash":
        record["source_check"]["comparison_quote_sha256"] = "0" * 64
    elif field == "source_coordinate":
        record["source_check"]["coordinates"]["end"] += 1
    else:
        record["source_check"]["comparison_text_sha256"] = "0" * 64
    _, evidence = capture(case, seal(case), journal=journal)
    assert evidence["counts"]["matched"] == 0 and evidence["counts"]["unknown"] == 1


async def test_previous_phase_record_does_not_apply_to_identical_current_bytes(tmp_path):
    case = await prepare(tmp_path)
    case["task"] = {"phase": "task_2", "root_task_id": "t2"}
    _, evidence = capture(case, seal(case), task_ids={"t2", "t2.edit"})
    assert evidence["counts"]["unapplied"] == 1 and evidence["counts"]["matched"] == 0
    record, = evidence["records"]
    assert record["task_id"] == "t1.edit" and record["reason"] == "outside_current_task_tree"
    assert evidence["phase"] == "task_2"


@pytest.mark.parametrize("damage", ["missing_ref", "corrupt_blob", "corrupt_manifest"])
async def test_unknown_or_corrupt_snapshot_publishes_unavailable_without_fabricated_success(
    tmp_path, damage,
):
    case = await prepare(tmp_path)
    reference = seal(case)
    saved = validate_artifact_snapshot(case["scenario"], reference, task=case["task"])
    if damage == "missing_ref":
        reference = None
    elif damage == "corrupt_blob":
        (case["scenario"] / saved["entries"][0]["file"]).write_bytes(b"corrupt")
    else:
        (case["scenario"] / reference["path"]).write_text("{broken", encoding="utf-8")
    result, evidence = capture(case, reference)
    assert result["status"] == "unavailable"
    assert evidence["snapshot_status"] == "unavailable"
    assert evidence["counts"]["matched"] == 0 and evidence["counts"]["unknown"] == 1
    assert evidence["snapshot_manifest_sha256"] is None


async def test_snapshot_changed_during_binding_cannot_keep_a_matched_credit(tmp_path, monkeypatch):
    case = await prepare(tmp_path)
    reference = seal(case)
    manifest = validate_artifact_snapshot(case["scenario"], reference, task=case["task"])
    blob = case["scenario"] / manifest["entries"][0]["file"]
    original = quote_delivery._source_proof

    def change_after_read(proof, quote):
        result = original(proof, quote)
        blob.write_bytes(b"tampered after slice validation")
        return result

    monkeypatch.setattr(quote_delivery, "_source_proof", change_after_read)
    _, evidence = capture(case, reference)
    assert evidence["counts"]["matched"] == 0 and evidence["counts"]["unknown"] == 1
    assert evidence["status"] == "unavailable"


async def test_dropped_actual_tool_journal_is_incomplete_without_inventing_missing_results(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(artifact_quote, "_MAX_AUDIT_RECORDS", 1)
    case = await prepare(tmp_path)
    await case["tool"].run(case["args"], case["ctx"])
    result, evidence = capture(case, seal(case))
    assert evidence["journal_dropped_records"] == result["journal_dropped_records"] == 1
    assert result["status"] == "incomplete" and len(evidence["records"]) == 1
    assert evidence["counts"]["matched"] == 1 and evidence["quality_pass"] is None


async def test_private_path_or_error_prose_is_not_saved_and_configured_secret_is_scrubbed(tmp_path):
    case = await prepare(tmp_path)
    journal = case["tool"].audit_records()
    secret = "offline-fixture-operator-credential"
    journal["records"][0]["agent_id"] = secret
    broken = copy.deepcopy(journal["records"][0])
    broken["check_id"] = "f" * 32
    broken["artifact"]["path"] = ".evaluation/private-human-answers.json"
    broken["source_check"]["error"] = "Do not leak this provider error or private text"
    journal["records"].append(broken)
    _, evidence = capture(case, seal(case), journal=journal, secrets=(secret,))
    raw = json.dumps(evidence, ensure_ascii=False)
    assert secret not in raw and "private-human-answers" not in raw and "provider error" not in raw
    assert "[REDACTED]" in raw
    assert evidence["counts"]["matched"] == 1 and evidence["counts"]["unknown"] == 1


@pytest.mark.parametrize("private", [
    ".aws/credentials", ".SSH/config", ".codex/auth.json", ".envbackup", ".env.example",
    "id_rsa", "id_dsa", "nested/id_ecdsa", "id_ed25519", "credentials.json",
    "nested/client.pem", "client.KEY", "identity.p12", "identity.pfx",
])
async def test_private_journal_paths_share_artifact_tools_precise_policy(tmp_path, private):
    case = await prepare(tmp_path)
    journal = case["tool"].audit_records()
    journal["records"][0]["artifact"]["path"] = private
    _, evidence = capture(case, seal(case), journal=journal)
    assert evidence["counts"]["matched"] == 0 and evidence["counts"]["unknown"] == 1
    assert private not in json.dumps(evidence)


async def test_regular_credentials_markdown_deliverable_remains_allowed(tmp_path):
    case = await prepare(tmp_path, filename="credentials.md")
    _, evidence = capture(case, seal(case, files=["credentials.md"]))
    assert evidence["counts"]["matched"] == 1
    assert evidence["records"][0]["artifact"]["path"] == "credentials.md"


async def test_write_once_refuses_overwrite_and_failed_publication_leaves_no_partial_file(
    tmp_path, monkeypatch,
):
    case = await prepare(tmp_path)
    reference = seal(case)
    real_link = quote_delivery.os.link

    def fail_publication(*args, **kwargs):
        raise OSError("Simulated filesystem cannot publish this evidence")

    monkeypatch.setattr(quote_delivery.os, "link", fail_publication)
    with pytest.raises(OSError):
        capture(case, reference)
    assert not (case["scenario"] / "task_1-artifact-quote-evidence.json").exists()
    assert not list(case["scenario"].glob(".task_1-artifact-quote-evidence.json.*.tmp"))
    monkeypatch.setattr(quote_delivery.os, "link", real_link)
    result, _ = capture(case, reference)
    path = case["scenario"] / result["path"]
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        capture(case, reference)
    assert path.read_bytes() == before
