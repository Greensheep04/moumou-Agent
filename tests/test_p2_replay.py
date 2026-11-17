"""P2 replay consumer regressions; authored without executing them during integration."""

from __future__ import annotations

import io
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from moumou.contracts import TraceEvent

pytest.importorskip("rich", reason="P2 renderer requires the optional p2 extra")

from rich.console import Console  # noqa: E402

from moumou.ui.replay import load_events, load_replay, replay, replay_tree  # noqa: E402

START = datetime(2026, 11, 16, tzinfo=UTC)


def event(span, *, task="t1", parent=None, name=None, offset=0):
    return TraceEvent(span_id=span, parent_span=parent, task_id=task,
                      kind="task", name=name or span, t0=START + timedelta(seconds=offset),
                      t1=START + timedelta(seconds=offset + 1), status="ok")


def output():
    stream = io.StringIO()
    return Console(file=stream, width=2000, color_system=None), stream


def export_for(*scenario_ids):
    scenarios, tasks = [], []
    for identity in scenario_ids:
        root = event("same-root-span", name=f"root-{identity}")
        node = {"id": "t1", "project_id": identity, "conversation_id": "instructor-group",
                "role": "lead", "status": "DONE"}
        scenarios.append({
            "scenario_id": identity,
            "saved_status": "agent_completed_quality_unreviewed",
            "trace": {"state": "available", "source": None, "events": [{
                "record": root.model_dump(mode="json"),
                "identity": {"scenario_id": identity, "task_id": "t1"},
                "source": {"line_number": 1, "line_sha256": "f" * 64},
            }]},
            "task_results": [{
                "record": {"phase": "task_1", "root_task_id": "t1", "root_status": "DONE",
                           "nodes": [node],
                           "quality_status": "awaiting_human_or_independent_review",
                           "verified_goal_success": None},
                "effective_quality": {
                    "quality_status": "human_rejected", "verified_goal_success": False,
                    "quality_source": "offline_human_review",
                    "raw_quality": {"verified_goal_success": None},
                },
                "artifact_snapshot": {
                    "state": "validated_saved_capture",
                    "reference": {"path": "task_1-artifacts/manifest.json", "sha256": "a" * 64},
                },
                "memory_snapshot": {"status": "partial"},
            }],
            "state_snapshot": {"state": "unknown", "record": None},
            "human": {"answered": [], "pending_files": [], "staged_response_files": []},
        })
        tasks.append({"identity": {"scenario_id": identity, "task_id": "t1"}, "node": node})
    return {"schema_version": 1, "kind": "instructor_replay", "read_only": True,
            "root": "DO_NOT_READ_A_LIVE_WORKSPACE",
            "saved_suite_status": "agent_completed_quality_unreviewed",
            "metrics": {"suite_wall_time_s": None},
            "scenarios": scenarios, "tasks": tasks}


def test_query_only_store_uses_the_requested_task_without_inventing_all_events():
    class Store:
        def __init__(self):
            self.calls = []

        def query(self, task_id):
            self.calls.append(task_id)
            return [event("stored", task=task_id)]

    store = Store()
    with pytest.raises(ValueError, match="requires task_id"):
        load_events(store)
    assert store.calls == []
    assert [row.span_id for row in load_events(store, task_id="t1")] == ["stored"]
    assert store.calls == ["t1"]


def test_query_result_cannot_smuggle_in_another_task():
    class Store:
        def query(self, task_id):
            return [event("wrong", task="another-task")]

    with pytest.raises(ValueError, match="another task"):
        load_events(Store(), task_id="t1")


def test_iterable_and_explicit_jsonl_return_original_sorted_events(tmp_path):
    late, early = event("late", offset=10), event("early")
    assert [row.span_id for row in load_events(iter([late, early]))] == ["early", "late"]
    path = tmp_path / "trace.jsonl"
    path.write_text(late.model_dump_json() + "\n" + early.model_dump_json() + "\n",
                    encoding="utf-8")
    assert [row.span_id for row in load_events(path)] == ["early", "late"]


def test_missing_saved_identity_is_rejected_without_generating_replacement_ids(tmp_path):
    path = tmp_path / "trace.jsonl"
    raw = event("original").model_dump(mode="json")
    raw.pop("span_id")
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="required identity"):
        load_events(path)


def test_equal_task_and_span_ids_in_different_scenarios_stay_separate(tmp_path):
    original = export_for("scenario_1", "scenario_2")
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    view = load_replay(path)
    assert {(item.scenario_id, item.event.span_id) for item in view.items} == {
        ("scenario_1", "same-root-span"), ("scenario_2", "same-root-span"),
    }
    with pytest.raises(ValueError, match="multiple scenarios"):
        load_events(path, task_id="t1")
    with pytest.raises(ValueError, match="multiple scenarios"):
        replay_tree(path, "t1", console=output()[0])
    selected = load_replay(path, task_id="t1", scenario_id="scenario_2")
    assert [item.scenario_id for item in selected.items] == ["scenario_2"]
    assert selected.items[0].event.name == "root-scenario_2"


