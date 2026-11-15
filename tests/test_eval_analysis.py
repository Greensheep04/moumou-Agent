"""Failure attribution and memory comparisons stay tied to saved evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from test_eval_report import fixture, read, selected_ablation, write, write_ablation, write_batch

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.context.builder import LESSONS_HEADER
from moumou.contracts import Acceptance, MemoryRecord
from moumou.eval import analysis
from moumou.eval.loader import WarmupSpec
from moumou.eval.options import EvaluationOptions
from moumou.eval.telemetry import EvalLLM


def change_result(root, **values):
    path = root / "sample/r0/result.json"
    row = read(path)
    row.update(values)
    write(path, row)
    return row


def test_missing_plans_failures_and_unverified_have_distinct_counts(tmp_path):
    root = write_batch(tmp_path / "run", outcomes=(True, False, None))
    model = analysis.build_analysis(root)
    assert model["summary"] == {
        "planned": 3, "selected": 3, "persisted": 2, "succeeded": 1, "failed": 1,
        "unverified": 0, "missing": 1, "deferred": 0, "unexplained_missing": 1,
        "planned_goal_coverage": 1 / 3, "selected_success_rate": 1 / 3,
        "observed_success_rate": 1 / 2, "statuses": {"failed": 1, "succeeded": 1},
        "failure_categories": {"acceptance": 1},
    }
    assert model["not_persisted"][0]["state"] == "not_persisted"
    assert model["purpose"] == "offline_validation"
    assert model["memory_comparisons"] == []


def test_deferred_scope_is_not_counted_as_a_model_failure(tmp_path):
    root, _ = selected_ablation(tmp_path / "selected")
    summary = analysis.build_analysis(root)["summary"]
    assert summary["planned"] == 8
    assert summary["selected"] == summary["persisted"] == 4
    assert summary["deferred"] == summary["missing"] == 4
    assert summary["unexplained_missing"] == summary["failed"] == 0
    assert summary["planned_goal_coverage"] == 0.5
    assert summary["selected_success_rate"] == summary["observed_success_rate"] == 1


@pytest.mark.parametrize(("error", "category"), [
    ("timeout", "timeout"), ("worker_crash", "infrastructure"),
    ("invalid_worker_result", "infrastructure"), ("cleanup_failed", "infrastructure"),
    ("setup_error", "infrastructure"), ("cancelled", "cancelled"),
    ("behavior_failed", "behavior"), ("acceptance_failed", "acceptance"),
    ("task_failed", "unknown"),
])
def test_failure_categories_use_recorded_terminal_evidence(tmp_path, error, category):
    root = write_batch(tmp_path / "run", outcomes=(False,))
    change_result(root, error_type=error)
    result = analysis.build_analysis(root)["attempts"][0]["classification"]
    assert result["category"] == category
    assert result["confidence"] == ("unknown" if category == "unknown" else "recorded")
    assert result["source"].endswith("result.json")


def test_explicit_budget_error_is_not_guessed_from_arbitrary_prose(tmp_path):
    root = write_batch(tmp_path / "run", outcomes=(False,))
    change_result(root, error_type="task_failed", error_message="budget:tokens")
    assert analysis.build_analysis(root)["summary"]["failure_categories"] == {"budget": 1}
    change_result(root, error_message="The model mentioned a budget once")
    assert analysis.build_analysis(root)["summary"]["failure_categories"] == {"unknown": 1}


def test_rejected_approval_and_tool_error_are_symptoms_not_proven_cause(tmp_path):
    root = write_batch(tmp_path / "run", outcomes=(False,))
    change_result(root, error_type="task_failed", error_message="no terminal explanation")
    trace = [
        {"kind": "approval", "name": "delete_file", "span_id": "a", "task_id": "t1",
         "status": "ok", "payload": {"decision": "reject"}},
        {"kind": "tool", "name": "shell", "span_id": "b", "task_id": "t1",
         "status": "error", "payload": {"error": "some tool error"}},
    ]
    (root / "sample/r0/trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in trace), encoding="utf-8")
    row = analysis.build_analysis(root)["attempts"][0]
    assert row["classification"]["category"] == "unknown"
    assert [item["category"] for item in row["observed_symptoms"]] == [
        "human_rejection", "tool_error",
    ]


def test_saved_success_audit_hashes_files_without_running_acceptance(tmp_path):
    root = write_batch(tmp_path / "run", outcomes=(True,))
    directory = root / "sample/r0"
    target = directory / "workspaces/project/conversation/planner@t1/answer.txt"
    target.parent.mkdir(parents=True)
    target.write_text("ok", encoding="utf-8")
    (directory / "trace.jsonl").write_text(json.dumps({
        "kind": "acceptance", "name": "verdict", "task_id": "t1", "payload": {"ok": True},
    }), encoding="utf-8")
    row = analysis.build_analysis(root)["attempts"][0]
    assert row["success_audit"] == {
        "saved_root_machine_accepted": True, "trace_verdict": True,
        "trace_verdict_matches_saved": True, "acceptance_reexecuted": False,
    }
    assert row["artifacts"]["files"][0]["sha256"] == hashlib.sha256(b"ok").hexdigest()


@pytest.mark.parametrize(("tail", "state"), [
    ('{"partial":', "partial"), ('{"partial":\n', "malformed"),
    ('{"kind":"x","kind":"y"}', "malformed"),
])
def test_partial_trace_tail_is_distinct_from_malformed_complete_evidence(tmp_path, tail, state):
    root = write_batch(tmp_path / "run", outcomes=(False,))
    (root / "sample/r0/trace.jsonl").write_text(
        '{"kind":"tool","payload":{}}\n' + tail, encoding="utf-8")
    row = analysis.build_analysis(root)["attempts"][0]
    assert row["trace"]["state"] == state
    assert row["status"] == "failed"


async def memory_ablation(root):
    task = fixture(scenario="use_memory").model_copy(update={"warmup": WarmupSpec(
        goal="Learn text encoding", acceptance=[Acceptance(kind="file_exists", target="old.txt")],
    )})
    root = write_ablation(root, tasks=[task])
    manifest = read(root / "ablation.json")
    seed = MemoryRecord(id="historical-lesson", project_id=task.id, role="planner", task_id="old",
                        situation="nested paths", action="create parents", outcome="success",
                        lesson="Create parent directories before writing UTF-8 text.")
    warm = root / "warmups/r0/f0"
    for job in manifest["jobs"]:
        options = EvaluationOptions.model_validate({**job["options"], "memory_records": [seed]})
        job.update(options=options.model_dump(mode="json"), memory_sha256=options.memory_sha256)
        nested_path = root / job["relative_dir"] / "manifest.json"
        nested = read(nested_path)
        nested["settings"]["initial_memory_sha256"] = options.memory_sha256
        write(nested_path, nested)
        directory = nested_path.parent / "sample/r0"
        meter = EvalLLM(FakeLLM([LLMResponse(content="done")]),
                        directory / "llm-telemetry.json", memory_records=[seed])
        content = f"{LESSONS_HEADER}\n- {seed.lesson}\n" if job["memory_enabled"] else "No memory"
        await meter.chat([{"role": "system", "content": content}])
    digest = manifest["jobs"][0]["memory_sha256"]
    write(warm / "memory-snapshot.json", [seed.model_dump(mode="json")])
    write(warm / "warmup.json", {
        "fixture_id": "sample", "repeat": 0, "source": task.warmup.model_dump(mode="json"),
        "status": "completed", "memory_sha256": digest, "memory_record_count": 1,
    })
    manifest["warmups"] = [{"fixture_id": "sample", "repeat": 0,
                            "relative_dir": "warmups/r0/f0"}]
    write(root / "ablation.json", manifest)
    return root


async def test_memory_chain_matches_actual_warmup_seed_and_prompt_telemetry(tmp_path):
    model = analysis.build_analysis(await memory_ablation(tmp_path / "run"))
    assert len(model["memory_comparisons"]) == 2
    for pair in model["memory_comparisons"]:
        assert pair["success_relation"] == "unchanged_success"
        assert pair["on_chain"]["state"] == pair["off_chain"]["state"] == "consistent"
        assert pair["on_chain"]["injected_seed_ids"] == ["historical-lesson"]
        assert pair["off_chain"]["injected_seed_ids"] == []
        assert pair["on_chain"]["warmup_snapshot_matches_seed"] is True
    assert "no causal" in model["memory_comparisons"][0]["conclusion"]


@pytest.mark.parametrize(("on_success", "off_success", "relation"), [
    (True, False, "memory_on_only_success"), (False, True, "memory_off_only_success"),
    (False, False, "unchanged_failure"),
])
async def test_memory_comparison_retains_positive_negative_and_no_gain_cases(
        tmp_path, on_success, off_success, relation):
    root = await memory_ablation(tmp_path / "run")
    for index, success in ((0, on_success), (2, off_success)):
        path = root / f"heldout/r0/f0/g{index}/sample/r0/result.json"
        row = read(path)
        if not success:
            row.update(status="failed", root_status="DEAD", root_success=False,
                       root_machine_accepted=False, done=0, dead=1)
            row["nodes"][0].update(status="DEAD", machine_accepted=False)
            row["root_acceptance_checks"][0]["ok"] = False
        write(path, row)
    pair = next(pair for pair in analysis.build_analysis(root)["memory_comparisons"]
                if pair["max_slots"] == 4)
    assert pair["success_relation"] == relation


async def test_tampered_warmup_snapshot_does_not_become_a_verified_memory_chain(tmp_path):
    root = await memory_ablation(tmp_path / "run")
    path = root / "warmups/r0/f0/memory-snapshot.json"
    records = read(path)
    records[0]["lesson"] = "Different lesson after evaluation"
    write(path, records)
    for pair in analysis.build_analysis(root)["memory_comparisons"]:
        assert pair["on_chain"]["state"] == "inconsistent"
        assert pair["on_chain"]["warmup_snapshot_matches_seed"] is False


async def test_unverified_memory_outcome_is_not_a_failure_or_an_improvement(tmp_path):
    root = await memory_ablation(tmp_path / "run")
    path = root / "heldout/r0/f0/g0/sample/r0/result.json"
    row = read(path)
    row.update(status="unverified", root_success=None, root_machine_accepted=None)
    write(path, row)
    pair = next(pair for pair in analysis.build_analysis(root)["memory_comparisons"]
                if pair["max_slots"] == 4)
    assert pair["memory_on_success"] is None
    assert pair["success_relation"] == "not_comparable"


def test_analysis_is_reproducible_and_keeps_raw_results_untouched(tmp_path):
    root = write_batch(tmp_path / "run", outcomes=(False,))
    original = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    output = analysis.generate_analysis(root)
    first = output.read_bytes()
    assert analysis.generate_analysis(root).read_bytes() == first
    json_output = analysis.generate_analysis(root, tmp_path / "export/analysis.json")
    assert read(json_output)["summary"]["planned"] == 1
    assert all(path.read_bytes() == data for path, data in original.items())
    assert b"sk-abcdefgh123456" not in first
    assert "sk-abcdefgh123456" not in json_output.read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", [
    "manifest.json", "ablation.json", "metrics.json", "result.json",
    "sample/r0/derived.md", "sample/r0/new.json", "analysis.csv",
])
def test_analysis_cannot_overwrite_evidence_or_use_unsupported_formats(tmp_path, relative):
    root = write_batch(tmp_path / "run")
    with pytest.raises(ValueError):
        analysis.generate_analysis(root, root / relative)


def test_cli_uses_no_environment_client_or_event_loop(tmp_path, monkeypatch, capsys):
    root = write_batch(tmp_path / "run", outcomes=(True,))

    def forbidden(*args, **kwargs):
        raise AssertionError("analysis must remain offline and synchronous")

    monkeypatch.setattr(asyncio, "run", forbidden)
    monkeypatch.setattr("moumou.config.LLMSettings.from_env", forbidden)
    monkeypatch.setattr("moumou.adapters.DeepSeekClient", forbidden)
    assert analysis.main(["--run", str(root)]) == 0
    assert "analysis rebuilt" in capsys.readouterr().out
    assert analysis.main([str(tmp_path / "missing")]) == 2


def test_ablation_source_mismatch_is_rejected_before_analysis(tmp_path):
    root = write_ablation(tmp_path / "run")
    path = root / "heldout/r0/f0/g0/manifest.json"
    nested = read(path)
    nested["code"]["source_sha256"] = "different"
    write(path, nested)
    with pytest.raises(ValueError, match="mixed source"):
        analysis.build_analysis(root)
