"""Snapshots must preserve the full plan and stop invalid starts before clients."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from moumou.adapters import DeepSeekClient
from moumou.eval import freeze
from moumou.eval.loader import TaskFixture, load_tasks

MODEL = {"provider": "hkgai", "model": "course-model",
         "base_url": "https://api1.hkgai.net", "timeout_s": 35,
         "max_retries": 2, "temperature": 0.2}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "example").mkdir(parents=True)
    (root / "src/example/runtime.py").write_bytes(b"answer = 42\r\n")
    (root / "pyproject.toml").write_text('[project]\nname="example"\nversion="1"\n',
                                         encoding="utf-8")
    (root / "requirements-win-py311.lock").write_text("pydantic==2.12.4\n", encoding="utf-8")
    (root / ".env").write_text("DO_NOT_COPY=private-local-value\n", encoding="utf-8")
    (root / "private.txt").write_text("not runtime\n", encoding="utf-8")
    (root / "docs/p3").mkdir(parents=True)
    (root / "docs/p3/private.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setattr(freeze, "_repository_root", lambda: root)
    return root


def task(**changes):
    base = {"id": "sample", "title": "Snapshot test", "scenario": "from_scratch",
            "goal": "Create a receipt", "setup": {"input.txt": "independent data"},
            "acceptance": [{"kind": "file_exists", "target": "receipt.txt"}]}
    return TaskFixture.model_validate({**base, **changes})


def prepare(out, fixtures=None, **changes):
    return freeze.prepare_experiment(
        fixtures or [task()], out, model_config=changes.pop("model_config", MODEL),
        capabilities=changes.pop("capabilities", {"web_search": False}), **changes,
    )


def read(out):
    return json.loads((out / "freeze.json").read_text(encoding="utf-8"))


def rewrite(out, value):
    """Update the public hash too, to exercise semantic validation, not signatures."""
    freeze.atomic_json(out / "freeze.json", value)
    freeze.atomic_text(out / "freeze.sha256", freeze._sha((out / "freeze.json").read_bytes()))


def context(manifest, **changes):
    fixture = manifest["fixtures"][0]
    row = next(row for row in manifest["plan"]["schedule"] if row["fixture_id"] == fixture["id"])
    return {"phase": "heldout", "fixture": copy.deepcopy(fixture),
            "planned": copy.deepcopy(row), "workspace_backend": "local",
            "hard_timeout_s": 180, "model_config": copy.deepcopy(MODEL), **changes}


def test_full_180_plan_keeps_36_blocked_without_creating_a_client(repo, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("freeze and readiness must not create model clients")

    monkeypatch.setattr(DeepSeekClient, "__init__", forbidden)
    fixtures = load_tasks(Path(__file__).resolve().parents[1] / "eval/tasks")
    out = tmp_path / "freeze"
    result = prepare(out, fixtures, budget_override={"max_tokens": 150_000, "max_steps": 20},
                     warmup_budget_override={"max_tokens": 150_000, "max_steps": 25})
    assert result["plan"]["planned_runs"] == 180
    assert result["readiness"]["ready_count"] == 144
    assert result["readiness"]["blocked_count"] == 36
    assert result["readiness"]["full_run_ready"] is False
    assert {item["fixture_id"] for item in result["readiness"]["blocked_fixtures"]} == {
        "s4_http_research", "s4_python_research", "s8_composite",
    }
    assert all(row["status"] == "blocked_not_started" for row in result["plan"]["schedule"]
               if row["fixture_id"].startswith("s4_") or row["fixture_id"] == "s8_composite")
    full = freeze.check_start(out)
    assert full["ready"] is False and full["selected_count"] == 0
    partial = freeze.check_start(out, scope="non-network")
    assert partial["eligible_count"] == 144 and partial["deferred_count"] == 36
    assert partial["selected_count"] == (144 if sys.platform == "win32" else 0)
    assert full["model_calls"] == partial["model_calls"] == 0
    assert read(out)["execution"]["started"] is False
    assert read(out)["plan"]["planned_runs"] == 180


def test_snapshot_copies_only_allowlisted_runtime_and_exact_bytes(repo, tmp_path):
    out = tmp_path / "freeze"
    manifest = prepare(out)
    files = manifest["snapshot"]["files"]
    assert set(files) == {"src/example/runtime.py", "pyproject.toml",
                          "requirements-win-py311.lock", "fixtures.json"}
    assert (out / "snapshot/src/example/runtime.py").read_bytes() == b"answer = 42\r\n"
    assert not (out / "snapshot/.env").exists()
    assert not (out / "snapshot/docs").exists()
    assert not (out / "snapshot/private.txt").exists()
    assert freeze.verify_experiment(out)["valid"] is True
    assert manifest["execution"]["frozen_runtime_automatically_executed"] is False


def test_dirty_snapshot_and_separate_actual_budgets_are_explicit(repo, tmp_path, monkeypatch):
    original = freeze._provenance

    def dirty(*args):
        result = original(*args)
        result.update(git_dirty=True, git_commit="a" * 40,
                      working_tree_status=[" M .gitignore", "?? src/example/new.py"])
        return result

    monkeypatch.setattr(freeze, "_provenance", dirty)
    warm = {"goal": "independent training", "budget": {"max_tokens": 2000}}
    fixture = task(warmup=warm)
    out = tmp_path / "freeze"
    data = prepare(out, [fixture], budget_override={"max_tokens": 150_000, "max_steps": 20},
                   warmup_budget_override={"max_tokens": 80_000, "max_steps": 25})
    assert data["code"]["git_dirty"] is True and data["code"]["git_commit"] == "a" * 40
    assert data["original_fixtures"][0]["budget"]["max_tokens"] == fixture.budget.max_tokens
    assert data["fixtures"][0]["budget"]["max_tokens"] == 150_000
    assert data["fixtures"][0]["warmup"]["budget"]["max_tokens"] == 80_000
    assert fixture.warmup.budget.max_tokens == 2000
    assert "per-task" in data["budget_overrides"]["scope"]
    assert freeze.verify_experiment(out)["valid"] is True


@pytest.mark.parametrize("mutation", ["change", "add", "remove", "lock"])
def test_runtime_drift_stops_start_but_does_not_destroy_the_archive(repo, tmp_path, mutation):
    out = tmp_path / "freeze"
    prepare(out)
    if mutation == "change":
        (repo / "src/example/runtime.py").write_text("answer=43", encoding="utf-8")
    elif mutation == "add":
        (repo / "src/example/new.py").write_text("new=True", encoding="utf-8")
    elif mutation == "remove":
        (repo / "src/example/runtime.py").unlink()
    else:
        (repo / "requirements-win-py311.lock").write_text("different==1", encoding="utf-8")
    with pytest.raises(ValueError, match="current runtime"):
        freeze.check_start(out, scope="non-network")
    assert freeze.verify_experiment(out, check_current=False)["valid"] is True


def test_environment_drift_stops_current_execution(repo, tmp_path, monkeypatch):
    out = tmp_path / "freeze"
    prepare(out)
    monkeypatch.setattr(freeze, "_environment", lambda: {"python": "different"})
    with pytest.raises(ValueError, match="environment"):
        freeze.verify_experiment(out)


def test_snapshot_bytes_and_manifest_fingerprints_are_verified(repo, tmp_path):
    out = tmp_path / "freeze"
    prepare(out)
    (out / "snapshot/src/example/runtime.py").write_text("corrupted", encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot file"):
        freeze.verify_experiment(out)
    other = tmp_path / "other"
    prepare(other)
    (other / "freeze.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        freeze.verify_experiment(other)


@pytest.mark.parametrize("path", ["../escape.txt", "C:/escape.py", "/escape.py", "src/../x.py",
                                 "src\\x.py", "src/NUL.py", "src/x.py "])
def test_unsafe_snapshot_paths_are_rejected_after_recomputing_public_hash(repo, tmp_path, path):
    out = tmp_path / "freeze"
    data = prepare(out)
    data["snapshot"]["files"][path] = {"sha256": "0" * 64, "bytes": 0}
    rewrite(out, data)
    with pytest.raises(ValueError, match="snapshot path"):
        freeze.verify_experiment(out)


def test_unknown_snapshot_files_and_inconsistent_readiness_are_rejected(repo, tmp_path):
    out = tmp_path / "freeze"
    prepare(out)
    (out / "snapshot/.env").write_text("not permitted", encoding="utf-8")
    with pytest.raises(ValueError, match="unrecorded"):
        freeze.verify_experiment(out)
    other = tmp_path / "other"
    data = prepare(other)
    data["readiness"]["ready_count"] = 999
    rewrite(other, data)
    with pytest.raises(ValueError, match="readiness"):
        freeze.verify_experiment(other)


@pytest.mark.parametrize("changes", [
    {"api_key": "never-write-this-secret"}, {"headers": {"Authorization": "private"}},
    {"provider": "openai"}, {"base_url": "https://user:password@api1.hkgai.net"},
    {"base_url": "https://api1.hkgai.net?api_key=private"}, {"model": "sk-private-key-123456"},
    {"temperature": 0.7}, {"max_retries": True}, {"timeout_s": float("nan")},
])
def test_model_secrets_and_unimplemented_parameters_are_rejected_before_output(repo, tmp_path,
                                                                              changes):
    out = tmp_path / "freeze"
    with pytest.raises(ValueError):
        prepare(out, model_config={**MODEL, **changes})
    assert not out.exists()


@pytest.mark.parametrize("capabilities", [{"web_search": "false"}, {"web_search": 1},
                                          {"TAVILY_API_KEY": "private"}])
def test_capability_flags_are_not_a_secret_or_environment_container(repo, tmp_path, capabilities):
    with pytest.raises(ValueError, match="capabilities"):
        prepare(tmp_path / "freeze", capabilities=capabilities)


def test_fresh_output_and_source_boundaries_are_enforced(repo, tmp_path):
    with pytest.raises(ValueError, match="source or Git"):
        prepare(repo)
    with pytest.raises(ValueError, match="source or Git"):
        prepare(repo / "src/frozen")
    out = tmp_path / "freeze"
    out.mkdir()
    receipt = out / "old-result.txt"
    receipt.write_text("keep me", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare(out)
    assert receipt.read_text(encoding="utf-8") == "keep me"


def test_actual_attempt_fixture_and_settings_are_guarded(repo, tmp_path):
    out = tmp_path / "freeze"
    data = prepare(out, capabilities={"web_search": True})
    good = context(data)
    result = freeze.check_attempt(out, good)
    assert result["valid"] is True and result["model_config_checked"] is True
    assert result["started"] is False and result["model_calls"] == 0
    changed = copy.deepcopy(good)
    changed["fixture"]["goal"] = "a different held-out problem"
    with pytest.raises(ValueError, match="fixture or budget"):
        freeze.check_attempt(out, changed)


def test_warmup_budget_and_public_model_are_part_of_the_guard(repo, tmp_path):
    out = tmp_path / "freeze"
    data = prepare(out, [task(warmup={"goal": "independent warmup example"})])
    good = context(data, phase="warmup")
    assert freeze.check_attempt(out, good)["valid"] is True
    changed = copy.deepcopy(good)
    changed["fixture"]["warmup"]["budget"]["max_tokens"] += 1000
    with pytest.raises(ValueError, match="fixture or budget"):
        freeze.check_attempt(out, changed)
    changed = copy.deepcopy(good)
    changed["model_config"]["max_retries"] += 1
    with pytest.raises(ValueError, match="model configuration"):
        freeze.check_attempt(out, changed)
    changed = copy.deepcopy(good)
    changed["fixture"]["budget"]["max_tokens"] += 1000
    with pytest.raises(ValueError, match="fixture or budget"):
        freeze.check_attempt(out, changed)


@pytest.mark.parametrize("change,match", [
    ({"phase": "unknown"}, "context"), ({"workspace_backend": "git"}, "backend"),
    ({"hard_timeout_s": 900}, "deadline"), ({"hard_timeout_s": True}, "deadline"),
])
def test_attempt_context_drift_is_rejected(repo, tmp_path, change, match):
    out = tmp_path / "freeze"
    data = prepare(out)
    with pytest.raises(ValueError, match=match):
        freeze.check_attempt(out, context(data, **change))


@pytest.mark.parametrize("field,value", [("max_slots", 1), ("memory_enabled", False),
                                        ("role_review_enabled", True), ("repeat", True)])
def test_group_controls_cannot_be_silently_changed(repo, tmp_path, field, value):
    out = tmp_path / "freeze"
    data = prepare(out)
    actual = context(data)
    actual["planned"][field] = value
    with pytest.raises(ValueError):
        freeze.check_attempt(out, actual)


def test_blocked_attempt_and_missing_warmup_cannot_create_a_paid_client(repo, tmp_path):
    blocked = task(requirements={"required_tools": ["web_search"]})
    out = tmp_path / "blocked"
    data = prepare(out, [blocked])
    with pytest.raises(ValueError, match="blocked"):
        freeze.check_attempt(out, context(data))
    other = tmp_path / "other"
    data = prepare(other)
    with pytest.raises(ValueError, match="no frozen warmup"):
        freeze.check_attempt(other, context(data, phase="warmup"))


def test_cli_is_synchronous_call_free_and_refuses_blocked_full_start(repo, tmp_path, monkeypatch,
                                                                  capsys):
    import asyncio

    def forbidden(*args, **kwargs):
        raise AssertionError("no async loop or model should be created")

    monkeypatch.setattr(asyncio, "run", forbidden)
    monkeypatch.setattr(DeepSeekClient, "__init__", forbidden)
    tasks = Path(__file__).resolve().parents[1] / "eval/tasks"
    model = tmp_path / "model.json"
    model.write_text(json.dumps(MODEL), encoding="utf-8")
    out = tmp_path / "freeze"
    assert freeze.main(["prepare", "--tasks", str(tasks), "--out", str(out),
                        "--model-config", str(model), "--max-tokens", "150000",
                        "--warmup-max-tokens", "100000"]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    assert freeze.main(["verify", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert freeze.main(["check-start", str(out)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["planned_runs"] == 180 and result["model_calls"] == 0
    assert result["blocked_count"] == 36 and result["selected_count"] == 0
