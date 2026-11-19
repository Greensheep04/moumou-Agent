"""Native receipts reach opening messages without displacing mandatory context.

These are offline contracts: no real model, search, subprocess or private run
fixture is needed. A real verifier journal and SQLite task repository supply
the provenance; FakeLLM observes the actual engine's role-to-role handoff.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_quality_tools_cli import single_case

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.context.lineage import collect_assignment_references
from moumou.contracts import (
    Budget,
    HumanRequest,
    HumanResponse,
    TaskNode,
    TaskResult,
    ToolContext,
    ToolResult,
)
from moumou.eval.source_evidence import SourceEvidenceStore, digest
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools
from moumou.tools.artifact_quote import VerifyArtifactQuoteTool

HEADER = "## Native dependency artifact checks\n"
URL = "https://example.test/offline-provenance"
QUOTE = "原始文字 café 😀，保留12.5%。"
BODY = "# Synthetic fixture\r\n" + QUOTE + "\r\n"
ROOT_GOAL = "Review the original document; unpublished appendix availability is UNKNOWN."
CHECK_GOAL = "Check article.md without inventing whether anyone previously checked it."
ANSWER = "Keep the author's original spelling; do not expand tool permissions."
PRIVATE = "PRIVATE_WRITER_NOTE_never_a_deliverable"


def call(name, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=arguments)])


def seed_source(store, workspace, *, task_id):
    ctx = ToolContext(agent_id="writer", task_id=task_id, workspace=workspace)
    store.record_search("Offline fixture", ToolResult(ok=True, stdout=json.dumps([
        {"url": URL, "title": "Synthetic public fixture", "snippet": "Not a full page."},
    ])), ctx, request_id="offline-fixture")
    return store.record_document(URL, URL, QUOTE.encode(), QUOTE, "text/plain", "Fixture")


def native_payload(messages):
    system = messages[0]["content"]
    assert HEADER in system
    return json.loads(system.split(HEADER, 1)[1].splitlines()[-1])


@pytest.fixture
async def native_context(tmp_path):
    _bank, case, _role_path, _role_hash = single_case(tmp_path)
    with Engine(FakeLLM([]), settings=Settings(
        root=tmp_path / "run", project_id="fixture", conversation_id="group",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        root = TaskNode(id="root", project_id="fixture", conversation_id="group",
                        role="coordinator", goal=ROOT_GOAL)
        producer = TaskNode(
            id="root.write", project_id="fixture", conversation_id="group",
            parent_id=root.id, depth=1, role="writer", goal="Write article.md",
            status="DONE", artifacts=["article.md"],
            result=TaskResult(summary="Literal quote checked", files=["article.md"]),
        )
        current = TaskNode(
            id="root.check", project_id="fixture", conversation_id="group",
            parent_id=root.id, depth=1, role="checker", goal=CHECK_GOAL,
            deps=[producer.id], status="RUNNING",
        )
        for node in (root, producer, current):
            run.repo.add(node)
        for actor in ("writer", "checker"):
            (state.workspaces[actor] / "article.md").write_bytes(BODY.encode())
        (state.workspaces["writer"] / "private.txt").write_text(PRIVATE, encoding="utf-8")
        store = SourceEvidenceStore(tmp_path / "source-evidence")
        metadata = seed_source(store, state.workspaces["writer"], task_id=producer.id)
        verifier = VerifyArtifactQuoteTool(store, workspaces=state.workspaces.values())
        result = await verifier.run({
            "metadata_sha256": metadata["metadata_sha256"], "path": "article.md",
            "start": BODY.index(QUOTE), "end": BODY.index(QUOTE) + len(QUOTE),
        }, ToolContext(agent_id="writer", task_id=producer.id,
                       workspace=state.workspaces["writer"]))
        assert result.ok
        role = run.roles.get(current.project_id, current.role)
        yield SimpleNamespace(run=run, state=state, root=root, producer=producer,
                              node=current, role=role, verifier=verifier)


def build_context(case, **options):
    return case.state.context.build(
        case.node, case.role, assignment_references=collect_assignment_references(
            case.run.repo, case.node,
        ), **options,
    )


async def test_real_sqlite_native_receipt_enters_checker_opening_without_private_history(
    native_context,
):
    case = native_context
    original = build_context(case).messages()
    assert HEADER not in original[0]["content"]
    case.state.context.bind_artifact_receipts(case.run.repo, case.verifier.audit_records)
    opening = build_context(case)
    item, = native_payload(opening.messages())["files"]
    actual, = case.verifier.audit_records()["records"]
    assert item["task"] == case.producer.id and item["actor"] == "writer"
    assert item["sha256"] == digest(BODY.encode())
    assert item["checks"]["matched"] == 1
    assert item["samples"] == [{"id": actual["check_id"], "status": "matched"}]
    assert opening.pinned == 2
    assert opening.messages()[1] == original[1]
    assert ROOT_GOAL in opening.messages()[1]["content"]
    metadata = opening.messages()[0]["content"].split(HEADER, 1)[1]
    for private in (URL, QUOTE, PRIVATE, "private.txt", str(case.state.workspaces["writer"])):
        assert private not in metadata
    assert not (case.state.workspaces["checker"] / "private.txt").exists()


async def test_rebuilt_opening_rejects_the_old_receipt_after_recipient_file_edit(native_context):
    case = native_context
    case.state.context.bind_artifact_receipts(case.run.repo, case.verifier.audit_records)
    before, = native_payload(build_context(case).messages())["files"]
    assert before["checks"]["matched"] == 1
    updated = BODY.encode() + b"\nChanged after handoff"
    (case.state.workspaces["checker"] / "article.md").write_bytes(updated)
    after, = native_payload(build_context(case).messages())["files"]
    assert after["sha256"] == digest(updated)
    assert after["checks"]["matched"] == 0 and after["non_applicable"] == 1


async def test_receipts_yield_to_exact_mandatory_goal_lineage_and_recorded_answer_budget(
    native_context,
):
    case = native_context
    request = HumanRequest(
        id="spelling-question", task_id=case.node.id, agent_id="checker",
        kind="question", summary="Which spelling should remain?",
        project_id=case.node.project_id, conversation_id=case.node.conversation_id,
        role=case.node.role,
    )
    case.run.repo.record_human_request(case.node, request)
    case.run.repo.record_human_response(
        case.node, HumanResponse(request_id=request.id, text=ANSWER),
    )
    exchanges = case.run.repo.human_exchanges(case.node)
    baseline = build_context(case, human_exchanges=exchanges).messages()
    required = case.state.context.budget.estimate(baseline)
    assert required >= 500
    case.state.context.policy = case.state.context.policy.model_copy(update={
        "opening_budget_tokens": required,
    })
    case.state.context.bind_artifact_receipts(case.run.repo, case.verifier.audit_records)
    result = build_context(case, human_exchanges=exchanges)
    assert result.messages() == baseline
    assert HEADER not in result.messages()[0]["content"]
    for mandatory in (ROOT_GOAL, CHECK_GOAL, ANSWER):
        assert mandatory in result.messages()[1]["content"]
    assert case.state.context.budget.estimate(result.messages()) <= required
    assert result.pinned == 2


@pytest.mark.parametrize("repo,getter", [(object(), lambda: {}), (None, None)])
async def test_invalid_binding_is_rejected_without_consuming_the_one_time_binding(
    native_context, repo, getter,
):
    case = native_context
    with pytest.raises(ValueError, match="native journal"):
        case.state.context.bind_artifact_receipts(repo, getter)
    case.state.context.bind_artifact_receipts(case.run.repo, case.verifier.audit_records)
    assert native_payload(build_context(case).messages())["files"][0]["checks"]["matched"] == 1
    with pytest.raises(ValueError, match="already bound"):
        case.state.context.bind_artifact_receipts(case.run.repo, case.verifier.audit_records)


async def test_actual_engine_producer_checker_handoff_reaches_the_fake_model(tmp_path):
    _bank, case, _role_path, _role_hash = single_case(tmp_path)
    store = SourceEvidenceStore(tmp_path / "source-evidence")
    verifier = VerifyArtifactQuoteTool(store)
    registry = ToolRegistry()
    registry.register_all(*default_tools(), verifier)
    state_holder, seeded, checker_openings = {}, {}, []
    planned = False

    def script(messages, tools):
        nonlocal planned
        system = messages[0]["content"]
        previous = messages[-1].get("tool_call_id") if messages[-1]["role"] == "tool" else None
        if case.roles[1].description in system:
            if previous is None:
                return call("write_file", path="article.md", content=BODY)
            if previous == "write_file":
                return call("verify_artifact_quote", path="article.md",
                            start=BODY.index(QUOTE), end=BODY.index(QUOTE) + len(QUOTE),
                            metadata_sha256=seeded["metadata_sha256"])
            assert previous == "verify_artifact_quote"
            assert "ok=true" in messages[-1]["content"]
            return call("submit_result", summary="Verified selected literal slice",
                        files=["article.md"])
        if case.roles[2].description in system:
            if previous is None:
                item, = native_payload(messages)["files"]
                actual, = verifier.audit_records()["records"]
                assert item["task"] == actual["task_id"] and item["actor"] == "writer"
                assert item["sha256"] == digest(BODY.encode())
                assert item["checks"]["matched"] == 1
                assert item["samples"][0]["id"] == actual["check_id"]
                metadata = system.split(HEADER, 1)[1]
                assert PRIVATE not in metadata and QUOTE not in metadata
                workspace = state_holder["state"].workspaces["checker"]
                assert (workspace / "article.md").read_bytes() == BODY.encode()
                assert not (workspace / "private.txt").exists()
                checker_openings.append(messages[:2])
                return call("read_file", path="article.md")
            if previous == "read_file":
                return call("write_file", path="review.md", content="Selected slice only.")
            return call("submit_result", summary="Checked explicit input", files=["review.md"])
        if not planned:
            planned = True
            return call("submit_plan", reasoning="Explicit producer and consumer", roles=[],
                        subtasks=[
                            {"key": "write", "role": "writer", "goal": "Write article.md",
                             "deps": [], "acceptance": [
                                 {"kind": "file_exists", "target": "article.md"},
                             ]},
                            {"key": "check", "role": "checker", "goal": CHECK_GOAL,
                             "deps": ["write"], "acceptance": [
                                 {"kind": "file_exists", "target": "review.md"},
                             ]},
                        ])
        return call("submit_result", summary="Integrated explicit handoffs",
                    files=["article.md", "review.md"])

    with Engine(FakeLLM(script), tools=registry, settings=Settings(
        root=tmp_path / "run", memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        state_holder["state"] = state
        verifier.bind_workspaces(state.workspaces.values())
        state.context.bind_artifact_receipts(run.repo, verifier.audit_records)
        (state.workspaces["writer"] / "private.txt").write_text(PRIVATE, encoding="utf-8")
        seeded.update(seed_source(store, state.workspaces["writer"], task_id="fixture-seed"))
        root = submit_teacher_task(run, case, ROOT_GOAL, Budget(max_retries=0))
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        assert run.repo.get(root.id + ".write").status == "DONE"
        assert run.repo.get(root.id + ".check").status == "DONE"
        assert len(checker_openings) == 1
        assert ROOT_GOAL in checker_openings[0][1]["content"]
