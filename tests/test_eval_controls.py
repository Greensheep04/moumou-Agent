"""Controls and behavior gates must survive the actual Windows worker boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.contracts import Acceptance, Budget, MemoryRecord
from moumou.eval.cli import parse_args
from moumou.eval.isolation import validate_candidate
from moumou.eval.loader import BehaviorRequirements, TaskFixture
from moumou.eval.options import EvaluationOptions
from moumou.eval.runner import run_all


def sample(**changes):
    data = {
        "id": "controls", "title": "Control integration", "scenario": "from_scratch",
        "goal": "The answer is already prepared", "setup": {"answer.txt": "correct"},
        "acceptance": [Acceptance(kind="file_contains", target="answer.txt", expect="^correct$")],
        "budget": Budget(max_steps=1, max_retries=0),
    }
    return TaskFixture(**{**data, **changes})


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows worker containment")
async def test_explicit_controls_and_memory_snapshot_reach_the_real_worker(tmp_path):
    record = MemoryRecord(project_id="controls", role="planner", task_id="warmup-source",
                          situation="previous example", action="check inputs", outcome="success",
                          lesson="Always inspect input boundaries before computing the answer")
    options = EvaluationOptions(memory_enabled=False, max_slots=1, role_review_enabled=False,
                                memory_records=[record], warmup_id="warmup-only")
    result, = await run_all([sample()], FakeLLM([LLMResponse(content="Ready")]), tmp_path,
                           repeats=1, hard_timeout_s=15, options=options)
    assert result.status == "succeeded", result.error_message
    assert result.execution_mode == "process" and result.cleanup_complete is True
    assert result.evaluation["max_slots"] == 1
    assert result.evaluation["memory_enabled"] is False
    assert result.evaluation["conversation_id"] == "heldout"
    assert result.evaluation["initial_memory_sha256"] == options.memory_sha256
    assert not any(call["injected"] for call in result.memory_observations)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["engine"]["max_slots"] == 1
    assert manifest["settings"]["initial_memory_sha256"] == options.memory_sha256
    assert result.task_elapsed_s is not None and result.maintenance_s is not None
    assert result.total_tokens_in is None  # scripted behavior is not measured provider cost
    assert Path(result.artifacts["llm_telemetry"]).is_file()
    request = json.loads(Path(result.artifacts["worker_request"]).read_text(encoding="utf-8"))
    candidate = json.loads(Path(result.artifacts["worker_candidate"]).read_text(encoding="utf-8"))
    candidate["result"]["evaluation"] = {}
    changed = tmp_path / "candidate-without-controls.json"
    changed.write_text(json.dumps(candidate), encoding="utf-8")
    with pytest.raises(ValueError, match="actual evaluation settings"):
        validate_candidate(changed, nonce=request["nonce"], fixture=sample(), repeat=0,
                           eval_id=result.eval_id, run_dir=Path(result.run_dir), options=options)
    candidate = json.loads(Path(result.artifacts["worker_candidate"]).read_text(encoding="utf-8"))
    candidate["result"]["total_tokens_in"] = 0
    candidate["result"]["metrics"]["llm"]["total_tokens_in"] = 0
    changed.write_text(json.dumps(candidate), encoding="utf-8")
    with pytest.raises(ValueError, match="persisted client observations"):
        validate_candidate(changed, nonce=request["nonce"], fixture=sample(), repeat=0,
                           eval_id=result.eval_id, run_dir=Path(result.run_dir), options=options)


@pytest.mark.parametrize("isolated", [False, True])
async def test_artifact_success_cannot_hide_missing_approval(isolated, tmp_path):
    if isolated and sys.platform != "win32":
        pytest.skip("native Windows worker containment")
    fixture = sample(requirements=BehaviorRequirements(
        approvals=[{"tool": "delete_file", "decision": "reject"}]
    ))
    result, = await run_all([fixture], FakeLLM([LLMResponse(content="Ready")]), tmp_path,
                           repeats=1, hard_timeout_s=15 if isolated else None)
    assert result.root_status == "DONE" and result.root_machine_accepted is True
    assert result.root_success is False and result.status == "failed"
    assert result.error_type == "behavior_failed"
    assert result.behavior_checks[0]["ok"] is False
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["batch"]["root_completion_rate"] == 0
    if isolated:
        request = json.loads(Path(result.artifacts["worker_request"]).read_text(encoding="utf-8"))
        candidate = json.loads(
            Path(result.artifacts["worker_candidate"]).read_text(encoding="utf-8")
        )
        candidate["result"].update(status="succeeded", root_success=True, error_type=None)
        candidate["result"]["behavior_checks"][0]["ok"] = True
        changed = tmp_path / "candidate-with-invented-approval.json"
        changed.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises(ValueError, match="persisted evidence"):
            validate_candidate(changed, nonce=request["nonce"], fixture=fixture, repeat=0,
                               eval_id=result.eval_id, run_dir=Path(result.run_dir))


async def test_terminal_timing_and_post_terminal_reflection_are_separate(tmp_path):
    fixture = sample(setup={"answer.txt": "wrong"})
    result, = await run_all([fixture], FakeLLM(lambda messages, tools: LLMResponse(content="Done")),
                           tmp_path, repeats=1)
    assert result.root_status == "DEAD"
    assert result.submitted_at and result.terminal_at
    assert result.elapsed_s >= result.task_elapsed_s >= 0
    assert result.maintenance_s >= 0
    summary = result.metrics["llm"]
    assert summary["calls_total"] == 2
    assert summary["by_phase"]["post_terminal"]["calls_total"] == 1
    assert summary["by_kind"]["reflection"]["calls_total"] == 1


def test_cli_exposes_dry_plan_and_controls():
    args = parse_args(["--ablation", "--plan", "--repeats", "3"])
    assert args.ablation and args.plan and args.repeats == 3
    controls = parse_args(["--no-memory", "--max-slots", "1", "--no-role-review"])
    assert controls.memory is False and controls.max_slots == 1 and controls.role_review is False


def test_cli_dry_plan_needs_neither_event_loop_nor_api(monkeypatch, capsys, tmp_path):
    from moumou.eval import cli

    def forbidden(*args, **kwargs):
        raise AssertionError("a dry plan must not execute a model or create an event loop")

    monkeypatch.setattr(cli.asyncio, "run", forbidden)
    monkeypatch.setattr(cli, "DeepSeekClient", forbidden)
    tasks = Path(__file__).resolve().parents[1] / "eval" / "tasks"
    output = tmp_path / "never-created"
    assert cli.main(["--ablation", "--plan", "--tasks", str(tasks), "--out", str(output)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["planned_runs"] == 180 and len(plan["groups"]) == 4
    assert not output.exists()
