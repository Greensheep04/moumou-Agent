"""Scoring, decay and eviction — the part that makes "evolution" more than a word.

A lesson that keeps being recalled before successful tasks gets stronger. One that
keeps being recalled before failures gets weaker, and is eventually retired to a
soft-deleted state so the decision stays auditable.
"""

from __future__ import annotations


def _trigrams(text: str) -> set[str]:
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if len(cleaned) < 3:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 3] for i in range(len(cleaned) - 2)}


def near_duplicate(a: str, b: str, threshold: float = 0.85) -> bool:
    """Jaccard over character trigrams; deterministic and dependency free."""
    left, right = _trigrams(a), _trigrams(b)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= threshold


class Evolution:
    def __init__(self, store, policy, trace=None):
        self.store = store
        self.policy = policy
        self.trace = trace

    def record_use(self, memory_ids: list[str], success: bool) -> None:
        if self.store is None:
            return
        delta = 0.2 if success else -0.3
        for rec_id in memory_ids:
            self.store.update_score(rec_id, delta)
        if self.trace is not None and memory_ids:
            self.trace.record(
                task_id="-",
                kind="memory",
                name="scored",
                count=len(memory_ids),
                success=success,
                delta=delta,
            )

    def decay_scope(self, scope: str, used_ids: set[str], skip_created_after=None) -> int:
        """Everything recalled this run keeps its score; the rest fades.

        Memories written during this very run are exempt -- a lesson should not be
        penalised for not being used before it existed.
        """
        if self.store is None:
            return 0
        decayed = 0
        for record in self.store.all(scope):
            if record.id in used_ids:
                continue
            if skip_created_after is not None and record.created_at >= skip_created_after:
                continue
            penalty = -(record.score * (1 - self.policy.decay_factor))
            self.store.update_score(record.id, penalty, used=False)
            decayed += 1
        return decayed

    def evict(self, scope: str) -> list[str]:
        if self.store is None:
            return []
        doomed = self.store.evict_candidates(scope, self.policy.evict_threshold)
        if doomed:
            self.store.mark_deleted(doomed)
            if self.trace is not None:
                self.trace.record(
                    task_id="-", kind="memory", name="evicted", count=len(doomed), scope=scope
                )
        return doomed
