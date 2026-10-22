"""Memory storage and retrieval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from moumou.contracts import ContextPolicy, MemoryRecord, RoleSpec, TaskNode
from moumou.memory import Retriever, SqliteMemoryStore, memory_scope


@pytest.fixture
def store(tmp_path):
    s = SqliteMemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def rec(lesson: str, situation: str = "installing dependencies", **kw) -> MemoryRecord:
    base = dict(
        project_id="p",
        role="coder",
        task_id="t0",
        situation=situation,
        action="tried the mirror",
        outcome="success",
        lesson=lesson,
        score=1.0,
    )
    return MemoryRecord(**{**base, **kw})


# -- matching ---------------------------------------------------------------


def test_search_handles_reordering_and_short_queries(store):
    """The two cases FTS5 trigram could not do: reordered words, and two characters."""
    store.write(rec("修复依赖安装失败的经验"))
    for query in ("安装依赖", "依赖安装", "依赖"):
        assert store.search(memory_scope("p", "coder"), query), query


def test_unrelated_query_returns_nothing(store):
    store.write(rec("修复依赖安装失败的经验"))
    assert store.search(memory_scope("p", "coder"), "前端样式") == []


def test_search_is_scoped_by_role(store):
    store.write(rec("coder lesson", role="coder"))
    store.write(rec("writer lesson", role="tech_writer"))

    assert len(store.search(memory_scope("p", "coder"), "lesson")) == 1
    assert store.search(memory_scope("p", "reviewer"), "lesson") == []


# -- lifecycle --------------------------------------------------------------


def test_write_then_read_back(store):
    item = rec("retry with a mirror")
    store.write(item)

    hits = store.all(memory_scope("p", "coder"))
    assert len(hits) == 1
    assert hits[0].lesson == "retry with a mirror"
    assert hits[0].uses == 0


def test_scoring_is_clamped_and_counts_uses(store):
    item = rec("x")
    store.write(item)

    for _ in range(20):
        store.update_score(item.id, 0.2)
    assert store.all(memory_scope("p", "coder"))[0].score == 3.0

    for _ in range(40):
        store.update_score(item.id, -0.3)
    assert store.all(memory_scope("p", "coder"))[0].score == 0.0


def test_using_a_memory_stamps_last_used_at(store):
    item = rec("x")
    store.write(item)
    store.update_score(item.id, 0.2)

    fresh = store.all(memory_scope("p", "coder"))[0]
    assert fresh.uses == 1
    assert fresh.last_used_at is not None


def test_soft_deleted_memories_disappear_from_reads(store):
    item = rec("x")
    store.write(item)
    store.mark_deleted([item.id])

    assert store.all(memory_scope("p", "coder")) == []
    assert store.search(memory_scope("p", "coder"), "installing") == []


def test_eviction_candidates_respect_the_threshold(store):
    low = rec("low", score=0.2)
    high = rec("high", score=1.0)
    store.write(low)
    store.write(high)

    assert store.evict_candidates(memory_scope("p", "coder")) == [low.id]


def test_expired_soft_deletes_are_removed_permanently(store):
    item = rec("x")
    store.write(item)
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    store.conn.execute("UPDATE memories SET deleted_at = ? WHERE id = ?", (old, item.id))

    assert store.hard_delete_expired(days=30) == 1
    assert store.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0


# -- retrieval --------------------------------------------------------------


def node(goal="安装依赖并跑通测试") -> TaskNode:
    return TaskNode(
        id="t1", project_id="p", conversation_id="c", goal=goal, role="coder"
    )


def role() -> RoleSpec:
    return RoleSpec(
        name="coder", title="Coder", description="writes code", project_id="p",
        created_by="human",
    )


def test_retriever_builds_the_query_from_goal_and_role(store):
    store.write(rec("依赖装不上时先换镜像源"))
    hits = Retriever(store, ContextPolicy()).fetch(node(), role())
    assert hits, "a relevant lesson must be recalled for this goal"


def test_retriever_drops_memories_below_the_score_floor(store):
    store.write(rec("依赖安装的旧经验", score=0.1))
    hits = Retriever(store, ContextPolicy()).fetch(node(), role())
    assert hits == [], "a low-scoring memory is not worth prompt space"


def test_retriever_prefers_the_higher_scoring_memory(store):
    store.write(rec("依赖安装失败时换镜像", score=0.5))
    store.write(rec("依赖安装失败时换镜像", score=2.0))

    hits = Retriever(store, ContextPolicy()).fetch(node(), role())
    assert len(hits) == 2
    assert hits[0].score > hits[1].score


def test_retriever_caps_the_number_of_lessons(store):
    for index in range(10):
        store.write(rec(f"依赖安装经验 {index}"))

    policy = ContextPolicy(memory_top_k=2, memory_recall_k=8)
    hits = Retriever(store, policy).fetch(node(), role())
    assert len(hits) == 2
