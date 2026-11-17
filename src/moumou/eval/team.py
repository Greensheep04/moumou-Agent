"""Opt-in instructor teams using the existing Engine without changing its defaults."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from moumou.context import ContextBuilder
from moumou.context.builder import (
    BOARD_HEADER,
    BRIEF_TEMPLATE,
    LESSONS_HEADER,
    AgentContext,
    ContextAdmissionError,
)
from moumou.contracts import AgentInstance, Budget, IntegrateResult, TaskNode
from moumou.eval.cases import Scenario
from moumou.human_history import render_human_exchanges
from moumou.orchestrator.delegation import Delegation, DelegationError
from moumou.prompts import FRAMEWORK_RULES
from moumou.sandbox.local import LocalJail

_TEAM_ATTR = "_evaluation_team"
_HISTORY_FILE = ".evaluation/prior-dialogue.txt"


@dataclass
class TeamState:
    scenario_id: str
    leader: str
    role_ids: tuple[str, ...]
    project_id: str
    conversation_id: str
    workspaces: dict[str, Path]
    context: TeamContextBuilder
    previous_dialogue_path: Path | None = None
    previous_root_id: str | None = None
    prior_dialogue_sha256: str | None = None
    prior_context: dict = field(default_factory=dict)


class StableRoleWorkspace:
    """Keep four private role directories across both teacher tasks."""

    def __init__(self, provider, project_id, conversation_id, roles):
        self.provider = provider
        self.project_id, self.conversation_id = project_id, conversation_id
        self.roles = frozenset(roles)
        self.paths = {
            role: Path(provider.allocate(project_id, conversation_id, role)).resolve()
            for role in roles
        }
        if len(set(self.paths.values())) != len(self.paths):
            raise ValueError("instructor team members must have distinct private workspaces")
        if any(left != right and left.is_relative_to(right)
               for left in self.paths.values() for right in self.paths.values()):
            raise ValueError("instructor role workspaces may not be nested inside each other")

    def allocate(self, project_id, conversation_id, agent_id):
        role = agent_id.split("@", 1)[0]
        if (project_id != self.project_id or conversation_id != self.conversation_id
                or role not in self.roles):
            raise PermissionError("workspace is outside this instructor team")
        return self.paths[role]

    def release(self, path):
        return self.provider.release(path)

    def integrate(self, src, dst, artifacts):
        source, target = Path(src).resolve(), Path(dst).resolve()
        if source not in self.paths.values() or target not in self.paths.values():
            return IntegrateResult(ok=False, error="integration is outside this instructor team")
        try:
            for relative in artifacts:
                LocalJail(source).resolve_artifact(relative)
                LocalJail(target).resolve_artifact(relative)
        except (OSError, ValueError, RuntimeError) as exc:
            return IntegrateResult(ok=False, error=str(exc))
        if source == target:
            # The same agent already owns these bytes; do not copy a file onto itself.
            return IntegrateResult(ok=True)
        return self.provider.integrate(source, target, artifacts)

    def register_private_evidence(self, workspace, relative):
        path = Path(workspace).resolve()
        if path not in self.paths.values():
            raise PermissionError("private evidence is outside this instructor team")
        register = getattr(self.provider, "register_private_evidence", None)
        if callable(register):
            register(path, relative)


class FixedTeamDelegation(Delegation):
    def __init__(self, *args, allowed_roles, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_roles = frozenset(allowed_roles)

    def validate(self, parent, plan):
        unknown = {draft.name for draft in plan.roles} | {spec.role for spec in plan.subtasks}
        unknown -= self.allowed_roles
        if unknown:
            raise DelegationError(
                f"instructor team has exactly {sorted(self.allowed_roles)}; "
                f"reuse those identities instead of adding {sorted(unknown)}"
            )
        super().validate(parent, plan)


class TeamContextBuilder(ContextBuilder):
    """Pin the original instructor role; use factual prior handoffs, not invented summaries."""

    def __init__(self, *args, scenario, workspaces=None, register_private_evidence=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario
        self.prior_context = {}
        self.workspaces = dict(workspaces or {})
        self.register_private_evidence = register_private_evidence

    @staticmethod
    def _json(value):
        def encode(item):
            if hasattr(item, "model_dump"):
                return item.model_dump(mode="json")
            if isinstance(item, Path):
                return str(item)
            raise TypeError(f"unsupported context evidence value: {type(item).__name__}")

        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=encode,
        )

    @staticmethod
    def _preview(text, limit):
        if not limit:
            return ""
        if len(text) <= limit:
            return text
        return text[:limit] + "\n[truncated auxiliary preview; inspect the full context archive]"

    def _history_summary(self):
        prior = self.prior_context
        if not prior:
            return ""
        result = prior.get("previous_result")
        if isinstance(result, dict):
            result = {
                "summary": self._preview(str(result.get("summary", "")), 1000),
                "files": [self._preview(str(item), 160) for item in result.get("files", [])[:24]],
                "blockers": [self._preview(str(item), 300)
                             for item in result.get("blockers", [])[:4]],
            }
        exchanges = prior.get("actual_human_exchanges", [])
        business = [item for item in exchanges
                    if item.get("request", {}).get("kind") in {"question", "review"}]
        summary = {
            "truncated": True,
            "summary_policy": "bounded index; full original evidence remains in the cited archives",
            "previous_root_id": prior.get("previous_root_id"),
            "previous_status": prior.get("previous_status"),
            "previous_goal": self._preview(str(prior.get("previous_goal", "")), 800),
            "previous_result": result,
            "declared_delivered_files": [
                self._preview(str(item), 160)
                for item in prior.get("declared_delivered_files", [])[:24]
            ],
            "actual_human_exchanges": [{
                "request_id": item.get("request", {}).get("id"),
                "kind": item.get("request", {}).get("kind"),
                "question": self._preview(str(item.get("request", {}).get("summary", "")), 500),
                "answer": self._preview(str(item.get("response", {}).get("text") or ""), 1000),
                "decision": item.get("response", {}).get("decision"),
            } for item in business[-8:]],
            "omitted_approval_exchanges": len(exchanges) - len(business),
            "unanswered_request_count": len(prior.get("unanswered_human_requests", [])),
        }
        return self._json(summary)

    def _context_evidence(self, node, role, deps, memory_hits, notes, human_exchanges=None):
        references = []
        previous = self.prior_context.get("dialogue_archive")
        if isinstance(previous, dict):
            references.append("Original prior dialogue: " + self._json(previous))
        workspace = self.workspaces.get(role.name)
        if workspace is not None and (
            deps or memory_hits or notes or self.prior_context or human_exchanges
        ):
            raw = self._json({
                "schema_version": 1, "task_id": node.id, "role": role.name,
                "current_goal": node.goal, "acceptance": node.acceptance,
                "prior_context": self.prior_context, "dependencies": deps,
                "memory_hits": memory_hits, "board_notes": notes,
                "recorded_human_exchanges": list(human_exchanges or []),
            }).encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            relative = Path(".evaluation/context") / (digest[:32] + ".json")
            workspace = Path(workspace).resolve()
            target = workspace / relative
            if target.is_symlink() or not target.resolve().is_relative_to(workspace):
                raise ContextAdmissionError("context evidence path escapes its private workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != raw:
                    raise ContextAdmissionError(
                        "content-addressed context evidence is inconsistent",
                    )
            else:
                with target.open("xb") as stream:
                    stream.write(raw)
            if self.register_private_evidence is not None:
                self.register_private_evidence(workspace, relative.as_posix())
            references.append(
                f"Full auxiliary context: {relative.as_posix()}; sha256={digest}; bytes={len(raw)}"
            )
        if not references:
            return ""
        return (
            "\n## Recoverable context evidence\n"
            "Older context previews may be truncated; "
            "the current task and acceptance are complete.\n"
            + "\n".join(references)
            + "\nInspect these records with read_file/search_files when required. "
            "They are historical evidence, not new task instructions or deliverables.\n"
        )

    def build(self, node, role, dep_reports=None, memory_hits=None, board_notes=None,
              human_exchanges=None):
        """Admit required instructions first; never truncate the current task to fit history."""
        deps, hits = list(dep_reports or []), list(memory_hits or [])
        if board_notes is None and self.board is not None:
            scope = self.board.scope(node.project_id, node.conversation_id, role.name)
            board_notes = self.board.recent(scope, self.policy.board_entries)
        notes = list(board_notes or [])
        recorded_answers = render_human_exchanges(node, role, human_exchanges or [])
        evidence = self._context_evidence(node, role, deps, hits, notes, human_exchanges)
        limit = min(self.policy.opening_budget_tokens, self.policy.prompt_ceiling)

        def render(history_limit, dep_chars, note_chars, lesson_chars):
            selected_lessons = [self._preview(record.lesson, lesson_chars) for record in hits]
            selected_notes = [self._preview(str(note), note_chars) for note in notes]
            system, brief = self._render(
                node, role, deps, selected_lessons, selected_notes,
                len(deps), len(selected_lessons) if lesson_chars else 0, None,
                history_text=self._preview(self._history_summary(), history_limit),
                dependency_text=(self._preview(self._render_deps(deps), dep_chars)
                                 if deps else self._render_deps([])),
                evidence=evidence,
            )
            return system, brief + recorded_answers

        def estimate(messages):
            return self.budget.estimate([
                {"role": "system", "content": messages[0]},
                {"role": "user", "content": messages[1]},
            ])

        mandatory = render(0, 0, 0, 0)
        if estimate(mandatory) > limit:
            raise ContextAdmissionError(
                "mandatory original role, full current goal, acceptance and recorded human answers "
                "exceed the opening budget "
                f"({estimate(mandatory)} estimated tokens > {limit}); "
                "no truncated task was admitted"
            )
        sizes = [6000, 6000, 800, 1000]
        candidates = [tuple(sizes)]
        for index in range(len(sizes)):
            for value in ([3000, 1200, 0] if index < 2 else [300, 0]):
                sizes[index] = value
                candidates.append(tuple(sizes))
        for sizes in candidates:
            system, brief = render(*sizes)
            if estimate((system, brief)) <= limit:
                return AgentContext([
                    {"role": "system", "content": system},
                    {"role": "user", "content": brief},
                ], pinned=2, memory_ids=[record.id for record in hits] if sizes[3] else [])
        # The final candidate equals the already-checked mandatory admission.
        raise ContextAdmissionError("mandatory context no longer fits its opening budget")

    def _render(self, node, role, deps, lessons, notes, dep_limit, lesson_limit, goal_limit,
                *, history_text=None, dependency_text=None, evidence=""):
        members = "\n".join(f"- {item.name}: {item.title}" for item in self.scenario.roles)
        system = role.description + "\n\n## Fixed instructor team\n" + members
        system += (
            "\nReuse exactly these four identities and their original responsibilities. "
            "Do not invent additional members or rewrite their role prompts. "
            "Coordinate the workflow and completion criteria with the actual human requester. "
            "Ask genuine questions through ask_human; do not invent a human answer.\n"
        )
        command_shell = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
        system += (
            "\n## Execution environment (runtime facts)\n"
            f"Platform: {platform.system()} ({sys.platform}, os.name={os.name}).\n"
            f"Active Python executable: {sys.executable}\n"
            f"The shell tool uses this command interpreter: {command_shell}.\n"
            "The shell tool accepts a command string. Prefer run_python for Python code "
            "or files; inside Python use subprocess argument lists and sys.executable. "
            "Keep paths relative to your own private role workspace and use pathlib "
            "for portable file operations.\n"
        )
        if os.name == "nt":
            system += (
                "This is native Windows. Shell command strings use cmd.exe syntax, "
                "not Bash or PowerShell syntax; Bash heredocs, export, rm and ls are "
                "not available as native cmd commands. Use run_python instead of "
                "Unix shell fragments when generating or verifying files.\n"
            )
        system += (
            f"Current date: {datetime.now(UTC).date().isoformat()}. "
            "Distinguish observed facts, source dates and future assumptions. "
            "Use only the metered web_search tool for search; never obtain credentials "
            "or bypass its quota with shell/Python requests. Reuse the team's actual "
            "sources before repeating a search. Cache results retain their original "
            "retrieval time. If the tool reports its quota exhausted, ask the real "
            "human for an explicit grant rather than inventing evidence.\n"
        )
        system += getattr(self, "search_budget_notice", "")
        if role.name == self.scenario.leader:
            system += (
                "You receive the group's human tasks, coordinate the other fixed members, "
                "integrate their declared work and explain evidence and remaining uncertainty. "
                "Delegate with submit_plan when useful, referencing the existing role IDs.\n"
            )
        system += FRAMEWORK_RULES
        system += (
            "\n## File handoff and delegation contract\n"
            "Every role has its own persistent private workspace. Files are delivered "
            "only from direct task dependencies and only when those dependencies declare "
            "them in their result artifacts. Dependency files are not inherited "
            "transitively; parent files and sibling role directories are not shared. "
            "For every consumer, include direct dependencies on all producers whose "
            "files it needs, or have an intermediary explicitly return those files. "
            "Delegated goals must include the requirements and inputs the child needs; "
            "do not rely on a parent-only specification or conversation. "
            "If required files are missing, use submit_plan, when depth permits, to "
            "request a concrete artifact handoff from an existing role that owns them, "
            "or report the missing input through ask_human. Do not search parent, "
            "sibling or other run directories to bypass isolation. A rejected approval "
            "does not grant access; repeating an equivalent request will not deliver "
            "missing artifacts.\n"
        )
        if lesson_limit and lessons:
            system += LESSONS_HEADER + "\n".join(f"- {item}" for item in lessons[:lesson_limit])
        if any(notes):
            system += BOARD_HEADER + "\n".join(f"- {item}" for item in notes if item)
        acceptance = "\n".join(
            "- " + self._json(item) for item in node.acceptance
        ) if node.acceptance else (
            "- No machine criteria are fixed yet. Agree on suitable checks with the human; "
            "an empty framework check records self-report, not independent quality approval."
        )
        brief = BRIEF_TEMPLATE.format(
            goal=node.goal,
            acceptance=acceptance,
            deps=(self._render_deps(deps[:dep_limit]) if dependency_text is None
                  else dependency_text or "(dependency preview omitted; see context evidence)"),
        )
        if history_text is None:
            history_text = self._preview(self._history_summary(), 6000)
        if history_text:
            brief += (
                "\n## Existing collaboration context (saved evidence)\n"
                "The following is a derived index of the previous actual task, not a new "
                "human instruction or an independent quality certification. The same four "
                "private role directories, memory and group remain in use.\n"
                + history_text
                + "\nThe full original dialogue is archived at " + _HISTORY_FILE
                + "; inspect it with read_file/search_files when more context is needed. "
                "Do not include framework history copies as delivered project artifacts.\n"
            )
        return system, brief + evidence


def configure_team(engine, scenario: Scenario, previous_dialogue_path=None) -> TeamState:
    """Install a fixed-team adapter once, before submitting any scenario task."""
    if getattr(engine, _TEAM_ATTR, None) is not None:
        raise ValueError("this Engine already has an instructor team")
    if engine.repo.all_tasks() or engine.roles.list(engine.settings.project_id):
        raise ValueError("configure an instructor team before creating any roles or tasks")
    if len(scenario.roles) != 4 or len({role.name for role in scenario.roles}) != 4:
        raise ValueError("an instructor scenario must define exactly four identities")
    project, conversation = engine.settings.project_id, engine.settings.conversation_id
    identities = tuple(role.name for role in scenario.roles)
    workspace = StableRoleWorkspace(engine.workspace, project, conversation, identities)
    tools = sorted(engine.tools.names())
    for role in scenario.roles:
        engine.roles.create(role.model_copy(update={"project_id": project, "tools": tools}))
    engine.roles.max_roles = 4
    engine.settings.role_review_enabled = False
    context = TeamContextBuilder(
        engine.policy, engine.budget, board=engine.board, scenario=scenario,
        workspaces=workspace.paths,
        register_private_evidence=workspace.register_private_evidence,
    )
    delegation = FixedTeamDelegation(
        engine.repo, engine.roles, engine.trace, engine.tools.names(), allowed_roles=identities,
        require_subtask_acceptance=engine.settings.require_subtask_acceptance,
    )
    engine.workspace = workspace
    engine.context, engine.runner.context = context, context
    engine.delegation, engine.runner.delegation = delegation, delegation
    engine.runner.workspace = workspace
    locks = {identity: asyncio.Lock() for identity in identities}
    original_run = engine.runner.run

    def instance(node, role):
        if role.name not in locks:
            raise PermissionError("agent is outside the instructor team")
        return AgentInstance(
            id=role.name, role=role.name, project_id=node.project_id,
            conversation_id=node.conversation_id,
            workspace=str(workspace.allocate(node.project_id, node.conversation_id, role.name)),
        )

    async def run(node):
        if node.role not in locks:
            raise PermissionError("task is outside the instructor team")
        async with locks[node.role]:
            return await original_run(node)

    engine.runner._instance = instance
    engine.runner.run = run
    state = TeamState(
        scenario_id=scenario.id, leader=scenario.leader, role_ids=identities,
        project_id=project, conversation_id=conversation, workspaces=workspace.paths,
        context=context,
        previous_dialogue_path=(Path(previous_dialogue_path).resolve()
                                if previous_dialogue_path is not None else None),
    )
    setattr(engine, _TEAM_ATTR, state)
    return state


def _previous_context(engine, state, previous):
    result = previous.result.model_dump(mode="json") if previous.result else None
    context = {
        "previous_root_id": previous.id, "previous_goal": previous.goal,
        "previous_status": previous.status, "previous_error": previous.error,
        "previous_result": result, "declared_delivered_files": list(previous.artifacts),
        "quality_note": "saved task outcome; independent human/model quality review is separate",
    }
    if state.previous_dialogue_path is not None:
        archive = state.previous_dialogue_path
        if not archive.is_file():
            raise ValueError("previous dialogue archive is missing")
        raw = archive.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        questions, exchanges = {}, []
        for line in raw.decode("utf-8-sig").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("previous dialogue must contain JSONL event objects")
            if record.get("event") == "human_request":
                request = record.get("request")
                if isinstance(request, dict) and isinstance(request.get("id"), str):
                    questions[request["id"]] = request
            elif record.get("event") == "human_response":
                response = record.get("response")
                if not isinstance(response, dict):
                    raise ValueError("previous human response must be an object")
                request_id = response.get("request_id") or record.get("request_id")
                if request_id in questions:
                    exchanges.append({"request": questions.pop(request_id), "response": response})
                else:
                    raise ValueError("previous human response has no matching actual request")
        for workspace in state.workspaces.values():
            target = workspace / _HISTORY_FILE
            if target.is_symlink() or not target.resolve().is_relative_to(workspace):
                raise PermissionError("prior dialogue copy escapes the role workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and state.context.register_private_evidence is not None:
                # Reject tracked/aliased evidence before replacing any old bytes.
                state.context.register_private_evidence(workspace, _HISTORY_FILE)
            target.write_bytes(raw)
            if state.context.register_private_evidence is not None:
                state.context.register_private_evidence(workspace, _HISTORY_FILE)
        state.prior_dialogue_sha256 = digest
        context["dialogue_archive"] = {
            "role_private_path": _HISTORY_FILE, "sha256": digest, "bytes": len(raw),
            "kind": "complete original transcript; not replayed as API tool-call history",
        }
        context["actual_human_exchanges"] = exchanges
        context["unanswered_human_requests"] = list(questions.values())
    return context


def submit_teacher_task(
    engine, scenario: Scenario, text: str, budget: Budget,
    previous_root: TaskNode | str | None = None, *, acceptance=None,
) -> TaskNode:
    """Submit an original human task to the configured leader, retaining real prior state."""
    state = getattr(engine, _TEAM_ATTR, None)
    if state is None or state.scenario_id != scenario.id:
        raise ValueError("configure this instructor scenario before submitting its task")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("teacher task text must be nonempty")
    previous = None
    if previous_root is not None:
        previous_id = previous_root if isinstance(previous_root, str) else previous_root.id
        previous = engine.repo.get(previous_id)
        if (previous is None or not previous.is_terminal or previous.parent_id is not None
                or previous.project_id != state.project_id
                or previous.conversation_id != state.conversation_id):
            raise ValueError("previous root must be this same team's actual terminal task")
        state.prior_context = _previous_context(engine, state, previous)
        state.context.prior_context = state.prior_context
        state.previous_root_id = previous.id
    node = TaskNode(
        id=f"t{engine.repo.count_root_tasks() + 1}", project_id=state.project_id,
        conversation_id=state.conversation_id, goal=text, role=state.leader,
        budget=budget.model_copy(deep=True), acceptance=acceptance or [],
        deps=[previous.id] if previous is not None and previous.status == "DONE" else [],
    )
    engine.repo.add(node)
    engine.repo.transition(node.id, "READY", "instructor_task_posted")
    engine.trace.record(
        task_id=node.id, kind="task", name="instructor_task_posted",
        scenario_id=scenario.id, leader=state.leader, team=list(state.role_ids),
        previous_root=state.previous_root_id,
    )
    return engine.repo.get(node.id)


__all__ = ["ContextAdmissionError", "TeamState", "configure_team", "submit_teacher_task"]
