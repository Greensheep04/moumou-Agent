"""P3 evidence stays readable/private without hiding real dirty Git work."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM, GitWorkspaces
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Budget, HumanRequest, HumanResponse, ToolContext
from moumou.eval.cases import load_cases
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.tools.filesystem import ReadFileTool

ROOT = Path(__file__).resolve().parents[1]


def make_engine(tmp_path, client=None):
    workspace = GitWorkspaces(tmp_path / "workspaces")
    settings = Settings(
        root=tmp_path / "run", project_id="evidence-project", conversation_id="same-team",
        memory_enabled=False, role_review_enabled=False,
    )
    return Engine(client or FakeLLM([]), workspace=workspace, settings=settings), workspace


def build_context(run, case, state, role="lead"):
    node = submit_teacher_task(run, case, "SYNTHETIC evidence registration fixture", Budget())
    node = node.model_copy(update={"role": role})
    identity = run.roles.get(node.project_id, role)
    messages = state.context.build(
        node, identity, board_notes=["SYNTHETIC recorded context, not a new instruction"],
    ).messages()
    archive, = (state.workspaces[role] / ".evaluation/context").glob("*.json")
    return node, identity, archive, messages


def exclude_rules(workspace, provider):
    common = Path(provider.git(
        workspace, "rev-parse", "--path-format=absolute", "--git-common-dir",
    ).stdout.strip())
    return (common / "info/exclude").read_text(encoding="utf-8").splitlines()


async def test_registered_context_remains_readable_and_clean_across_delivery_and_reopen(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]
    run, provider = make_engine(tmp_path)
    with run:
        state = configure_team(run, case)
        node, role, archive, messages = build_context(run, case, state)
        target, child = state.workspaces["lead"], state.workspaces["dev"]
        relative = archive.relative_to(target).as_posix()
        original = archive.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        assert archive.name == digest[:32] + ".json"
        assert relative in messages[1]["content"] and digest in messages[1]["content"]
        assert not provider.git(target, "status", "--porcelain").stdout.strip()
        assert provider.git(target, "check-ignore", "--", relative).returncode == 0
        rules = exclude_rules(target, provider)
        assert rules.count("/" + relative) == 1
        assert "/.evaluation/" not in rules
        assert not (target / ".gitignore").exists()

        read = await ReadFileTool().run(
            {"path": relative}, ToolContext(agent_id="lead", task_id=node.id, workspace=target),
        )
        assert read.ok and json.loads(read.stdout)["current_goal"] == node.goal
        state.context.build(
            node, role, board_notes=["SYNTHETIC recorded context, not a new instruction"],
        )
        assert exclude_rules(target, provider).count("/" + relative) == 1

        (child / "delivery.txt").write_text("declared first delivery", encoding="utf-8")
        (child / "private.txt").write_text("not declared", encoding="utf-8")
        delivered = run.workspace.integrate(child, target, ["delivery.txt"])
        assert delivered.ok, delivered
        assert (target / "delivery.txt").read_text(encoding="utf-8") == "declared first delivery"
        assert not (target / "private.txt").exists()
        assert archive.read_bytes() == original

        reopened = GitWorkspaces(provider.root)
        assert reopened.allocate(node.project_id, node.conversation_id, "lead") == target
        assert not reopened.git(target, "status", "--porcelain").stdout.strip()
        (child / "delivery.txt").write_text("declared second delivery", encoding="utf-8")
        assert reopened.integrate(child, target, ["delivery.txt"]).ok
        assert (target / "delivery.txt").read_text(encoding="utf-8") == "declared second delivery"
        assert archive.read_bytes() == original


async def test_second_task_registers_original_dialogue_for_all_four_git_workspaces(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]

    def finish_fixture(messages, tools):
        return LLMResponse(tool_calls=[ToolCall(
            id="finish-synthetic-first", name="submit_result",
            args={"summary": "SYNTHETIC first task fixture complete", "files": []},
        )])

    run, provider = make_engine(tmp_path, FakeLLM(finish_fixture))
    dialogue = tmp_path / "dialogue.jsonl"
    with run:
        state = configure_team(run, case, previous_dialogue_path=dialogue)
        first = submit_teacher_task(run, case, "SYNTHETIC first fixture task", Budget())
        await run.run()
        assert run.repo.get(first.id).status == "DONE"
        request = HumanRequest(
            id="original-history-fixture", task_id=first.id, agent_id="lead", kind="question",
            summary="SYNTHETIC fixture: retain which history?", project_id=first.project_id,
            conversation_id=first.conversation_id, role="lead",
        )
        answer = HumanResponse(request_id=request.id, text="SYNTHETIC retain cancellation history")
        events = [
            {"event": "human_request", "request": request.model_dump(mode="json")},
            {"event": "human_response", "response": answer.model_dump(mode="json")},
        ]
        raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")
        dialogue.write_bytes(raw)
        second = submit_teacher_task(
            run, case, "SYNTHETIC second fixture task", Budget(), previous_root=first.id,
        )
        role = run.roles.get(second.project_id, second.role)
        brief = state.context.build(second, role).messages()[1]["content"]
        assert answer.text in brief
        assert state.prior_dialogue_sha256 == hashlib.sha256(raw).hexdigest()
        assert second.deps == [first.id]
        for workspace in state.workspaces.values():
            history = workspace / ".evaluation/prior-dialogue.txt"
            assert history.read_bytes() == raw
            assert exclude_rules(workspace, provider).count("/.evaluation/prior-dialogue.txt") == 1
            assert not provider.git(workspace, "status", "--porcelain").stdout.strip()


async def test_task_two_never_overwrites_existing_tracked_history_before_rejection(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]

    def finish_fixture(messages, tools):
        return LLMResponse(tool_calls=[ToolCall(
            id="finish-tracked-history-fixture", name="submit_result",
            args={"summary": "SYNTHETIC completed first task", "files": []},
        )])

    run, provider = make_engine(tmp_path, FakeLLM(finish_fixture))
    dialogue = tmp_path / "dialogue.jsonl"
    with run:
        state = configure_team(run, case, previous_dialogue_path=dialogue)
        first = submit_teacher_task(run, case, "SYNTHETIC first task", Budget())
        await run.run()
        assert run.repo.get(first.id).status == "DONE"
        dialogue.write_text(
            json.dumps({"event": "fixture_transcript", "text": "SYNTHETIC new history"}) + "\n",
            encoding="utf-8",
        )
        target = state.workspaces["lead"]
        relative = ".evaluation/prior-dialogue.txt"
        history = target / relative
        history.parent.mkdir(parents=True)
        history.write_bytes(b"committed history must survive\n")
        provider.git(target, "add", "--force", "--", relative)
        provider.git(target, "commit", "-m", "Seed synthetic tracked history fixture")
        history.write_bytes(b"staged history must survive\n")
        provider.git(target, "add", "--", relative)
        history.write_bytes(b"unstaged history must survive\n")
        before_bytes = history.read_bytes()
        before_head = provider.git(target, "rev-parse", "HEAD").stdout
        before_index = provider.git(target, "ls-files", "--stage").stdout
        before_staged_bytes = provider.git(target, "show", ":" + relative).stdout
        common = Path(provider.git(
            target, "rev-parse", "--path-format=absolute", "--git-common-dir",
        ).stdout.strip())
        exclude = common / "info/exclude"
        before_exclude = exclude.read_bytes()
        assert before_bytes != dialogue.read_bytes()

        with pytest.raises(ValueError, match="tracked"):
            submit_teacher_task(
                run, case, "SYNTHETIC second task", Budget(), previous_root=first.id,
            )

        assert history.read_bytes() == before_bytes
        assert provider.git(target, "rev-parse", "HEAD").stdout == before_head
        assert provider.git(target, "ls-files", "--stage").stdout == before_index
        assert provider.git(target, "show", ":" + relative).stdout == before_staged_bytes
        assert exclude.read_bytes() == before_exclude
        assert [node.id for node in run.repo.all_tasks()] == [first.id]


@pytest.mark.parametrize("dirty_path", ["uncommitted.txt", ".evaluation/agent-note.txt"])
def test_registered_p3_context_does_not_hide_unregistered_dirty_work(tmp_path, dirty_path):
    case = load_cases(ROOT / "eval/tasks")[0]
    run, provider = make_engine(tmp_path)
    with run:
        state = configure_team(run, case)
        _, _, archive, _ = build_context(run, case, state)
        target, child = state.workspaces["lead"], state.workspaces["dev"]
        original_evidence = archive.read_bytes()
        dirty = target / dirty_path
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_bytes(b"uncommitted bytes must survive\n")
        (child / "delivery.txt").write_text("declared", encoding="utf-8")
        before_head = provider.git(target, "rev-parse", "HEAD").stdout
        before_index = provider.git(target, "ls-files", "--stage").stdout
        result = run.workspace.integrate(child, target, ["delivery.txt"])
        assert not result.ok and "dirty" in result.error
        assert dirty.read_bytes() == b"uncommitted bytes must survive\n"
        assert archive.read_bytes() == original_evidence
        assert not (target / "delivery.txt").exists()
        assert provider.git(target, "rev-parse", "HEAD").stdout == before_head
        assert provider.git(target, "ls-files", "--stage").stdout == before_index


@pytest.mark.parametrize("same_role", [False, True])
def test_framework_context_cannot_be_delivered_even_when_explicitly_named(tmp_path, same_role):
    case = load_cases(ROOT / "eval/tasks")[0]
    run, _ = make_engine(tmp_path)
    with run:
        state = configure_team(run, case)
        _, _, archive, _ = build_context(run, case, state, role="dev")
        child = state.workspaces["dev"]
        target = child if same_role else state.workspaces["lead"]
        relative = archive.relative_to(child).as_posix()
        original = archive.read_bytes()
        result = run.workspace.integrate(child, target, [relative])
        assert not result.ok
        assert archive.read_bytes() == original
        if not same_role:
            assert not (target / relative).exists()
