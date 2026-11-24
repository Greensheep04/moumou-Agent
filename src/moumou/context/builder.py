"""Context assembly, four layers deep.

    role        who I am and how I must behave          (never trimmed)
    task        full goal and acceptance               (never trimmed)
    questions   recorded same-task clarification       (never trimmed)
    assignment  scoped ancestor goal references        (never trimmed)
    upstream    dependency reports and board notes     (trimmed under pressure)
    experience  lessons retrieved from long-term memory (trimmed next)
    dialogue    this task's tool history                (compacted at runtime)

Everything that decides *what is allowed in* lives here; everything that decides
*how much fits* lives in the compactor.
"""

from __future__ import annotations

import json

from moumou.contracts import (
    MAX_RESULT_CHARS,
    ContextPolicy,
    MemoryRecord,
    RoleSpec,
    TaskNode,
    TaskResult,
    ToolResult,
)
from moumou.human_history import render_human_exchanges
from moumou.prompts import FRAMEWORK_RULES, LEAD_ROLE, PLANNER_PROMPT, SPECIALIST_TEMPLATE

from .compactor import CompactionResult, Compactor
from .lineage import AssignmentReference, AssignmentReferenceError, render_assignment_references
from .tokens import TokenBudget

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
#: Upstream reports are structured hand-offs, rendered and capped here.
MAX_REPORT_CHARS = MAX_RESULT_CHARS
MAX_REASON_CHARS = 200

CLOSING_HANDOFF = """
## Framework closing handoff
All delegated children passed machine checks and their current artifact handoffs succeeded.
Reuse the framework acceptance evidence above. Only inspect or execute again for a missing
requirement, a changed artifact, conflicting evidence, or an unresolved issue. Group any
necessary reads into one round instead of repeating listing, reading and execution.
When the goal is covered, call submit_result with a concise summary and the delivered files.
The framework will run the parent acceptance criteria in this workspace before marking DONE;
submit_result requests that final validation and does not bypass it. Do not claim an unchecked
requirement passed. If there is a new issue, fix or report that specific issue first.
"""


class ContextAdmissionError(ValueError):
    """Mandatory instructions cannot fit without changing their meaning."""


def _machine_checked(evidence) -> bool:
    if not isinstance(evidence, dict) or evidence.get("ok") is not True:
        return False
    checks = evidence.get("checks") or []
    return bool(checks) and all(
        isinstance(check, dict) and check.get("ok") is True
        and check.get("kind") in {"command", "pytest", "file_exists", "file_not_exists",
                                  "file_contains", "json_key"}
        for check in checks
    )


def _check_summary(evidence) -> str:
    if not isinstance(evidence, dict):
        return "framework acceptance: unavailable"
    if not _machine_checked(evidence):
        return "framework acceptance: unverified, self-report only, or failed"
    lines = ["framework acceptance: passed"]
    for check in evidence["checks"][:6]:
        line = f"- {check['kind']} {str(check.get('target', ''))[:120]}: passed"
        if check.get("exit_code") is not None:
            line += f"; exit_code={check['exit_code']}"
        if "stdout" in check:
            line += "; stdout=" + json.dumps(str(check["stdout"])[:160], ensure_ascii=False)
        if "value" in check:
            line += "; value=" + str(check["value"])[:160]
        lines.append(line)
    return "\n".join(lines)[:1200]


def _delivery_summary(delivery) -> str:
    if not isinstance(delivery, dict):
        return "artifact delivery: unverified"
    if delivery.get("ok") is True and not delivery.get("conflicts") and not delivery.get("error"):
        return "artifact delivery: succeeded"
    details = str(delivery.get("error") or delivery.get("conflicts") or "unknown issue")
    return "artifact delivery: failed; " + details[:MAX_REASON_CHARS]


def _can_finish_from_handoff(node: TaskNode, role: RoleSpec, deps: list[dict]) -> bool:
    if (role.name != LEAD_ROLE or not node.acceptance or not deps
            or len(deps) != len(node.deps) or {d.get("id") for d in deps} != set(node.deps)):
        return False
    for dep in deps:
        result = dep.get("result")
        blockers = result.get("blockers", []) if isinstance(result, dict) else getattr(
            result, "blockers", [])
        delivery = dep.get("delivery") or {}
        if (dep.get("parent_id") != node.id or dep.get("status") != "DONE"
                or dep.get("error") or blockers or not dep.get("artifacts")
                or not _machine_checked(dep.get("acceptance"))
                or delivery.get("ok") is not True or delivery.get("conflicts")
                or delivery.get("error")):
            return False
    return True


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
    if result.human_approval is not None:
        parts.append(
            "human_approval_feedback (scoped to this task, role and tool call only; "
            "not standing permission, a new search allowance, clarification history "
            "or quality acceptance):\n"
            + json.dumps(result.human_approval.model_dump(mode="json"), ensure_ascii=False)
        )
    return "\n".join(parts)


#: How much of a long stdout/stderr survives the first hop into the messages.
DEFAULT_TOOL_RESULT_CHARS = 4_000


