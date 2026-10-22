"""Role revision: the agents propose, a human decides.

A role invented for the first task is still in use at the tenth, by which time its
description may be actively wrong. Every few finished tasks the framework asks the
model whether one description should change -- and refuses to apply anything without
a human saying yes.

A rejection that carries a note feeds the next proposal, so a difficult case can go
round a few times. A rejection with no note ends the cycle: there is nothing to
iterate on.
"""

from __future__ import annotations

from moumou.contracts import HumanRequest, RoleRevisionDraft, RoleSpec, TaskNode
from moumou.prompts import REVISION_GUIDANCE, REVISION_PROMPT
from moumou.tools.registry import flatten_schema

#: A role description becomes part of every prompt for that role, so it is capped.
MAX_DESCRIPTION_CHARS = 800

SUBMIT_REVISION = {
    "type": "function",
    "function": {
        "name": "submit_revision",
        "description": (
            "Propose one change to a role description, or submit an empty role if the "
            "evidence does not justify a change."
        ),
        "parameters": flatten_schema(RoleRevisionDraft.model_json_schema()),
    },
}


class RoleReviser:
    def __init__(self, llm, roles, human, trace=None, max_rounds: int = 3):
        self.llm = llm
        self.roles = roles
        self.human = human
        self.trace = trace
        self.max_rounds = max_rounds

    async def review(self, project_id: str, recent: list[TaskNode]) -> RoleSpec | None:
        guidance: str | None = None

        for round_no in range(1, self.max_rounds + 1):
            draft = await self._propose(project_id, recent, guidance)
            if draft is None or draft.is_noop:
                self._trace("no_change", project_id=project_id, round=round_no)
                return None

            role = self.roles.get(project_id, draft.role)
            if role is None:
                self._trace("unknown_role", project_id=project_id, role=draft.role)
                return None

            proposed = draft.proposed_description.strip()[:MAX_DESCRIPTION_CHARS]
            if proposed == role.description.strip():
                self._trace("no_change", project_id=project_id, role=role.name)
                return None

            answer = await self.human.ask(self._request(role, draft, round_no))
            approved = answer.decision == "approve"

            self.roles.log_revision(
                project_id=project_id,
                role=role.name,
                version=role.version + 1,
                previous=role.description,
                proposed=proposed,
                reason=draft.reason,
                decision="approve" if approved else "reject",
                note=answer.text,
            )

            if approved:
                updated = self.roles.revise(project_id, role.name, proposed)
                self._trace(
                    "approved",
                    project_id=project_id,
                    role=role.name,
                    version=updated.version if updated else role.version + 1,
                    round=round_no,
                )
                return updated

            guidance = (answer.text or "").strip() or None
            self._trace(
                "rejected", project_id=project_id, role=role.name,
                round=round_no, has_guidance=guidance is not None,
            )
            if guidance is None:
                return None      # nothing to iterate on

        self._trace("gave_up", project_id=project_id, rounds=self.max_rounds)
        return None

    # -- internals -------------------------------------------------------
    async def _propose(
        self, project_id: str, recent: list[TaskNode], guidance: str | None
    ) -> RoleRevisionDraft | None:
        prompt = REVISION_PROMPT.format(
            roles=self._render_roles(project_id),
            tasks=self._render_tasks(recent),
            guidance=REVISION_GUIDANCE.format(note=guidance) if guidance else "",
        )
        try:
            response = await self.llm.chat([{"role": "user", "content": prompt}], [SUBMIT_REVISION])
        except Exception:
            return None

        if not response.tool_calls:
            return None
        try:
            return RoleRevisionDraft.model_validate(response.tool_calls[0].args)
        except Exception:
            return None

    def _render_roles(self, project_id: str) -> str:
        roles = self.roles.list(project_id)
        if not roles:
            return "(no roles)"
        return "\n".join(
            f"- {role.name} (v{role.version}): {role.description}" for role in roles
        )

    @staticmethod
    def _render_tasks(recent: list[TaskNode]) -> str:
        if not recent:
            return "(no finished tasks)"
        lines = []
        for task in recent[-12:]:
            line = (
                f"- {task.id} [{task.role}] {task.status} "
                f"retries={task.retries} goal: {task.goal[:80]}"
            )
            if task.error:
                line += f" -- error: {task.error[:160]}"
            lines.append(line)
        return "\n".join(lines)

    def _request(self, role: RoleSpec, draft: RoleRevisionDraft, round_no: int) -> HumanRequest:
        return HumanRequest(
            task_id="-",
            agent_id="planner",
            kind="review",
            summary=f"Revise role `{role.name}` to v{role.version + 1}?",
            context=draft.reason,
            payload={
                "role": role.name,
                "current": role.description,
                "proposed": draft.proposed_description[:MAX_DESCRIPTION_CHARS],
                "evidence": draft.evidence,
                "round": round_no,
            },
            options=["approve", "reject"],
        )

    def _trace(self, name: str, **payload) -> None:
        if self.trace is None:
            return
        self.trace.record(task_id="-", kind="role", name=f"revision:{name}", **payload)
