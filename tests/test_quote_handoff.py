"""Native dependency-check provenance is scoped to actual declared recipient bytes."""

from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

from moumou.contracts import TaskNode, TaskResult, ToolContext, ToolResult
from moumou.eval import quote_handoff
from moumou.eval.quote_handoff import summarize_dependency_checks
from moumou.eval.source_evidence import SourceEvidenceStore, digest
from moumou.tools.artifact_quote import VerifyArtifactQuoteTool

URL = "https://example.test/retained-source"
QUOTE = "真实来源中的文字😀，增长12.5%。"


class Repo:
    def __init__(self, *nodes):
        self.nodes = {node.id: node for node in nodes}
        self.reads = []

    def get(self, task_id):
        self.reads.append(task_id)
        node = self.nodes.get(task_id)
        return node.model_copy(deep=True) if node else None


async def prepare(tmp_path, *, source=QUOTE, mode="exact"):
    paths = {name: tmp_path / "provider" / name for name in ("reporter", "checker", "editor")}
    for path in paths.values():
        path.mkdir(parents=True)
    root = TaskNode(id="t1", project_id="project", conversation_id="group",
                    role="editor", goal="Original shared scope")
    producer = TaskNode(id="t1.report", parent_id="t1", project_id="project",
                        conversation_id="group", role="reporter", goal="Write article",
                        status="DONE", artifacts=["article.md"],
                        result=TaskResult(files=["article.md"]))
    current = TaskNode(id="t1.check", parent_id="t1", project_id="project",
                       conversation_id="group", role="checker", goal="Check actual article",
                       status="RUNNING", deps=[producer.id])
    repo = Repo(root, producer, current)
    body = "# 标题😀\r\n" + QUOTE + "\r\n尾注"
    raw = body.encode()
    for name in ("reporter", "checker"):
        (paths[name] / "article.md").write_bytes(raw)
    store = SourceEvidenceStore(tmp_path / "public-evidence")
    ctx = ToolContext(agent_id="reporter", task_id=producer.id, workspace=paths["reporter"])
    store.record_search("Fixture", ToolResult(ok=True, stdout=json.dumps([
        {"url": URL, "title": "Public fixture", "snippet": "Not the full original body."},
    ])), ctx, request_id="fixture", retrieved_at="2026-11-18T00:00:00+00:00")
    metadata = store.record_document(URL, URL, source.encode(), source, "text/plain", "Fixture")
    tool = VerifyArtifactQuoteTool(store, workspaces=paths.values())
    args = {"metadata_sha256": metadata["metadata_sha256"], "path": "article.md",
            "start": body.index(QUOTE), "end": body.index(QUOTE) + len(QUOTE), "mode": mode}
    await tool.run(args, ctx)
    return SimpleNamespace(paths=paths, node=current, producer=producer, repo=repo, body=body,
                           tool=tool, ctx=ctx, args=args, metadata=metadata, store=store)


def summarize(case, **overrides):
    options = {"repo": case.repo, "get_journal": case.tool.audit_records,
               "workspaces": case.paths}
    options.update(overrides)
    return summarize_dependency_checks(case.node, SimpleNamespace(name=case.node.role),
                                       case.paths[case.node.role], **options)


def payload(options):
    return json.loads(options[0].splitlines()[-1])


async def test_actual_tool_receipt_binds_owned_unicode_file_without_sharing_source_or_private_path(
    tmp_path,
):
    case = await prepare(tmp_path)
    journal_before = case.tool.audit_records()
    full, compact, empty = summarize(case)
    value = payload((full, compact, empty))
    file, = value["files"]
    assert file["task"] == case.producer.id and file["actor"] == "reporter"
    assert file["path"] == "article.md" and file["sha256"] == digest(case.body.encode())
    assert file["bytes"] == len(case.body.encode())
    assert file["checks"] == {"matched": 1, "not_found": 0, "unknown": 0, "rejected": 0}
    assert file["samples"][0]["id"] == journal_before["records"][0]["check_id"]
    assert value["history"] == "recorded_window" and empty == ""
    assert "selected literal slices" in full and "prior checks remain unknown" in compact
    assert URL not in full and QUOTE not in full and str(tmp_path) not in full
    assert case.tool.audit_records() == journal_before
    assert not set(case.repo.reads) - {"t1", "t1.report", "t1.check"}