def test_formal_run_directory_delegates_to_the_existing_p3_reader(tmp_path, monkeypatch):
    original = export_for("scenario_1")
    called = []

    def saved_reader(root):
        called.append(root)
        return original

    monkeypatch.setattr("moumou.eval.replay.build_replay", saved_reader)
    view = load_replay(tmp_path, task_id="t1", scenario_id="scenario_1")
    assert called == [tmp_path]
    assert view.source_kind == "validated_saved_run"
    assert view.items[0].event.span_id == "same-root-span"


def test_saved_quality_provenance_and_capture_refs_are_displayed_without_rejudging(
        tmp_path, monkeypatch):
    original = export_for("scenario_1")
    before = deepcopy(original)
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(original), encoding="utf-8")

    def no_live_reconstruction(*args, **kwargs):
        raise AssertionError("an exported view must not rebuild from a later live workspace")

    monkeypatch.setattr("moumou.eval.replay.build_replay", no_live_reconstruction)
    console, stream = output()
    rows = replay(path, task_id="t1", scenario_id="scenario_1", console=console)
    text = stream.getvalue()
    assert len(rows) == 1 and original == before
    assert "awaiting_human_or_independent_review" in text
    assert "human_rejected" in text and "offline_human_review" in text
    assert "task_1-artifacts/manifest.json" in text and "a" * 64 in text
    assert "suite_wall_time_s：None" in text
    assert "不据事件跨度重算" in text
    assert "总跨度" not in text
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_tree_prints_original_roots_children_and_unknown_parent_once():
    root = event("root-id", name="ROOT_UNIQUE")
    child = event("child-id", parent="root-id", name="CHILD_UNIQUE")
    orphan = event("orphan-id", parent="outside-this-task", name="ORPHAN_UNIQUE")
    console, stream = output()
    replay_tree([root, child, orphan, root], "t1", console=console)
    text = stream.getvalue()
    assert text.count("ROOT_UNIQUE") == 1
    assert text.count("CHILD_UNIQUE") == 1
    assert text.count("ORPHAN_UNIQUE") == 1
    assert "parent=outside-this-task" in text
    assert "父 span 未包含在本任务中" in text
    assert "重复记录 1 条" in text


def test_conflicting_duplicate_span_identity_fails_instead_of_hiding_a_node():
    first = event("duplicate", name="original")
    other = first.model_copy(update={"name": "changed"})
    with pytest.raises(ValueError, match="conflicting duplicate"):
        load_replay([first, other])


def test_cycle_component_preserves_original_edges_without_recursing_forever():
    rows = [event("a", parent="b", name="CYCLE_A_UNIQUE"),
            event("b", parent="a", name="CYCLE_B_UNIQUE"),
            event("child", parent="a", name="CYCLE_CHILD_UNIQUE")]
    console, stream = output()
    replay_tree(rows, "t1", console=console)
    text = stream.getvalue()
    assert "循环/无自然根分量" in text
    assert text.count("CYCLE_A_UNIQUE") == text.count("CYCLE_B_UNIQUE") == 1
    assert text.count("CYCLE_CHILD_UNIQUE") == 1
    assert "span=a, parent=b" in text and "span=b, parent=a" in text


def test_deep_tree_is_walked_iteratively_without_inventing_parent_links():
    rows = [event(str(i), parent=str(i - 1) if i else None, name=f"EVENT_{i}_UNIQUE")
            for i in range(1100)]
    console, stream = output()
    replay_tree(rows, "t1", console=console)
    text = stream.getvalue()
    assert text.count("EVENT_0_UNIQUE") == 1
    assert text.count("EVENT_1099_UNIQUE") == 1
    assert "span=1099, parent=1098" in text


def test_single_scenario_source_does_not_accept_an_unverified_scenario_label():
    with pytest.raises(ValueError, match="formal run"):
        load_replay([event("root")], scenario_id="invented-scenario")


def test_real_all_events_capability_remains_supported_without_fabricating_it():
    class Store:
        def all_events(self):
            return [event("whole-journal")]

        def query(self, task_id):
            return [event("exact-task", task=task_id)]

    assert [row.span_id for row in load_events(Store())] == ["whole-journal"]
    assert [row.span_id for row in load_events(Store(), task_id="t1")] == ["exact-task"]


@pytest.mark.parametrize("label", ["task_id", "scenario_id"])
def test_empty_identity_selectors_are_rejected(label):
    with pytest.raises(ValueError, match="non-empty original identity"):
        load_replay([event("root")], **{label: ""})


def test_legacy_event_list_cannot_drop_multiple_scenario_namespaces():
    model = export_for("scenario_1", "scenario_2")
    with pytest.raises(ValueError, match="use load_replay"):
        load_events(model)
    assert len(load_events(model, scenario_id="scenario_1")) == 1


def test_task_filter_cannot_hide_a_conflicting_duplicate_span():
    first = event("duplicate", task="t1")
    other = first.model_copy(update={"task_id": "t2"})
    with pytest.raises(ValueError, match="conflicting duplicate"):
        load_replay([first, other], task_id="t1")


@pytest.mark.parametrize("invalid_task", [None, "", False, " "])
def test_tree_requires_an_explicit_nonempty_task_identity(invalid_task):
    with pytest.raises(ValueError, match="requires a non-empty original task_id"):
        replay_tree(export_for("scenario_1", "scenario_2"), invalid_task, console=output()[0])
