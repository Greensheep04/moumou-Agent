"""Evaluation exercises the selected provider with isolated, committed inputs."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM, GitWorkspaces
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance
from moumou.demo.skeleton import PLAN, responder
from moumou.eval.cli import parse_args
from moumou.eval.loader import TaskFixture
from moumou.eval.runner import run_all, run_once


def fixture(**changes):
    values = dict(id="p1_eval", title="P1 evaluation", scenario="parallel", goal="build hello")
    values.update(changes)
    return TaskFixture(**values)


def parent_path(result):
    return GitWorkspaces(Path(result.run_dir) / "workspaces").allocate(
        result.fixture_id, "c1", "planner@t1"
    )


def events(result):
    return [json.loads(line) for line in (Path(result.run_dir) / "trace.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()]


def test_cli_selects_workspace_backend():
    assert parse_args([]).workspace_backend == "local"
    assert parse_args(["--workspace-backend", "git"]).workspace_backend == "git"
    with pytest.raises(SystemExit):
        parse_args(["--workspace-backend", "other"])


async def test_default_backend_preserves_local_layout(tmp_path):
    result = await run_once(fixture(), FakeLLM(responder), tmp_path, 0)
    assert result.done == 3 and result.dead == 0
    parent = Path(result.run_dir) / "workspaces" / "p1_eval" / "c1" / "planner@t1"
    assert (parent / "hello.py").read_text(encoding="utf-8").strip() == "print('hello, moumou')"
    assert not (Path(result.run_dir) / "workspaces" / ".repos").exists()


async def test_git_backend_delivers_parallel_artifacts(tmp_path):
    task = fixture(acceptance=[Acceptance(kind="file_exists", target="README.md")])
    result = await run_once(task, FakeLLM(responder), tmp_path, 0, workspace_backend="git")
    assert result.done == 3 and result.dead == 0
    parent = parent_path(result)
    assert (parent / ".git").is_file()
    assert (parent / "hello.py").is_file() and (parent / "README.md").is_file()
    provider = GitWorkspaces(Path(result.run_dir) / "workspaces")
    assert "P1 declared artifact delivery" in provider.git(parent, "log", "--oneline").stdout
    assert any(e["name"] == "integrated_into_parent" for e in events(result))


async def test_seed_is_visible_in_child_and_repair_delivers_to_clean_parent(tmp_path):
    plan = deepcopy(PLAN)
    plan["roles"] = [dict(plan["roles"][0], tools=["read_file", "apply_patch"])]
    plan["subtasks"] = [dict(plan["subtasks"][0], goal="repair calc.py", acceptance=[
        {"kind": "file_contains", "target": "calc.py", "expect": "return a \\+ b"}
    ])]
    reads = []

    def repair(messages, tools):
        system, brief = messages[0]["content"], messages[1]["content"]
        if "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" in brief:
                return LLMResponse(tool_calls=[ToolCall(id="p", name="submit_plan", args=plan)])
            return LLMResponse(content="repair delivered")
        step = sum(m["role"] == "assistant" for m in messages)
        if step == 0:
            return LLMResponse(tool_calls=[
                ToolCall(id="r", name="read_file", args={"path": "calc.py"})
            ])
        if step == 1:
            reads.append(str(messages[-1]))
            return LLMResponse(tool_calls=[ToolCall(id="w", name="apply_patch", args={
                "path": "calc.py", "old": "return a - b", "new": "return a + b"
            })])
        return LLMResponse(content="fixed")

    task = fixture(setup={"calc.py": "def add(a, b):\n    return a - b\n"}, acceptance=[
        Acceptance(kind="file_contains", target="calc.py", expect=r"return a \+ b")
    ])
    result = await run_once(task, FakeLLM(repair), tmp_path, 0, workspace_backend="git")
    assert result.done == 2 and result.dead == 0
    assert reads and "return a - b" in reads[0]
    parent = parent_path(result)
    assert "return a + b" in (parent / "calc.py").read_text(encoding="utf-8")
    provider = GitWorkspaces(Path(result.run_dir) / "workspaces")
    assert provider.git(parent, "status", "--porcelain").stdout == ""
    assert not any(e["name"] == "materialise_failed" for e in events(result))


async def test_repeats_use_independent_git_baselines(tmp_path):
    results = await run_all([fixture(repeats=2, setup={"input.txt": "original"})],
                            FakeLLM(responder), tmp_path, workspace_backend="git")
    assert len(results) == 2 and all(r.done == 3 and not r.dead for r in results)
    first, second = map(parent_path, results)
    (first / "input.txt").write_text("changed", encoding="utf-8")
    assert (second / "input.txt").read_text(encoding="utf-8") == "original"
    provider = GitWorkspaces(Path(results[1].run_dir) / "workspaces")
    assert provider.git(second, "status", "--porcelain").stdout == ""
    assert first != second


async def test_reusing_repeat_directory_is_rejected(tmp_path):
    await run_once(fixture(), FakeLLM(responder), tmp_path, 0, workspace_backend="git")
    with pytest.raises(FileExistsError):
        await run_once(fixture(), FakeLLM(responder), tmp_path, 0, workspace_backend="git")


@pytest.mark.parametrize("backend", ["local", "git"])
@pytest.mark.parametrize("path", ["../../escape.txt", ".git/config", "sub/../../escape.txt",
                                  "C:/escape.txt", "../outside.txt", "sub/.GIT/config"])
async def test_setup_rejects_outside_and_metadata_paths(tmp_path, backend, path):
    with pytest.raises((ValueError, PermissionError)):
        await run_once(fixture(setup={path: "bad"}), FakeLLM(responder), tmp_path, 0,
                       workspace_backend=backend)
    assert not (tmp_path / "escape.txt").exists()


async def test_fixture_id_cannot_escape_output_root(tmp_path):
    with pytest.raises((ValueError, PermissionError)):
        await run_once(fixture(id="../../escaped"), FakeLLM(responder), tmp_path / "out", 0,
                       workspace_backend="git")
    assert not (tmp_path / "escaped").exists()


async def test_unknown_backend_fails_before_model_calls(tmp_path):
    llm = FakeLLM(responder)
    with pytest.raises(ValueError, match="backend"):
        await run_once(fixture(), llm, tmp_path, 0, workspace_backend="typo")
    assert llm.calls == []


async def test_cli_passes_selected_backend_to_real_runner(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from moumou.eval import cli
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder")
    fake = FakeLLM(responder)
    fake.aclose = AsyncMock()
    monkeypatch.setattr(cli, "DeepSeekClient", lambda **kwargs: fake)
    args = parse_args(["--only", "s1_hello", "--workspace-backend", "git",
                       "--out", str(tmp_path)])
    assert await cli.main_async(args) == 0
    parent = GitWorkspaces(tmp_path / "s1_hello/r0/workspaces").allocate(
        "s1_hello", "c1", "planner@t1"
    )
    assert (parent / ".git").is_file() and (parent / "hello.py").is_file()
    fake.aclose.assert_awaited_once()


async def test_git_evaluation_materialises_dependency(tmp_path):
    from test_orchestration import sequential_llm
    result = await run_once(fixture(), sequential_llm(), tmp_path, 0, workspace_backend="git")
    assert result.done == 3 and result.dead == 0
    child = GitWorkspaces(Path(result.run_dir) / "workspaces").allocate(
        result.fixture_id, "c1", "python_dev@t1.verify"
    )
    assert (child / "hello.py").is_file()
    assert any(e["name"] == "materialised_from_dep" for e in events(result))


async def test_git_evaluation_reports_conflict_without_losing_target(tmp_path):
    plan = deepcopy(PLAN)
    for task in plan["subtasks"]:
        task["acceptance"] = [{"kind": "file_exists", "target": "same.txt"}]

    def conflicting(messages, tools):
        system, brief = messages[0]["content"], messages[1]["content"]
        if "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" in brief:
                return LLMResponse(tool_calls=[ToolCall(id="p", name="submit_plan", args=plan)])
            return LLMResponse(content="conflict recorded")
        if not any(m["role"] == "assistant" for m in messages):
            return LLMResponse(tool_calls=[ToolCall(id="w", name="write_file", args={
                "path": "same.txt", "content": "A" if "role: python_dev" in system else "B"
            })])
        return LLMResponse(content="written")

    result = await run_once(fixture(), FakeLLM(conflicting), tmp_path, 0, workspace_backend="git")
    assert result.done == 3 and result.dead == 0
    conflicts = [e for e in events(result) if e["name"] == "integration_conflict"]
    assert len(conflicts) == 1 and conflicts[0]["payload"]["conflicts"] == ["same.txt"]
    assert (parent_path(result) / "same.txt").read_text(encoding="utf-8") in {"A", "B"}
