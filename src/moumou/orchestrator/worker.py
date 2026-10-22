"""One agent working one task.

The loop itself is deliberately dumb: build context, ask the model, run the
tools it asked for, append results, repeat. All the interesting policy lives in
``Gate`` (approval, fault isolation, tracing) and ``ContextBuilder`` (what the
model gets to see).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from time import monotonic

from moumou.contracts import (
    AgentInstance,
    HumanRequest,
    Message,
    RoleSpec,
    TaskNode,
    ToolResult,
)


class BudgetExceeded(RuntimeError):
    """A task hit max_steps / max_tokens / max_wall_s."""


class TaskRunner:
    def __init__(
        self,
        *,
        repo,
        roles,
        llm,
        tools,
        gate,
        context,
        trace,
        bus,
        acceptance,
        delegation,
        workspace,
        human,
    ):
        self.repo = repo
        self.roles = roles
        self.llm = llm
        self.tools = tools
        self.gate = gate
        self.context = context
        self.trace = trace
        self.bus = bus
        self.acceptance = acceptance
        self.delegation = delegation
        self.workspace = workspace
        self.human = human

        self._steer: dict[str, list[Message]] = defaultdict(list)
        self._subscribed: set[str] = set()

    # -- entry point -----------------------------------------------------
    async def run(self, node: TaskNode) -> None:
        role = self.roles.get(node.project_id, node.role)
        if role is None:
            self._fail(node.id, f"role_not_found:{node.role}")
            return

        agent = self._instance(node, role)
        workspace = Path(agent.workspace)
        self._materialise_dependencies(node, workspace)

        try:
            await self._loop(node, role, agent, workspace)
        except asyncio.CancelledError:
            self._park(node.id, "DEAD", "cancelled")
            raise
        except BudgetExceeded as exc:
            self._fail(node.id, f"budget:{exc}")
        except Exception as exc:
            self._fail(node.id, f"crash:{type(exc).__name__}:{exc}")

    # -- the loop --------------------------------------------------------
    async def _loop(
        self, node: TaskNode, role: RoleSpec, agent: AgentInstance, workspace: Path
    ) -> None:
        ctx = self.context.build(node, role, self._dep_reports(node))
        self._subscribe(agent.id)
        tool_specs = self.tools.subset(role.tools)

        started = monotonic()
        tokens = 0

        for step in range(1, node.budget.max_steps + 1):
            if monotonic() - started > node.budget.max_wall_s:
                raise BudgetExceeded("wall_clock")
            if tokens > node.budget.max_tokens:
                raise BudgetExceeded("tokens")

            nudges = self._drain(agent.id)
            if nudges:
                ctx.inject_steering([m.content for m in nudges])
                self.trace.record(
                    task_id=node.id,
                    kind="steer",
                    name="received",
                    agent_id=agent.id,
                    count=len(nudges),
                )

            with self.trace.span(
                task_id=node.id, kind="llm", name=f"{role.name}:step{step}", agent_id=agent.id
            ) as span:
                response = await self.llm.chat(ctx.messages(), tool_specs)
                span.tokens_in = response.tokens_in
                span.tokens_out = response.tokens_out
            tokens += response.tokens

            ctx.add_assistant(response.content, response.tool_calls or None)

            if not response.tool_calls:
                await self._finish(node, workspace, response.content)
                return

            for call in response.tool_calls:
                if call.name == "submit_plan":
                    # Delegation transitions the parent to PENDING itself.
                    self.delegation.apply(node, call.args)
                    return
                if call.name == "ask_human":
                    result = await self._ask_human(call, node, agent)
                else:
                    result = await self.gate.execute(call, node, agent, workspace)
                ctx.add_tool_result(call.id, result)

        raise BudgetExceeded("max_steps")

    # -- completion ------------------------------------------------------
    async def _finish(self, node: TaskNode, workspace: Path, answer: str) -> None:
        # tools recorded artifacts against the database row, not this snapshot
        node = self.repo.get(node.id) or node
        ok, details = await self.acceptance.check(node, workspace)
        self.trace.record(
            task_id=node.id, kind="acceptance", name="verdict", ok=ok, checks=details
        )
        self.repo.set_result(node.id, answer or "")

        if ok:
            self.repo.transition(node.id, "DONE", "accepted")
            self._integrate_into_parent(node, workspace)
        else:
            failed = [d for d in details if not d.get("ok")]
            self.repo.set_error(node.id, json.dumps(failed, ensure_ascii=False)[:1000])
            self.repo.transition(node.id, "FAILED", "acceptance_failed")

    # -- helpers ---------------------------------------------------------
    def _instance(self, node: TaskNode, role: RoleSpec) -> AgentInstance:
        agent_id = f"{role.name}@{node.id}"
        workspace = self.workspace.allocate(
            node.project_id, node.conversation_id, agent_id
        )
        return AgentInstance(
            id=agent_id,
            role=role.name,
            project_id=node.project_id,
            conversation_id=node.conversation_id,
            workspace=str(workspace),
        )

    def _subscribe(self, agent_id: str) -> None:
        if agent_id in self._subscribed:
            return

        def callback(msg: Message) -> None:
            if msg.kind != "steer":
                return
            if msg.recipients and agent_id not in msg.recipients:
                return
            self._steer[agent_id].append(msg)

        self.bus.subscribe(agent_id, callback)
        self._subscribed.add(agent_id)

    def _drain(self, agent_id: str) -> list[Message]:
        return self._steer.pop(agent_id, [])

    def _dep_reports(self, node: TaskNode) -> list[dict]:
        reports = []
        for dep_id in node.deps:
            dep = self.repo.get(dep_id)
            if dep is None:
                continue
            reports.append(
                {
                    "id": dep.id,
                    "role": dep.role,
                    "status": dep.status,
                    "goal": dep.goal,
                    "artifacts": dep.artifacts,
                    "result": dep.result,
                    "error": dep.error,
                }
            )
        return reports

    async def _ask_human(self, call, node: TaskNode, agent: AgentInstance) -> ToolResult:
        question = str(call.args.get("question", "")).strip()
        if not question:
            return ToolResult(ok=False, error="ask_human requires a non-empty question")

        request = HumanRequest(
            task_id=node.id,
            agent_id=agent.id,
            kind="question",
            summary=question,
            context=str(call.args.get("why", "")),
        )
        # Park the task the same way an approval does, so a crash mid-question
        # is recoverable instead of leaving the task silently stuck.
        self.repo.transition(node.id, "WAITING_HUMAN", "question")
        with self.trace.span(
            task_id=node.id, kind="question", name="ask_human", agent_id=agent.id
        ) as span:
            answer = await self.human.ask(request)
            span.payload["answer"] = answer.text
        self.repo.transition(node.id, "RUNNING", "human:answered")
        return ToolResult(ok=True, stdout=answer.text or "(the human gave no answer)")

    def _materialise_dependencies(self, node: TaskNode, workspace: Path) -> None:
        """Pull each dependency's declared artifacts into our workspace.

        Artifacts are the only thing that crosses a workspace boundary, which is
        why "task B depends on task A" is both an ordering constraint and a data
        handoff. P1's git backend will do this with branches instead of copies.
        """
        for dep_id in node.deps:
            dep = self.repo.get(dep_id)
            if dep is None or not dep.artifacts:
                continue
            source = self.workspace.allocate(
                dep.project_id, dep.conversation_id, f"{dep.role}@{dep.id}"
            )
            result = self.workspace.integrate(source, workspace, dep.artifacts)
            if result.copied:
                self.trace.record(
                    task_id=node.id,
                    kind="task",
                    name="materialised_from_dep",
                    source=dep.id,
                    files=result.copied,
                )
            if result.conflicts or not result.ok:
                self.trace.record(
                    task_id=node.id,
                    kind="task",
                    name="materialise_failed",
                    source=dep.id,
                    conflicts=result.conflicts,
                    error=result.error,
                )

    def _integrate_into_parent(self, node: TaskNode, workspace: Path) -> None:
        """Push this task's deliverables up so the parent can verify them."""
        if node.parent_id is None or not node.artifacts:
            return
        parent = self.repo.get(node.parent_id)
        if parent is None:
            return
        target = self.workspace.allocate(
            parent.project_id, parent.conversation_id, f"{parent.role}@{parent.id}"
        )
        result = self.workspace.integrate(workspace, target, node.artifacts)

        # A conflict is not a failure of this task: the work was accepted, it is
        # the parent that has to reconcile it. Record it so the parent can see it.
        if result.conflicts:
            self.repo.set_error(node.id, "integration_conflict:" + ",".join(result.conflicts))
            self.trace.record(
                task_id=node.id,
                kind="task",
                name="integration_conflict",
                parent=parent.id,
                conflicts=result.conflicts,
            )
        elif not result.ok:
            self.trace.record(
                task_id=node.id, kind="task", name="integration_failed", error=result.error
            )
        if result.copied:
            self.trace.record(
                task_id=node.id,
                kind="task",
                name="integrated_into_parent",
                parent=parent.id,
                files=result.copied,
            )

    def _fail(self, task_id: str, error: str) -> None:
        self.repo.set_error(task_id, error)
        self._park(task_id, "FAILED", error)

    def _park(self, task_id: str, to: str, reason: str) -> None:
        node = self.repo.get(task_id)
        if node is None or node.is_terminal:
            return
        try:
            self.repo.transition(task_id, to, reason)
        except Exception:
            pass          # the node already moved; never mask the original error