async def test_mixed_native_states_and_rollover_keep_counts_and_bounded_real_sample_ids(tmp_path):
    case = await prepare(tmp_path)
    journal = case.tool.audit_records()
    negative = copy.deepcopy(journal["records"][0])
    negative["check_id"] = "a" * 32
    negative["source_check"].update(status="not_found", lexical_match=False)
    negative["source_check"]["coordinates"].update(start=None, end=None)
    unknown = copy.deepcopy(negative)
    unknown["check_id"] = "b" * 32
    unknown["source_check"] = {"status": "unknown", "lexical_match": None}
    rejected = copy.deepcopy(unknown)
    rejected["check_id"] = "c" * 32
    rejected["source_check"]["status"] = "rejected"
    journal["records"].extend([negative, unknown, rejected])
    journal["dropped_records"] = 991
    value = payload(summarize(case, get_journal=lambda: journal))
    file, = value["files"]
    assert file["checks"] == {"matched": 1, "not_found": 1, "unknown": 1, "rejected": 1}
    assert len(file["samples"]) == 3 and file["sample_ids_omitted"] == 1
    assert value["history"] == "unknown"
    assert "991" not in json.dumps(value) and "dropped_records" not in value


@pytest.mark.parametrize("getter", [None, lambda: None, lambda: {},
                                    lambda: {"schema_version": 1, "records": [],
                                             "dropped_records": True}])
async def test_missing_or_malformed_journal_is_unknown_not_zero_checks(tmp_path, getter):
    case = await prepare(tmp_path)
    value = payload(summarize(case, get_journal=getter))
    assert value["history"] == "unknown" and value["files"][0]["checks"] is None


async def test_empty_native_window_does_not_claim_producer_performed_no_checks(tmp_path):
    case = await prepare(tmp_path)
    full, _, _ = summarize(case, get_journal=lambda: {
        "schema_version": 1, "records": [], "dropped_records": 0,
    })
    assert payload((full,))["files"][0]["checks"]["matched"] == 0
    assert "Missing history is unknown" in full and "do not infer" in full


async def test_source_string_check_and_unrelated_private_receipts_are_not_artifact_handoffs(
    tmp_path,
):
    case = await prepare(tmp_path)
    journal = case.tool.audit_records()
    source_only = copy.deepcopy(journal["records"][0])
    source_only["artifact"] = None
    unrelated = copy.deepcopy(journal["records"][0])
    unrelated.update(task_id="unlisted.private", agent_id="editor", check_id="f" * 32)
    unrelated["artifact"]["path"] = ".evaluation/private-ledger.md"
    journal["records"] = [source_only, unrelated]
    full, _, _ = summarize(case, get_journal=lambda: journal)
    assert payload((full,))["files"][0]["checks"]["matched"] == 0
    for private in ("unlisted.private", "private-ledger", "f" * 32):
        assert private not in full


@pytest.mark.parametrize("mutation", ["stale_file", "stale_size", "span", "slice_hash",
                                       "unstable", "source_shape", "duplicate_id"])
async def test_invalid_or_stale_evidence_never_becomes_an_applicable_positive(tmp_path, mutation):
    case = await prepare(tmp_path)
    journal = case.tool.audit_records()
    record = journal["records"][0]
    if mutation == "stale_file":
        (case.paths["checker"] / "article.md").write_bytes(case.body.encode() + b"\nEdit")
    elif mutation == "stale_size":
        record["artifact"]["bytes"] += 1
    elif mutation == "span":
        record["artifact"]["span"]["byte_start"] += 1
    elif mutation == "slice_hash":
        record["artifact"]["quote_sha256"] = "0" * 64
    elif mutation == "unstable":
        record["artifact"]["stable_at_checks"] = False
    elif mutation == "source_shape":
        record["source_check"]["normalization"] = "invented typography normalization"
    else:
        journal["records"].append(copy.deepcopy(record))
    file, = payload(summarize(case, get_journal=lambda: journal))["files"]
    assert file["checks"]["matched"] == 0
    if mutation == "source_shape":
        assert file["checks"]["unknown"] == 1
    else:
        assert file["non_applicable"] >= 1


