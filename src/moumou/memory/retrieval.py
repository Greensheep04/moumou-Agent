"""Turning a task into a memory query, and ranking what comes back.

The store decides *recall* (what is textually relevant); this module decides
*ranking* (what is worth spending prompt space on).
"""

from __future__ import annotations

from datetime import UTC, datetime

from moumou.contracts import ContextPolicy, MemoryRecord, RoleSpec, TaskNode

from .store import memory_scope

MAX_QUERY_CHARS = 200
#: Relevance falls off with rank; the store already sorted by match quality.
RANK_DECAY = 1.0
#: A lesson older than this contributes proportionally less.
RECENCY_HORIZON_DAYS = 30.0


class Retriever:
    def __init__(self, store, policy: ContextPolicy | None = None):
        self.store = store
        self.policy = policy or ContextPolicy()

    def query_for(self, node: TaskNode, role: RoleSpec) -> str:
        return f"{node.goal} {role.description}".strip()[:MAX_QUERY_CHARS]

    def fetch(self, node: TaskNode, role: RoleSpec) -> list[MemoryRecord]:
        if self.store is None:
            return []

        scope = memory_scope(node.project_id, role.name)
        candidates = self.store.search(
            scope, self.query_for(node, role), k=self.policy.memory_recall_k
        )
        return self.rank(candidates)[: self.policy.memory_top_k]

    def rank(self, candidates: list[MemoryRecord]) -> list[MemoryRecord]:
        now = datetime.now(UTC)
        scored: list[tuple[float, MemoryRecord]] = []

        for position, record in enumerate(candidates):
            if record.deleted_at is not None:
                continue
            if record.score < self.policy.memory_min_score:
                continue

            relevance = RANK_DECAY / (1 + position)
            age_days = max(0.0, (now - record.created_at).total_seconds() / 86_400)
            recency = 1.0 / (1.0 + age_days / RECENCY_HORIZON_DAYS)
            scored.append((relevance * record.score * recency, record))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _, record in scored]
