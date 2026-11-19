"""Offline lexical checks use archived versions, never model-provided source bodies."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from test_eval_cases import write_cases

from moumou.adapters import FakeLLM, GitWorkspaces
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Budget, ToolContext, ToolResult
from moumou.eval.cases import load_cases
from moumou.eval.source_evidence import SourceEvidenceStore
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools
from moumou.tools.source import ReadSourceTool, SourceHistoryTool
from moumou.tools.source_quote import VerifySourceQuoteTool

URL = "https://example.test/article"


def setup(tmp_path):
    folder = tmp_path / "run"
    scope = folder / "workspaces/p/group"
    store = SourceEvidenceStore(folder / "source-evidence")
    ctx = ToolContext(agent_id="research", task_id="t1", workspace=scope / "research")
    return store, ctx, VerifySourceQuoteTool(
        store, workspaces=[scope / "research", scope / "editor"],
    )


def register(store, ctx):
    store.record_search("Fixture query", ToolResult(ok=True, stdout=json.dumps([{
        "url": URL, "title": "Fixture public source", "snippet": "Only a snippet.",
    }])), ctx, request_id="fixture-request", retrieved_at="2030-01-02T00:00:00+00:00")


def save(store, ctx, text, *, registered=True):
    if registered:
        register(store, ctx)
    metadata = store.record_document(URL, URL, text.encode(), text, "text/plain", "Fixture")
    store.record_read(URL, ctx, ok=True, metadata=metadata)
    return metadata


def decode(result):
    return json.loads(result.stdout)


async def test_read_source_versions_are_pinned_and_quote_checks_do_not_fetch_or_write(
    tmp_path, monkeypatch,
):
    store, ctx, tool = setup(tmp_path)
    register(store, ctx)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"},
                              text="Version A: a forecast." if len(calls) == 1 else
                                   "Version B: a measured result.")

    reader = ReadSourceTool(store, cache_ttl_s=0, resolver=lambda *args: ["93.184.216.34"],
                            transport=httpx.MockTransport(respond))
    first = decode(await reader.run({"url": URL}, ctx))
    second = decode(await reader.run({"url": URL}, ctx))
    before = store.history(limit=30)

    def no_latest(*args):
        raise AssertionError("Quote verification must not consult latest or fetch")

    monkeypatch.setattr(store, "document", no_latest)
    monkeypatch.setattr(reader, "_fetch", no_latest)
    later_role = ctx.model_copy(update={"agent_id": "editor", "task_id": "t2",
                                       "workspace": ctx.workspace.parent / "editor"})
    one = decode(await tool.run({"metadata_sha256": first["metadata_sha256"],
                                 "quote": "a forecast."}, later_role))
    wrong = decode(await tool.run({"metadata_sha256": first["metadata_sha256"],
                                   "quote": "a measured result."}, ctx))
    two = decode(await tool.run({"metadata_sha256": second["metadata_sha256"],
                                 "quote": "a measured result."}, ctx))
    assert one["lexical_match"] is True and wrong["lexical_match"] is False
    assert wrong["status"] == "not_found" and two["status"] == "matched"
    assert one["source"]["metadata_sha256"] == first["metadata_sha256"]
    assert one["source"]["text_sha256"] == first["text_sha256"]
    assert one["source"]["retrieved_at"] == first["retrieved_at"]
    assert two["source"]["metadata_sha256"] != one["source"]["metadata_sha256"]
    assert len(calls) == 2 and store.history(limit=30) == before


async def test_whitespace_normalization_has_explicit_derived_text_coordinates(tmp_path):
    store, ctx, tool = setup(tmp_path)
    metadata = save(store, ctx, "Preface.\r\nProjected\t revenue will\nincrease 12.5% in 2030.")
    quote = "Projected revenue will increase 12.5% in 2030."
    exact = decode(await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                                   "quote": quote}, ctx))
    normalized = decode(await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                                        "quote": quote, "mode": "collapse_whitespace"}, ctx))
    assert exact["status"] == "not_found" and exact["lexical_match"] is False
    assert normalized["lexical_match"] is True
    location = normalized["coordinates"]
    assert location["space"] == "whitespace_normalized_retained_extractor_text"
    assert location["unit"] == "Unicode code points"
    assert location["start"] == len("Preface. ")
    assert location["end"] == len("Preface. ") + len(quote)
    assert normalized["evidence_excerpt"]["text"] == "Preface. " + quote
    assert normalized["source"]["text_sha256"] == metadata["text_sha256"]
    assert normalized["comparison_text_sha256"] != metadata["text_sha256"]


@pytest.mark.parametrize("quote", [
    "Projected revenue increased 12.5% in 2030.",
    "Projected revenue will increase 12.6% in 2030.",
    "Projected revenue will increase 12.5% in 2031.",
    "PROJECTED revenue will increase 12.5% in 2030.",
    "Projected revenue will increase 12.5 % in 2030.",
    "cafe\u0301 - excerpt",
])
async def test_normalization_does_not_rewrite_words_numbers_dates_case_or_punctuation(
    tmp_path, quote,
):
    store, ctx, tool = setup(tmp_path)
    metadata = save(store, ctx, "Projected revenue will increase 12.5% in 2030. café — excerpt")
    result = await tool.run({"metadata_sha256": metadata["metadata_sha256"], "quote": quote,
                             "mode": "collapse_whitespace"}, ctx)
    assert result.ok and decode(result)["lexical_match"] is False


async def test_saved_extractor_text_is_the_coordinate_source_not_raw_markup(tmp_path):
    store, ctx, tool = setup(tmp_path)
    register(store, ctx)
    body = b"<p>A saved sentence.</p>"
    metadata = store.record_document(URL, URL, body, "A saved sentence.", "text/html", "Fixture")
    found = decode(await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                                   "quote": "A saved sentence."}, ctx))
    markup = decode(await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                                    "quote": "<p>A saved sentence.</p>"}, ctx))
    assert found["coordinates"]["space"] == "retained_extractor_text"
    assert found["coordinates"]["start"] == 0
    assert markup["status"] == "not_found"
    assert found["source"]["raw_sha256"] != found["source"]["text_sha256"]


async def test_unread_unregistered_foreign_and_lost_versions_remain_unknown(tmp_path):
    store, ctx, tool = setup(tmp_path / "first")
    register(store, ctx)
    missing = decode(await tool.run({"metadata_sha256": "0" * 64,
                                     "quote": "Only a snippet."}, ctx))
    assert missing["status"] == "unknown" and missing["lexical_match"] is None
    first = save(store, ctx, "Older page.")
    latest = save(store, ctx, "Current page also contains Older page.")
    with sqlite3.connect(store.database) as db:
        db.execute("DELETE FROM document_versions WHERE metadata_sha256=?",
                   (first["metadata_sha256"],))
    lost = decode(await tool.run({"metadata_sha256": first["metadata_sha256"],
                                  "quote": "Older page."}, ctx))
    assert lost["status"] == "unknown" and lost["lexical_match"] is None

    other_store, other_ctx, other_tool = setup(tmp_path / "second")
    unregistered = save(other_store, other_ctx, "Older page.", registered=False)
    denied = decode(await other_tool.run({"metadata_sha256": unregistered["metadata_sha256"],
                                          "quote": "Older page."}, other_ctx))
    assert denied["status"] == "unknown"
    register(other_store, other_ctx)
    foreign = decode(await other_tool.run({"metadata_sha256": latest["metadata_sha256"],
                                           "quote": "Older page."}, other_ctx))
    assert foreign["status"] == "unknown" and foreign["lexical_match"] is None


@pytest.mark.parametrize("args", [
    {"metadata_sha256": "A" * 64, "quote": "Quote"},
    {"metadata_sha256": "../source.txt", "quote": "Quote"},
    {"metadata_sha256": "0" * 63, "quote": "Quote"},
    {"metadata_sha256": "0" * 64, "quote": ""},
    {"metadata_sha256": "0" * 64, "quote": "\t \n"},
    {"metadata_sha256": "0" * 64, "quote": "x" * 4097},
    {"metadata_sha256": "0" * 64, "quote": "Quote", "mode": "fuzzy"},
    {"metadata_sha256": "0" * 64, "quote": "Quote", "document": "Quote"},
    {"metadata_sha256": "0" * 64, "quote": "Quote", "namespace": "another-run"},
    {"metadata_sha256": "0" * 64, "quote": "Quote", "path": "../private"},
])
async def test_bad_or_model_supplied_document_arguments_are_rejected_before_lookup(
    tmp_path, monkeypatch, args,
):
    store, ctx, tool = setup(tmp_path)

    def no_lookup(*args):
        raise AssertionError("Invalid tool input must not reach saved documents")

    monkeypatch.setattr(store, "document_version", no_lookup)
    result = await tool.run(args, ctx)
    assert not result.ok
    assert decode(result)["status"] == "rejected"
    assert decode(result)["lexical_match"] is None


async def test_wrong_workspace_scope_is_rejected_before_lookup(tmp_path, monkeypatch):
    store, ctx, tool = setup(tmp_path)
    metadata = save(store, ctx, "A saved sentence.")

    def no_lookup(*args):
        raise AssertionError("Foreign scope must not access the bound store")

    monkeypatch.setattr(store, "document_version", no_lookup)
    foreign = ctx.model_copy(update={"workspace": tmp_path / "other-run/private-role"})
    result = await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                             "quote": "A saved sentence."}, foreign)
    assert not result.ok and decode(result)["status"] == "rejected"


@pytest.mark.parametrize("damage", ["metadata", "raw", "text", "missing-text"])
async def test_corrupt_older_archived_content_is_unknown_without_latest_fallback(
    tmp_path, damage,
):
    store, ctx, tool = setup(tmp_path)
    metadata = save(store, ctx, "Older genuine page.")
    save(store, ctx, "Latest page includes Older genuine page.")
    with sqlite3.connect(store.database) as db:
        if damage == "metadata":
            db.execute("UPDATE document_versions SET payload='{}' WHERE metadata_sha256=?",
                       (metadata["metadata_sha256"],))
        elif damage == "raw":
            db.execute("UPDATE raw_blobs SET body=? WHERE sha256=?",
                       (b"tampered", metadata["raw_sha256"]))
        elif damage == "text":
            db.execute("UPDATE text_blobs SET text=? WHERE sha256=?",
                       ("tampered", metadata["text_sha256"]))
        else:
            db.execute("DELETE FROM text_blobs WHERE sha256=?", (metadata["text_sha256"],))
    result = await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                             "quote": "Older genuine page."}, ctx)
    assert not result.ok and decode(result)["status"] == "unknown"
    assert decode(result)["lexical_match"] is None


async def test_long_repetitions_return_only_first_span_and_a_bounded_source_excerpt(tmp_path):
    store, ctx, tool = setup(tmp_path)
    metadata = save(store, ctx, "Prefix. " + "x" * 100000)
    result = await tool.run({"metadata_sha256": metadata["metadata_sha256"],
                             "quote": "x" * 4096}, ctx)
    payload = decode(result)
    assert result.ok and payload["lexical_match"] is True
    assert payload["coordinates"]["start"] == len("Prefix. ")
    assert payload["coordinates"]["end"] == len("Prefix. ") + 4096
    assert len(payload["evidence_excerpt"]["text"]) <= 360
    assert payload["evidence_excerpt"]["contains_entire_match"] is False
    assert len(result.stdout.encode()) <= 16384
    assert "truth" not in payload and "human approval" in payload["limitation"]
    assert "fetch" not in payload and "cached" not in payload


async def test_actual_teacher_adapter_exposes_lexical_tool_without_changing_four_roles(tmp_path):
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    folder = tmp_path / "teacher-run"
    scope = folder / "workspaces" / case.id / "instructor-group"
    store = SourceEvidenceStore(folder / "source-evidence")
    ctx = ToolContext(agent_id=case.leader, task_id="fixture", workspace=scope / case.leader)
    metadata = save(store, ctx, "A conditional forecast is not a measured fact.")
    registry = ToolRegistry()
    verifier = VerifySourceQuoteTool(store)
    registry.register_all(*default_tools(), SourceHistoryTool(store), ReadSourceTool(store),
                          verifier)
    observations = []

    def script(messages, tools):
        assert "verify_source_quote" in messages[0]["content"]
        assert "two model-pasted literals" in messages[0]["content"]
        assert any(spec["function"]["name"] == "verify_source_quote" for spec in tools)
        if messages[-1]["role"] != "tool":
            return LLMResponse(tool_calls=[
                ToolCall(id="quote-check", name="verify_source_quote", args={
                    "metadata_sha256": metadata["metadata_sha256"],
                    "quote": "conditional forecast",
                }),
            ])
        observations.append(messages[-1]["content"])
        return LLMResponse(tool_calls=[
            ToolCall(id="result", name="submit_result",
                     args={"summary": "Lexical check only", "files": []}),
        ])

    with Engine(FakeLLM(script), tools=registry, settings=Settings(
            root=folder, project_id=case.id, conversation_id="instructor-group",
            memory_enabled=False, role_review_enabled=False)) as run:
        state = configure_team(run, case)
        verifier.bind_workspaces(state.workspaces.values())
        root = submit_teacher_task(run, case, case.task1, Budget(max_retries=0))
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        assert len(observations) == 1 and metadata["metadata_sha256"] in observations[0]
        assert "Lexical presence only" in observations[0]
        assert len(run.roles.list(case.id)) == 4
        assert {role.name: role.description for role in run.roles.list(case.id)} == {
            role.name: role.description for role in case.roles
        }


async def test_unbound_tool_and_rebinding_fail_closed_but_actual_opaque_paths_are_valid(tmp_path):
    store, ctx, _ = setup(tmp_path)
    metadata = save(store, ctx, "A saved sentence.")
    tool = VerifySourceQuoteTool(store)
    args = {"metadata_sha256": metadata["metadata_sha256"], "quote": "A saved sentence."}
    unbound = await tool.run(args, ctx)
    assert not unbound.ok and decode(unbound)["reason"] == "workspace_binding_unavailable"
    # A provider may allocate opaque worktree paths rather than project/group/role directories.
    actual = tmp_path / "provider/workspaces/worktrees/opaque-4711"
    other = tmp_path / "provider/workspaces/worktrees/opaque-8822"
    tool.bind_workspaces([actual, other])
    legal = ctx.model_copy(update={"workspace": actual})
    assert decode(await tool.run(args, legal))["lexical_match"] is True
    second_role = ctx.model_copy(update={"workspace": other, "agent_id": "editor"})
    assert decode(await tool.run(args, second_role))["lexical_match"] is True
    denied = await tool.run(args, ctx)
    assert not denied.ok and decode(denied)["status"] == "rejected"
    with pytest.raises(ValueError, match="already bound"):
        tool.bind_workspaces([ctx.workspace])


async def test_real_git_worktrees_bind_teacher_quote_tool_and_reject_another_group(tmp_path):
    case = load_cases(write_cases(tmp_path / "cases"))[0]
    provider = GitWorkspaces(tmp_path / "managed")
    store = SourceEvidenceStore(tmp_path / "public-evidence")
    verifier = VerifySourceQuoteTool(store)
    registry = ToolRegistry()
    registry.register_all(*default_tools(), SourceHistoryTool(store), ReadSourceTool(store),
                          verifier)
    observations = []

    def script(messages, tools):
        assert any(spec["function"]["name"] == "verify_source_quote" for spec in tools)
        if messages[-1]["role"] != "tool":
            return LLMResponse(tool_calls=[
                ToolCall(id="git-quote", name="verify_source_quote", args=arguments),
            ])
        observations.append(messages[-1]["content"])
        return LLMResponse(tool_calls=[
            ToolCall(id="result", name="submit_result",
                     args={"summary": "Lexical evidence only", "files": []}),
        ])

    with Engine(FakeLLM(script), workspace=provider, tools=registry, settings=Settings(
            root=tmp_path / "engine", project_id=case.id, conversation_id="instructor-group",
            memory_enabled=False, role_review_enabled=False)) as run:
        state = configure_team(run, case)
        assert len(set(state.workspaces.values())) == 4
        assert all(path.is_relative_to(tmp_path.resolve()) for path in state.workspaces.values())
        common_dirs, git_dirs = set(), set()
        for path in state.workspaces.values():
            assert path.parent == provider.root / "worktrees"
            marker = path / ".git"
            assert marker.is_file(), "A real linked worktree has a .git file, not a directory"
            pointer = marker.read_text(encoding="utf-8").strip()
            assert pointer.startswith("gitdir: ")
            git_dir = (path / pointer.removeprefix("gitdir: ")).resolve()
            actual = Path(provider.git(path, "rev-parse", "--absolute-git-dir").stdout.strip())
            assert git_dir == actual.resolve()
            common = Path(provider.git(
                path, "rev-parse", "--path-format=absolute", "--git-common-dir",
            ).stdout.strip()).resolve()
            assert common.is_relative_to(provider.root / ".repos")
            assert git_dir.is_relative_to(common)
            assert provider.git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
            common_dirs.add(common)
            git_dirs.add(git_dir)
        assert len(common_dirs) == 1 and len(git_dirs) == 4
        listed = provider.git(next(iter(state.workspaces.values())),
                              "worktree", "list", "--porcelain").stdout
        linked_paths = {Path(line.removeprefix("worktree ")).resolve()
                        for line in listed.splitlines() if line.startswith("worktree ")}
        assert set(state.workspaces.values()) <= linked_paths

        verifier.bind_workspaces(state.workspaces.values())
        leader_ctx = ToolContext(agent_id=case.leader, task_id="fixture",
                                 workspace=state.workspaces[case.leader])
        metadata = save(store, leader_ctx, "An archived passage in the current group.")
        arguments = {"metadata_sha256": metadata["metadata_sha256"], "quote": "archived passage"}
        before = store.history(limit=30)
        for role, path in state.workspaces.items():
            result = await verifier.run(arguments, ToolContext(
                agent_id=role, task_id="t1.verify", workspace=path,
            ))
            assert result.ok and decode(result)["lexical_match"] is True
            assert decode(result)["source"]["metadata_sha256"] == metadata["metadata_sha256"]

        foreign = provider.allocate(case.id, "another-group", case.leader)
        assert foreign.is_relative_to(tmp_path.resolve()) and (foreign / ".git").is_file()
        denied = await verifier.run(arguments, leader_ctx.model_copy(update={"workspace": foreign}))
        assert not denied.ok and decode(denied)["reason"] == "outside_bound_workspace_scope"
        root = submit_teacher_task(run, case, case.task1, Budget(max_retries=0))
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        assert len(observations) == 1 and metadata["metadata_sha256"] in observations[0]
        assert store.history(limit=30) == before
        assert {role.name: role.description for role in run.roles.list(case.id)} == {
            role.name: role.description for role in case.roles
        }