@pytest.mark.parametrize("change", ["missing", "not_done", "foreign_group", "foreign_project",
                                    "undeclared", "unaccepted", "wrong_actor", "wrong_path"])
async def test_only_current_done_direct_producer_declared_files_are_eligible(tmp_path, change):
    case = await prepare(tmp_path)
    journal = case.tool.audit_records()
    producer = case.repo.nodes[case.producer.id]
    if change == "missing":
        del case.repo.nodes[producer.id]
    elif change == "not_done":
        producer.status = "RUNNING"
    elif change == "foreign_group":
        producer.conversation_id = "foreign"
    elif change == "foreign_project":
        producer.project_id = "foreign"
    elif change == "undeclared":
        producer.result.files = []
    elif change == "unaccepted":
        producer.artifacts = []
    elif change == "wrong_actor":
        journal["records"][0]["agent_id"] = "editor"
    else:
        journal["records"][0]["artifact"]["path"] = "undeclared-copy.md"
    value = payload(summarize(case, get_journal=lambda: journal))
    assert all(file["checks"] is None or file["checks"]["matched"] == 0
               for file in value["files"])


async def test_explicit_previous_phase_root_is_allowed_without_inheriting_its_child_checks(
    tmp_path,
):
    case = await prepare(tmp_path)
    previous = case.repo.nodes[case.producer.id]
    previous.parent_id = None
    # This direct dependency is itself a completed previous-phase root.
    file, = payload(summarize(case))["files"]
    assert file["checks"]["matched"] == 1 and file["task"] == previous.id
    journal = case.tool.audit_records()
    old_child = copy.deepcopy(journal["records"][0])
    old_child["task_id"] = "unlisted.old.child"
    old_child["check_id"] = "e" * 32
    journal["records"].append(old_child)
    file, = payload(summarize(case, get_journal=lambda: journal))["files"]
    assert file["checks"]["matched"] == 1
    assert "e" * 32 not in json.dumps(file)


async def test_undeclared_checker_input_and_transitive_reporter_receipts_stay_private(tmp_path):
    case = await prepare(tmp_path)
    checker = case.repo.nodes[case.node.id]
    checker.status = "DONE"
    checker.result = TaskResult(files=["fact_check.md"])
    checker.artifacts = ["fact_check.md"]
    for role in ("checker", "editor"):
        (case.paths[role] / "fact_check.md").write_text("Independent review.", encoding="utf-8")
        (case.paths[role] / "article.md").write_bytes(case.body.encode())
    editor = TaskNode(id="t1.edit", parent_id="t1", project_id="project", conversation_id="group",
                      role="editor", goal="Edit review", deps=[checker.id], status="RUNNING")
    case.repo.nodes[editor.id] = editor
    case.node = editor
    value = payload(summarize(case))
    assert [file["path"] for file in value["files"]] == ["fact_check.md"]
    assert "reporter" not in json.dumps(value) and "article.md" not in json.dumps(value)


@pytest.mark.parametrize("damage", ["goal", "deps", "role", "cycle", "missing_parent"])
async def test_repo_current_task_coherence_and_bounded_ancestry_fail_closed(tmp_path, damage):
    case = await prepare(tmp_path)
    if damage == "goal":
        case.node = case.node.model_copy(update={"goal": "Forged scope"})
    elif damage == "deps":
        case.node = case.node.model_copy(update={"deps": []})
    elif damage == "role":
        case.node = case.node.model_copy(update={"role": "editor"})
    elif damage == "cycle":
        case.repo.nodes["t1"].parent_id = case.node.id
    else:
        case.repo.nodes["t1"].parent_id = "absent"
    options = summarize(case)
    assert options[0] == options[1] and "prior checks remain unknown" in options[0]
    assert "sha256" not in options[0]


@pytest.mark.parametrize("filename", [".envrc", ".env_backup", ".ssh/id_rsa", "credentials.json",
                                     "private.key", "article.md:secret", "../foreign.md"])
