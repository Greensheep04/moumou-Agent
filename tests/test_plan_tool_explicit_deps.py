"""Model plans declare data-flow edges explicitly; Python plan defaults stay valid."""

from __future__ import annotations

from copy import deepcopy

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance, Plan, SubtaskSpec
from moumou.orchestrator import Engine, Settings


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=args)])


def settings(root):
    return Settings(root=root, project_id="p", conversation_id="c",
                    memory_enabled=False, role_review_enabled=False)


def writer_plan():
    return {
        "roles": [{"name": "writer", "title": "Writer",
                   "description": "Dedicated writer for explicit-deps recovery.",
                   "reason": "Produce the requested file",
                   "tools": ["write_file", "submit_result"]}],
        "subtasks": [{"key": "write", "role": "writer", "goal": "Write result.txt with agreed data",
                      "acceptance": [{"kind": "file_contains", "target": "result.txt",
                                      "expect": "agreed data"}]}],
    }


async def test_model_schema_requires_deps_without_changing_python_or_registry_defaults(tmp_path):
    captured = []

    def script(messages, tools):
        captured.extend(deepcopy(tools))
        return call("submit_result", summary="Schema inspected")

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        original = deepcopy(run.tools.get("submit_plan").params_schema)
        root = run.submit("Inspect the model-facing plan contract")
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        assert run.tools.get("submit_plan").params_schema == original
    plan_tool = next(item["function"] for item in captured
                     if item["function"]["name"] == "submit_plan")
    subtask = plan_tool["parameters"]["properties"]["subtasks"]["items"]
    assert "deps" in subtask["required"]
    assert subtask["properties"]["deps"]["type"] == "array"
    assert subtask["properties"]["deps"]["items"]["type"] == "string"
    assert "depends_on" not in subtask["properties"]
    assert SubtaskSpec(key="legacy", goal="An independent task", role="writer").deps == []
    assert Plan.model_validate(writer_plan()).subtasks[0].deps == []


async def test_missing_model_deps_is_correctable_before_roles_or_children_are_created(tmp_path):
    lead_calls = 0

    def script(messages, tools):
        nonlocal lead_calls
        if "Dedicated writer for explicit-deps recovery." in messages[0]["content"]:
            if messages[-1]["role"] != "tool":
                return call("write_file", path="result.txt", content="agreed data")
            return call("submit_result", summary="Produced the agreed data", files=["result.txt"])
        lead_calls += 1
        if lead_calls == 1:
            return call("submit_plan", **writer_plan())
        if lead_calls == 2:
            assert messages[-1]["role"] == "tool"
            assert "invalid_plan" in messages[-1]["content"]
            assert "deps" in messages[-1]["content"]
            assert run.repo.children(root.id) == []
            assert run.roles.get("p", "writer") is None
            assert run.repo.get(root.id).deps == []
            fixed = writer_plan()
            fixed["subtasks"][0]["deps"] = []
            return call("submit_plan", **fixed)
        return call("submit_result", summary="Delivered the declared file", files=["result.txt"])

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        root = run.submit("Delegate a writer and return result.txt", acceptance=[
            Acceptance(kind="file_contains", target="result.txt", expect="agreed data"),
        ])
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        assert run.repo.get(root.id + ".write").status == "DONE"
        assert len(run.repo.children(root.id)) == 1
        rejections = [e for e in run.trace.query(root.id)
                      if e.kind == "delegate" and e.name == "plan_rejected"]
        assert len(rejections) == 1
        assert lead_calls == 3


async def test_explicit_producer_edge_materializes_declared_file_before_consumer_runs(tmp_path):
    lead_calls, consumer_calls = 0, 0

    def script(messages, tools):
        nonlocal lead_calls, consumer_calls
        system = messages[0]["content"]
        if "A producer of dependency evidence." in system:
            if messages[-1]["role"] != "tool":
                return call("write_file", path="evidence.txt", content="producer evidence")
            return call("submit_result", summary="Declared the producer evidence",
                        files=["evidence.txt"])
        if "A consumer of explicit dependency evidence." in system:
            consumer_calls += 1
            assert "t1.produce" in messages[1]["content"]
            assert "evidence.txt" in messages[1]["content"]
            assert "artifact delivery: succeeded" in messages[1]["content"]
            if consumer_calls == 1:
                assert run.repo.get("t1.produce").status == "DONE"
                return call("read_file", path="evidence.txt")
            assert "producer evidence" in messages[-1]["content"]
            return call("submit_result", summary="Consumed the actual delivered evidence",
                        files=["evidence.txt"])
        lead_calls += 1
        if lead_calls == 1:
            return call("submit_plan", roles=[
                {"name": "producer", "title": "Producer",
                 "description": "A producer of dependency evidence.",
                 "reason": "Produce a file before it is consumed",
                 "tools": ["write_file", "submit_result"]},
                {"name": "consumer", "title": "Consumer",
                 "description": "A consumer of explicit dependency evidence.",
                 "reason": "Independently consume the declared producer output",
                 "tools": ["read_file", "submit_result"]},
            ], subtasks=[
                {"key": "produce", "role": "producer", "deps": [],
                 "goal": "Produce and declare evidence.txt", "acceptance": [
                     {"kind": "file_contains", "target": "evidence.txt",
                      "expect": "producer evidence"}]},
                {"key": "consume", "role": "consumer", "deps": ["produce"],
                 "goal": "Read the producer's evidence.txt and explicitly return it",
                 "acceptance": [{"kind": "file_contains", "target": "evidence.txt",
                                 "expect": "producer evidence"}]},
            ])
        return call("submit_result", summary="Delivered the consumed evidence",
                    files=["evidence.txt"])

    with Engine(FakeLLM(script), settings=settings(tmp_path / "run")) as run:
        root = run.submit("Produce evidence, then have a distinct consumer read and return it")
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        consumer = run.repo.get("t1.consume")
        assert consumer.status == "DONE" and consumer.deps == ["t1.produce"]
        assert consumer_calls == 2
        transfers = [event for event in run.trace.query(consumer.id)
                     if event.name == "materialised_from_dep"]
        assert len(transfers) == 1
        assert transfers[0].payload["source"] == "t1.produce"
        assert transfers[0].payload["files"] == ["evidence.txt"]


def test_direct_python_delegation_keeps_legacy_omitted_deps_contract(tmp_path):
    with Engine(FakeLLM([]), settings=settings(tmp_path / "run")) as run:
        root = run.submit("A legacy direct Python caller creates an independent writer")
        run.repo.transition(root.id, "RUNNING", "direct-python-caller")
        legacy_payload = writer_plan()
        assert "deps" not in legacy_payload["subtasks"][0]
        children = run.delegation.apply(run.repo.get(root.id), legacy_payload)
        assert [child.id for child in children] == [root.id + ".write"]
        assert children[0].deps == []
        assert run.repo.get(root.id).status == "PENDING"