def _persist_tool_output(workspace, call_id: str, stream: str, text: str) -> dict | None:
    """Keep the captured body so the same role can re-read it later.

    Returns provenance (relative path, bytes, sha256) or ``None`` when the
    workspace is unavailable or not writable.
    """
    if workspace is None:
        return None
    try:
        import hashlib
        from pathlib import Path

        folder = Path(workspace) / ".evaluation" / "tool_outputs"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{call_id or 'call'}-{stream}.txt".replace("/", "_")
        target = folder / name
        data = text.encode("utf-8", "replace")
        target.write_bytes(data)
        relative = target.relative_to(Path(workspace)).as_posix()
        return {
            "path": relative,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
        }
    except Exception:
        return None


def _bounded_stream(
    label: str, text: str, *, call_id: str, workspace, limit: int
) -> list[str]:
    """One stream (stdout/stderr), shortened with a reference when it is long."""
    if len(text) <= limit:
        return [f"{label}:\n{text}"]

    head_chars = limit * 2 // 3
    tail_chars = limit - head_chars
    head = text[:head_chars]
    tail = text[-tail_chars:]
    omitted = len(text) - len(head) - len(tail)
    saved = _persist_tool_output(workspace, call_id, label, text)
    lines = [
        f"{label}:\n{head}",
        f"...[{omitted} of {len(text)} chars omitted]...",
    ]
    lines.append(tail)
    if saved:
        lines.append(
            f"{label}_full: {saved['path']} ({saved['bytes']} bytes, "
            f"sha256:{saved['sha256']}); re-read it with read_file path={saved['path']} "
            "offset=0 if you need the whole body"
        )
    else:
        lines.append(
            f"[{label} shortened; the full body was not captured here - re-run the tool "
            "if you need it in full]"
        )
    return lines


