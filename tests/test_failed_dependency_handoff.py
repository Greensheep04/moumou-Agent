"""Isolated offline contracts for failed-child recovery and complete file handoff.

No live run, environment file, model or search is used. All databases and files
are pytest-owned temporary fixtures. Failures on the frozen source are retained
as evidence, not rewritten into expected implementation behavior.
"""

from types import SimpleNamespace

import pytest

from moumou.adapters import FakeLLM
from moumou.contracts import MAX_WAKEUPS, TaskNode, TaskResult
from moumou.orchestrator import Engine, Settings
from moumou.orchestrator.scheduler import Scheduler
from moumou.orchestrator.state import Database, SqliteTaskRepo


def node(task_id, *, parent_id=None, **values):
    return TaskNode(id=task_id, project_id="isolated", conversation_id="fixture",
                    parent_id=parent_id, depth=0 if parent_id is None else 1,
                    role=task_id, goal="Synthetic dependency contract", **values)


@pytest.fixture
def repo(tmp_path):
    database = Database(tmp_path / "tasks.sqlite3")
    try:
        yield SqliteTaskRepo(database)
    finally:
        database.close()


def recovery_graph(repo, *, checker_status="PENDING"):
    # Parent-first insertion is how normal delegation creates the graph.
    for item in (
        node("coordinator", deps=["draft", "check", "adapt"]),
        node("draft", parent_id="coordinator", status="DEAD", artifacts=["article.md"]),
        node("check", parent_id="coordinator", deps=["draft"], status=checker_status),
        node("adapt", parent_id="coordinator", deps=["draft", "check"]),
    ):
        repo.add(item)


@pytest.mark.parametrize("other_status", ["PENDING", "READY", "RUNNING",
                                         "WAITING_HUMAN", "FAILED"])
def test_dead_sibling_does_not_release_consumer_with_unfinished_required_input(repo, other_status):
    recovery_graph(repo, checker_status=other_status)
    repo.promote()
    consumer = repo.get("adapt")
    assert consumer.status != "READY", (
        "a failed sibling is not this consumer's failed child; it cannot bypass "
        f"the other required input in {other_status} (actual={consumer.status})"
    )
    assert consumer.wakeups == 0, "sibling failure must not consume parent-recovery attempts"


def test_parent_recovery_waits_for_its_other_live_children(repo):
    recovery_graph(repo, checker_status="RUNNING")
    repo.promote()
    parent = repo.get("coordinator")
    assert parent.status == "PENDING", "do not re-plan over still-running child work"
    assert parent.wakeups == 0


def test_completed_own_child_failure_still_allows_bounded_parent_recovery(repo):
    repo.add(node("coordinator", deps=["draft", "check"]))
    repo.add(node("draft", parent_id="coordinator", status="DEAD"))
    repo.add(node("check", parent_id="coordinator", status="DONE"))
    repo.promote()
    parent = repo.get("coordinator")
    assert parent.status == "READY" and parent.wakeups == 1
    assert repo.event_log(parent.id)[-1]["reason"] == "upstream_failed_review"


def test_failed_foreign_dependency_does_not_pose_as_an_own_child(repo):
    repo.add(node("upstream", status="DEAD"))
    repo.add(node("consumer", deps=["upstream"]))
    repo.promote()
    consumer = repo.get("consumer")
    assert consumer.status == "DEAD"
    assert consumer.wakeups == 0
    assert repo.event_log(consumer.id)[-1]["reason"] == "upstream_failed"


def test_real_parent_recovery_budget_is_not_removed(repo):
    repo.add(node("coordinator", deps=["draft"], wakeups=MAX_WAKEUPS))
    repo.add(node("draft", parent_id="coordinator", status="DEAD"))
    repo.promote()
    assert repo.get("coordinator").status == "DEAD"
    assert repo.event_log("coordinator")[-1]["reason"] == "upstream_failed"


