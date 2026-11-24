"""Autonomous decomposition.

The planner proposes a plan as a function-call argument; this module decides
whether that plan is admissible. Everything a model can get wrong about a DAG is
checked here, in code, before a single child is created.
"""

from __future__ import annotations

from collections import deque
from graphlib import CycleError, TopologicalSorter

from moumou.contracts import (
    MAX_DEPTH,
    MAX_SUBTASKS,
    Plan,
    RoleSpec,
    TaskNode,
)
from moumou.contracts.plan import RoleDraft
from moumou.orchestrator.acceptance import validate_check
from moumou.prompts import LEAD_ROLE


class DelegationError(ValueError):
    """A proposed plan is not admissible. The message goes back to the planner."""


class Delegation:
    def __init__(
        self, repo, roles, trace, tool_names: set[str], *, require_subtask_acceptance: bool = False,
        handoff_preflight=None,
    ):
        self.repo = repo
        self.roles = roles
        self.trace = trace
        self.tool_names = tool_names
        self.require_subtask_acceptance = require_subtask_acceptance
        self.handoff_preflight = handoff_preflight

    def child_ids(self, parent: TaskNode, plan: Plan) -> dict[str, str]:
        round_no = parent.wakeups
        while True:
            prefix = parent.id if round_no == 0 else f"{parent.id}.r{round_no}"
            ids = {spec.key: f"{prefix}.{spec.key}" for spec in plan.subtasks}
            if not any(self.repo.get(task_id) for task_id in ids.values()):
                return ids
            round_no += 1

    def child_status(self, child: TaskNode) -> str:
        siblings = self.repo.children(child.parent_id) if child.parent_id else []
        dependents = [item.id for item in siblings
                      if not item.superseded_by and child.id in item.deps]
        waits = f"; downstream waiting: {', '.join(dependents)}" if dependents else ""
        return f"{child.id} [{child.status}]{waits}{self._repair_hint(child)}"

    @staticmethod
    def can_resume(node: TaskNode) -> bool:
        blocker = node.blocker or {}
        return (node.status == "BLOCKED" and not node.superseded_by
                and (not node.recovery_used or (blocker.get("handoff", {}).get("before_model")
                     and not blocker.get("admission_retry_used"))))

    @staticmethod
    def _repair_hint(node: TaskNode) -> str:
        if node.superseded_by:
            return ""
        replace = f'submit a repair with replaces: "{node.id}"'
        if Delegation.can_resume(node):
            resume = f'; use resume with task_id "{node.id}" after fixing the input'
            return resume if node.recovery_used else f"{resume}, or {replace}"
        if node.recovery_used and node.status in {"DEAD", "BLOCKED"}:
            return "; recovery exhausted for this assignment"
        if node.status == "DEAD":
            return f"; {replace}"
        return ""

    def _pending_status(self, node: TaskNode) -> str:
        waits = []
        pending = deque(node.deps)
        seen = {node.id}
        while pending:
            dep_id = pending.popleft()
            if dep_id in seen:
                continue
            seen.add(dep_id)
            dep = self.repo.get(dep_id)
            if dep is None:
                waits.append(f"{dep_id} [missing]")
            elif dep.superseded_by:
                waits.append(f"{dep.id} [{dep.status}], replaced by {dep.superseded_by}")
            elif dep.status != "DONE":
                hint = self._repair_hint(dep) if dep.parent_id == node.parent_id else ""
                if dep.status == "PENDING":
                    pending.extend(dep.deps)
                elif dep.status in {"BLOCKED", "DEAD"} and dep.parent_id != node.parent_id:
                    if dep.parent_id is not None:
                        hint = f"; coordinate repair through {dep.parent_id}"
                waits.append(f"{dep.id} [{dep.status}]{hint}")
        detail = "waiting for " + " | ".join(waits) if waits else "await scheduler dispatch"
        return f"{node.id} [PENDING]: {detail}"

    def retained_plan_nodes(self, parent: TaskNode, replacements: dict[str, str]) -> list[TaskNode]:
        return [node for child in self.repo.children(parent.id)
                if not child.superseded_by and child.id not in replacements
                for node in (child, *self.repo.active_descendants(child.id))]

    # -- validation ------------------------------------------------------
    def validate(self, parent: TaskNode, plan: Plan) -> None:
        if plan.resume is not None:
            child = self.repo.get(plan.resume.task_id)
            if (child is not None and child.parent_id == parent.id
                    and not child.superseded_by and child.status == "PENDING"):
                raise DelegationError(self._pending_status(child))
            if (plan.subtasks or plan.roles or not plan.resume.resolution.strip()
                    or child is None or child.parent_id != parent.id):
                raise DelegationError("resume needs one direct blocked child and a resolution")
            if not self.can_resume(child):
                raise DelegationError(f"cannot resume {self.child_status(child)}")
            recovery = (child.blocker or {}).get("base_recovery")
            allowed_inputs = set(parent.deps)
            if recovery is not None:
                root = parent
                while root.parent_id is not None:
                    root = self.repo.get(root.parent_id)
                if recovery["base_task"] in root.deps:
                    allowed_inputs.add(recovery["base_task"])
            for dep_id in plan.resume.deps:
                source = self.repo.get(dep_id)
                if (source is None or source.status != "DONE" or source.superseded_by
                        or source.project_id != parent.project_id
                        or source.conversation_id != parent.conversation_id
                        or dep_id not in allowed_inputs):
                    raise DelegationError(
                        f"{dep_id}: resume inputs must be current parent inputs "
                        "or the root's recorded recovery baseline"
                    )
            if recovery is not None:
                base, version = recovery["base_task"], recovery["base_version"]
                publication = self.repo.published_version(base)
                if (base not in plan.resume.deps or publication is None
                        or publication["version"] != version):
                    raise DelegationError(f"{child.id}: resume needs inherited {base}/{version}")
            return
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

        children = {child.id: child for child in self.repo.children(parent.id)
                    if child.superseded_by is None}
        known = set(keys) | set(children) | set(parent.deps)
        replacements = [spec.replaces for spec in plan.subtasks if spec.replaces]
        count = len(children) + len(plan.subtasks) - len(replacements)
        if self.repo.targeted_recovery and count > MAX_SUBTASKS:
            raise DelegationError(f"at most {MAX_SUBTASKS} active direct children are allowed")
        if len(replacements) != len(set(replacements)):
            raise DelegationError("each child can be replaced only once")
        for old_id in replacements:
            old = children.get(old_id)
            if old is not None and old.status == "PENDING":
                raise DelegationError(self._pending_status(old))
            if old is not None and old.recovery_used:
                raise DelegationError(f"cannot replace {self.child_status(old)}")
            if (old is None or not plan.reasoning.strip()
                    or old.status not in {"BLOCKED", "DEAD", "DONE"}
                    or any(not item.is_terminal for item in self.repo.active_descendants(old_id))):
                raise DelegationError(f"{old_id}: replacement requires a scoped child and a reason")
        if self.repo.targeted_recovery:
            for spec in plan.subtasks:
                unresolved = [child for child in children.values()
                              if child.role == spec.role and child.status in {"BLOCKED", "DEAD"}]
                if unresolved and spec.replaces not in {child.id for child in unresolved}:
                    details = " | ".join(self.child_status(child) for child in unresolved)
                    raise DelegationError(f"recover the existing {spec.role} assignment: {details}")
        for s in plan.subtasks:
            unknown = [d for d in s.deps if d not in known]
            if unknown:
                raise DelegationError(f"subtask {s.key!r} depends on unknown keys {unknown}")
            if s.key in s.deps:
                raise DelegationError(f"subtask {s.key!r} depends on itself")
            for index, check in enumerate(s.acceptance, 1):
                try:
                    validate_check(check)
                except ValueError as exc:
                    raise DelegationError(f"{s.key} acceptance check {index}: {exc}") from exc

        ids = self.child_ids(parent, plan)
        redirect = {spec.replaces: ids[spec.key] for spec in plan.subtasks if spec.replaces}
        graph = {ids[spec.key]: [ids.get(dep, redirect.get(dep, dep)) for dep in spec.deps]
                 for spec in plan.subtasks}
        retained = self.retained_plan_nodes(parent, redirect) if redirect else children.values()
        for child in retained:
            changed = set(child.deps) & redirect.keys()
            handoff = (child.blocker or {}).get("handoff", {})
            waiting_for_repair = (
                self.repo.targeted_recovery and child.parent_id == parent.id
                and self.can_resume(child) and handoff.get("before_model")
                and changed == {handoff.get("producer")}
            )
            if changed and child.status not in {"PENDING", "READY"} and not waiting_for_repair:
                if child.parent_id != parent.id:
                    raise DelegationError(
                        f"{child.id} [{child.status}]: coordinate revalidation through "
                        f"{child.parent_id} before replacing its inputs"
                    )
                raise DelegationError(
                    f"{child.id} [{child.status}]: replace the dependent task as well "
                    "to verify the new inputs"
                )
            graph[child.id] = [redirect.get(dep, dep) for dep in child.deps]
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise DelegationError(f"dependency cycle among {exc.args[1]}") from exc

        for s in plan.subtasks:
            if s.role == LEAD_ROLE:
                raise DelegationError(
                    f"subtask {s.key!r} may not be assigned the lead role; that role owns "
                    "the whole task and cannot be a delegate"
                )

        if self.require_subtask_acceptance:
            missing = [s.key for s in plan.subtasks if not s.acceptance]
            if missing:
                raise DelegationError(
                    f"subtasks {missing} require non-empty acceptance in strict mode; "
                    "provide executable machine checks for each subtask, with target and "
                    "expect where required. Empty acceptance would only record self_report."
                )

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
        if plan.resume is not None:
            task_id = plan.resume.task_id
            child = self.repo.get(task_id)
            deps = list(dict.fromkeys([*child.deps, *plan.resume.deps]))
            if self.handoff_preflight is not None:
                self.handoff_preflight(child, deps)
            blocker = {**(child.blocker or {}), "resolution": plan.resume.resolution.strip()}
            blocker.pop("escalated", None)
            if blocker.get("handoff", {}).get("before_model"):
                blocker["admission_retry_used"] = True
            with self.repo.db.transaction():
                self.repo.use_recovery(task_id)
                self.repo.set_blocker(task_id, blocker)
                self.repo.set_deps(task_id, deps)
                self.repo.set_error(task_id, None)
                self.repo.transition(task_id, "PENDING", "blocker_resolved")
                self.repo.transition(parent.id, "PENDING", "waiting_for_children")
            self.trace.record(task_id=parent.id, kind="delegate", name="child_resumed",
                              child=task_id, resolution=plan.resume.resolution,
                              new_inputs=plan.resume.deps)
            return []

        ids = self.child_ids(parent, plan)
        redirect = {spec.replaces: ids[spec.key] for spec in plan.subtasks if spec.replaces}
        retained_nodes = self.retained_plan_nodes(parent, redirect) if redirect else []
        children = [
            TaskNode(
                id=ids[spec.key],
                project_id=parent.project_id,
                conversation_id=parent.conversation_id,
                parent_id=parent.id,
                depth=parent.depth + 1,
                goal=spec.goal,
                role=spec.role,
                deps=[ids.get(dep, redirect.get(dep, dep)) for dep in spec.deps],
                acceptance=spec.acceptance,
                budget=parent.budget.model_copy(),
                recovery_used=spec.replaces is not None,
            )
            for spec in plan.subtasks
        ]
        if self.handoff_preflight is not None:
            for spec, child in zip(plan.subtasks, children, strict=True):
                if spec.replaces is not None:
                    old = self.repo.get(spec.replaces)
                    if old.role == child.role:
                        self.handoff_preflight(old, child.deps, replacement_id=child.id)
        with self.repo.db.transaction():
            self._materialise_roles(parent, plan.roles)
            for child in children:
                self.repo.add(child)
            for old_id, replacement_id in redirect.items():
                old = self.repo.get(old_id)
                self.repo.use_recovery(old_id, superseded_by=replacement_id)
                if old.status == "BLOCKED":
                    self.repo.transition(old_id, "DEAD", "superseded")
            for consumer in retained_nodes:
                if set(consumer.deps) & redirect.keys():
                    deps = [redirect.get(dep, dep) for dep in consumer.deps]
                    self.repo.set_deps(consumer.id, deps)
                    if consumer.status == "READY":
                        self.repo.transition(consumer.id, "PENDING", "dependency_replaced")

            retained = [dep for dep in parent.deps if dep not in redirect
                        and (self.repo.targeted_recovery or redirect)]
            self.repo.set_deps(parent.id, list(dict.fromkeys([*retained, *ids.values()])))
            self.repo.transition(parent.id, "PENDING", "waiting_for_children")

        if self.trace is not None:
            self.trace.record(
                task_id=parent.id,
                kind="delegate",
                name="plan_applied",
                reasoning=plan.reasoning,
                children=[c.id for c in children],
                replaced=redirect,
                roles=[r.name for r in plan.roles],
                require_subtask_acceptance=self.require_subtask_acceptance,
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
