"""Positive artifacts, plausible mistakes, and execution-evidence requirements.

The solutions below are deterministic acceptance probes, never model scores or
preloaded answers in evaluated workspaces. Acceptance commands live in the
fixture, so replacing a workspace test file cannot make these probes pass.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from moumou.contracts import TaskNode, TraceEvent
from moumou.eval.behavior import evaluate_behavior, expected_behavior_targets
from moumou.eval.loader import TaskFixture, load_tasks
from moumou.eval.workspace import write_setup
from moumou.orchestrator.acceptance import AcceptanceChecker

TASKS = Path(__file__).resolve().parents[1] / "eval/tasks"
FIXTURES = {fixture.id: fixture for fixture in load_tasks(TASKS)}


def _json(data):
    return json.dumps(data, ensure_ascii=False) + "\n"


ARTIFACT_SOLUTIONS = {
    "s1_hello": {"hello.py": "print('hello, moumou')\n"},
    "s1_stats": {
        "stats.py": (
            "import json, sys\n"
            "def describe(values):\n"
            "    return dict(count=len(values),sum=sum(values),min=min(values) if values "
            "else None,max=max(values) if values else None,mean=sum(values)/len(values) "
            "if values else None)\n"
            "if __name__ == '__main__':\n"
            "    with open(sys.argv[1],encoding='utf-8') as f:\n"
            "        data=list(map(int,f.read().split()))\n"
            "    with open(sys.argv[2],'w',encoding='utf-8') as f: json.dump(describe(data),f)\n"
        ),
    },
    "s2_bugfix": {
        "calc.py": "def add(a,b):\n    return a+b\n\n"
        "if __name__ == '__main__':\n    print(add(2,3))\n",
    },
    "s2_tags": {
        "tags.py": "def normalize_tags(tags):\n"
        "    return list(dict.fromkeys(t.strip().lower() for t in tags if t.strip()))\n",
    },
    "s3_cli": {
        "greet.py": (
            "import argparse\n"
            "def positive(value):\n"
            "    n=int(value)\n"
            "    if n<1: raise argparse.ArgumentTypeError('positive count required')\n"
            "    return n\n"
            "def main():\n"
            "    p=argparse.ArgumentParser(); p.add_argument('name')\n"
            "    p.add_argument('--uppercase',action='store_true')\n"
            "    p.add_argument('--times',type=positive,default=1); a=p.parse_args()\n"
            "    text='hello, '+a.name\n"
            "    if a.uppercase: text=text.upper()\n"
            "    for _ in range(a.times): print(text)\n"
            "if __name__ == '__main__': main()\n"
        ),
    },
    "s3_catalog": {
        "catalog.py": "def select(items,min_price=0,category=None):\n"
        "    return [r for r in items if r['price']>=min_price and "
        "(category is None or r['category'].lower()==category.lower())]\n",
    },
    "s4_python_research": {
        "runtime.json": _json({
            "topic": "python-interpreter", "executable_attribute": "sys.executable",
            "venv_prefix_attribute": "sys.prefix", "base_prefix_attribute": "sys.base_prefix",
            "retrieved_on": "2026-11-13", "sources": [{
                "title": "Python sys documentation",
                "url": "https://docs.python.org/3/library/sys.html#sys.executable",
            }],
        }),
        "runtime.md": "sys.executable identifies the interpreter binary; sys.prefix and "
        "sys.base_prefix identify current and base environments.\n"
        "https://docs.python.org/3/library/sys.html#sys.executable\n",
    },
    "s4_http_research": {
        "rate_limit.json": _json({
            "status": 429, "meaning": "Too Many Requests", "retry_after_optional": True,
            "retrieved_on": "2026-11-13", "sources": [{
                "title": "RFC 6585 section 4",
                "url": "https://www.rfc-editor.org/rfc/rfc6585#section-4",
            }],
        }),
        "rate_limit.md": "HTTP 429 means Too Many Requests. Retry-After is optional.\n"
        "https://www.rfc-editor.org/rfc/rfc6585#section-4\n",
    },
    "s5_approval": {},
    "s5_approval_reject": {"decision.md": "Deletion rejected; protected.txt retained.\n"},
    "s6_csv_memory": {
        "customers.py": "import csv,io\n"
        "def parse_customers(text):\n"
        "    return list(csv.DictReader(io.StringIO(text.lstrip('\\ufeff'))))\n",
        "customers.json": _json([
            {"name": "Lee, Kim", "email": "lee@example.test"}, {"name": "Zoë", "email": ""},
        ]),
    },
    "s6_path_memory": {
        "report_writer.py": "from pathlib import Path\n"
        "def write_report(path,text):\n"
        "    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)\n"
        "    path.write_text(text,encoding='utf-8')\n",
        "output/weekly/summary.txt": "résumé ready\n",
    },
    "s7_parallel_stats": {
        "left.json": _json({"count": 3, "sum": 7}),
        "right.json": _json({"count": 3, "sum": 8}),
        "combined.json": _json({"count": 6, "sum": 15}),
    },
    "s7_parallel_words": {
        "words_a.json": _json({"red": 2, "blue": 1}),
        "words_b.json": _json({"blue": 1, "green": 1}),
        "words.json": _json({"red": 2, "blue": 2, "green": 1}),
    },
    "s8_composite": {
        "status_policy.py": "def classify_status(code):\n"
        "    groups=['informational','success','redirection','client_error','server_error']\n"
        "    return groups[code//100-1] if 100<=code<600 else 'unknown'\n",
        "policy.md": "informational success redirection client_error server_error\n"
        "https://www.rfc-editor.org/rfc/rfc9110#section-15\n",
    },
}

DELETED_ARTIFACTS = {"s5_approval": ["legacy.py"], "s8_composite": ["legacy_policy.txt"]}

WRONG_ARTIFACTS = {
    "s1_hello": {"hello.py": "print('Hello, moumou')\n"},
    "s1_stats": {"stats.py": "def describe(values):\n    return {'count':5,'sum':17}\n"},
    "s2_bugfix": {"calc.py": "def add(a,b):\n    return 5\n\n"
                  "if __name__ == '__main__': print(5)\n"},
    "s2_tags": {"tags.py": "def normalize_tags(tags):\n"
                "    return [t.strip().lower() for t in tags if t.strip()]\n"},
    "s3_cli": {"greet.py": "import sys\nprint('hello, '+sys.argv[1])\n"},
    "s3_catalog": {"catalog.py": "def select(items,min_price=0,category=None):\n"
                   "    return [r for r in items if r['price']>=min_price]\n"},
    "s4_python_research": {"runtime.json": ARTIFACT_SOLUTIONS["s4_python_research"][
        "runtime.json"].replace("docs.python.org", "example.test")},
    "s4_http_research": {"rate_limit.json": ARTIFACT_SOLUTIONS["s4_http_research"][
        "rate_limit.json"].replace(
            '"retry_after_optional": true', '"retry_after_optional": false')},
    "s5_approval": {"app.py": "print('changed without permission')\n"},
    "s5_approval_reject": {"protected.txt": "tampered\n"},
    "s6_csv_memory": {"customers.py": "def parse_customers(text):\n"
                      "    rows=[line.split(',') for line in text.splitlines()]\n"
                      "    return [dict(zip(rows[0],r)) for r in rows[1:]]\n"},
    "s6_path_memory": {"report_writer.py": "from pathlib import Path\n"
                       "def write_report(path,text):\n"
                       "    Path(path).write_text(text,encoding='utf-8')\n"},
    "s7_parallel_stats": {"combined.json": _json({"count": 6, "sum": 14})},
    "s7_parallel_words": {"words.json": _json({"red": 1, "blue": 2, "green": 1})},
    "s8_composite": {"status_policy.py": "def classify_status(code):\n    return 'success'\n"},
}


def seed_solution(workspace: Path, fixture: TaskFixture, *, wrong=False):
    """Set up an acceptance probe without performing or claiming an agent run."""
    write_setup(workspace, fixture.setup)
    write_setup(workspace, ARTIFACT_SOLUTIONS[fixture.id])
    for name in DELETED_ARTIFACTS.get(fixture.id, []):
        (workspace / name).unlink()
    if wrong:
        write_setup(workspace, WRONG_ARTIFACTS[fixture.id])


def test_fifteen_unique_fixtures_cover_each_scenario_twice_plus_composite():
    assert len(FIXTURES) == 15
    ordinary = [f for f in FIXTURES.values() if "composite" not in f.tags]
    assert set(Counter(f.scenario for f in ordinary).values()) == {2}
    assert len({f.scenario for f in ordinary}) == 7
    assert sum("composite" in f.tags for f in FIXTURES.values()) == 1
    assert all(f.repeats == 3 and f.acceptance for f in FIXTURES.values())
    assert set(ARTIFACT_SOLUTIONS) == set(WRONG_ARTIFACTS) == set(FIXTURES)


@pytest.mark.parametrize("fixture_id", sorted(FIXTURES))
@pytest.mark.parametrize("wrong", [False, True], ids=["correct", "typical-mistake"])
async def test_each_fixture_accepts_correct_artifacts_and_rejects_a_typical_mistake(
    tmp_path, fixture_id, wrong,
):
    fixture = FIXTURES[fixture_id]
    workspace = tmp_path / fixture.id
    workspace.mkdir()
    seed_solution(workspace, fixture, wrong=wrong)
    node = TaskNode(id="t1", project_id="probe", conversation_id="probe", role="planner",
                    goal=fixture.goal, acceptance=fixture.acceptance)
    passed, checks = await AcceptanceChecker().check(node, workspace)
    assert passed is not wrong, checks
    assert all(check["kind"] != "self_report" for check in checks)


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


def test_memory_warmups_have_separate_goals_data_and_artifact_paths():
    for fixture in FIXTURES.values():
        if not fixture.requirements.memory_injection:
            assert fixture.warmup is None
            continue
        assert fixture.warmup and fixture.warmup.acceptance
        assert fixture.warmup.goal != fixture.goal
        assert not set(fixture.warmup.setup) & set(fixture.setup)
        assert "customers.json" not in fixture.warmup.goal
        assert "résumé ready" not in fixture.warmup.goal


def event(kind, name, start=0, end=1, *, task_id="t1", **payload):
    base = datetime(2026, 11, 13, tzinfo=UTC)
    return TraceEvent(task_id=task_id, kind=kind, name=name,
                      t0=base + timedelta(seconds=start), t1=base + timedelta(seconds=end),
                      status="ok", payload=payload)


def judge(fixture, events=(), *, nodes=(), options=None, observations=None):
    return evaluate_behavior(fixture, events, nodes=nodes, options=options or {},
                             memory_observations={} if observations is None else observations)


def test_approval_requires_a_real_same_task_effect_after_the_decision():
    fixture = FIXTURES["s5_approval"].model_copy(deep=True)
    fixture.requirements.required_tools = []
    approved = event("approval", "delete_file", decision="approve")
    deleted = event("tool", "delete_file", 2, 3, ok=True)
    assert judge(fixture, [approved, deleted])[0]["ok"]
    for events in ([deleted], [approved],
                   [approved, deleted.model_copy(update={"task_id": "other"})],
                   [approved, event("tool", "delete_file", -2, -1, ok=True)]):
        assert not judge(fixture, events)[0]["ok"]


def test_rejection_requires_a_decision_and_forbids_later_successful_effects():
    fixture = FIXTURES["s5_approval_reject"]
    rejected = event("approval", "delete_file", decision="reject")
    assert judge(fixture, [rejected])[0]["ok"]
    assert not judge(fixture, [])[0]["ok"]
    assert not judge(fixture, [rejected, event("tool", "delete_file", 2, 3, ok=True)])[0]["ok"]
    assert judge(fixture, [rejected, event("tool", "delete_file", 2, 3, ok=False)])[0]["ok"]


def test_research_requires_successful_search_instead_of_a_source_claim():
    fixture = FIXTURES["s4_python_research"]
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
    checks = judge(FIXTURES["s6_csv_memory"], options={"memory_enabled": enabled},
                   observations=observations)
    assert checks[0]["ok"] is wanted


def test_missing_memory_observations_fail_closed_even_when_memory_is_disabled():
    checks = evaluate_behavior(FIXTURES["s6_csv_memory"], [], nodes=[],
                               options={"memory_enabled": False}, memory_observations=None)
    assert not checks[0]["ok"]


@pytest.mark.parametrize("slots,overlap,wanted", [(4, True, True), (4, False, False),
                                                (1, False, True), (1, True, False)])
def test_parallel_and_serial_groups_check_real_child_intervals(slots, overlap, wanted):
    fixture = FIXTURES["s7_parallel_stats"]
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
    fixture = FIXTURES["s7_parallel_words"]
    events = [event("delegate", "plan_applied", children=["t1.a", "t1.b"]),
              event("llm", "chat", 0, 10), event("tool", "run_python", 1, 9, ok=True)]
    checks = judge(fixture, events)
    assert not any(check["ok"] for check in checks)


def test_zero_duration_child_markers_do_not_establish_concurrency():
    fixture = FIXTURES["s7_parallel_words"]
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
    fixture = FIXTURES["s7_parallel_words"]
    nodes = [{"id": "t1", "parent_id": None},
             {"id": "t1.a", "parent_id": "t1", "deps": deps},
             {"id": "t1.b", "parent_id": "t1", "deps": []}, *extra]
    events = [event("delegate", "plan_applied", children=["t1.a", "t1.b"]),
              event("llm", "chat", 1, 3, task_id="t1.a"),
              event("llm", "chat", 2, 4, task_id="t1.b")]
    checks = judge(fixture, events, nodes=nodes, options={"max_slots": 1})
    assert not checks[0]["ok"]


def test_nested_descendants_are_not_counted_as_independent_root_children():
    fixture = FIXTURES["s7_parallel_words"]
    nodes = [{"id": "t1", "parent_id": None}, {"id": "t1.a", "parent_id": "t1"},
             {"id": "t1.a.b", "parent_id": "t1.a"}]
    events = [event("delegate", "plan_applied", children=["t1.a"]),
              event("delegate", "plan_applied", task_id="t1.a", children=["t1.a.b"])]
    checks = judge(fixture, events, nodes=nodes)
    assert not checks[0]["ok"]
