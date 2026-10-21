"""Context assembly — the cheapest version that can still run a task.

Token budgeting and summarisation are deliberately deferred to v0.2. What is
already correct here: every agent gets its own message list built from the role,
the task, upstream results, and retrieved memories — never the whole chat log.
"""

from __future__ import annotations

import json

from moumou.contracts import RoleSpec, TaskNode, ToolResult

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

BRIEF_TEMPLATE = """## Task
{goal}

## Acceptance criteria (you will be judged on these)
{acceptance}

## Upstream work you depend on
{deps}
"""


def _render_result(r: ToolResult) -> str:
    parts = [f"ok={str(r.ok).lower()}"]
    if r.exit_code is not None:
        parts.append(f"exit_code={r.exit_code}")
    if r.stdout:
        parts.append("stdout:\n" + r.stdout)
    if r.stderr:
        parts.append("stderr:\n" + r.stderr)
    if r.error:
        parts.append("error: " + r.error)
    if r.artifacts:
        parts.append("artifacts: " + ", ".join(r.artifacts))
    return "\n".join(parts)


class AgentContext:
    """One agent's message list. Mutable, but only through the methods below."""

    def __init__(self, messages: list[dict]):
        self._messages = messages

    def messages(self) -> list[dict]:
        return self._messages

    def add_assistant(self, content: str, tool_calls: list[dict] | None = None) -> None:
        msg: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.args, ensure_ascii=False),
                    },
                }
                for c in tool_calls
            ]
        self._messages.append(msg)

    def add_tool_result(self, call_id: str, result: ToolResult) -> None:
        self._messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": _render_result(result)}
        )

    def inject_steering(self, texts: list[str]) -> None:
        for text in texts:
            self._messages.append({"role": "user", "content": f"[Human steering] {text}"})

    def __len__(self) -> int:
        return len(self._messages)


class ContextBuilder:
    """Builds the opening messages for one (task, role) pair."""

    def __init__(self, memory=None):
        self.memory = memory       # optional MemoryStore; wired in v0.4

    def build(
        self,
        node: TaskNode,
        role: RoleSpec,
        dep_reports: list[dict] | None = None,
        memory_snippets: list[str] | None = None,
    ) -> AgentContext:
        system = SYSTEM_TEMPLATE.format(
            title=role.title, name=role.name, description=role.description
        )
        if memory_snippets:
            system += "\n## Lessons from previous runs\n" + "\n".join(
                f"- {s}" for s in memory_snippets
            )

        acceptance = (
            "\n".join(f"- [{a.kind}] {a.target}" for a in node.acceptance)
            if node.acceptance
            else "- (none declared — your own summary is the only check)"
        )

        deps = self._render_deps(dep_reports or [])
        brief = BRIEF_TEMPLATE.format(goal=node.goal, acceptance=acceptance, deps=deps)

        return AgentContext(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": brief},
            ]
        )

    @staticmethod
    def _render_deps(dep_reports: list[dict]) -> str:
        if not dep_reports:
            return "(this task has no upstream dependencies)"
        blocks = []
        for r in dep_reports:
            files = ", ".join(r.get("artifacts") or []) or "(none)"
            blocks.append(
                f"### {r['id']} — {r['role']} — {r['status']}\n"
                f"goal: {r['goal']}\n"
                f"files: {files}\n"
                f"report: {r.get('result') or '(no report)'}"
            )
        return "\n\n".join(blocks)
