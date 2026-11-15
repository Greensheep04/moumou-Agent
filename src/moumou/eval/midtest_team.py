"""Opt-in instructor teams using the existing Engine without changing its defaults."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from moumou.context import ContextBuilder
from moumou.context.builder import BOARD_HEADER, BRIEF_TEMPLATE, LESSONS_HEADER
from moumou.contracts import AgentInstance, Budget, IntegrateResult, TaskNode
from moumou.eval.midtest_cases import Scenario
from moumou.orchestrator.delegation import Delegation, DelegationError
from moumou.prompts import FRAMEWORK_RULES

_TEAM_ATTR = "_midtest_team"
_HISTORY_FILE = ".midtest/prior-dialogue.txt"


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
        if source == target:
            for relative in artifacts:
                if not (source / relative).resolve().is_relative_to(source):
                    return IntegrateResult(ok=False, error="artifact escapes its role workspace")
            # The same agent already owns these bytes; do not copy a file onto itself.
            return IntegrateResult(ok=True)
        return self.provider.integrate(source, target, artifacts)


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

    def __init__(self, *args, scenario, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario
        self.prior_context = {}

    def _render(self, node, role, deps, lessons, notes, dep_limit, lesson_limit, goal_limit):
        members = "\n".join(f"- {item.name}: {item.title}" for item in self.scenario.roles)
        system = role.description + "\n\n## Fixed instructor team\n" + members
        system += (
            "\nReuse exactly these four identities and their original responsibilities. "
            "Do not invent additional members or rewrite their role prompts. "
            "Coordinate the workflow and completion criteria with the actual human requester. "
            "Ask genuine questions through ask_human; do not invent a human answer.\n"
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
        if lesson_limit and lessons:
            system += LESSONS_HEADER + "\n".join(f"- {item}" for item in lessons[:lesson_limit])
        if notes:
            system += BOARD_HEADER + "\n".join(f"- {item}" for item in notes)
        acceptance = "\n".join(
            f"- [{item.kind}] {item.target}"
            + (f" expect={json.dumps(item.expect, ensure_ascii=False)}"
               if item.expect is not None else "") for item in node.acceptance
        ) if node.acceptance else (
            "- No machine criteria are fixed yet. Agree on suitable checks with the human; "
            "an empty framework check records self-report, not independent quality approval."
        )
        brief = BRIEF_TEMPLATE.format(
            goal=node.goal if goal_limit is None else node.goal[:goal_limit],
            acceptance=acceptance, deps=self._render_deps(deps[:dep_limit]),
        )
        if self.prior_context:
            brief += (
                "\n## Existing collaboration context (saved evidence)\n"
                "The following is a derived index of the previous actual task, not a new "
                "human instruction or an independent quality certification. The same four "
                "private role directories, memory and group remain in use.\n"
                + json.dumps(self.prior_context, ensure_ascii=False, sort_keys=True)
                + "\nThe full original dialogue is archived at " + _HISTORY_FILE
                + "; inspect it with read_file/search_files when more context is needed. "
                "Do not include framework history copies as delivered project artifacts.\n"
            )
        return system, brief


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
            target.write_bytes(raw)
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


__all__ = ["TeamState", "configure_team", "submit_teacher_task"]
