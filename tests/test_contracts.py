"""Contract tests.

These do not test business logic. They test that the data shapes did not drift:
a teammate renaming a field or changing a type breaks CI here instead of
breaking the integration a week later.
"""

from __future__ import annotations

import json

import pytest

from moumou.contracts import (
    LEGAL_TRANSITIONS,
    MAX_DEPTH,
    Acceptance,
    AgentInstance,
    HumanRequest,
    HumanResponse,
    MemoryRecord,
    Message,
    RoleSpec,
    TaskNode,
    TraceEvent,
    can_transition,
)


def _task(**kw) -> TaskNode:
    base = dict(
        id="t1",
        project_id="p1",
        conversation_id="c1",
        goal="write a hello script",
        role="coder",
    )
    return TaskNode(**{**base, **kw})


def test_task_roundtrip_through_json():
    t = _task(acceptance=[Acceptance(kind="command", target="python hello.py", expect="0")])
    again = TaskNode.model_validate_json(t.model_dump_json())
    assert again == t


def test_task_defaults_are_independent():
    a, b = _task(id="a"), _task(id="b")
    a.deps.append("x")
    assert b.deps == [], "mutable default leaked between instances"


def test_legal_and_illegal_transitions():
    assert can_transition("PENDING", "READY")
    assert can_transition("RUNNING", "WAITING_HUMAN")
    assert can_transition("RUNNING", "PENDING")     # handed off to children
    assert can_transition("FAILED", "READY")
    assert not can_transition("DONE", "RUNNING")
    assert not can_transition("PENDING", "RUNNING")
    assert not can_transition("READY", "DONE")


def test_terminal_states_have_no_exits():
    assert LEGAL_TRANSITIONS["DONE"] == frozenset()
    assert LEGAL_TRANSITIONS["DEAD"] == frozenset()


def test_task_rejects_self_dependency():
    with pytest.raises(ValueError, match="depends on itself"):
        _task(id="t1", deps=["t1"])


def test_task_depth_is_capped():
    with pytest.raises(ValueError):
        _task(depth=MAX_DEPTH + 1)


def test_acceptance_requires_expect_for_executable_kinds():
    with pytest.raises(ValueError, match="requires `expect`"):
        Acceptance(kind="command", target="pytest")
    Acceptance(kind="command", target="pytest", expect="0")     # ok
    Acceptance(kind="file_exists", target="out.txt")            # ok


def test_model_created_role_must_justify_itself():
    with pytest.raises(ValueError, match="must supply a reason"):
        RoleSpec(name="coder", title="Coder", description="writes code", project_id="p1")
    RoleSpec(
        name="coder",
        title="Coder",
        description="writes code",
        project_id="p1",
        reason="no existing role can write Python",
    )
    RoleSpec(name="human_made", title="X", description="y", project_id="p1", created_by="human")


def test_role_name_must_be_snake_case():
    for bad in ("Coder", "api-designer", "9lives", ""):
        with pytest.raises(ValueError):
            RoleSpec(name=bad, title="x", description="y", project_id="p1", created_by="human")


def test_three_isolation_scopes():
    inst = AgentInstance(
        id="coder#2",
        role="coder",
        project_id="p1",
        conversation_id="c9",
        workspace="/tmp/ws",
    )
    assert inst.context_scope == "p1:c9:coder"   # conversation-scoped
    assert inst.memory_scope == "p1:coder"       # project-scoped
    # two instances of the same role share both
    other = inst.model_copy(update={"id": "coder#3"})
    assert other.context_scope == inst.context_scope


def test_trace_duration_is_derived_not_stored():
    e = TraceEvent(task_id="t1", kind="tool", name="shell_exec")
    assert e.duration_ms is None
    e.t1 = e.t0
    assert e.duration_ms == 0.0


def test_memory_record_outcome_is_closed_set():
    with pytest.raises(ValueError):
        MemoryRecord(
            project_id="p1",
            role="coder",
            task_id="t1",
            situation="s",
            action="a",
            outcome="maybe",
            lesson="l",
        )


def test_json_schema_is_emittable_for_function_calling():
    # The planner schema and every tool schema are generated from these models.
    for model in (TaskNode, RoleSpec, Acceptance, HumanRequest, HumanResponse, Message):
        schema = model.model_json_schema()
        assert schema["type"] == "object"
        json.dumps(schema)  # must be serialisable for the API request body


def test_message_defaults_to_single_recipient_list():
    m1 = Message(project_id="p1", conversation_id="c1", sender="a", content="hi")
    m2 = Message(project_id="p1", conversation_id="c1", sender="b", content="yo")
    m1.recipients.append("x")
    assert m2.recipients == []
