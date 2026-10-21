"""The approval gate and the tool wrapper.

Every tool call in the system passes through ``Gate.execute``. That single choke
point is where three framework guarantees live:

  1. approval policy  — risky actions park the task in WAITING_HUMAN
  2. tracing          — one span per call, success or failure
  3. fault isolation  — a tool may not raise into the agent loop
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from moumou.contracts import (
    DEFAULT_APPROVAL,
    AgentInstance,
    HumanRequest,
    TaskNode,
    ToolContext,
    ToolResult,
)


class ApprovalPolicy:
    """A table, not a pile of ``if`` statements. Overridable at construction."""

    def __init__(self, overrides: dict[str, bool] | None = None):
        self.table = dict(DEFAULT_APPROVAL)
        self.table.update(overrides or {})

    def needs_approval(self, action_class: str) -> bool:
        return self.table.get(action_class, True)     # unknown action => ask


class Gate:
    def __init__(self, repo, tools, human, trace, policy: ApprovalPolicy | None = None):
        self.repo = repo
        self.tools = tools
        self.human = human
        self.trace = trace
        self.policy = policy or ApprovalPolicy()

    async def execute(
        self, call, node: TaskNode, agent: AgentInstance, workspace: Path
    ) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool: {call.name}")

        if self.policy.needs_approval(tool.action_class):
            verdict = await self._ask_human(call, tool, node, agent)
            if verdict != "approve":
                return ToolResult(ok=False, error="rejected_by_human")

        ctx = ToolContext(agent_id=agent.id, task_id=node.id, workspace=workspace)
        with self.trace.span(
            task_id=node.id, kind="tool", name=tool.name, agent_id=agent.id
        ) as span:
            try:
                result = await asyncio.wait_for(
                    tool.run(call.args, ctx), timeout=tool.timeout_s
                )
            except TimeoutError:
                result = ToolResult(ok=False, error=f"tool timed out after {tool.timeout_s}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # a tool must never break the loop
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            span.payload["ok"] = result.ok
            span.payload["artifacts"] = result.artifacts

        self.repo.add_artifacts(node.id, result.artifacts)
        return result

    async def _ask_human(self, call, tool, node: TaskNode, agent: AgentInstance) -> str:
        self.repo.transition(node.id, "WAITING_HUMAN", f"approve:{tool.name}")
        request = HumanRequest(
            task_id=node.id,
            agent_id=agent.id,
            kind="approval",
            summary=f"`{tool.name}` — {json.dumps(call.args, ensure_ascii=False)[:180]}",
            action=tool.name,
            action_class=tool.action_class,
            payload=call.args,
            options=["approve", "reject"],
        )
        with self.trace.span(
            task_id=node.id, kind="approval", name=tool.name, agent_id=agent.id
        ) as span:
            answer = await self.human.ask(request)
            span.payload["decision"] = answer.decision
            span.payload["note"] = answer.text

        decision = answer.decision or "reject"
        self.repo.transition(node.id, "RUNNING", f"human:{decision}")
        return decision
