"""Reference tools.

These exist so the framework runs end to end before P1 lands. They implement
the same ``Tool`` contract P1 will, so swapping them out is a one-line change.

Two rules every tool here follows, because the contract tests enforce them:
  * never raise — turn failures into ``ToolResult(ok=False, error=...)``
  * never escape the workspace — everything goes through ``resolve_in``
"""

from __future__ import annotations

from pathlib import Path

from moumou.contracts import RoleRevisionDraft, TaskResult, ToolContext, ToolResult
from moumou.contracts.plan import Plan
from moumou.sandbox.local import LocalJail
from moumou.sandbox.process import run_process as run_process
from moumou.tools.execution import RunPythonTool, ShellTool
from moumou.tools.filesystem import (
    ApplyPatchTool,
    DeleteFileTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from moumou.tools.registry import flatten_schema

MAX_OUTPUT = 8 * 1024


def resolve_in(workspace: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``workspace`` or raise PermissionError."""
    return LocalJail(workspace).resolve(rel)


class EchoTool:
    name = "echo"
    description = "Echo text back. Useful for connectivity checks."
    action_class = "fs_read"
    timeout_s = 5.0
    params_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, stdout=str(args.get("text", "")))


class AskHumanTool:
    """Marker tool: intercepted by the runner and routed to ``HumanIO``.

    It exists so the agent can negotiate ambiguity (a course requirement) rather
    than guessing, and so the UI can render the question as a chat card.
    """

    name = "ask_human"
    description = (
        "Ask the human a clarifying question and wait for their answer. Use this "
        "when the goal is genuinely ambiguous and guessing would waste work."
    )
    action_class = "fs_read"
    timeout_s = 1.0
    params_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "one specific question"},
            "why": {"type": "string", "description": "why you cannot proceed without it"},
        },
        "required": ["question"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=False, error="ask_human is handled by the orchestrator")


class SubmitResultTool:
    """Marker tool: intercepted by the runner, which then runs acceptance.

    Its schema is the structured hand-off, so the parent gets fields instead of a
    paragraph it has to truncate.
    """

    name = "submit_result"
    description = (
        "Hand the finished work back. Include a summary, the files you touched, "
        "anything the next agent should know, and anything still blocking."
    )
    action_class = "fs_read"
    timeout_s = 1.0
    params_schema = flatten_schema(TaskResult.model_json_schema())

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=False, error="submit_result is handled by the orchestrator")


class SubmitRevisionTool:
    """Marker tool used by the role reviewer; the orchestrator consumes it."""

    name = "submit_revision"
    description = (
        "Propose one change to a role description, or submit an empty role if the "
        "evidence does not justify a change."
    )
    action_class = "fs_read"
    timeout_s = 1.0
    params_schema = flatten_schema(RoleRevisionDraft.model_json_schema())

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=False, error="submit_revision is handled by the orchestrator")


class SubmitPlanTool:
    """Marker tool: the runner intercepts it and never dispatches it.

    It exists so the planner's output is schema-constrained by the model itself
    rather than by prompt wording we would have to parse.
    """

    name = "submit_plan"
    description = (
        "Submit a decomposition plan. Call this exactly once when the task must be "
        "split across sub-agents. Do not call it for tasks you can finish yourself."
    )
    action_class = "fs_read"
    timeout_s = 1.0
    params_schema = flatten_schema(Plan.model_json_schema())

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=False, error="submit_plan is handled by the orchestrator")


def default_tools() -> list:
    return [
        EchoTool(),
        ReadFileTool(),
        WriteFileTool(),
        DeleteFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
        ApplyPatchTool(),
        ShellTool(),
        RunPythonTool(),
        AskHumanTool(),
        SubmitResultTool(),
        SubmitRevisionTool(),
        SubmitPlanTool(),
    ]
