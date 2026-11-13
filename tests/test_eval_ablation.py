"""Four real Engine groups: independent memory, observed prompts and overlap."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.context.builder import BOARD_HEADER, LESSONS_HEADER
from moumou.contracts import MemoryRecord, TraceEvent
from moumou.eval.ablation import GROUPS, plan_ablation, run_ablation
from moumou.eval.loader import TaskFixture
from moumou.eval.options import EvaluationOptions, memory_snapshot_sha256
from moumou.memory import SqliteMemoryStore, memory_scope
from moumou.prompts import LEAD_ROLE

LESSON = "For portable receipt reports, validate the input before writing the receipt."


def fixtures():
    return [
        TaskFixture.model_validate({
            "id": "memory_receipt", "title": "Independent receipt", "scenario": "use_memory",
            "goal": "heldout: portable receipt report for the new input",
            "setup": {"heldout-input.txt": "a new independent input"},
            "acceptance": [{
                "kind": "file_contains", "target": "receipt.txt", "expect": "validated",
            }],
            "requirements": {"memory_injection": True},
            "budget": {"max_retries": 0},
            "warmup": {
                "goal": "warmup: practise portable receipt reports on training input",
                "setup": {"training-input.txt": "unrelated training values"},
                "acceptance": [{
                    "kind": "file_contains", "target": "training-receipt.txt",
                    "expect": "training only",
                }],
                "budget": {"max_retries": 0},
            },
        }),
        TaskFixture.model_validate({
            "id": "parallel_receipts", "title": "Independent delegated receipts",
            "scenario": "parallel",
            "goal": "heldout: parallel probe producing alpha.txt and beta.txt",
            "acceptance": [
                {"kind": "file_contains", "target": "alpha.txt", "expect": "alpha"},
                {"kind": "file_contains", "target": "beta.txt", "expect": "beta"},
            ],
            "requirements": {"min_children": 2, "parallel_children": True},
            "budget": {"max_retries": 0},
        }),
    ]


class ProbeLLM(FakeLLM):
    """One policy for all groups; real scheduling awaits independent child calls."""

    def __init__(self, *, produce_lesson=True):
        super().__init__([])
        self.produce_lesson = produce_lesson
        self.closed = False

    async def chat(self, messages, tools=None):
        self.calls.append(copy.deepcopy(messages))
        names = {tool.get("function", {}).get("name") for tool in tools or []}
        if "submit_lesson" in names:
            lessons = [{
                "situation": "portable receipt reports",
                "action": "validate the independent input before writing",
                "outcome": "success", "lesson": LESSON,
            }] if self.produce_lesson else []
            return LLMResponse(tool_calls=[ToolCall(
                id="lesson", name="submit_lesson", args={"lessons": lessons},
            )])
        system, brief = messages[0]["content"], messages[1]["content"]
        steps = sum(message["role"] == "assistant" for message in messages)
        if "parallel probe" in brief and "You are the lead agent" in system:
            if "(this task has no upstream dependencies)" in brief:
                plan = {
                    "reasoning": "independent receipts", "roles": [{
                        "name": "receipt_writer", "title": "Receipt writer",
                        "description": "writes an independent receipt", "tools": ["write_file"],
                        "reason": "parallel independent output",
                    }],
                    "subtasks": [{
                        "key": name, "goal": f"child receipt {name}", "role": "receipt_writer",
                        "deps": [], "acceptance": [{
                            "kind": "file_contains", "target": f"{name}.txt", "expect": name,
                        }],
                    } for name in ("alpha", "beta")],
                }
                return LLMResponse(tool_calls=[ToolCall(id="plan", name="submit_plan", args=plan)])
            return LLMResponse(content="both independently checked receipts are ready")
        if "child receipt" in brief:
            name = "alpha" if "child receipt alpha" in brief else "beta"
            if steps == 0:
                await asyncio.sleep(0.03)
                return LLMResponse(tool_calls=[ToolCall(
                    id="write", name="write_file", args={"path": f"{name}.txt", "content": name},
                )])
            return LLMResponse(content=f"wrote {name}.txt")
        if steps == 0:
            warmup = "warmup:" in brief
            return LLMResponse(tool_calls=[ToolCall(id="write", name="write_file", args={
                "path": "training-receipt.txt" if warmup else "receipt.txt",
                "content": "checked training only" if warmup else "validated independent input",
            })])
        return LLMResponse(content="receipt is ready")

    async def aclose(self):
        self.closed = True


class ProbeFactory:
    def __init__(self, *, produce_lesson=True, fail_at=None):
        self.clients = []
        self.produce_lesson = produce_lesson
        self.fail_at = fail_at

    def __call__(self):
        if self.fail_at == len(self.clients):
            raise RuntimeError("deliberate client initialization failure")
        client = ProbeLLM(produce_lesson=self.produce_lesson)
        self.clients.append(client)
        return client


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sibling_llm_overlap(result):
    children = {
        node["id"] for node in result["nodes"] if node["parent_id"] == result["root_task_id"]
    }
    lines = Path(result["artifacts"]["trace_jsonl"]).read_text(encoding="utf-8").splitlines()
    events = [
        TraceEvent.model_validate_json(line)
        for line in lines
    ]
    spans = [
        event for event in events
        if event.task_id in children and event.kind == "llm" and event.t1 is not None
    ]
    return any(
        left.task_id != right.task_id and left.t0 < right.t1 and right.t0 < left.t1
        for left in spans for right in spans
    )


def test_formal_plan_is_180_predeclared_attempts_without_execution():
    source = fixtures()[0]
    tasks = [source.model_copy(update={"id": f"task_{index}"}) for index in range(15)]
    plan = plan_ablation(tasks)
    assert plan["planned_runs"] == 180
    assert plan["harness_concurrency"] == 1
    assert plan == plan_ablation(tasks)
    for repeat in range(3):
        for fixture in tasks:
            rows = [row for row in plan["schedule"] if row["repeat"] == repeat
                    and row["fixture_id"] == fixture.id]
            assert {row["group"] for row in rows} == {group[0] for group in GROUPS}
            assert all(row["role_review_enabled"] is False for row in rows)
    assert len({row["relative_dir"] for row in plan["schedule"]}) == 180
    for row in plan["schedule"]:
        assert row["fixture_id"] == tasks[row["fixture_index"]].id
        assert row["group"] == GROUPS[row["group_index"]][0]
        assert row["relative_dir"] == (
            f"heldout/r{row['repeat']}/f{row['fixture_index']}/g{row['group_index']}"
        )


@pytest.mark.parametrize("repeats", [None, True, 0, 1.5])
def test_ablation_repeats_are_explicit_positive_integers(repeats):
    with pytest.raises(ValueError, match="positive integer"):
        plan_ablation(fixtures(), repeats=repeats)


@pytest.mark.parametrize("values", [
    {"memory_enabled": 1}, {"memory_enabled": "false"}, {"max_slots": True},
    {"max_slots": 0}, {"max_slots": 1.5}, {"unknown_flag": True},
])
def test_experimental_settings_reject_ambiguous_controls(values):
    with pytest.raises(ValidationError):
        EvaluationOptions(**values)


@pytest.mark.parametrize("identity", [
    "", ".", "..", "../outside", "/absolute", "C:\\absolute", "C:relative",
    "nested/path", "nested\\path", "CON", "LPT1.txt", "trailing.", "trailing ",
])
@pytest.mark.parametrize("field", ["project_id", "conversation_id", "warmup_id"])
def test_option_identities_cannot_escape_or_use_windows_reserved_paths(field, identity):
    with pytest.raises(ValidationError):
        EvaluationOptions(**{field: identity})


def test_snapshot_is_ordered_copied_and_project_scoped():
    first = MemoryRecord(
        id="b", project_id="p", role=LEAD_ROLE, task_id="warmup-task",
        situation="training", action="check", outcome="success", lesson="check inputs",
    )
    second = first.model_copy(update={"id": "a"}, deep=True)
    options = EvaluationOptions(project_id="p", memory_records=[first, second])
    assert options.memory_sha256 == memory_snapshot_sha256([second, first])
    assert [record["id"] for record in options.memory_snapshot()] == ["a", "b"]
    first.lesson = "caller mutated the source"
    assert options.memory_records[0].lesson == "check inputs"
    copied = options.records_for_project("p")
    copied[0].score = 3
    assert options.memory_records[0].score == 1
    with pytest.raises(ValueError, match="project"):
        options.records_for_project("other")
    with pytest.raises(ValidationError, match="duplicate"):
        EvaluationOptions(memory_records=[second, second])
    with pytest.raises(ValidationError, match="finite"):
        EvaluationOptions(memory_records=[second.model_copy(update={"score": float("nan")})])


async def test_eight_real_groups_have_fair_seeds_prompt_evidence_and_actual_overlap(tmp_path):
    factory = ProbeFactory()
    root = tmp_path / "experiment"
    manifest = await run_ablation(fixtures(), factory, root, repeats=1)
    assert manifest["status"] == "completed"
    assert manifest["completed_runs"] == manifest["planned_runs"] == 8
    assert len(manifest["warmups"]) == 1
    assert len(factory.clients) == 9
    assert all(client.closed for client in factory.clients)
    assert all(job["purpose"] == "offline_validation" for job in manifest["jobs"])
    assert all(job["results"][0]["root_success"] is True for job in manifest["jobs"])

    memory_jobs = [job for job in manifest["jobs"] if job["fixture_id"] == "memory_receipt"]
    assert len({job["memory_sha256"] for job in memory_jobs}) == 1
    memory_paths = []
    for job in memory_jobs:
        result = job["results"][0]
        memory_path = Path(result["artifacts"]["memory_database"])
        memory_paths.append(memory_path)
        store = SqliteMemoryStore(memory_path)
        try:
            records = store.all(memory_scope("memory_receipt", LEAD_ROLE))
        finally:
            store.close()
        assert len(records) == 1
        assert records[0].uses == (1 if job["memory_enabled"] else 0)
        assert len(result["nodes"]) == 1
        assert not list(Path(result["artifacts"]["workspaces"]).rglob("training-receipt.txt"))
        with sqlite3.connect(Path(result["run_dir"]) / "moumou.db") as connection:
            values = connection.execute("SELECT DISTINCT conversation_id FROM tasks").fetchall()
        assert values == [(job["options"]["conversation_id"],)]
    assert len(set(memory_paths)) == 4
    heldout_clients = factory.clients[1:5]
    for job, client in zip(memory_jobs, heldout_clients, strict=True):
        system = client.calls[0][0]["content"]
        assert (LESSONS_HEADER in system) is job["memory_enabled"]
        assert (LESSON in system) is job["memory_enabled"]
        assert BOARD_HEADER not in system

    for job in manifest["jobs"]:
        if job["fixture_id"] == "parallel_receipts":
            result = job["results"][0]
            assert len(result["nodes"]) == 3
            assert sibling_llm_overlap(result) is (job["max_slots"] == 4)
    warmup = read_json(root / manifest["warmups"][0]["relative_dir"] / "warmup.json")
    assert warmup["explicit_reflection"] is True
    assert warmup["api_calls"] == 0
    assert warmup["llm_call_count"] == 3
    assert warmup["execution_mode"] == "in_process"
    assert warmup["heldout_hard_timeout_applies"] is False


async def test_each_repeat_rebuilds_warmup_and_never_learns_from_other_groups(tmp_path):
    manifest = await run_ablation([fixtures()[0]], ProbeFactory(), tmp_path / "repeat", repeats=2)
    assert len(manifest["warmups"]) == 2
    assert manifest["completed_runs"] == 8
    hashes = []
    for repeat in range(2):
        jobs = [job for job in manifest["jobs"] if job["repeat"] == repeat]
        assert len({job["memory_sha256"] for job in jobs}) == 1
        hashes.append(jobs[0]["memory_sha256"])
        for job in jobs:
            assert job["options"]["memory_records"][0]["uses"] == 0
            assert job["options"]["memory_records"][0]["score"] == 1
    assert hashes[0] != hashes[1]


async def test_no_lesson_is_a_recorded_warmup_failure_not_a_fabricated_seed(tmp_path):
    factory = ProbeFactory(produce_lesson=False)
    root = tmp_path / "no-lesson"
    with pytest.raises(RuntimeError, match="no reusable planner lesson"):
        await run_ablation([fixtures()[0]], factory, root, repeats=1)
    manifest = read_json(root / "ablation.json")
    assert manifest["status"] == "error"
    assert manifest["completed_runs"] == 0
    assert manifest["jobs"] == []
    warmup_dir = root / manifest["warmups"][0]["relative_dir"]
    assert read_json(warmup_dir / "warmup.json")["status"] == "error"
    assert not (warmup_dir / "memory-snapshot.json").exists()
    assert factory.clients[0].closed


async def test_later_factory_failure_keeps_the_completed_group_and_report(tmp_path):
    root = tmp_path / "partial"
    factory = ProbeFactory(fail_at=1)
    with pytest.raises(RuntimeError, match="client initialization failure"):
        await run_ablation([fixtures()[1]], factory, root, repeats=1)
    manifest = read_json(root / "ablation.json")
    assert manifest["status"] == "error"
    assert manifest["completed_runs"] == 1
    job = manifest["jobs"][0]
    assert job["status"] == "completed"
    assert Path(job["results"][0]["artifacts"]["result"]).exists()
    assert (root / "report.md").is_file()
    assert factory.clients[0].closed


async def test_missing_warmup_fails_before_creating_an_experiment(tmp_path):
    fixture = fixtures()[0].model_copy(update={"warmup": None})
    root = tmp_path / "no-warmup"
    with pytest.raises(ValueError, match="independent warmup"):
        await run_ablation([fixture], ProbeFactory(), root, repeats=1)
    assert not root.exists()


async def test_reused_client_and_existing_directory_are_not_silently_accepted(tmp_path):
    client = ProbeLLM()
    root = tmp_path / "reused"
    with pytest.raises(ValueError, match="reused a client"):
        await run_ablation([fixtures()[1]], lambda: client, root, repeats=1)
    with pytest.raises(FileExistsError):
        await run_ablation([fixtures()[1]], ProbeFactory(), root, repeats=1)


class WaitingWarmup(ProbeLLM):
    def __init__(self, entered):
        super().__init__()
        self.entered = entered
        self.active_calls = 0

    async def chat(self, messages, tools=None):
        self.active_calls += 1
        self.entered.set()
        try:
            await asyncio.sleep(60)
            return await super().chat(messages, tools)
        finally:
            self.active_calls -= 1


async def test_cancelled_warmup_drains_owned_runners_before_closing_resources(tmp_path):
    entered = asyncio.Event()
    client = WaitingWarmup(entered)
    root = tmp_path / "cancelled-warmup"
    task = asyncio.create_task(run_ablation([fixtures()[0]], lambda: client, root, repeats=1))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    manifest = read_json(root / "ablation.json")
    assert manifest["status"] == "interrupted"
    assert manifest["jobs"] == []
    warmup = read_json(root / manifest["warmups"][0]["relative_dir"] / "warmup.json")
    assert warmup["status"] == "interrupted"
    assert warmup["cooperative_cleanup_complete"] is True
    assert client.active_calls == 0
    assert client.closed


async def test_warmup_soft_deadline_cancels_a_cooperative_model_wait(tmp_path):
    entered = asyncio.Event()
    client = WaitingWarmup(entered)
    fixture = fixtures()[0]
    fixture.warmup.budget.max_wall_s = 10
    root = tmp_path / "timeout-warmup"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_ablation([fixture], lambda: client, root, repeats=1), timeout=15,
        )
    manifest = read_json(root / "ablation.json")
    assert manifest["status"] == "error"
    warmup = read_json(root / manifest["warmups"][0]["relative_dir"] / "warmup.json")
    assert warmup["error_type"] == "TimeoutError"
    assert warmup["cooperative_timeout_s"] == 10
    assert warmup["cooperative_cleanup_complete"] is True
    assert client.active_calls == 0
    assert client.closed


async def test_nested_output_root_uses_compact_paths_without_losing_group_identity(tmp_path):
    root = tmp_path / "reasonable" / "nested" / "out"
    manifest = await run_ablation(fixtures(), ProbeFactory(), root, repeats=1)
    assert manifest["status"] == "completed"
    assert manifest["completed_runs"] == 8
    assert manifest["code"]["source_sha256"]
    assert len(manifest["fixtures"]) == 2
    for job in manifest["jobs"]:
        assert job["group"] == GROUPS[job["group_index"]][0]
        assert job["options"]["conversation_id"] == f"h0-g{job['group_index']}"
        assert job["results"][0]["root_success"] is True
        assert (root / job["relative_dir"] / "manifest.json").is_file()
    warmup = read_json(root / manifest["warmups"][0]["relative_dir"] / "warmup.json")
    assert warmup["settings"]["conversation_id"] == "w0"
    assert warmup["warmup_id"] == "memory_receipt-r0-warmup"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path budget policy")
async def test_overlong_root_is_rejected_before_client_factory_or_output_creation(tmp_path):
    root = tmp_path / ("output-root-" + "x" * 160)
    factory = ProbeFactory()
    with pytest.raises(ValueError, match="shorter --out"):
        await run_ablation(fixtures(), factory, root, repeats=1)
    assert factory.clients == []
    assert not root.exists()
