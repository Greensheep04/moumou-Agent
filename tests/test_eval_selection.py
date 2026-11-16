"""Explicit execution selections preserve the complete, indexed experiment plan."""

from __future__ import annotations

import json

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.eval import ablation
from moumou.eval.ablation import plan_ablation, plan_selected_ablation, run_ablation
from moumou.eval.loader import TaskFixture


def fixture(fixture_id, *, correct=True, search=False, warmup=False):
    values = {
        "id": fixture_id, "title": "Selected execution probe", "scenario": "from_scratch",
        "goal": "The answer is already prepared",
        "setup": {"answer.txt": "yes" if correct else "no"},
        "acceptance": [{"kind": "file_contains", "target": "answer.txt", "expect": "^yes$"}],
        "budget": {"max_retries": 0},
    }
    if search:
        values["requirements"] = {"required_tools": ["web_search"]}
    if warmup:
        values["warmup"] = {"goal": "Separate training", "acceptance": values["acceptance"]}
    return TaskFixture.model_validate(values)


def test_non_network_selection_preserves_full_original_plan_and_indices():
    sources = [fixture("first"), fixture("network", search=True), fixture("last")]
    original = plan_ablation(sources, repeats=2)
    selected = {item.id for item in sources if "web_search" not in item.requirements.required_tools}
    planned = plan_selected_ablation(sources, repeats=2, selected_fixture_ids=selected,
                                     deferred_reason="search configuration unavailable")
    assert planned["schedule"] == original["schedule"]
    assert planned["planned_runs"] == 24
    assert planned["selected_runs"] == 16 and planned["deferred_runs"] == 8
    assert len(planned["deferred_jobs"]) == 8
    for row in planned["deferred_jobs"]:
        assert row["status"] == "deferred"
        assert row["reason"] == "search configuration unavailable"
        assert {key: row[key] for key in original["schedule"][0]} in original["schedule"]


@pytest.mark.parametrize("selection,reason,error", [
    ("ready", "scope", TypeError),
    ([1], "scope", TypeError),
    (["ready", "ready"], "scope", ValueError),
    ({"unknown"}, "scope", ValueError),
    (set(), None, ValueError),
    (set(), "  ", ValueError),
    (set(), 1, ValueError),
])
async def test_invalid_selection_is_rejected_before_client_or_directory(tmp_path, selection, reason,
                                                                       error):
    output = tmp_path / "never"

    def forbidden():
        raise AssertionError("invalid selections cannot create a client")

    with pytest.raises(error):
        await run_ablation([fixture("ready")], forbidden, output, repeats=1,
                           selected_fixture_ids=selection, deferred_reason=reason)
    assert not output.exists()


async def test_selected_execution_retains_deferred_rows_and_never_warms_them(tmp_path, monkeypatch):
    sources = [fixture("correct"), fixture("blocked", search=True, warmup=True),
               fixture("incorrect", correct=False)]
    clients, guards = [], []

    def factory():
        client = FakeLLM(lambda messages, tools=None: LLMResponse(content="Done"))
        clients.append(client)
        return client

    async def forbidden_warmup(*args, **kwargs):
        raise AssertionError("deferred training must not run")

    monkeypatch.setattr(ablation, "_warmup", forbidden_warmup)
    manifest = await run_ablation(
        sources, factory, tmp_path, repeats=1,
        selected_fixture_ids={"correct", "incorrect"}, deferred_reason="no search key",
        before_attempt=lambda context: guards.append(context),
    )
    assert manifest["status"] == "partial"
    assert manifest["planned_runs"] == 12
    assert manifest["selected_runs"] == manifest["started_runs"] == manifest["completed_runs"] == 8
    assert manifest["deferred_runs"] == 4
    assert manifest["succeeded_runs"] == manifest["failed_runs"] == 4
    assert manifest["errored_runs"] == manifest["interrupted_runs"] == 0
    assert len(clients) == len(guards) == len(manifest["jobs"]) == 8
    assert manifest["warmups"] == []
    assert manifest["schedule"] == plan_ablation(sources, 1)["schedule"]
    assert {row["fixture_id"] for row in manifest["deferred_jobs"]} == {"blocked"}
    assert all(row["phase"] == "heldout" for row in guards)
    assert all(row["planned"]["fixture_index"] in {0, 2} for row in guards)
    for row in manifest["deferred_jobs"]:
        assert not (tmp_path / row["relative_dir"]).exists()
        assert "options" not in row and "results" not in row and "started_at" not in row
    saved = json.loads((tmp_path / "ablation.json").read_text(encoding="utf-8"))
    assert saved == manifest
    assert "deferred: 4" in (tmp_path / "report.md").read_text(encoding="utf-8")


async def test_fully_deferred_execution_is_not_completed_and_never_creates_a_client(tmp_path):
    def forbidden():
        raise AssertionError("fully deferred experiment must make zero calls")

    manifest = await run_ablation(
        [fixture("blocked", search=True)], forbidden, tmp_path, repeats=1,
        selected_fixture_ids=set(), deferred_reason="not ready",
    )
    assert manifest["status"] == "partial"
    assert manifest["jobs"] == [] and manifest["warmups"] == []
    assert manifest["planned_runs"] == manifest["deferred_runs"] == 4
    assert manifest["selected_runs"] == manifest["started_runs"] == manifest["completed_runs"] == 0


async def test_guard_rejection_preserves_full_deferred_plan_before_any_client(tmp_path):
    def forbidden():
        raise AssertionError("guard rejection precedes client creation")

    with pytest.raises(ValueError, match="integrity guard"):
        await run_ablation([fixture("ready"), fixture("blocked", search=True)], forbidden,
                           tmp_path, repeats=1, selected_fixture_ids={"ready"},
                           deferred_reason="no search key", before_attempt=lambda context: False)
    saved = json.loads((tmp_path / "ablation.json").read_text(encoding="utf-8"))
    assert saved["status"] == "error" and saved["started_runs"] == 0
    assert saved["planned_runs"] == 8 and len(saved["deferred_jobs"]) == 4
    assert saved["jobs"] == []
