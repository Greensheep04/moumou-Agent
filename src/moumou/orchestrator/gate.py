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
    HumanApprovalFeedback,
    HumanRequest,
    HumanResponse,
    TaskNode,
    ToolContext,
    ToolResult,
)
from moumou.sandbox.workspace import requests_package_install
from moumou.tools.execution import ShellTool
from moumou.tools.filesystem import DeleteFileTool


class ApprovalPolicy:
    """A table, not a pile of ``if`` statements. Overridable at construction."""

    def __init__(self, overrides: dict[str, bool] | None = None):
        self.table = dict(DEFAULT_APPROVAL)
        self.table.update(overrides or {})

    def needs_approval(self, action_class: str) -> bool:
        return self.table.get(action_class, True)     # unknown action => ask


class Gate:
    def __init__(self, repo, tools, human, trace, policy: ApprovalPolicy | None = None,
                 *, execution_root: Path | None = None):
        self.repo = repo
        self.tools = tools
        self.human = human
        self.trace = trace
        self.policy = policy or ApprovalPolicy()
        self.execution_root = execution_root

    async def execute(
        self, call, node: TaskNode, agent: AgentInstance, workspace: Path, *, handler=None
    ) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool: {call.name}")

        action_class = tool.action_class
        scoped_command = self.execution_root is not None and isinstance(tool, ShellTool)
        if isinstance(tool, ShellTool):
            scope = call.args.get("approval_scope")
            if scope is not None and scope not in ("install", "external_write"):
                return ToolResult(
                    ok=False, error="approval_scope must be install or external_write",
                )
            text = call.args.get("command") or call.args.get("code")
            if isinstance(text, str) and requests_package_install(text):
                scope = "install"
            if scope is not None:
                action_class = scope

        local_action = (scoped_command and action_class == "shell") or (
            self.execution_root is not None and isinstance(tool, DeleteFileTool)
        )
        needs_approval = not local_action and self.policy.needs_approval(action_class)
        feedback = None
        if needs_approval:
            try:
                answer = await self._ask_human(call, tool, node, agent, action_class)
            except (ValueError, TypeError) as exc:
                return ToolResult(ok=False, error="invalid_human_approval", stderr=str(exc))
            if answer.decision != "approve":
                return ToolResult(ok=False, error="rejected_by_human", stderr=answer.text or "")
            if answer.text and answer.text.strip():
                feedback = HumanApprovalFeedback(
                    request_id=answer.request_id, tool_call_id=call.id,
                    project_id=node.project_id, conversation_id=node.conversation_id,
                    task_id=node.id, agent_id=agent.id, role=node.role,
                    action=tool.name, action_class=action_class, note=answer.text,
                )

        ctx = ToolContext(
            agent_id=agent.id, task_id=node.id, workspace=workspace,
            execution_root=self.execution_root if scoped_command else None,
            external_access=needs_approval and action_class in {"install", "external_write"},
        )
        with self.trace.span(
            task_id=node.id, kind="tool", name=tool.name, agent_id=agent.id
        ) as span:
            try:
                result = await asyncio.wait_for(
                    (handler or tool.run)(call.args, ctx), timeout=tool.timeout_s
                )
            except TimeoutError:
                result = ToolResult(ok=False, error=f"tool timed out after {tool.timeout_s}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # a tool must never break the loop
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            span.payload["ok"] = result.ok
            span.payload["artifacts"] = result.artifacts
            if self.execution_root is not None:
                span.payload["approval"] = "human" if needs_approval else "automatic"
                span.payload["action_class"] = action_class

        # Only the gate may attest human provenance. Preserve the tool's content
        # fields, including JSON/pagination stdout and error diagnostics, verbatim.
        if feedback is not None or result.human_approval is not None:
            result = result.model_copy(update={"human_approval": feedback})
        # Tool-written paths are diagnostic evidence, not a publication claim.
        # They stay on the span above; the task's formal file list is written
        # only by a successful publish in Worker._finish().
        return result

    async def _ask_human(
        self, call, tool, node: TaskNode, agent: AgentInstance, action_class: str,
    ) -> HumanResponse:
        self.repo.transition(node.id, "WAITING_HUMAN", f"approve:{tool.name}")
        request = HumanRequest(
            task_id=node.id,
            agent_id=agent.id,
            kind="approval",
            summary=f"`{tool.name}` — {json.dumps(call.args, ensure_ascii=False)[:180]}",
            action=tool.name,
            action_class=action_class,
            payload=call.args,
            options=["approve", "reject"],
            project_id=node.project_id, conversation_id=node.conversation_id, role=node.role,
        )
        with self.trace.span(
            task_id=node.id, kind="approval", name=tool.name, agent_id=agent.id
        ) as span:
            try:
                answer = HumanResponse.model_validate(await self.human.ask(request))
                if answer.request_id != request.id:
                    raise ValueError("approval response request_id mismatch")
                if answer.decision not in {"approve", "reject"}:
                    raise ValueError("approval response requires an explicit decision")
            except (ValueError, TypeError):
                self.repo.transition(node.id, "RUNNING", "human:invalid")
                raise
            span.payload["decision"] = answer.decision
            span.payload["note"] = answer.text
            span.payload["request_id"] = request.id

        decision = answer.decision or "reject"
        self.repo.transition(node.id, "RUNNING", f"human:{decision}")
        return answer
