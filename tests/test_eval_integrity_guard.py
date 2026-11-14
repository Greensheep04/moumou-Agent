"""Integrity guards run before a paid client exists and preserve partial results."""

from __future__ import annotations

import json

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.contracts import Acceptance
from moumou.eval.ablation import run_ablation
from moumou.eval.loader import TaskFixture, WarmupSpec


def fixture(**changes):
    values = {
        "id": "guarded", "title": "Integrity probe", "scenario": "from_scratch",
        "goal": "Inspect the prepared answer", "setup": {"answer.txt": "correct"},
        "acceptance": [Acceptance(kind="file_contains", target="answer.txt", expect="^correct$")],
    }
    return TaskFixture(**{**values, **changes})


@pytest.mark.parametrize("warmup", [False, True])
async def test_rejected_guard_prevents_any_client_creation(tmp_path, warmup):
    source = fixture()
    if warmup:
        source = fixture(warmup=WarmupSpec(
            goal="Independent training", acceptance=source.acceptance,
        ))
    observed = []

    def no_client():
        raise AssertionError("guard should reject before client initialization")

    def guard(context):
        observed.append(context)
        return False

    with pytest.raises(ValueError, match="integrity guard"):
        await run_ablation([source], no_client, tmp_path, repeats=1, before_attempt=guard)
    manifest = json.loads((tmp_path / "ablation.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "error" and manifest["completed_runs"] == 0
    assert manifest["jobs"] == [] and manifest["integrity_guard_enabled"] is True
    assert observed[0]["phase"] == ("warmup" if warmup else "heldout")


async def test_source_change_between_groups_keeps_first_authoritative_result(tmp_path):
    calls = []
    clients = []

    async def guard(context):
        calls.append(context)
        if len(calls) == 2:
            raise ValueError("frozen source changed")

    def factory():
        client = FakeLLM([LLMResponse(content="Ready")])
        clients.append(client)
        return client

    with pytest.raises(ValueError, match="frozen source changed"):
        await run_ablation([fixture()], factory, tmp_path, repeats=1, before_attempt=guard)
    manifest = json.loads((tmp_path / "ablation.json").read_text(encoding="utf-8"))
    assert manifest["planned_runs"] == 4 and manifest["completed_runs"] == 1
    assert len(clients) == 1 and manifest["jobs"][0]["results"][0]["root_success"] is True
    assert (tmp_path / manifest["jobs"][0]["relative_dir"] / "guarded/r0/result.json").is_file()
    assert calls[0]["planned"]["group"] != calls[1]["planned"]["group"]


async def test_invalid_guard_fails_before_output_allocation(tmp_path):
    output = tmp_path / "never-created"
    with pytest.raises(TypeError, match="before_attempt"):
        await run_ablation([fixture()], lambda: None, output, repeats=1, before_attempt=True)
    assert not output.exists()


async def test_frozen_guard_rejects_changed_fixture_before_client(tmp_path):
    from functools import partial

    from moumou.eval.freeze import check_attempt, prepare_experiment

    source = fixture()
    frozen = tmp_path / "f"
    prepare_experiment([source], frozen, repeats=1, capabilities={"web_search": False},
                       model_config={"model": "test", "base_url": "https://api.hkgai.net",
                                     "timeout_s": 35, "max_retries": 2, "temperature": 0.2})

    def forbidden():
        raise AssertionError("a modified task must be rejected before client creation")

    changed = source.model_copy(update={"goal": "A changed experiment task"})
    with pytest.raises(ValueError, match="fixture"):
        await run_ablation([changed], forbidden, tmp_path / "r", repeats=1,
                           hard_timeout_s=180, before_attempt=partial(check_attempt, frozen))


async def test_missing_search_never_creates_a_model(monkeypatch, capsys, tmp_path):
    from moumou.config import LLMSettings
    from moumou.eval import cli

    source = fixture(scenario="research", requirements={"required_tools": ["web_search"]})
    monkeypatch.setattr(cli, "load_tasks", lambda _: [source])
    monkeypatch.setattr(cli.LLMSettings, "from_env", lambda: LLMSettings(api_key="offline-test"))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("known-blocked research must not create a paid client")

    monkeypatch.setattr(cli, "DeepSeekClient", forbidden)
    output = tmp_path / "never-created"
    args = cli.parse_args(["--ablation", "--repeats", "3", "--out", str(output)])
    assert await cli.main_async(args) == 2
    assert "TAVILY_API_KEY" in capsys.readouterr().err
    assert not output.exists()