async def test_private_and_invalid_declared_paths_are_not_read_or_named(tmp_path, monkeypatch,
                                                                     filename):
    case = await prepare(tmp_path)
    producer = case.repo.nodes[case.producer.id]
    producer.result.files = [filename]
    producer.artifacts = [filename]
    if ":" not in filename and not filename.startswith("../"):
        for role in ("reporter", "checker"):
            path = case.paths[role] / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Private fixture must never be opened.", encoding="utf-8")

    def forbidden(*args):
        raise AssertionError("private file must never be opened")

    monkeypatch.setattr(quote_handoff, "_snapshot", forbidden)
    full, _, _ = summarize(case)
    assert filename not in full and not payload((full,))["files"]


async def test_alias_or_replaced_recipient_file_cannot_be_credited(tmp_path, monkeypatch):
    case = await prepare(tmp_path)

    def changed(*args):
        raise ValueError("artifact_changed_during_read/private/absolute/path")

    monkeypatch.setattr(quote_handoff, "_snapshot", changed)
    full, _, _ = summarize(case)
    assert not payload((full,))["files"]
    assert "absolute/path" not in full and payload((full,))["files_omitted"] == 1


async def test_actual_hardlink_recipient_is_refused(tmp_path):
    case = await prepare(tmp_path)
    path = case.paths["checker"] / "article.md"
    link = case.paths["checker"] / "linked.md"
    try:
        os.link(path, link)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {type(exc).__name__}")
    value = payload(summarize(case))
    assert not value["files"] and value["files_omitted"] == 1


async def test_unbound_or_shared_workspace_mapping_is_unknown_without_secret_diagnostics(tmp_path):
    case = await prepare(tmp_path)
    paths = dict(case.paths, reporter=case.paths["checker"])
    options = summarize(case, workspaces=paths)
    assert options[0] == options[1] and str(tmp_path) not in options[0]


async def test_bounded_whole_json_entries_compact_fallback_and_single_journal_snapshot(tmp_path):
    case = await prepare(tmp_path)
    count = 0

    def getter():
        nonlocal count
        count += 1
        return case.tool.audit_records()

    full, compact, empty = summarize(case, get_journal=getter)
    assert count == 1 and len(full) <= 2000 and len(full.encode()) <= 8000
    assert payload((full,))["files"][0]["sha256"] == digest(case.body.encode())
    options = summarize(case, max_chars=len(compact))
    assert all(len(value) <= len(compact) for value in options) and options[2] == empty == ""
    assert summarize(case, max_chars=0) == ("", "", "")
    for invalid in (True, -1, 1.5):
        with pytest.raises(ValueError, match="max_chars"):
            summarize(case, max_chars=invalid)


async def test_many_declared_files_are_omitted_as_whole_entries_with_explicit_counts(tmp_path):
    case = await prepare(tmp_path)
    producer = case.repo.nodes[case.producer.id]
    names = ["article.md", "newsletter.md", "web.md", "social.md", "source_notes.md"]
    producer.result.files = names
    producer.artifacts = names
    for name in names[1:]:
        for role in ("reporter", "checker"):
            (case.paths[role] / name).write_bytes(case.body.encode())
        await case.tool.run({**case.args, "path": name}, case.ctx)
    full, _, _ = summarize(case)
    value = payload((full,))
    assert 0 < len(value["files"]) < len(names)
    assert value["files_omitted"] == len(names) - len(value["files"])
    assert all(file["checks"]["matched"] == 1 and len(file["sha256"]) == 64
               for file in value["files"])
    assert len(full) <= 2000 and len(full.encode()) <= 8000


async def test_real_whitespace_mode_remains_a_lexical_check_not_a_text_rewrite(tmp_path):
    case = await prepare(tmp_path, source="\n" + QUOTE + "\t", mode="collapse_whitespace")
    file, = payload(summarize(case))["files"]
    assert file["checks"]["matched"] == 1 and file["sha256"] == digest(case.body.encode())


async def test_closed_corrupt_source_check_is_unknown_not_negative_or_positive(tmp_path):
    case = await prepare(tmp_path)
    with case.store._db() as db:
        db.execute("DELETE FROM document_versions WHERE metadata_sha256=?",
                   (case.metadata["metadata_sha256"],))
    await case.tool.run(case.args, case.ctx)
    journal = case.tool.audit_records()
    journal["records"] = journal["records"][-1:]
    file, = payload(summarize(case, get_journal=lambda: journal))["files"]
    assert file["checks"] == {"matched": 0, "not_found": 0, "unknown": 1, "rejected": 0}
