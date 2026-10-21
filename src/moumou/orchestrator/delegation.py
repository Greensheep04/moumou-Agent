"""Autonomous decomposition.

The planner proposes a plan as a function-call argument; this module decides
whether that plan is admissible. Everything a model can get wrong about a DAG is
checked here, in code, before a single child is created.
"""

from __future__ import annotations

from moumou.contracts import (
    MAX_DEPTH,
    MAX_SUBTASKS,
    Plan,
    RoleSpec,
    SubtaskSpec,
    TaskNode,
)
from moumou.contracts.plan import RoleDraft


class DelegationError(ValueError):
    """A proposed plan is not admissible. The message goes back to the planner."""


def find_cycle(specs: list[SubtaskSpec]) -> list[str] | None:
    """Kahn's algorithm. Returns the keys left in a cycle, or None."""
    indeg = {s.key: 0 for s in specs}
    adj: dict[str, list[str]] = {s.key: [] for s in specs}
    for s in specs:
        for dep in s.deps:
            adj[dep].append(s.key)
            indeg[s.key] += 1

    queue = [k for k, v in indeg.items() if v == 0]
    seen = 0
    while queue:
        key = queue.pop()
        seen += 1
        for nxt in adj[key]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if seen == len(specs):
        return None
    return [k for k, v in indeg.items() if v > 0]


class Delegation:
    def __init__(self, repo, roles, trace, tool_names: set[str]):
        self.repo = repo
        self.roles = roles
        self.trace = trace
        self.tool_names = tool_names

    # -- validation ------------------------------------------------------
    def validate(self, parent: TaskNode, plan: Plan) -> None:
        if parent.depth + 1 > MAX_DEPTH:
            raise DelegationError(
                f"depth limit reached ({MAX_DEPTH}); this task must be executed, not split"
            )
        if not plan.subtasks:
            raise DelegationError("plan has no subtasks")
        if len(plan.subtasks) > MAX_SUBTASKS:
            raise DelegationError(f"too many subtasks ({len(plan.subtasks)} > {MAX_SUBTASKS})")

        keys = [s.key for s in plan.subtasks]
        if len(set(keys)) != len(keys):
            raise DelegationError(f"duplicate subtask keys: {keys}")

        known = set(keys)
        for s in plan.subtasks:
            unknown = [d for d in s.deps if d not in known]
            if unknown:
                raise DelegationError(f"subtask {s.key!r} depends on unknown keys {unknown}")
            if s.key in s.deps:
                raise DelegationError(f"subtask {s.key!r} depends on itself")

        cycle = find_cycle(plan.subtasks)
        if cycle:
            raise DelegationError(f"dependency cycle among {cycle}")

        existing = {r.name for r in self.roles.list(parent.project_id)}
        offered = {d.name for d in plan.roles}
        for s in plan.subtasks:
            if s.role not in existing and s.role not in offered:
                raise DelegationError(
                    f"subtask {s.key!r} wants role {s.role!r}, which is neither an existing "
                    f"role ({sorted(existing)}) nor declared in plan.roles"
                )

        for draft in plan.roles:
            bad = set(draft.tools) - self.tool_names
            if bad:
                raise DelegationError(
                    f"role {draft.name!r} requests unknown tools {sorted(bad)}; "
                    f"available: {sorted(self.tool_names)}"
                )

    # -- application -----------------------------------------------------
    def apply(self, parent: TaskNode, payload: dict) -> list[TaskNode]:
        try:
            plan = Plan.model_validate(payload)
        except Exception as exc:
            raise DelegationError(f"plan does not match the schema: {exc}") from exc

        self.validate(parent, plan)
        self._materialise_roles(parent, plan.roles)

        children: list[TaskNode] = []
        for spec in plan.subtasks:
            child = TaskNode(
                id=f"{parent.id}.{spec.key}",
                project_id=parent.project_id,
                conversation_id=parent.conversation_id,
                parent_id=parent.id,
                depth=parent.depth + 1,
                goal=spec.goal,
                role=spec.role,
                deps=[f"{parent.id}.{d}" for d in spec.deps],
                acceptance=spec.acceptance,
                budget=parent.budget.model_copy(),
            )
            self.repo.add(child)
            children.append(child)

        # The parent now depends on its children: it re-enters READY once they
        # are all DONE, and reviews their reports before finishing.
        self.repo.set_deps(parent.id, [c.id for c in children])
        self.repo.transition(parent.id, "PENDING", "waiting_for_children")

        if self.trace is not None:
            self.trace.record(
                task_id=parent.id,
                kind="delegate",
                name="plan_applied",
                reasoning=plan.reasoning,
                children=[c.id for c in children],
                roles=[r.name for r in plan.roles],
            )
        return children

    def _materialise_roles(self, parent: TaskNode, drafts: list[RoleDraft]) -> None:
        for draft in drafts:
            if self.roles.get(parent.project_id, draft.name) is not None:
                continue                      # query-before-create: reuse, never duplicate
            spec = RoleSpec(
                name=draft.name,
                title=draft.title,
                description=draft.description,
                tools=draft.tools,
                created_by="model",
                reason=draft.reason,
                project_id=parent.project_id,
            )
            self.roles.create(spec)
            if self.trace is not None:
                self.trace.record(
                    task_id=parent.id, kind="role", name=f"created:{spec.name}"
                )
