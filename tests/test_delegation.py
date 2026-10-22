"""Delegation: what the framework refuses to turn into tasks.

These are the four gates between "the model proposed a plan" and "children exist".
"""

from __future__ import annotations

import pytest

from moumou.contracts import Plan, RoleDraft, RoleSpec, SubtaskSpec, TaskNode
from moumou.orchestrator.delegation import Delegation, DelegationError
from moumou.orchestrator.state import Database, SqliteRoleRegistry, SqliteTaskRepo

TOOLS = {"read_file", "write_file", "submit_plan", "ask_human"}


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "delegation.db")
    repo = SqliteTaskRepo(db)
    roles = SqliteRoleRegistry(db)
    yield Delegation(repo, roles, None, TOOLS), repo, roles
    db.close()


def a_parent(depth: int = 0) -> TaskNode:
    return TaskNode(
        id="t1",
        project_id="p",
        conversation_id="c",
        goal="build the thing",
        role="planner",
        depth=depth,
    )


def a_draft(name: str = "coder", tools: list[str] | None = None) -> RoleDraft:
    return RoleDraft(
        name=name,
        title="Coder",
        description="writes code",
        tools=["read_file"] if tools is None else tools,
        reason="no existing role writes code",
    )


def a_spec(key: str = "a", role: str = "coder", deps: list[str] | None = None) -> SubtaskSpec:
    return SubtaskSpec(key=key, goal="do the thing", role=role, deps=deps or [])


def a_plan(*subtasks: SubtaskSpec, roles: list[RoleDraft] | None = None) -> Plan:
    return Plan(reasoning="", roles=roles or [], subtasks=list(subtasks))


def test_a_clean_plan_passes(env):
    delegation, _, _ = env
    delegation.validate(a_parent(), a_plan(a_spec(), roles=[a_draft()]))


def test_the_lead_role_cannot_be_a_delegate(env):
    delegation, _, roles = env
    roles.create(
        RoleSpec(
            name="planner", title="Lead", description="leads", project_id="p", created_by="human"
        )
    )
    with pytest.raises(DelegationError, match="lead role"):
        delegation.validate(a_parent(), a_plan(a_spec(role="planner")))


def test_a_dependency_cycle_is_refused(env):
    delegation, _, _ = env
    plan = a_plan(
        a_spec("a", deps=["b"]),
        a_spec("b", deps=["a"]),
        roles=[a_draft()],
    )
    with pytest.raises(DelegationError, match="cycle"):
        delegation.validate(a_parent(), plan)


def test_too_many_subtasks_are_refused(env):
    delegation, _, _ = env
    plan = a_plan(*[a_spec(f"s{i}") for i in range(7)], roles=[a_draft()])
    with pytest.raises(DelegationError, match="too many"):
        delegation.validate(a_parent(), plan)


def test_an_unknown_tool_is_refused(env):
    delegation, _, _ = env
    plan = a_plan(a_spec(), roles=[a_draft(tools=["rm_rf_everything"])])
    with pytest.raises(DelegationError, match="unknown tools"):
        delegation.validate(a_parent(), plan)


def test_a_role_that_is_neither_declared_nor_known_is_refused(env):
    delegation, _, _ = env
    with pytest.raises(DelegationError, match="neither an existing role"):
        delegation.validate(a_parent(), a_plan(a_spec(role="ghost")))


def test_the_depth_limit_is_enforced(env):
    delegation, _, _ = env
    with pytest.raises(DelegationError, match="depth limit"):
        delegation.validate(a_parent(depth=2), a_plan(a_spec(), roles=[a_draft()]))


def test_applying_a_plan_creates_children_and_parks_the_parent(env):
    delegation, repo, roles = env
    parent = a_parent()
    repo.add(parent)
    repo.transition(parent.id, "READY", "submitted")
    repo.transition(parent.id, "RUNNING", "dispatched")

    plan = a_plan(a_spec(), roles=[a_draft()]).model_dump()
    children = delegation.apply(repo.get(parent.id), plan)

    assert [c.id for c in children] == ["t1.a"]
    assert roles.get("p", "coder") is not None, "a declared role is persisted for reuse"
    parked = repo.get("t1")
    assert parked.status == "PENDING"
    assert parked.deps == ["t1.a"], "the parent now depends on its children"
