"""Reports are reproducible views of authoritative files, not cached scores."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from moumou.contracts import Acceptance, MemoryRecord
from moumou.eval import report
from moumou.eval.ablation import plan_ablation
from moumou.eval.loader import TaskFixture, WarmupSpec
from moumou.eval.options import EvaluationOptions
from moumou.eval.runner import FixtureResult

CODE = {"git_commit": "a" * 40, "source_sha256": "b" * 64, "git_dirty": False}
MODEL = {"provider": "fake", "model": "FakeLLM", "client": "FakeLLM"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def fixture(name="sample", scenario="from_scratch"):
    return TaskFixture(
        id=name, title="A saved result", scenario=scenario, goal="Make answer.txt",
        acceptance=[Acceptance(kind="file_contains", target="answer.txt", expect="ok")],
    )


def write_batch(root, task=None, *, outcomes=(True, False, None), options=None):
    root = Path(root).resolve()
    task = task or fixture()
    options = options or EvaluationOptions(project_id=task.id, conversation_id="heldout")
    raw = task.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    plan = [{"fixture_id": task.id, "repeat": n, "run_dir": f"{task.id}/r{n}"}
            for n in range(len(outcomes))]
    manifest = {
        "schema_version": 1, "eval_id": "test-batch", "status": "completed",
        "purpose": "offline_validation", "code": dict(CODE), "model": dict(MODEL),
        "dataset": {"fixtures": [{"fixture_id": task.id, "scenario": task.scenario,
                                  "sha256": digest, "repeats": len(outcomes)}]},
        "runs": plan, "planned_runs": len(plan),
        "settings": {"engine": {"memory_enabled": options.memory_enabled,
                                "max_slots": options.max_slots,
                                "role_review_enabled": False, "context_window": 1000000},
                     "project_id": task.id, "conversation_id": options.conversation_id,
                     "initial_memory_sha256": options.memory_sha256,
                     "workspace_backend": "local", "harness_concurrency": 1,
                     "fresh_state_per_run": True,
                     "worker": {"execution_mode": "in_process", "hard_timeout_s": None}},
        "environment": {"platform": "native Windows test", "python": "3.12", "interpreter": "nlp"},
    }
    write(root / "manifest.json", manifest)
    for repeat, success in enumerate(outcomes):
        if success is None:
            continue
        directory = root / task.id / f"r{repeat}"
        result = FixtureResult(
            fixture_id=task.id, scenario=task.scenario, repeat=repeat,
            done=int(success), dead=int(not success), total=1,
            completion_rate=float(success), wall_s=repeat + 0.5,
            tokens_in=None, tokens_out=None, run_dir=str(directory),
            eval_id=manifest["eval_id"], status="succeeded" if success else "failed",
            root_task_id="t1", root_status="DONE" if success else "DEAD",
            root_machine_accepted=success, root_success=success,
            elapsed_s=repeat + 1, task_elapsed_s=repeat + 0.75, maintenance_s=0.1,
            root_acceptance_checks=[{
                "kind": "file_contains", "target": "answer.txt", "ok": success,
            }],
            nodes=[{"id": "t1", "parent_id": None, "status": "DONE" if success else "DEAD",
                    "acceptance_count": 1, "machine_accepted": success}],
            error_type=None if success else "acceptance_failed",
            error_message=None if success else "wrong | value\nnext <line> sk-abcdefgh123456",
        )
        write(directory / "result.json", asdict(result))
    return root


def write_ablation(root, *, tasks=None, repeats=1, missing=()):
    root = Path(root).resolve()
    tasks = tasks or [fixture()]
    plan = plan_ablation(tasks, repeats=repeats)
    manifest = {"schema_version": 1, **plan, "code": dict(CODE), "status": "completed",
                "fixtures": [task.model_dump(mode="json") for task in tasks],
                "workspace_backend": "local", "jobs": [], "warmups": []}
    lookup = {task.id: task for task in tasks}
    for item in plan["schedule"]:
        if (item["group"], item["fixture_id"], item["repeat"]) in missing:
            continue
        options = EvaluationOptions(memory_enabled=item["memory_enabled"],
                                    max_slots=item["max_slots"], project_id=item["fixture_id"],
                                    conversation_id=f"h{item['repeat']}-g{item['group_index']}")
        directory = write_batch(root / item["relative_dir"], lookup[item["fixture_id"]],
                                outcomes=(True,), options=options)
        manifest["jobs"].append({**item, "options": options.model_dump(mode="json"),
                                 "memory_sha256": options.memory_sha256,
                                 "model": dict(MODEL), "purpose": "offline_validation",
                                 "status": "completed", "results": [{"status": "invented cache"}]})
        assert directory.is_relative_to(root)
    write(root / "ablation.json", manifest)
    return root


def first_nested(root):
    outer = read(root / "ablation.json")
    path = root / outer["jobs"][0]["relative_dir"] / "manifest.json"
    return outer, path, read(path)


def test_missing_results_stay_in_goal_denominator_and_bad_indexes_are_ignored(tmp_path):
    root = write_batch(tmp_path / "run")
    (root / "results.jsonl").write_text('{"broken":', encoding="utf-8")
    (root / "metrics.json").write_text("not json", encoding="utf-8")
    data = report.build_report(root)
    group = data["groups"][0]
    assert group["metrics"]["batch"]["planned_runs"] == 3
    assert group["metrics"]["batch"]["persisted_runs"] == 2
    assert group["metrics"]["batch"]["verified_root_successes"] == 1
    assert group["metrics"]["batch"]["root_completion_rate"] == pytest.approx(1 / 3)
    assert group["missing"][0]["repeat"] == 2
    text = report.render_report(data)
    assert "Offline validation; not real-model performance" in text
    assert "Verified root goals: 1/3" in text and "not persisted" in text
    assert "N/A" in text and "not declared" in text


def test_mean_sample_std_scenario_and_nonroot_counts_are_rebuilt(tmp_path):
    root = write_batch(tmp_path / "run", outcomes=(True, True, True))
    path = root / "sample/r0/result.json"
    row = read(path)
    row["nodes"].append({"id": "t1.child", "parent_id": "t1", "status": "DONE",
                          "acceptance_count": 0, "machine_accepted": None})
    row.update(total=2, done=2)
    write(path, row)
    data = report.build_report(root)
    metrics = data["groups"][0]["metrics"]
    assert metrics["batch"]["timing"]["runner_elapsed_s"] == {
        "n": 3, "mean": 2, "sample_std": 1,
    }
    assert metrics["batch"]["observed_subtasks"] == 1
    assert metrics["batch"]["machine_verified_subtasks_done"] == 0
    assert metrics["by_scenario"]["from_scratch"]["planned_runs"] == 3
    assert "### By scenario" in report.render_report(data)


def test_single_sample_spread_is_unknown(tmp_path):
    data = report.build_report(write_batch(tmp_path, outcomes=(True,)))
    assert data["groups"][0]["metrics"]["batch"]["timing"]["task_terminal_s"]["sample_std"] is None
    assert "0.750 / N/A / 1" in report.render_report(data)


def test_failure_markdown_is_escaped_and_common_credentials_are_redacted(tmp_path):
    data = report.build_report(write_batch(tmp_path, outcomes=(False,)))
    text = report.render_report(data)
    assert "wrong \\| value<br>next &lt;line&gt; [REDACTED]" in text
    assert "sk-abcdefgh123456" not in text
    assert "[result.json](sample/r0/result.json)" in text


def test_report_generation_changes_only_derived_output_and_is_deterministic(tmp_path):
    root = write_batch(tmp_path / "run")
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    target = report.generate_report(root)
    first = target.read_bytes()
    assert report.generate_report(root) == target
    assert target.read_bytes() == first
    assert all(path.read_bytes() == content for path, content in before.items())
    outside = tmp_path / "export/report.txt"
    report.generate_report(root, outside)
    assert "../run/sample/r0/result.json" in outside.read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", [
    "manifest.json", "sample/r0/report.md", "sample/r0/answer.txt",
])
def test_report_cannot_overwrite_raw_inputs_or_enter_an_attempt(tmp_path, relative):
    root = write_batch(tmp_path / "run")
    original = (root / "manifest.json").read_bytes()
    with pytest.raises(ValueError):
        report.generate_report(root, root / relative)
    assert (root / "manifest.json").read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("status", "unknown"), ("status", {}), ("elapsed_s", -1), ("elapsed_s", float("nan")),
    ("total_tokens_in", True), ("total_tokens_in", 1.5), ("root_success", "true"),
    ("root_machine_accepted", 1), ("root_acceptance_checks", []),
])
def test_damaged_saved_result_is_rejected(tmp_path, field, value):
    root = write_batch(tmp_path, outcomes=(True,))
    path = root / "sample/r0/result.json"
    row = read(path)
    row[field] = value
    write(path, row)
    with pytest.raises((ValueError, TypeError)):
        report.build_report(root)


def test_foreign_result_identity_is_rejected(tmp_path):
    root = write_batch(tmp_path, outcomes=(True,))
    path = root / "sample/r0/result.json"
    row = read(path)
    row["eval_id"] = "another batch"
    write(path, row)
    with pytest.raises(ValueError, match="identity"):
        report.build_report(root)


def test_duplicate_json_fields_are_not_silently_accepted(tmp_path):
    root = write_batch(tmp_path, outcomes=(True,))
    path = root / "sample/r0/result.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"status": "succeeded"',
                                '"status": "failed", "status": "succeeded"'), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON field"):
        report.build_report(root)


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "C:/outside", "sample./r0"])
def test_saved_plan_cannot_escape_or_alias_paths(tmp_path, relative):
    root = write_batch(tmp_path)
    data = read(root / "manifest.json")
    data["runs"][0]["run_dir"] = relative
    write(root / "manifest.json", data)
    with pytest.raises((ValueError, PermissionError)):
        report.build_report(root)


def test_ablation_uses_authoritative_nested_results_not_cached_job_results(tmp_path):
    root = write_ablation(tmp_path)
    data = report.build_report(root)
    assert data["kind"] == "ablation" and len(data["groups"]) == 4
    assert all(group["metrics"]["batch"]["root_completion_rate"] == 1 for group in data["groups"])
    assert sum(len(group["records"]) for group in data["groups"]) == 4
    assert "invented cache" not in report.render_report(data)


def test_ablation_supports_different_fixture_hashes_and_remaps_outer_repeats(tmp_path):
    root = write_ablation(tmp_path, tasks=[fixture("alpha"), fixture("beta", "debug")], repeats=2)
    data = report.build_report(root)
    for group in data["groups"]:
        assert group["metrics"]["batch"]["planned_runs"] == 4
        assert group["metrics"]["batch"]["verified_root_successes"] == 4
        assert {row["repeat"] for row in group["records"]} == {0, 1}
        assert {row["source_repeat"] for row in group["records"]} == {0}
        assert set(group["metrics"]["by_scenario"]) == {"from_scratch", "debug"}


def test_unstarted_ablation_job_stays_in_group_denominator(tmp_path):
    missing = ("memory_on_parallel_on", "sample", 0)
    root = write_ablation(tmp_path, missing=[missing])
    data = report.build_report(root)
    group = next(group for group in data["groups"] if group["name"] == missing[0])
    assert group["metrics"]["batch"]["planned_runs"] == 1
    assert group["metrics"]["batch"]["persisted_runs"] == 0
    assert group["metrics"]["batch"]["root_completion_rate"] == 0
    assert len(group["missing"]) == 1


def test_all_unstarted_ablation_has_no_model_provenance_claim(tmp_path):
    root = write_ablation(tmp_path)
    data = read(root / "ablation.json")
    # Keep only a plan in a new root; do not delete existing result evidence.
    newroot = tmp_path / "only-plan"
    data["jobs"] = []
    write(newroot / "ablation.json", data)
    rebuilt = report.build_report(newroot)
    assert rebuilt["purpose"] == "not_started"
    assert "no model-performance observations" in report.render_report(rebuilt)


@pytest.mark.parametrize("mutation", ["source", "fixture", "settings", "environment", "worker"])
def test_ablation_rejects_mixed_versions_or_fixed_configuration(tmp_path, mutation):
    root = write_ablation(tmp_path)
    _, path, nested = first_nested(root)
    if mutation == "source":
        nested["code"]["source_sha256"] = "f" * 64
    elif mutation == "fixture":
        nested["dataset"]["fixtures"][0]["sha256"] = "f" * 64
    elif mutation == "settings":
        nested["settings"]["engine"]["context_window"] = 999
    elif mutation == "environment":
        nested["environment"]["python"] = "another interpreter"
    else:
        nested["settings"]["worker"]["hard_timeout_s"] = 5
    write(path, nested)
    with pytest.raises(ValueError):
        report.build_report(root)


def test_ablation_rejects_mixed_offline_and_real_metadata(tmp_path):
    root = write_ablation(tmp_path)
    outer, path, nested = first_nested(root)
    nested["purpose"] = outer["jobs"][0]["purpose"] = "model_evaluation"
    model = {"provider": "hkgai", "model": "course", "client": "DeepSeekClient"}
    nested["model"] = outer["jobs"][0]["model"] = model
    write(path, nested)
    write(root / "ablation.json", outer)
    with pytest.raises(ValueError, match="mixed models or offline/real"):
        report.build_report(root)


def test_ablation_rejects_different_seed_states_between_groups(tmp_path):
    root = write_ablation(tmp_path)
    outer = read(root / "ablation.json")
    job = outer["jobs"][0]
    seed = MemoryRecord(project_id="sample", role="planner", task_id="training",
                        situation="previous input", action="check", outcome="success",
                        lesson="Validate inputs first")
    options = EvaluationOptions.model_validate({**job["options"], "memory_records": [seed]})
    job["options"] = options.model_dump(mode="json")
    job["memory_sha256"] = options.memory_sha256
    write(root / "ablation.json", outer)
    with pytest.raises(ValueError, match="same memory starting state"):
        report.build_report(root)


@pytest.mark.parametrize("mutation", ["missing_schedule", "duplicate_job", "duplicate_path",
                                      "wrong_controls", "wrong_project", "wrong_conversation"])
def test_ablation_plan_and_job_corruption_is_rejected(tmp_path, mutation):
    root = write_ablation(tmp_path)
    data = read(root / "ablation.json")
    if mutation == "missing_schedule":
        data["schedule"].pop()
        data["planned_runs"] -= 1
    elif mutation == "duplicate_job":
        data["jobs"].append(copy.deepcopy(data["jobs"][0]))
    elif mutation == "duplicate_path":
        data["schedule"][1]["relative_dir"] = data["schedule"][0]["relative_dir"]
    elif mutation == "wrong_controls":
        data["jobs"][0]["options"]["memory_enabled"] = 1
    elif mutation == "wrong_project":
        data["jobs"][0]["options"]["project_id"] = "elsewhere"
    else:
        data["jobs"][1]["options"]["conversation_id"] = (
            data["jobs"][0]["options"]["conversation_id"]
        )
    write(root / "ablation.json", data)
    with pytest.raises(ValueError):
        report.build_report(root)


def test_warmup_cost_is_displayed_separately(tmp_path):
    task = fixture().model_copy(update={"warmup": WarmupSpec(goal="Independent training")})
    root = write_ablation(tmp_path, tasks=[task])
    outer = read(root / "ablation.json")
    outer["warmups"] = [{"fixture_id": task.id, "repeat": 0, "relative_dir": "warmups/w0"}]
    write(root / "ablation.json", outer)
    write(root / "warmups/w0/warmup.json", {
        "fixture_id": task.id, "repeat": 0, "status": "completed",
        "source": task.warmup.model_dump(mode="json"),
        "memory_sha256": outer["jobs"][0]["memory_sha256"], "memory_record_count": 0,
        "elapsed_s": 3,
        "llm_usage": {"calls_total": 2, "total_tokens_in": 9, "total_tokens_out": 4},
    })
    data = report.build_report(root)
    assert data["warmups"][0]["tokens_in"] == 9
    assert "Warmup, separate from held-out measurements" in report.render_report(data)
    assert all(group["metrics"]["batch"]["usage"]["total_tokens_in"] is None
               for group in data["groups"])
    with pytest.raises(ValueError, match="original attempt evidence"):
        report.generate_report(root, root / "warmups/w0/new.md")


def test_cli_runs_synchronously_without_keys_client_or_event_loop(tmp_path, monkeypatch, capsys):
    from moumou.config import LLMSettings

    def forbidden(*args, **kwargs):
        raise AssertionError("offline reporting must not initialize API configuration or asyncio")

    monkeypatch.setattr(LLMSettings, "from_env", forbidden)
    monkeypatch.setattr(asyncio, "run", forbidden)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    root = write_batch(tmp_path / "run")
    assert report.main(["--run", str(root)]) == 0
    assert "report rebuilt" in capsys.readouterr().out
    assert (root / "report.md").is_file()


def test_custom_worker_purpose_is_not_called_real_performance(tmp_path):
    root = write_batch(tmp_path)
    data = read(root / "manifest.json")
    data["purpose"] = "custom_worker_validation"
    write(root / "manifest.json", data)
    assert "not model performance" in report.render_report(report.build_report(root))


def test_unknown_provider_does_not_become_certified_real_model_performance(tmp_path):
    root = write_batch(tmp_path)
    data = read(root / "manifest.json")
    data.update(purpose="model_evaluation", model={"client": "UnclassifiedClient"})
    write(root / "manifest.json", data)
    assert "provider provenance is unknown or unverified" in report.render_report(
        report.build_report(root)
    )


@pytest.mark.parametrize("mutation", ["count", "cycle", "missing_check_kind"])
def test_inconsistent_node_or_acceptance_evidence_is_rejected(tmp_path, mutation):
    root = write_batch(tmp_path, outcomes=(True,))
    path = root / "sample/r0/result.json"
    row = read(path)
    if mutation == "count":
        row["total"] = 999
    elif mutation == "cycle":
        row["nodes"][0]["parent_id"] = "t1"
    else:
        row["root_acceptance_checks"][0].pop("kind")
    write(path, row)
    with pytest.raises(ValueError):
        report.build_report(root)


def test_cli_reports_missing_or_ambiguous_inputs_without_writing(tmp_path, capsys):
    assert report.main([str(tmp_path / "missing")]) == 2
    assert "neither" in capsys.readouterr().err
    root = write_batch(tmp_path / "both")
    write(root / "ablation.json", {})
    assert report.main([str(root)]) == 2
    assert "ambiguous" in capsys.readouterr().err
    assert not (root / "report.md").exists()


def test_freeze_plan_is_not_mistaken_for_executed_results(tmp_path):
    write(tmp_path / "freeze.json", {"kind": "frozen_experiment", "status": "prepared_not_started"})
    assert report.main(["--run", str(tmp_path)]) == 2
    assert not (tmp_path / "report.md").exists()