def _render_result_bounded(
    result: ToolResult,
    *,
    call_id: str = "",
    tool_name: str | None = None,
    workspace=None,
    limit: int = DEFAULT_TOOL_RESULT_CHARS,
) -> str:
    """Render a tool result for the messages, bounded but re-readable.

    Status is never dropped: ok/exit_code/error/artifacts/human feedback always
    survive, because the caller needs them to decide the next step.
    """
    parts = [f"ok={str(result.ok).lower()}"]
    if tool_name:
        parts[0] += f" tool={tool_name}"
    if result.exit_code is not None:
        parts.append(f"exit_code={result.exit_code}")
    if result.stdout:
        parts.extend(
            _bounded_stream("stdout", result.stdout, call_id=call_id,
                            workspace=workspace, limit=limit)
        )
    if result.stderr:
        parts.extend(
            _bounded_stream("stderr", result.stderr, call_id=call_id,
                            workspace=workspace, limit=limit)
        )
    if result.error:
        parts.append("error: " + result.error)
    if result.artifacts:
        parts.append("artifacts: " + ", ".join(result.artifacts))
    if result.truncated:
        parts.append(
            "truncated_by_executor: true - part of this stream was dropped before "
            "capture, so the full body is NOT available for re-read"
        )
    if result.human_approval is not None:
        parts.append(
            "human_approval_feedback (scoped to this task, role and tool call only; "
            "not standing permission, a new search allowance, clarification history "
            "or quality acceptance):\n"
            + json.dumps(result.human_approval.model_dump(mode="json"), ensure_ascii=False)
        )
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

    def add_tool_result(
        self,
        call_id: str,
        result: ToolResult,
        *,
        tool_name: str | None = None,
        workspace=None,
        limit: int = DEFAULT_TOOL_RESULT_CHARS,
    ) -> None:
        """Append one tool result, bounded on first entry.

        The body that first enters the conversation is capped; the captured text
        is kept under the role's own ``.evaluation/tool_outputs`` so the same
        agent can re-read it with ``read_file`` instead of re-running the tool or
        re-reading a whole file.
        """
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _render_result_bounded(
                    result,
                    call_id=call_id,
                    tool_name=tool_name,
                    workspace=workspace,
                    limit=limit,
                ),
            }
        )

    def inject_steering(self, texts: list[str]) -> None:
        for text in texts:
            self._messages.append({"role": "user", "content": f"[Human steering] {text}"})

    def replace_opening(self, opening: AgentContext) -> None:
        """Pin newly recorded clarification facts while retaining valid dialogue turns."""
        initial = opening.messages()
        if len(initial) != 2 or [item.get("role") for item in initial] != ["system", "user"]:
            raise ContextAdmissionError(
                "replacement opening must contain system and user messages"
            )
        self._messages = [*initial, *self._messages[self.pinned:]]
        self.pinned = 2
        self.memory_ids = list(opening.memory_ids)

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
        human_exchanges: list[dict] | None = None,
        assignment_references: tuple[AssignmentReference, ...] | None = None,
    ) -> AgentContext:
        deps = dep_reports or []
        lessons = [record.lesson for record in (memory_hits or [])]
        if board_notes is None and self.board is not None:
            scope = self.board.scope(node.project_id, node.conversation_id, role.name)
            board_notes = self.board.recent(scope, self.policy.board_entries)
        notes = list(board_notes or [])
        recorded_answers = render_human_exchanges(node, role, human_exchanges or [])
        try:
            assignment_text = render_assignment_references(node, assignment_references)
        except AssignmentReferenceError as exc:
            raise ContextAdmissionError(str(exc)) from exc

        mandatory = self._render(node, role, deps, lessons, [], 0, 0, None)
        if not self._fits(mandatory[0], mandatory[1] + assignment_text + recorded_answers):
            raise ContextAdmissionError(
                "mandatory original role, full current goal, acceptance, assignment references "
                "and recorded human answers exceed the opening budget; "
                "no truncated instructions were admitted"
            )

        dep_limit = len(deps)
        lesson_limit = len(lessons)
        goal_limit: int | None = None
        system, brief = self._render(
            node, role, deps, lessons, notes, dep_limit, lesson_limit, goal_limit
        )

        for kind, value in self._ladder():
            if self._fits(system, brief + assignment_text + recorded_answers):
                break
            if kind == "deps":
                dep_limit = min(dep_limit, value)
            elif kind == "lessons":
                lesson_limit = min(lesson_limit, value)
            elif kind == "notes":
                notes = notes[:value]
            system, brief = self._render(
                node, role, deps, lessons, notes, dep_limit, lesson_limit, goal_limit
            )

        return AgentContext(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": brief + assignment_text + recorded_answers},
            ],
            pinned=2,
            memory_ids=[record.id for record in (memory_hits or [])[:lesson_limit]],
        )

    # -- internals -------------------------------------------------------
    @staticmethod
    def _ladder():
        """Reduce optional upstream reports, lessons and notes; never the current goal."""
        return [
            ("deps", 3),
            ("lessons", 3),
            ("deps", 1),
            ("lessons", 1),
            ("deps", 0),
            ("lessons", 0),
            ("notes", 0),
        ]

    def _fits(self, system: str, brief: str) -> bool:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
        return self.budget.estimate(messages) <= min(
            self.policy.opening_budget_tokens, self.policy.prompt_ceiling,
        )

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
        if role.name == LEAD_ROLE:
            system = PLANNER_PROMPT
            if role.description and role.description not in system:
                system += "\n## Assigned role instructions\n" + role.description
        else:
            system = SPECIALIST_TEMPLATE.format(
                title=role.title, name=role.name, description=role.description
            )
        system += FRAMEWORK_RULES
        if lesson_limit and lessons:
            system += LESSONS_HEADER + "\n".join(f"- {s}" for s in lessons[:lesson_limit])
        if notes:
            system += BOARD_HEADER + "\n".join(f"- {s}" for s in notes)

        acceptance = (
            "\n".join(
                "- " + json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
                for item in node.acceptance
            )
            if node.acceptance
            else "- (none declared — your own summary is the only check)"
        )
        goal = node.goal if goal_limit is None else node.goal[:goal_limit]

        brief = BRIEF_TEMPLATE.format(
            goal=goal,
            acceptance=acceptance,
            deps=self._render_deps(deps[:dep_limit]),
        )
        if _can_finish_from_handoff(node, role, deps[:dep_limit]):
            brief += CLOSING_HANDOFF
        return system, brief

    @staticmethod
    def _render_deps(deps: list[dict]) -> str:
        if not deps:
            return NO_DEPS

        blocks = []
        for dep in deps:
            status = dep.get("status")
            files = ", ".join(dep.get("artifacts") or []) or "(none)"

            if status == "BLOCKED":
                blocker = dep.get("blocker") or {}
                blocks.append(
                    f"### {dep['id']} — {dep['role']} — BLOCKED\n"
                    f"reason: {blocker.get('reason', '')}\n"
                    f"required input: {blocker.get('required_input', '')}\n"
                    f"saved progress: {blocker.get('progress', '')}\n"
                    f"evidence: {blocker.get('evidence_refs', [])}\n"
                    f"input versions: {blocker.get('versions', {})}"
                )
                continue
            if status == "DEAD":
                reason = (dep.get("error") or "unknown")[:MAX_REASON_CHARS]
                blocks.append(f"### {dep['id']} — {dep['role']} — FAILED\nreason: {reason}")
                continue

            report = _render_report(dep.get("result"))
            blocks.append(
                f"### {dep['id']} — {dep['role']} — {status}\n"
                f"goal: {dep['goal']}\n"
                f"files: {files}\n"
                f"{_check_summary(dep.get('acceptance'))}\n"
                f"{_delivery_summary(dep.get('delivery'))}\n"
                f"publication: {(dep.get('delivery') or {}).get('version')}\n"
                f"report: {report}"
            )
        return "\n\n".join(blocks)


def _render_report(result) -> str:
    if isinstance(result, TaskResult):
        text = result.as_text(MAX_REPORT_CHARS)
        return text or "(no report)"
    if result:
        return str(result)[:MAX_REPORT_CHARS]
    return "(no report)"
