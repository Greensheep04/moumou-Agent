"""Official instructor source integrity and independent behavior-evidence contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from moumou.contracts import TraceEvent
from moumou.eval.behavior import evaluate_behavior, expected_behavior_targets
from moumou.eval.cases import load_cases
from moumou.eval.loader import TaskFixture

TASKS = Path(__file__).resolve().parents[1] / "eval/tasks"


def probe(**requirements):
    return TaskFixture(id="behavior_probe", title="Evidence contract", scenario="from_scratch",
                       goal="Inspect recorded behavior", requirements=requirements)


def test_official_sources_contain_only_three_original_teams_and_six_tasks():
    assert len(list(TASKS.rglob("*.md"))) == 10
    assert not list(TASKS.rglob("*.yaml")) and not list(TASKS.rglob("*.yml"))
    cases = load_cases(TASKS)
    assert [case.id for case in cases] == ["scenario_1", "scenario_2", "scenario_3"]
    assert len({role.name for case in cases for role in case.roles}) == 12
    assert sum(bool(case.task1) + bool(case.task2) for case in cases) == 6


@pytest.mark.parametrize("index,identities", [
    (0, ["lead", "dev", "qa", "release"]),
    (1, ["chief_editor", "reporter", "fact_checker", "editor"]),
    (2, ["fund_manager", "research", "risk", "marketing"]),
])
def test_official_prompts_keep_source_bytes_identities_and_task_sequence(index, identities):
    case = load_cases(TASKS)[index]
    assert [role.name for role in case.roles] == identities
    assert case.leader == identities[0]
    assert set(case.source_paths) == {"readme", "roles", "task1", "task2"}
    for name, path in case.source_paths.items():
        assert path.resolve().is_relative_to(TASKS.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == case.source_sha256[name]
    role_text = case.source_paths["roles"].read_text(encoding="utf-8-sig")
    blocks = re.findall(r"^```text[^\S\n]*\n(.*?)^```[^\S\n]*$", role_text, re.M | re.S)
    assert [role.description for role in case.roles] == [block.rstrip("\n") for block in blocks]
    for name, filename in [("task1", "02_task_1.md"), ("task2", "03_task_2.md")]:
        path = case.source_paths[name]
        assert path.name == filename
        text = path.read_text(encoding="utf-8-sig")
        block, = re.findall(r"^```text[^\S\n]*\n(.*?)^```[^\S\n]*$", text, re.M | re.S)
        assert getattr(case, name) == block.rstrip("\n")


# SHA-256 of each original self-authored goal (UTF-8, terminal newline removed).
# These fixed provenance values do not need Git, local evidence or model answers.
SUPPLEMENTARY_GOALS = {
    "s1_hello": "4dfe93342b46f7775b4ba5f11570912a1174e4d75632469f4bf38dec5bacd08f",
    "s1_stats": "465bab97d05532284fa80af017dfd90090e7261fc2906bdd946d3508b1b0f912",
    "s2_bugfix": "e067b5ec388860b4e602420b88472fae73258cf536fe5a49a04ae4ada10b7485",
    "s2_tags": "4253b40ccb463c9a7ce3dbf7a150b027b624043210c93af208fe9846e7490a50",
    "s3_cli": "b13d62be02867c0192b52ae7d4593b206e7130d3527199343903bfd1e1894640",
    "s3_catalog": "4bd0976ec7d4d14f9de396062f51060ff8586a432a4aedb51c3a4004f9c5ca67",
    "s4_python_research": "614fb903d15eb2d9709f33c27b32db2c9ac9d715f8632d83bc6e4f8bba4da988",
    "s4_http_research": "97558ab3fc6fe9e956a518f2f253f8b360c6ffcd371698ba8356686d5c760574",
    "s5_approval": "3776c8aa5b8fe37d7d5e3751467ae9c8e74225acb438a15fdd091d98436d7d33",
    "s5_approval_reject": "7140bd76ef4e6a5606bcc863d8744b865d8d178fe5bdf8944d911ca6c39182d7",
    "s6_csv_memory": "c449302418e1dcad02c09ae7469e97b41517c4755782e38e00d26b3a3072e735",
    "s6_path_memory": "8426f484108ade5f0c4393b8385fb2e7fd9420b568fa1d2a9b8172d1cb3fd33f",
    "s7_parallel_stats": "70622f2d0f76ca5e159340eff09a7338835042e38964ea7edbf934a2beb5cc0a",
    "s7_parallel_words": "0fe1046e5c6cde30ed551608cfd232e94666af859a0947a40846dd0ebf82a4a6",
    "s8_composite": "96ca289f04427f5cc2fa7b9e6ba2e95e442bbc2e69f2e4ccb1b9c9570ec8d737",
}
SUPPLEMENTARY = TASKS.parent / "supplementary"


def supplementary_sources():
    paths = sorted(SUPPLEMENTARY.glob("*/0[23]_task_*.md"))
    return [(path, path.read_text(encoding="utf-8-sig")) for path in paths]


def text_prompt(text):
    prompt, = re.findall(r"^```text[^\S\n]*\n(.*?)^```[^\S\n]*$", text, re.M | re.S)
    return prompt.rstrip("\n")


def test_supplementary_keeps_fifteen_unique_original_identities_and_goals():
    assert len(list(SUPPLEMENTARY.rglob("*.md"))) == 24
    assert not list(SUPPLEMENTARY.rglob("*.yaml"))
    assert not list(SUPPLEMENTARY.rglob("*.yml"))
    observed = {}
    for _, text in supplementary_sources():
        identity, = re.findall(r"^Original task ID: `([^`]+)`\.$", text, re.M)
        assert identity not in observed
        goal = text_prompt(text).split("\n\n", 1)[0]
        observed[identity] = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    assert observed == SUPPLEMENTARY_GOALS


def test_supplementary_uses_same_sequence_without_inventing_a_sixteenth_task():
    cases = load_cases(SUPPLEMENTARY)
    assert len(cases) == 8
    assert [len(case.tasks) for case in cases] == [2] * 7 + [1]
    assert sum(len(case.tasks) for case in cases) == 15
    assert cases[-1].task2 is None and "task2" not in cases[-1].source_paths
    assert all(case.source_kind == "supplementary" for case in cases)
    assert all(case.leader == "lead" and len(case.roles) == 4 for case in cases)
    expected = [text_prompt(text) for _, text in supplementary_sources()]
    assert [task for case in cases for task in case.tasks] == expected


def test_supplementary_historical_scripted_settings_are_not_live_prompt_instructions():
    for _, text in supplementary_sources():
        historical, = re.findall(r"^```json[^\S\n]*\n(.*?)^```[^\S\n]*$", text, re.M | re.S)
        metadata = json.loads(historical)
        assert metadata["id"] in SUPPLEMENTARY_GOALS
        assert metadata["human"]["default"] in {"approve", "reject"}
        prompt = text_prompt(text)
        assert historical not in prompt
        assert '"default": "approve"' not in prompt and '"default": "reject"' not in prompt
        assert "Ask the actual human" in prompt


def test_legacy_fixture_schema_defaults_are_compatible():
    bare = TaskFixture(id="legacy", title="t", scenario="from_scratch", goal="g")
    assert expected_behavior_targets(bare) == []
    assert bare.warmup is None
    assert evaluate_behavior(bare, [], nodes=[], options={}, memory_observations={}) == []


@pytest.mark.parametrize("requirements", [
    {"parallel_childen": True}, {"min_children": -1},
    {"approvals": [{"tool": "delete_file", "decision": "maybe"}]},
    {"required_tools": ["web_search", "web_search"]}, {"required_tools": [" "]},
])
def test_invalid_behavior_requirements_are_rejected(requirements):
    with pytest.raises(ValidationError):
        TaskFixture(id="bad", title="t", scenario="from_scratch", goal="g",
                    requirements=requirements)


def event(kind, name, start=0, end=1, *, task_id="t1", **payload):
    base = datetime(2026, 11, 13, tzinfo=UTC)
    return TraceEvent(task_id=task_id, kind=kind, name=name,
                      t0=base + timedelta(seconds=start), t1=base + timedelta(seconds=end),
                      status="ok", payload=payload)


def judge(fixture, events=(), *, nodes=(), options=None, observations=None):
    return evaluate_behavior(fixture, events, nodes=nodes, options=options or {},
                             memory_observations={} if observations is None else observations)


def test_approval_requires_a_real_same_task_effect_after_the_decision():
    fixture = probe(approvals=[{"tool": "delete_file", "decision": "approve"}])
    fixture.requirements.required_tools = []
    approved = event("approval", "delete_file", decision="approve")
    deleted = event("tool", "delete_file", 2, 3, ok=True)
    assert judge(fixture, [approved, deleted])[0]["ok"]
    for events in ([deleted], [approved],
                   [approved, deleted.model_copy(update={"task_id": "other"})],
                   [approved, event("tool", "delete_file", -2, -1, ok=True)]):
        assert not judge(fixture, events)[0]["ok"]


def test_rejection_requires_a_decision_and_forbids_later_successful_effects():
    fixture = probe(approvals=[{"tool": "delete_file", "decision": "reject"}])
    rejected = event("approval", "delete_file", decision="reject")
    assert judge(fixture, [rejected])[0]["ok"]
    assert not judge(fixture, [])[0]["ok"]
    assert not judge(fixture, [rejected, event("tool", "delete_file", 2, 3, ok=True)])[0]["ok"]
    assert judge(fixture, [rejected, event("tool", "delete_file", 2, 3, ok=False)])[0]["ok"]


def test_research_requires_successful_search_instead_of_a_source_claim():
    fixture = probe(required_tools=["web_search"])
    assert judge(fixture, [event("tool", "web_search", ok=True)])[0]["ok"]
    assert not judge(fixture, [event("tool", "web_search", ok=False)])[0]["ok"]
    assert not judge(fixture, [event("tool", "write_file", ok=True)])[0]["ok"]


@pytest.mark.parametrize("enabled,observations,wanted", [
    (True, {"t1": ["historical-lesson"]}, True), (True, {}, False),
    (False, {}, True), (False, {"t1": ["historical-lesson"]}, False),
    (False, [{"task_id": "t1", "memory_ids": [], "injected": True}], False),
    (True, [{"task_id": "t1", "memory_ids": [], "injected": True}], False),
    (True, [{"task_id": "t1", "memory_ids": ["lesson"], "injected": False}], False),
])
def test_memory_controls_are_checked_against_real_context_citations(enabled, observations, wanted):
    checks = judge(probe(memory_injection=True), options={"memory_enabled": enabled},
                   observations=observations)
    assert checks[0]["ok"] is wanted


def test_missing_memory_observations_fail_closed_even_when_memory_is_disabled():
    checks = evaluate_behavior(probe(memory_injection=True), [], nodes=[],
                               options={"memory_enabled": False}, memory_observations=None)
    assert not checks[0]["ok"]


@pytest.mark.parametrize("slots,overlap,wanted", [(4, True, True), (4, False, False),
                                                (1, False, True), (1, True, False)])
def test_parallel_and_serial_groups_check_real_child_intervals(slots, overlap, wanted):
    fixture = probe(min_children=2, parallel_children=True)
    nodes = [{"id": "t1", "parent_id": None}, {"id": "t1.a", "parent_id": "t1"},
             {"id": "t1.b", "parent_id": "t1"}]
    events = [event("delegate", "plan_applied", children=["t1.a", "t1.b"]),
              event("llm", "chat", 2, 4, task_id="t1.a"),
              event("tool", "run_python", 3 if overlap else 5, 6, task_id="t1.b", ok=True)]
    checks = judge(fixture, events, nodes=nodes, options={"max_slots": slots})
    assert checks[0]["ok"]
    assert checks[1]["ok"] is wanted
    assert [c["target"] for c in checks] == expected_behavior_targets(fixture)


def test_parent_intervals_and_unmaterialized_plans_cannot_fake_collaboration():
    fixture = probe(min_children=2, parallel_children=True)
    events = [event("delegate", "plan_applied", children=["t1.a", "t1.b"]),
              event("llm", "chat", 0, 10), event("tool", "run_python", 1, 9, ok=True)]
    checks = judge(fixture, events)
    assert not any(check["ok"] for check in checks)


def test_zero_duration_child_markers_do_not_establish_concurrency():
    fixture = probe(min_children=2, parallel_children=True)
    nodes = [{"id": "t1.a", "parent_id": "t1"}, {"id": "t1.b", "parent_id": "t1"}]
    events = [event("delegate", "plan_applied", children=["t1.a", "t1.b"]),
              event("llm", "chat", 1, 1, task_id="t1.a"),
              event("llm", "chat", 1, 1, task_id="t1.b")]
    checks = judge(fixture, events, nodes=nodes)
    assert checks[0]["ok"] and not checks[1]["ok"]


@pytest.mark.parametrize("deps,extra", [
    (["t1.b"], []),
    (["t1.bridge"], [{"id": "t1.bridge", "parent_id": "t1", "deps": ["t1.b"]}]),
    (["missing"], []),
    (["t1.a"], []),
])
def test_parallel_fixtures_reject_dependent_or_invalid_sibling_plans(deps, extra):
    fixture = probe(min_children=2, parallel_children=True)
    nodes = [{"id": "t1", "parent_id": None},
             {"id": "t1.a", "parent_id": "t1", "deps": deps},
             {"id": "t1.b", "parent_id": "t1", "deps": []}, *extra]
    events = [event("delegate", "plan_applied", children=["t1.a", "t1.b"]),
              event("llm", "chat", 1, 3, task_id="t1.a"),
              event("llm", "chat", 2, 4, task_id="t1.b")]
    checks = judge(fixture, events, nodes=nodes, options={"max_slots": 1})
    assert not checks[0]["ok"]


def test_nested_descendants_are_not_counted_as_independent_root_children():
    fixture = probe(min_children=2, parallel_children=True)
    nodes = [{"id": "t1", "parent_id": None}, {"id": "t1.a", "parent_id": "t1"},
             {"id": "t1.a.b", "parent_id": "t1.a"}]
    events = [event("delegate", "plan_applied", children=["t1.a"]),
              event("delegate", "plan_applied", task_id="t1.a", children=["t1.a.b"])]
    checks = judge(fixture, events, nodes=nodes)
    assert not checks[0]["ok"]
