"""Context assembly, four layers deep.

    role        who I am and how I must behave          (never trimmed)
    task        goal, acceptance, upstream reports      (trimmed under pressure)
    experience  lessons retrieved from long-term memory (trimmed next)
    dialogue    this task's tool history                (compacted at runtime)

Everything that decides *what is allowed in* lives here; everything that decides
*how much fits* lives in the compactor.
"""

from __future__ import annotations

import json

from moumou.contracts import ContextPolicy, MemoryRecord, RoleSpec, TaskNode, ToolResult

from .compactor import CompactionResult, Compactor
from .tokens import TokenBudget

SYSTEM_TEMPLATE = """You are {title} (role: {name}) on a multi-agent software team.

Your responsibility: {description}

You work in your own private workspace. All file paths are relative to it.

Operating rules:
- Inspect before you edit. Never assume a file's contents.
- One tool call at a time, then read the result before deciding what is next.
- When the job is done, stop calling tools and reply with a short summary:
  what you built, which files you touched, and how you verified it.
- If the work genuinely needs several specialists, call submit_plan instead of
  doing everything yourself. Do not call it for work you can finish alone.
- Your output will be checked against the acceptance criteria listed below.
"""

LESSONS_HEADER = "\n## Lessons from previous runs\n"
BOARD_HEADER = "\n## Notes from earlier work by this role\n"

BRIEF_TEMPLATE = """## Task
{goal}

## Acceptance criteria (you will be judged on these)
{acceptance}

## Upstream work you depend on
{deps}
"""

NO_DEPS = "(this task has no upstream dependencies)"
#: Upstream reports are summaries; the files themselves live in the workspace.
MAX_REPORT_CHARS = 800
MAX_REASON_CHARS = 200


def _render_result(result: ToolResult) -> str:
    parts = [f"ok={str(result.ok).lower()}"]
    if result.exit_code is not None:
        parts.append(f"exit_code={result.exit_code}")
    if result.stdout:
        parts.append("stdout:\n" + result.stdout)
    if result.stderr:
        parts.append("stderr:\n" + result.stderr)
    if result.error:
        parts.append("error: " + result.error)
    if result.artifacts:
        parts.append("artifacts: " + ", ".join(result.artifacts))
    return "\n".join(parts)


class AgentContext:
    """One agent's message list. Mutable, but only through the methods below."""

    def __init__(
        self,
        messages: list[dict],
        pinned: int = 2,
        memory_ids: list[str] | None = None,
    ):
        self._messages = messages
        self.pinned = pinned                  # role + brief: never compacted
        self.memory_ids = list(memory_ids or [])
        self.compactions = 0

    def messages(self) -> list[dict]:
        return self._messages

    def add_assistant(self, content: str, tool_calls: list[dict] | None = None) -> None:
        msg: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.args, ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ]
        self._messages.append(msg)

    def add_tool_result(self, call_id: str, result: ToolResult) -> None:
        self._messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": _render_result(result)}
        )

    def inject_steering(self, texts: list[str]) -> None:
        for text in texts:
            self._messages.append({"role": "user", "content": f"[Human steering] {text}"})

    def maybe_compact(self, compactor: Compactor) -> CompactionResult:
        """One big cut when over the trigger, never a per-step nibble."""
        result = compactor.compact(self._messages, self.pinned)
        if result.applied:
            self.compactions += 1
        return result

    def __len__(self) -> int:
        return len(self._messages)


class ContextBuilder:
    """Builds the opening messages for one (task, role) pair."""

    def __init__(
        self,
        policy: ContextPolicy | None = None,
        budget: TokenBudget | None = None,
        board=None,
    ):
        self.policy = policy or ContextPolicy()
        self.budget = budget or TokenBudget()
        # Memory retrieval lives in the runner (it needs the role spec); the board
        # is read here because it is keyed purely by scope.
        self.board = board

    def build(
        self,
        node: TaskNode,
        role: RoleSpec,
        dep_reports: list[dict] | None = None,
        memory_hits: list[MemoryRecord] | None = None,
        board_notes: list[str] | None = None,
    ) -> AgentContext:
        deps = dep_reports or []
        lessons = [record.lesson for record in (memory_hits or [])]
        if board_notes is None and self.board is not None:
            scope = self.board.scope(node.project_id, node.conversation_id, role.name)
            board_notes = self.board.recent(scope, self.policy.board_entries)
        notes = list(board_notes or [])

        dep_limit = len(deps)
        lesson_limit = len(lessons)
        goal_limit: int | None = None
        system, brief = self._render(
            node, role, deps, lessons, notes, dep_limit, lesson_limit, goal_limit
        )

        for kind, value in self._ladder():
            if self._fits(system, brief):
                break
            if kind == "deps":
                dep_limit = min(dep_limit, value)
            elif kind == "lessons":
                lesson_limit = min(lesson_limit, value)
            else:
                goal_limit = value
            system, brief = self._render(
                node, role, deps, lessons, notes, dep_limit, lesson_limit, goal_limit
            )

        return AgentContext(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": brief},
            ],
            pinned=2,
            memory_ids=[record.id for record in (memory_hits or [])],
        )

    # -- internals -------------------------------------------------------
    @staticmethod
    def _ladder():
        """Reduction order: upstream reports first, then lessons, then the goal."""
        return [
            ("deps", 3),
            ("lessons", 3),
            ("deps", 1),
            ("lessons", 1),
            ("deps", 0),
            ("lessons", 0),
            ("goal", 200),
        ]

    def _fits(self, system: str, brief: str) -> bool:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
        return self.budget.estimate(messages) <= self.policy.opening_budget_tokens

    def _render(
        self,
        node: TaskNode,
        role: RoleSpec,
        deps: list[dict],
        lessons: list[str],
        notes: list[str],
        dep_limit: int,
        lesson_limit: int,
        goal_limit: int | None,
    ) -> tuple[str, str]:
        system = SYSTEM_TEMPLATE.format(
            title=role.title, name=role.name, description=role.description
        )
        if lesson_limit and lessons:
            system += LESSONS_HEADER + "\n".join(f"- {s}" for s in lessons[:lesson_limit])
        if notes:
            system += BOARD_HEADER + "\n".join(f"- {s}" for s in notes)

        acceptance = (
            "\n".join(f"- [{item.kind}] {item.target}" for item in node.acceptance)
            if node.acceptance
            else "- (none declared — your own summary is the only check)"
        )
        goal = node.goal if goal_limit is None else node.goal[:goal_limit]

        brief = BRIEF_TEMPLATE.format(
            goal=goal,
            acceptance=acceptance,
            deps=self._render_deps(deps[:dep_limit]),
        )
        return system, brief

    @staticmethod
    def _render_deps(deps: list[dict]) -> str:
        if not deps:
            return NO_DEPS

        blocks = []
        for dep in deps:
            status = dep.get("status")
            files = ", ".join(dep.get("artifacts") or []) or "(none)"

            if status == "DEAD":
                reason = (dep.get("error") or "unknown")[:MAX_REASON_CHARS]
                blocks.append(f"### {dep['id']} — {dep['role']} — FAILED\nreason: {reason}")
                continue

            report = (dep.get("result") or "(no report)")[:MAX_REPORT_CHARS]
            blocks.append(
                f"### {dep['id']} — {dep['role']} — {status}\n"
                f"goal: {dep['goal']}\n"
                f"files: {files}\n"
                f"report: {report}"
            )
        return "\n\n".join(blocks)