async def test_failed_chain_reaches_coordinator_without_dispatching_failed_consumers(repo):
    recovery_graph(repo)
    dispatched = []

    async def run(item):
        dispatched.append(item.id)
        repo.transition(item.id, "DONE", "synthetic_recovery_observed")

    scheduler = Scheduler(repo, SimpleNamespace(run=run), max_slots=4, max_rounds=10)
    await scheduler.run()
    assert repo.get("coordinator").status == "DONE", (
        "failure propagation must not leave a ready coordinator undispatched"
    )
    assert dispatched == ["coordinator"], (
        "only the coordinator owns recovery; failed-input consumers must not start: "
        f"{dispatched}"
    )
    assert repo.get("check").status == repo.get("adapt").status == "DEAD"


def test_opening_cannot_snapshot_a_running_producer_as_empty_success(tmp_path):
    with Engine(FakeLLM([]), settings=Settings(
        root=tmp_path / "engine", memory_enabled=False, role_review_enabled=False,
    )) as engine:
        recovery_graph(engine.repo, checker_status="RUNNING")
        producer = engine.repo.get("draft")
        checker = engine.repo.get("check")
        consumer = engine.repo.get("adapt")
        source = engine.workspace.allocate(
            producer.project_id, producer.conversation_id, f"{producer.role}@{producer.id}",
        )
        target = engine.workspace.allocate(
            consumer.project_id, consumer.conversation_id, f"{consumer.role}@{consumer.id}",
        )
        (source / "article.md").write_text("Synthetic unaccepted candidate", encoding="utf-8")
        (source / "private.txt").write_text("Not a declared input", encoding="utf-8")
        engine.repo.promote()
        consumer = engine.repo.get(consumer.id)
        if consumer.status != "READY":
            assert consumer.status == "DEAD", "fail the unusable sibling dependency explicitly"
            assert not (target / "article.md").exists()
            return

        # This is the actual once-at-opening TaskRunner delivery method.
        opening = engine.runner._materialise_dependencies(consumer, target)
        assert (target / "article.md").exists()
        assert not (target / "private.txt").exists()
        assert opening[checker.id] == {"ok": True, "files": [], "conflicts": []}
        checker_source = engine.workspace.allocate(
            checker.project_id, checker.conversation_id, f"{checker.role}@{checker.id}",
        )
        (checker_source / "factcheck.md").write_text(
            "Required downstream verdict", encoding="utf-8",
        )
        engine.repo.add_artifacts(checker.id, ["factcheck.md"])
        engine.repo.set_result(checker.id, TaskResult(files=["factcheck.md"]))
        engine.repo.transition(checker.id, "DONE", "synthetic_producer_completed")
        engine.repo.promote()
        assert (target / "factcheck.md").exists(), (
            "consumer opened early: running producer was recorded as successful empty delivery; "
            "producer is now DONE but the opening handoff was not refreshed"
        )


def test_all_completed_inputs_are_delivered_before_healthy_consumer_opening(tmp_path):
    with Engine(FakeLLM([]), settings=Settings(
        root=tmp_path / "engine", memory_enabled=False, role_review_enabled=False,
    )) as engine:
        for item in (
            node("coordinator", status="DONE"),
            node("draft", parent_id="coordinator", status="DONE", artifacts=["article.md"],
                 result=TaskResult(files=["article.md"])),
            node("check", parent_id="coordinator", status="DONE", artifacts=["factcheck.md"],
                 result=TaskResult(files=["factcheck.md"])),
            node("adapt", parent_id="coordinator", deps=["draft", "check"]),
        ):
            engine.repo.add(item)
        for task_id, filename in (("draft", "article.md"), ("check", "factcheck.md")):
            producer = engine.repo.get(task_id)
            source = engine.workspace.allocate(
                producer.project_id, producer.conversation_id, f"{producer.role}@{producer.id}",
            )
            (source / filename).write_text("Synthetic completed input", encoding="utf-8")
        engine.repo.promote()
        consumer = engine.repo.get("adapt")
        assert consumer.status == "READY"
        target = engine.workspace.allocate(
            consumer.project_id, consumer.conversation_id, f"{consumer.role}@{consumer.id}",
        )
        handoff = engine.runner._materialise_dependencies(consumer, target)
        assert all(value["ok"] is True for value in handoff.values())
        assert (target / "article.md").exists() and (target / "factcheck.md").exists()
