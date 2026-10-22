"""State machine, persistence, and the role registry."""

from __future__ import annotations

import pytest

from moumou.contracts import RoleSpec, TaskNode
from moumou.orchestrator.state import (
    Database,
    IllegalTransition,
    RoleLimitExceeded,
    SqliteRoleRegistry,
    SqliteTaskRepo,
)


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "moumou.db")
    yield SqliteTaskRepo(db)
    db.close()


def node(**kw) -> TaskNode:
    base = dict(id="t1", project_id="p", conversation_id="c", goal="g", role="r")
    return TaskNode(**{**base, **kw})


def test_roundtrip_keeps_every_field(repo):
    n = node(deps=["a", "b"])
    repo.add(n)
    back = repo.get("t1")
    assert back is not None
    assert back.deps == ["a", "b"]
    assert back.goal == "g"
    assert back.budget.max_steps == n.budget.max_steps


def test_transition_writes_an_audit_event(repo):
    repo.add(node())
    repo.transition("t1", "READY", "submitted")
    repo.transition("t1", "RUNNING", "dispatched")

    log = repo.event_log("t1")
    assert [(e["from_status"], e["to_status"], e["reason"]) for e in log] == [
        ("PENDING", "READY", "submitted"),
        ("READY", "RUNNING", "dispatched"),
    ]


def test_illegal_transition_is_refused_and_leaves_state_untouched(repo):
    repo.add(node())
    repo.transition("t1", "READY", "submitted")
    with pytest.raises(IllegalTransition):
        repo.transition("t1", "DONE", "shortcut")
    assert repo.get("t1").status == "READY"
    assert len(repo.event_log("t1")) == 1


def test_promote_unblocks_when_deps_are_done(repo):
    repo.add(node(id="a"))
    repo.add(node(id="b", deps=["a"]))

    repo.promote()
    assert repo.get("a").status == "READY"
    assert repo.get("b").status == "PENDING"      # still waiting on a

    repo.transition("a", "RUNNING", "x")
    repo.transition("a", "DONE", "x")

    repo.promote()
    assert repo.get("b").status == "READY"


def test_promote_wakes_the_parent_when_a_child_dies(repo):
    repo.add(node(id="a"))
    repo.add(node(id="b", deps=["a"]))

    repo.transition("a", "DEAD", "gave_up")
    repo.promote()

    b = repo.get("b")
    assert b.status == "READY", "the parent gets a chance to re-dispatch"
    assert b.wakeups == 1
    assert repo.event_log("b")[-1]["reason"] == "upstream_failed_review"


def test_promote_gives_up_after_the_wakeup_budget(repo):
    from moumou.contracts import MAX_WAKEUPS

    repo.add(node(id="a"))
    repo.add(node(id="b", deps=["a"], wakeups=MAX_WAKEUPS))
    repo.transition("a", "DEAD", "gave_up")

    repo.promote()

    assert repo.get("b").status == "DEAD"
    assert repo.event_log("b")[-1]["reason"] == "upstream_failed"


def test_promote_kills_tasks_with_missing_dependencies(repo):
    repo.add(node(id="b", deps=["ghost"]))
    repo.promote()
    assert repo.get("b").status == "DEAD"
    assert repo.event_log("b")[-1]["reason"] == "missing_dependency"


def test_retries_are_counted(repo):
    repo.add(node())
    assert repo.bump_retries("t1") == 1
    assert repo.bump_retries("t1") == 2
    assert repo.get("t1").retries == 2


# -- roles -----------------------------------------------------------------


@pytest.fixture
def roles(tmp_path):
    db = Database(tmp_path / "roles.db")
    yield SqliteRoleRegistry(db, max_roles=3)
    db.close()


def spec(name: str, **kw) -> RoleSpec:
    base = dict(name=name, title="T", description="D", project_id="p", created_by="human")
    return RoleSpec(**{**base, **kw})


def test_role_registry_is_query_before_create(roles):
    first = roles.create(spec("coder"))
    again = roles.create(spec("coder", title="Something else"))
    assert again.title == first.title            # existing role wins, no duplicate
    assert roles.count("p") == 1


def test_role_cap_blocks_role_explosion(roles):
    for name in ("a_role", "b_role", "c_role"):
        roles.create(spec(name))
    with pytest.raises(RoleLimitExceeded):
        roles.create(spec("d_role"))


def test_roles_are_scoped_per_project(roles):
    roles.create(spec("coder"))
    assert roles.get("p", "coder") is not None
    assert roles.get("other_project", "coder") is None
