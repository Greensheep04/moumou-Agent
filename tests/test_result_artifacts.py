"""Explicit result files must become validated, durable dependency handoffs."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance, Budget
from moumou.eval.cases import load_cases
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings

ROOT = Path(__file__).resolve().parents[1]


def answer(name, **args):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=args)])


async def test_unchanged_role_file_is_persisted_and_delivered_to_consumer(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]
    lead_calls = 0

    def script(messages, tools):
        nonlocal lead_calls
        system = messages[0]["content"]
        if case.roles[1].description in system:
            return answer("submit_result", summary="Export existing source", files=["module.py"])
        if case.roles[3].description in system:
            return answer("submit_result", summary="Consumer received source", files=["module.py"])
        lead_calls += 1
        if lead_calls == 1:
            return answer("submit_plan", reasoning="Export unchanged input before consuming it",
                          roles=[], subtasks=[{
                              "key": "export", "role": "dev", "goal": "Export existing module.py",
                              "acceptance": [{"kind": "file_exists", "target": "module.py"}],
                              "budget": {"max_retries": 0},
                          }, {
                              "key": "consume", "role": "release", "deps": ["export"],
                              "goal": "Receive module.py through the explicit dependency handoff",
                              "acceptance": [{"kind": "file_contains", "target": "module.py",
                                              "expect": "VALUE = 7"}],
                              "budget": {"max_retries": 0},
                          }])
        return answer("submit_result", summary="Delivered source", files=["module.py"])

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="scenario_1", conversation_id="group",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        source = state.workspaces["dev"] / "module.py"
        source.write_bytes(b"VALUE = 7\n")
        private = state.workspaces["dev"] / "private.txt"
        private.write_text("never declared", encoding="utf-8")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        task = submit_teacher_task(run, case, "Export existing source without changing it",
                                   Budget(max_retries=0), acceptance=[
                                       Acceptance(kind="file_contains", target="module.py",
                                                  expect="VALUE = 7"),
                                   ])
        await run.run()
        exported = run.repo.get("t1.export")
        assert exported.status == "DONE"
        assert exported.result.files == ["module.py"]
        assert exported.artifacts == ["module.py"], "explicit result files were not published"
        assert run.repo.get("t1.consume").status == "DONE"
        assert run.repo.get(task.id).status == "DONE"
        for role in ("dev", "release", "lead"):
            delivered = state.workspaces[role] / "module.py"
            assert hashlib.sha256(delivered.read_bytes()).hexdigest() == before
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert not (state.workspaces["release"] / "private.txt").exists()
        assert not (state.workspaces["lead"] / "private.txt").exists()


@pytest.mark.parametrize("invalid_kind", [
    "missing", "directory", "absolute", "parent", "git", "blank",
    "files_type", "entry_type", "evaluation",
])
async def test_invalid_export_batch_is_atomic_and_can_be_corrected(tmp_path, invalid_kind):
    case = load_cases(ROOT / "eval/tasks")[0]
    calls = 0
    invalid_files = None

    def script(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return answer("submit_result", summary="Invalid export batch", files=invalid_files)
        assert calls == 2, "invalid declarations should permit a corrected tool result"
        feedback = messages[-1]
        assert feedback["role"] == "tool"
        assert "ok=false" in feedback["content"]
        assert "invalid_result_files" in feedback["content"]
        current = run.repo.get("t1")
        assert current.status == "RUNNING" and current.result is None
        assert current.artifacts == [], "the valid prefix must not leak from a rejected batch"
        assert not [e for e in run.trace.query("t1") if e.kind == "acceptance"]
        return answer("submit_result", summary="Corrected export", files=["safe.txt"])

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="scenario_1", conversation_id="group",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        workspace = state.workspaces["lead"]
        safe = workspace / "safe.txt"
        safe.write_bytes(b"original unchanged input\n")
        before = hashlib.sha256(safe.read_bytes()).hexdigest()
        (workspace / "directory").mkdir()
        (workspace / ".git").mkdir()
        (workspace / ".git/config").write_text("private metadata", encoding="utf-8")
        (workspace / ".evaluation").mkdir()
        (workspace / ".evaluation/prior-dialogue.txt").write_text(
            "private evidence", encoding="utf-8",
        )
        outside = workspace.parent / "outside.txt"
        outside.write_text("outside this agent", encoding="utf-8")
        paths = {
            "missing": "missing.txt", "directory": "directory", "absolute": str(safe.resolve()),
            "parent": "../outside.txt", "git": ".git/config", "blank": "",
            "evaluation": ".evaluation/prior-dialogue.txt",
        }
        if invalid_kind == "files_type":
            invalid_files = "safe.txt"
        elif invalid_kind == "entry_type":
            invalid_files = ["safe.txt", 123]
        else:
            invalid_files = ["safe.txt", paths[invalid_kind]]
        node = submit_teacher_task(run, case, "Publish a validated existing file",
                                   Budget(max_retries=0, max_steps=4), acceptance=[
                                       Acceptance(kind="file_exists", target="safe.txt"),
                                   ])
        await run.run()
        saved = run.repo.get(node.id)
        assert calls == 2
        assert saved.status == "DONE" and saved.error is None
        assert saved.artifacts == ["safe.txt"]
        assert saved.result.files == ["safe.txt"]
        assert hashlib.sha256(safe.read_bytes()).hexdigest() == before
        assert outside.read_text(encoding="utf-8") == "outside this agent"
        assert (workspace / ".git/config").read_text(encoding="utf-8") == "private metadata"
        accepted = [e for e in run.trace.query("t1") if e.kind == "acceptance"
                    and e.name == "verdict"]
        assert len(accepted) == 1 and accepted[0].payload["ok"] is True


async def test_explicit_exports_merge_with_tool_artifacts_and_keep_recorded_deletion(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]
    calls = 0

    def script(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return answer("write_file", path="generated.txt", content="generated by a tool")
        if calls == 2:
            return answer("delete_file", path="obsolete.txt")
        assert calls == 3
        return answer("submit_result", summary="Existing and changed files",
                      files=["generated.txt", "existing.txt", "./existing.txt", "existing.txt"])

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="scenario_1", conversation_id="group",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        workspace = state.workspaces["lead"]
        existing = workspace / "existing.txt"
        existing.write_bytes(b"untouched existing input\n")
        before = hashlib.sha256(existing.read_bytes()).hexdigest()
        (workspace / "obsolete.txt").write_text("delete only through the recorded tool",
                                               encoding="utf-8")
        (workspace / "private.txt").write_text("not declared", encoding="utf-8")
        node = submit_teacher_task(run, case, "Merge explicit exports with recorded file changes",
                                   Budget(max_retries=0, max_steps=5), acceptance=[
                                       Acceptance(kind="file_exists", target="existing.txt"),
                                       Acceptance(kind="file_exists", target="generated.txt"),
                                       Acceptance(kind="file_not_exists", target="obsolete.txt"),
                                   ])
        await run.run()
        saved = run.repo.get(node.id)
        assert saved.status == "DONE", saved.error
        assert saved.artifacts == ["generated.txt", "obsolete.txt", "existing.txt"]
        assert saved.artifacts.count("existing.txt") == saved.artifacts.count("generated.txt") == 1
        assert "private.txt" not in saved.artifacts
        assert not (workspace / "obsolete.txt").exists()
        assert hashlib.sha256(existing.read_bytes()).hexdigest() == before
        assert (workspace / "generated.txt").read_text(encoding="utf-8") == "generated by a tool"
        assert any(event.kind == "tool" and event.name == "delete_file"
                   and event.payload["ok"] for event in run.trace.query(node.id))


async def test_failed_acceptance_does_not_publish_declared_files_or_drop_tool_artifacts(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]
    calls = 0

    def script(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return answer("write_file", path="generated.txt", content="wrong")
        return answer("submit_result", summary="Acceptance must still run", files=["existing.txt"])

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="scenario_1", conversation_id="group",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        existing = state.workspaces["lead"] / "existing.txt"
        existing.write_bytes(b"unchanged input\n")
        before = hashlib.sha256(existing.read_bytes()).hexdigest()
        node = submit_teacher_task(run, case, "Reject an incorrect generated answer",
                                   Budget(max_retries=0), acceptance=[
                                       Acceptance(kind="file_contains", target="generated.txt",
                                                  expect="^correct$"),
                                   ])
        await run.run()
        saved = run.repo.get(node.id)
        assert saved.status == "DEAD"
        assert saved.artifacts == ["generated.txt"]
        assert saved.result.files == ["existing.txt"]
        assert hashlib.sha256(existing.read_bytes()).hexdigest() == before


async def test_acceptance_removing_export_file_requires_correction_before_publication(tmp_path):
    case = load_cases(ROOT / "eval/tasks")[0]
    code = (
        "from pathlib import Path; "
        "marker=Path('checked-once'); "
        "Path('moving.txt').unlink() if not marker.exists() else None; "
        "marker.write_text('checked',encoding='utf-8')"
    )
    arguments = [sys.executable, "-B", "-c", code]
    command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    calls = 0

    def script(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return answer("submit_result", summary="Initially present export", files=["moving.txt"])
        if calls == 2:
            assert messages[-1]["role"] == "tool"
            assert "invalid_result_files" in messages[-1]["content"]
            current = run.repo.get("t1")
            assert current.status == "RUNNING" and current.result is None
            assert current.artifacts == []
            assert not (state.workspaces["lead"] / "moving.txt").exists()
            return answer("write_file", path="moving.txt", content="temporary export\n")
        assert calls == 3
        return answer("submit_result", summary="Restored and validated export",
                      files=["moving.txt"])

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="scenario_1", conversation_id="group",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        state = configure_team(run, case)
        source = state.workspaces["lead"] / "moving.txt"
        source.write_bytes(b"temporary export\n")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        node = submit_teacher_task(run, case, "Publish only files surviving real acceptance",
                                   Budget(max_retries=0, max_steps=5), acceptance=[
                                       Acceptance(kind="command", target=command, expect="0"),
                                   ])
        await run.run()
        saved = run.repo.get(node.id)
        assert saved.status == "DONE", saved.error
        assert saved.artifacts == ["moving.txt"] and saved.result.files == ["moving.txt"]
        assert "checked-once" not in saved.artifacts
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        checks = [event for event in run.trace.query(node.id)
                  if event.kind == "acceptance" and event.payload.get("kind") == "command"]
        assert len(checks) == 2
        assert all(event.payload["ok"] and event.payload["exit_code"] == 0 for event in checks)
