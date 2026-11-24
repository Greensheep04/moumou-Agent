"""One agent working one task.

The loop itself is deliberately dumb: build context, ask the model, run the
tools it asked for, append results, repeat. All the interesting policy lives in
``Gate`` (approval, fault isolation, tracing) and ``ContextBuilder`` (what the
model gets to see).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import uuid
from collections import defaultdict, deque
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from inspect import signature
from pathlib import Path, PureWindowsPath
from time import monotonic

from pydantic import ValidationError

from moumou.context.builder import LESSONS_HEADER, ContextAdmissionError
from moumou.context.lineage import (
    AssignmentReferenceError,
    collect_assignment_references,
    render_assignment_references,
)
from moumou.context.observation import memory_prompt_observation
from moumou.contracts import (
    Acceptance,
    AgentInstance,
    HumanRequest,
    HumanResponse,
    Message,
    RoleSpec,
    TaskNode,
    TaskResult,
    ToolResult,
)
from moumou.contracts.plan import BlockerReport
from moumou.human_history import HumanExchangeError
from moumou.orchestrator.acceptance import command_entrypoint, validate_check
from moumou.orchestrator.delegation import DelegationError
from moumou.orchestrator.execution_progress import ExecutionProgress
from moumou.orchestrator.timing import ROOT_CLOSEOUT_SECONDS, ExecutionClock, ExecutionDeadline
from moumou.sandbox.local import LocalJail
from moumou.tools.filesystem import CheckDeliveryTool


class ContextCapacityError(RuntimeError):
    """The request cannot be made to fit the configured send cap."""

    def __init__(self, detail: dict):
        super().__init__(
            "context_budget: request would exceed the send cap; task={task} role={role} "
            "estimated={total} cap={cap} (messages={messages}, tools={tools}, "
            "tool_results={tool_results})".format(**detail)
        )
        self.detail = detail


class BudgetExceeded(RuntimeError):
    """A task hit max_steps / max_tokens / max_wall_s."""


class InvalidResultFiles(ValueError):
    """An explicit result file cannot be published from this workspace."""


class LocalAcceptanceRepair(InvalidResultFiles):
    """One actionable correction can be made in the current workspace."""


class HandoffError(ValueError):
    """依赖版本无法完整交付。"""

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class InputConflict(HandoffError):
    def __init__(self, path: str, publishers: dict[str, str]):
        self.path = path
        self.publishers = publishers
        versions = ", ".join(f"{task}/{version}" for task, version in publishers.items())
        super().__init__(f"{versions}: conflicting input path {path}", detail={
            "kind": "conflicting_input", "path": path, "publishers": publishers,
        })


class MissingBaseDependency(InvalidResultFiles):
    def __init__(self, base_task: str, base_version: str, paths: list[str]):
        self.detail = {
            "kind": "missing_base_dependency", "base_task": base_task,
            "base_version": base_version, "paths": paths,
        }
        super().__init__(
            f"Missing inherited dependency {base_task}/{base_version} for "
            f"{', '.join(paths)}. Request that the parent add {base_task} to deps."
        )


@dataclass(frozen=True)
class AcceptanceInputs:
    declared: list[str]
    own: dict[str, bytes]
    upstream: dict[str, tuple[bytes, TaskNode]]
    receipts: dict
    execute_bits: dict[str, int]
    isolated: bool


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
        compactor,
        budget,
        retriever=None,
        memory_enabled=True,
        execution_progress_enabled=False,
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
        self.compactor = compactor
        self.budget = budget
        self.retriever = retriever
        self.memory_enabled = memory_enabled
        self.execution_progress_enabled = execution_progress_enabled
        self.stable_role_imports = False
        self.scenario_clock: ExecutionClock | None = None
        self.phase_clock: ExecutionClock | None = None
        self.later_tasks = 0
        self._node_clocks: dict[str, ExecutionClock] = {}
        self.citations: dict[str, list[str]] = {}
        self._candidates: dict[str, dict] = {}

        self._steer: dict[str, list[Message]] = defaultdict(list)
        #: Tool-written paths per task. Diagnostic only: these never become the
        #: published manifest, they drive the "you forgot to declare" warning.
        self._tool_writes: dict[str, set[str]] = defaultdict(set)
        self._subscribed: set[str] = set()

    def execution_remaining(self, node_clock=None):
        clocks = (self.scenario_clock, node_clock)
        return min((clock.remaining for clock in clocks if clock is not None),
                   default=float("inf"))

    @contextmanager
    def wait_for_human(self, request):
        with ExitStack() as waits:
            for clock in (
                self.scenario_clock, self.phase_clock, self._node_clocks.get(request.task_id),
            ):
                if clock is not None:
                    waits.enter_context(clock.pause())
            yield

    def _teacher_root(self, node):
        return (node.parent_id is None and self.scenario_clock is not None
                and self.phase_clock is not None
                and bool(getattr(self.context, "minimum_specialists", 0)))

    def _booking_topics(self, node):
        if self._teacher_root(node) and node.id == "t1":
            return getattr(self.context, "booking_rule_topics", ())
        return ()

    def _missing_booking_rules(self, node):
        topics = set(self._booking_topics(node))
        if topics:
            for record in self.repo.human_exchanges(node, require_answered=False):
                if record["response"] is not None:
                    topics.difference_update(
                        record["request"]["payload"].get("requirement_topics", ()),
                    )
        return sorted(topics)

    def _root_closeout_ready(self, node):
        if not self._teacher_root(node) or self._missing_booking_rules(node):
            return False
        descendants = self.repo.active_descendants(node.id)
        roles = {child.role for child in descendants if child.acceptance
                 and (publication := self.repo.published_version(child.id))
                 and publication["files"]}
        required = self.context.required_delivery_roles
        if (len(roles - {node.role}) < self.context.minimum_specialists
                or set(required) - roles
                or not self.acceptance.subtree_verdict(node.id)[0]):
            return False
        if required:
            verdicts = self._accepted_specialist_verdicts(descendants)
            _, qa_checks = self._software_qa_evidence(descendants, verdicts)
            return bool(qa_checks)
        return True

    def _apply_plan(self, node, payload):
        _check_explicit_plan_deps(payload)
        metadata = None
        if self._teacher_root(node):
            missing = self._missing_booking_rules(node)
            subtasks = payload.get("subtasks", [])
            if missing and (not isinstance(subtasks, list) or not subtasks or any(
                not isinstance(spec, dict) or spec.get("role") not in {"qa", "release"}
                or spec.get("booking_independent_preparation") is not True
                for spec in subtasks
            )):
                raise DelegationError(
                    f"Unconfirmed booking rules: {', '.join(missing)}. Use ask_human "
                    "with requirement_topics before assigning dependent work. Only "
                    "independent QA and release preparation can start sooner."
                )
            target = payload.get("phase_time_target_s")
            later = payload.get("later_tasks_time_target_s", 0)
            if ("phase_time_target_s" in payload or "later_tasks_time_target_s" in payload
                    or not self.repo.children(node.id)):
                limit = self.scenario_clock.limit_s
                if (type(target) is not int or not 0 < target <= limit
                        or type(later) is not int or not 0 <= later <= limit
                        or bool(later) != bool(self.later_tasks)):
                    raise DelegationError(
                        "Give an estimated phase_time_target_s (total for this task), "
                        "and later_tasks_time_target_s (seconds reserved in the plan for "
                        "later tasks; use 0 on the final task). These are estimates."
                    )
                metadata = {"phase_time_target_s": target, "later_tasks_time_target_s": later,
                            "scenario_remaining_s": round(self.scenario_clock.remaining, 2)}
        result = self.delegation.apply(node, payload)
        if metadata is not None:
            self.trace.record(task_id=node.id, kind="task", name="phase_time_planned", **metadata)
        return result

    def _coordination_notice(self, time_plan, closeout_start):
        elapsed = int(self.phase_clock.active_seconds)
        remaining = max(0, int(self.scenario_clock.remaining))
        notice = (
            f"Scenario execution time remaining: {remaining} seconds. "
            f"Current task active time: {elapsed} seconds. "
            f"Later instructor tasks: {self.later_tasks}."
        )
        if time_plan is None:
            notice += (
                " On the first submit_plan, give phase_time_target_s for this task and "
                "later_tasks_time_target_s for the later tasks. Use 0 for later tasks if "
                "none remain. These targets guide planning and do not stop execution."
            )
        else:
            target = time_plan["phase_time_target_s"]
            later = time_plan["later_tasks_time_target_s"]
            notice += f" Planned active time: {target} seconds here, {later} for later tasks."
            if elapsed >= target or max(0, target - elapsed) + later > remaining:
                notice += " Reassess remaining work and prioritize the required delivery."
        if closeout_start is not None:
            notice += (
                f" Required specialist work is ready. Up to {ROOT_CLOSEOUT_SECONDS} extra "
                "node seconds are available for assembly, verification and submission."
            )
        return notice

    # -- entry point -----------------------------------------------------
    async def run(self, node: TaskNode) -> None:
        role = self.roles.get(node.project_id, node.role)
        if role is None:
            self._fail(node.id, f"role_not_found:{node.role}")
            return

        agent = self._instance(node, role)
        workspace = Path(agent.workspace)
        with self.trace.span(
            task_id=node.id, kind="task", name="execution", agent_id=agent.id,
        ) as span:
            clock = None
            closeout_start = None
            try:
                try:
                    prior = self.trace.query(node.id) if self.repo.targeted_recovery else []
                    spent = sum(
                        event.payload.get("execution_elapsed_s", (event.duration_ms or 0) / 1000)
                        for event in prior if event.kind == "task" and event.name == "execution"
                    )
                    remaining = node.budget.max_wall_s - spent
                    if self._root_closeout_ready(node):
                        closeout_start = remaining
                        if not any(event.name == "root_closeout_available" for event in prior):
                            self.trace.record(
                                task_id=node.id, kind="task", name="root_closeout_available",
                                seconds=ROOT_CLOSEOUT_SECONDS,
                            )
                    allowance = ROOT_CLOSEOUT_SECONDS if closeout_start is not None else 0
                    clock = ExecutionClock("node", remaining + allowance)
                    self._node_clocks[node.id] = clock
                    try:
                        async with clock.enforce():
                            node = self._prepare_replacement(node, workspace)
                            input_validator = getattr(self.context, "validate_inputs", None)
                            if callable(input_validator):
                                input_validator(node, workspace)
                            try:
                                deliveries = self._materialise_dependencies(node, workspace)
                            except HandoffError as exc:
                                if (not self.repo.targeted_recovery
                                        or not self.repo.children(node.id)):
                                    raise
                                deliveries = {key: {"ok": False, "error": str(exc)}
                                              for key in node.deps}
                                detail = (self._conflict_detail(node, exc)
                                          if isinstance(exc, InputConflict) else {})
                                self.trace.record(task_id=node.id, kind="task",
                                                  name="handoff_needs_repair",
                                                  error=str(exc), conflict=detail)
                                if detail:
                                    for item in deliveries.values():
                                        item["error"] = self._conflict_help(exc, detail)
                            await self._loop(node, role, agent, workspace, deliveries,
                                             prior=prior, node_clock=clock,
                                             closeout_start=closeout_start)
                    except ExecutionDeadline as exc:
                        raise BudgetExceeded("wall_clock") from exc
                except asyncio.CancelledError:
                    span.status = "cancelled"
                    expired = self.scenario_clock is not None and self.scenario_clock.remaining <= 0
                    reason = "budget:scenario" if expired else "cancelled"
                    current = self.repo.get(node.id)
                    if current is not None and not current.is_terminal:
                        self.repo.set_error(node.id, reason)
                    self._park(node.id, "DEAD", reason)
                    raise
                except BudgetExceeded as exc:
                    span.status = "error"
                    span.payload["handled_error"] = f"budget:{exc}"
                    self._fail(node.id, f"budget:{exc}")
                except ContextCapacityError as exc:
                    span.status = "error"
                    span.payload["handled_error"] = f"context_budget:{exc}"
                    span.payload["capacity_detail"] = exc.detail
                    if node.parent_id and not node.recovery_used and self.repo.targeted_recovery:
                        await self._report_blocked(node, agent, {
                            "reason": str(exc),
                            "required_input": "Continue with a narrower assignment from the "
                                              "saved workspace and confirmed requirements.",
                            "progress": "The latest messages and workspace files were retained.",
                            "evidence_refs": [],
                        })
                    else:
                        self._fail(node.id, str(exc))
                except HumanExchangeError as exc:
                    span.status = "error"
                    span.payload["handled_error"] = f"human_exchange:{exc}"
                    self._fail(node.id, f"human_exchange:{exc}")
                except HandoffError as exc:
                    span.status = "error"
                    span.payload["handled_error"] = f"handoff:{exc}"
                    before_model = not any(event.name == "llm_request_started"
                                           for event in self.trace.query(node.id))
                    admission_retry = before_model and not (node.blocker or {}).get(
                        "admission_retry_used"
                    )
                    if (node.parent_id and self.repo.targeted_recovery
                            and (not node.recovery_used or admission_retry)):
                        required = "Repair the authorized dependency publication."
                        if exc.detail.get("kind") == "workspace_conflict":
                            required = (
                                "Resolve the target path while preserving the recipient's "
                                "current work, or submit a replacement with corrected inputs."
                            )
                        await self._report_blocked(node, agent, {
                            "reason": str(exc)[:1000],
                            "required_input": required,
                            "progress": "The dependency could not be admitted.",
                            "evidence_refs": [],
                        }, handoff={**exc.detail, "before_model": before_model})
                    else:
                        self._fail(node.id, f"handoff:{exc}")
                except Exception as exc:
                    span.status = "error"
                    span.payload["handled_error"] = f"crash:{type(exc).__name__}:{exc}"
                    self._fail(node.id, f"crash:{type(exc).__name__}:{exc}")
            finally:
                self._node_clocks.pop(node.id, None)
                if clock is not None:
                    span.payload["execution_elapsed_s"] = round(clock.active_seconds, 6)
                    span.payload["human_wait_s"] = round(clock.paused_seconds, 6)
                    if closeout_start is not None:
                        started_in_closeout = max(0.0, -closeout_start)
                        total_closeout = max(0.0, clock.active_seconds - closeout_start)
                        span.payload["root_closeout_used_s"] = round(
                            total_closeout - started_in_closeout, 6,
                        )
                current = self.repo.get(node.id)
                if (current is not None and current.status in {"FAILED", "DEAD"}
                        and span.status is None):
                    span.status = "error"
                span.payload["task_status"] = current.status if current is not None else None
                span.payload["scope"] = (
                    "one execution attempt; not assignment-to-delivery wall time"
                )

    # -- the loop --------------------------------------------------------
    async def _loop(
        self, node: TaskNode, role: RoleSpec, agent: AgentInstance, workspace: Path,
        deliveries: dict | None = None, *, prior=(), node_clock, closeout_start=None,
    ) -> None:
        await self._resume_human_questions(node, agent)
        hits = (
            self.retriever.fetch(node, role)
            if self.retriever is not None and self.memory_enabled
            else []
        )
        self.trace.record(
            task_id=node.id, kind="memory", name="memory_retrieved", agent_id=agent.id,
            role=role.name, project_id=node.project_id, conversation_id=node.conversation_id,
            retrieved_ids=[record.id for record in hits],
            record_scopes=[{"id": record.id, "project_id": record.project_id, "role": record.role}
                           for record in hits],
            memory_enabled=self.memory_enabled,
        )
        qa_checkpoint, qa_versions, qa_hint = self._qa_progress_context(
            node, workspace, deliveries or {}
        )
        ctx = self._opening(node, role, agent, deliveries, hits, qa_hint=qa_hint)
        if ctx.memory_ids:
            self.citations[node.id] = list(ctx.memory_ids)
        self._subscribe(agent.id)
        tool_specs = self.tools.subset(role.tools)
        _explicit_plan_deps_schema(tool_specs)
        execution_progress = (ExecutionProgress(self.gate, self.trace)
                              if self.execution_progress_enabled else None)
        if execution_progress is not None:
            execution_progress.describe(tool_specs)
        dispatcher = execution_progress if execution_progress is not None else self.gate
        teacher_root = self._teacher_root(node)
        booking_topics = self._booking_topics(node)
        if teacher_root:
            for spec in tool_specs:
                function = spec["function"]
                if function["name"] not in {"ask_human", "submit_plan"}:
                    continue
                properties = function["parameters"]["properties"]
                if function["name"] == "submit_plan":
                    properties["phase_time_target_s"] = {
                        "type": "integer", "minimum": 1,
                        "description": "Estimated total active seconds for this instructor task.",
                    }
                    properties["later_tasks_time_target_s"] = {
                        "type": "integer", "minimum": 0,
                        "description": "Estimated active seconds for later tasks; 0 on the last.",
                    }
                    if booking_topics:
                        properties["subtasks"]["items"]["properties"][
                            "booking_independent_preparation"
                        ] = {
                            "type": "boolean",
                            "description": "QA or release preparation independent of room rules.",
                        }
                if function["name"] == "ask_human" and booking_topics:
                    properties["requirement_topics"] = {
                        "type": "array", "uniqueItems": True,
                        "items": {"type": "string", "enum": list(booking_topics)},
                        "description": "Booking rules asked here. Answers are shared.",
                    }
        shared_roles = getattr(self.context, "shared_requirement_roles", ())
        if shared_roles:
            for spec in tool_specs:
                function = spec["function"]
                if function["name"] == "ask_human":
                    function["parameters"]["properties"]["share_with_roles"] = {
                        "type": "array", "uniqueItems": True,
                        "items": {"type": "string", "enum": list(shared_roles)},
                        "description": "Roles that need this confirmed requirement",
                    }

        tokens = sum(event.tokens_in + event.tokens_out for event in prior if event.kind == "llm")
        last_call_tokens = next((event.tokens_in + event.tokens_out for event in reversed(prior)
                                 if event.kind == "llm" and event.status == "ok"), 0)
        attempted = sum(event.kind == "task" and event.name == "llm_request_started"
                        for event in prior)
        wrapup_reported = any(event.name == "budget_wrapup" for event in prior)
        reported_closeouts = {event.payload["scope"] for event in prior
                              if event.name == "time_closeout_started"}
        repairing = any(event.name == "acceptance_repair_requested" for event in prior)
        time_plan = next((event.payload for event in reversed(prior)
                          if event.name == "phase_time_planned"), None)
        recent_calls = deque(
            (event.duration_ms / 1000 for event in prior
             if event.kind == "llm" and event.duration_ms is not None),
            maxlen=3,
        )
        rejected_recoveries = defaultdict(int)

        for step in range(attempted + 1, node.budget.max_steps + 1):
            seconds = self.execution_remaining(node_clock)
            if seconds <= 0:
                raise BudgetExceeded("wall_clock")
            if tokens > node.budget.max_tokens:
                raise BudgetExceeded("tokens")
            nudges = self._drain(agent.id)
            if nudges:
                if execution_progress is not None:
                    execution_progress.reset()
                ctx.inject_steering([m.content for m in nudges if m.kind == "steer"])
                for notice in nudges:
                    if notice.kind == "report" and notice.task_id == node.id:
                        ctx.messages().append({
                            "role": "user", "content": f"[Framework blocker] {notice.content}",
                        })
                self.trace.record(
                    task_id=node.id,
                    kind="steer",
                    name="received",
                    agent_id=agent.id,
                    count=len(nudges),
                )

            submission_only = False
            turn_seconds = max(recent_calls, default=10.0)
            check_seconds = 5.0 + min(60.0, sum(
                check.timeout_s for check in node.acceptance
                if check.kind in {"pytest", "command"}
            ))
            if self.repo.targeted_recovery:
                submission_only = bool(last_call_tokens) and (
                    node.budget.max_steps - step + 1 <= 2
                    or node.budget.max_tokens - tokens <= 2 * last_call_tokens
                )
                scenario_closing = self.scenario_clock is not None and self.scenario_clock.closing
                if scenario_closing and "scenario" not in reported_closeouts:
                    self.trace.record(
                        task_id=node.id, kind="task", name="time_closeout_started",
                        scope="scenario", step=step,
                        seconds=round(self.scenario_clock.remaining, 2),
                    )
                    reported_closeouts.add("scenario")
                wrapup_reported = self._append_budget_notice(
                    node, ctx, step=step, tokens=tokens, seconds=seconds,
                    wrapup_reported=wrapup_reported, last_call_tokens=last_call_tokens,
                    submission_only=submission_only,
                    time_closeout=scenario_closing,
                    coordination=self._coordination_notice(time_plan, closeout_start)
                    if teacher_root else "",
                )
            estimated = self._ensure_request_fits(node, role, agent, ctx, tool_specs, step=step)
            self.trace.record(
                task_id=node.id, kind="memory", name="memory_prompt_observed", agent_id=agent.id,
                role=role.name, project_id=node.project_id, conversation_id=node.conversation_id,
                step=step,
                **memory_prompt_observation(ctx.messages(), hits, LESSONS_HEADER),
            )
            attempts: list[dict] = []
            call_started = monotonic()
            with self.trace.span(
                task_id=node.id, kind="llm", name=f"{role.name}:step{step}", agent_id=agent.id
            ) as span:
                self.trace.record(
                    task_id=node.id, kind="task", name="llm_request_started",
                    agent_id=agent.id, request_id=span.span_id, step=step,
                    request_estimated_tokens=estimated,
                )
                try:
                    response = await self._chat(ctx.messages(), tool_specs, attempts)
                finally:
                    span.payload["http_attempts"] = attempts
                span.tokens_in = response.tokens_in
                span.tokens_out = response.tokens_out
                span.payload["usage_present"] = response.usage_present
                span.payload["tokens_cached"] = response.cache_hit_tokens
                span.payload["request_estimated_tokens"] = estimated
                span.payload["request_parts"] = self.budget.split_request(
                    ctx.messages(), tool_specs
                )
            recent_calls.append(monotonic() - call_started)
            self.budget.observe(estimated, response.tokens_in)
            tokens += response.tokens
            last_call_tokens = response.tokens
            if self.repo.targeted_recovery and tokens > node.budget.max_tokens:
                if not (len(response.tool_calls) == 1
                        and response.tool_calls[0].name in {"submit_result", "report_blocked"}):
                    raise BudgetExceeded("tokens")

            ctx.add_assistant(response.content, response.tool_calls or None)

            if not response.tool_calls:
                if self.repo.targeted_recovery:
                    ctx.inject_steering([
                        "Use submit_result for delivery, or submit_plan for delegation.",
                    ])
                    continue
                await self._finish(node, workspace, TaskResult(summary=response.content or ""))
                return

            for call in response.tool_calls:
                if self.repo.targeted_recovery:
                    restricted = self._closeout_tool_error(
                        call.name, wrapup_reported, submission_only,
                        candidate=node.id in self._candidates,
                        repairing=repairing,
                        root_closeout=(closeout_start is not None
                                       and node_clock.active_seconds >= closeout_start),
                    )
                    if restricted is not None:
                        self.trace.record(task_id=node.id, kind="task", name="budget_tool_deferred",
                                          tool=call.name, step=step)
                        ctx.add_tool_result(call.id, ToolResult(ok=False, error=restricted))
                        continue
                if call.name == "report_blocked":
                    try:
                        await self._report_blocked(node, agent, call.args, remaining={
                            "steps": node.budget.max_steps - step,
                            "tokens": node.budget.max_tokens - tokens,
                            "seconds": max(0, int(self.execution_remaining(node_clock))),
                        })
                    except ValueError as exc:
                        ctx.add_tool_result(call.id, ToolResult(ok=False, error=str(exc)[:1500]))
                        continue
                    return
                if call.name == "submit_plan":
                    # Invalid proposals are tool feedback, not failed task attempts.
                    try:
                        self._apply_plan(node, call.args)
                    except DelegationError as exc:
                        result = ToolResult(
                            ok=False,
                            error=(
                                f"invalid_plan: {str(exc)[:2000]}\n"
                                "No subtasks were created. Correct the plan and "
                                "call submit_plan again."
                            ),
                        )
                        self.trace.record(
                            task_id=node.id, kind="delegate", name="plan_rejected",
                            agent_id=agent.id, error=result.error,
                        )
                        if self._stop_if_recovery_exhausted(node, str(exc), rejected_recoveries):
                            return
                        ctx.add_tool_result(
                            call.id, result, tool_name=call.name, workspace=workspace,
                            limit=self._tool_result_chars(),
                        )
                        continue
                    # Successful delegation transitions the parent to PENDING itself.
                    return
                if call.name == "submit_result":
                    remaining_budget = {
                        "steps": node.budget.max_steps - step,
                        "tokens": node.budget.max_tokens - tokens,
                        "seconds": max(0, int(self.execution_remaining(node_clock))),
                    }
                    result = await self._submit_result(
                        node, agent, workspace, call.args, remaining=remaining_budget,
                        repair_seconds=max(turn_seconds, 5) + check_seconds + 5,
                    )
                    if result is None:
                        return
                    if (result.error or "").startswith("local_acceptance_repair:"):
                        repairing = True
                    if self._stop_if_recovery_exhausted(node, result.error, rejected_recoveries):
                        return
                    ctx.add_tool_result(
                        call.id, result, tool_name=call.name, workspace=workspace,
                        limit=self._tool_result_chars(),
                    )
                    continue
                if call.name == "ask_human":
                    result = await self._ask_human(call, node, agent)
                    if result.ok and execution_progress is not None:
                        execution_progress.reset()
                elif (call.name == "check_delivery" and self.stable_role_imports
                      and self.repo.targeted_recovery
                      and isinstance(self.tools.get(call.name), CheckDeliveryTool)):
                    result = await self.gate.execute(
                        call, node, agent, workspace, handler=self._check_delivery,
                    )
                else:
                    result = await dispatcher.execute(call, node, agent, workspace)
                    if result.artifacts:
                        self._tool_writes[node.id].update(result.artifacts)
                        self._record_draft_writes(node, workspace, result)
                    if (qa_checkpoint and result.ok
                            and any(path.removeprefix("./") == qa_checkpoint
                                    for path in result.artifacts)):
                        checks = self._read_qa_progress(workspace, qa_checkpoint, qa_versions)
                        if checks is not None:
                            self.trace.record(
                                task_id=node.id, kind="task", name="qa_progress_saved",
                                path=qa_checkpoint, versions=qa_versions,
                                checks=len(checks),
                                reported_passes=sum(item["exit_code"] == 0 for item in checks),
                            )
                ctx.add_tool_result(
                    call.id, result, tool_name=call.name, workspace=workspace,
                    limit=self._tool_result_chars(),
                )
                if call.name == "ask_human":
                    ctx.replace_opening(self._opening(
                        node, role, agent, deliveries, hits, reason="after_recorded_answer",
                        qa_hint=qa_hint,
                    ))

        raise BudgetExceeded("max_steps")

    @staticmethod
    def _closeout_tool_error(
        name, wrapup, submission_only, *, candidate=False, repairing=False, root_closeout=False,
    ):
        if root_closeout and name in {"web_search", "submit_plan"}:
            return "Extra root time is limited to assembly, verification and submission."
        if repairing and name in {"web_search", "submit_plan"}:
            return "Correct the reported local check and resubmit, or report the exact blocker."
        if candidate and name in {
            "web_search", "submit_plan", "write_file", "apply_patch", "delete_file",
        }:
            return ("A delivery candidate is frozen. Complete its required validation and submit. "
                    "If an actual requirement is still missing, reopen via check_delivery "
                    "with freeze_candidate=false and list the required item.")
        if submission_only and not repairing and name not in {
            "check_delivery", "submit_result", "submit_plan", "report_blocked", "ask_human",
            "verify_artifact_quote", "read_file",
        }:
            return "Final budget reserved for delivery checks, submission, or reporting a blocker."
        if wrapup and name == "web_search":
            return "Search is closed for this assignment. Use retained evidence or report the gap."
        return None

    def _stop_if_recovery_exhausted(self, node, error, rejected_recoveries):
        if not self.repo.targeted_recovery:
            return False
        current = self.repo.get(node.id)
        children = [child for child in self.repo.children(node.id) if not child.superseded_by]
        blocked = [child for child in children if child.status in {"BLOCKED", "DEAD"}]
        if not blocked:
            return False
        deps = [dep for dep_id in current.deps if (dep := self.repo.get(dep_id)) is not None]
        if self.repo._has_live_dependency(deps):
            return False
        exhausted = [child.id for child in blocked
                     if child.status == "DEAD" and child.recovery_used]
        if not exhausted:
            state = tuple((dep.id, dep.status, dep.updated_at) for dep in deps)
            rejected_recoveries[state, error] += 1
            if rejected_recoveries[state, error] < 3:
                return False
            exhausted = [child.id for child in blocked]
            reason = "unchanged recovery rejected three times"
        else:
            reason = "recovery exhausted"
        self.trace.record(task_id=node.id, kind="delegate", name="recovery_exhausted",
                          failed_children=exhausted, reason=reason)
        self._fail(node.id, f"unresolved_child: {reason} for {', '.join(exhausted)}")
        return True

    def _append_budget_notice(
        self, node, ctx, *, step, tokens, seconds, wrapup_reported, last_call_tokens=0,
        submission_only=False, time_closeout=False, coordination="",
    ) -> bool:
        steps_left = node.budget.max_steps - step + 1
        tokens_left = node.budget.max_tokens - tokens
        wrapup = (wrapup_reported or time_closeout or submission_only
                  or tokens_left <= 3 * last_call_tokens
                  or min(steps_left / node.budget.max_steps,
                         tokens_left / node.budget.max_tokens) < 0.2)
        notice = (
            "[Framework runtime notice. Use the latest budget figures.] "
            f"Remaining budget: {steps_left} steps, {tokens_left} tokens, {int(seconds)} seconds."
        )
        if coordination:
            notice += "\n" + coordination
        if wrapup:
            notice += (
                "\nFinish now using verified evidence for the current inputs. Submit "
                "completed work for machine acceptance or report the exact blocker "
                "with saved progress. Complete mandatory corrections using retained evidence. "
                "Search is closed; avoid repeating unchanged checks."
            )
            if submission_only:
                notice += " The remaining calls are reserved for delivery or reporting a blocker."
            if not wrapup_reported:
                self.trace.record(
                    task_id=node.id, kind="task", name="budget_wrapup",
                    steps=steps_left, tokens=tokens_left, seconds=int(seconds),
                )
        ctx.messages().append({"role": "user", "content": notice})
        return wrapup_reported or wrapup

    def _qa_progress_context(self, node, workspace, deliveries):
        if (node.role != "qa" or not getattr(self.context, "required_delivery_roles", ())
                or not any((dep := self.repo.get(dep_id)) and dep.role == "dev"
                           for dep_id in deliveries)):
            return None, {}, ""
        versions = {dep_id: receipt["version"] for dep_id, receipt in deliveries.items()
                    if receipt.get("ok") and receipt.get("version") not in {None, "empty"}}
        if not versions:
            return None, {}, ""
        checkpoint = f"qa/{node.id}-progress.json"
        candidates = [checkpoint, *[
            f"qa/{child.id}-progress.json"
            for child in self.repo.children(node.parent_id)
            if child.role == node.role and child.superseded_by == node.id
        ]] if node.parent_id else [checkpoint]
        saved = [path for path in candidates
                 if self._read_qa_progress(workspace, path, versions) is not None]
        hint = (
            f"\nAfter a meaningful QA run, save progress to {checkpoint}. Write a JSON object "
            f"with input_versions exactly {json.dumps(versions, sort_keys=True)}, checks "
            "(list of command, exit_code, evidence objects), and pending (list of open items). "
            "Record actual results and local evidence paths. When returning to the task or "
            "after context compaction, read this short file before repeating tests. Reuse "
            "exploratory evidence for unchanged versions, test scripts and inputs, then call "
            "submit_result promptly with the real report and required acceptance files. "
            "The framework executes formal acceptance separately."
        )
        if saved:
            hint += " Saved progress for these input versions: " + ", ".join(saved) + "."
        return checkpoint, versions, hint

    @staticmethod
    def _read_qa_progress(workspace, relative, versions):
        try:
            path = LocalJail(workspace).resolve_artifact(relative)
            if not path.is_file() or path.stat().st_size > 8192:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, RuntimeError):
            return None  # 可选工作记录。格式无效时仍执行正式验收。
        if not isinstance(record, dict) or record.get("input_versions") != versions:
            return None
        checks = record.get("checks")
        if (not isinstance(checks, list) or not checks
                or not isinstance(record.get("pending"), list)
                or not all(isinstance(item, dict) and type(item.get("exit_code")) is int
                           and all(isinstance(item.get(key), str) and item[key].strip()
                                   for key in ("command", "evidence"))
                           for item in checks)):
            return None
        return checks

    async def _report_blocked(
        self, node, agent, args, *, remaining=None, base_recovery=None, handoff=None,
    ):
        parent = self.repo.get(node.parent_id) if node.parent_id else None
        if parent is None:
            raise ValueError("Only a delegated task can report a blocker to its parent.")
        report = BlockerReport.model_validate(args).model_dump()
        current = self.repo.get(node.id)
        admission_retry = (handoff and handoff.get("before_model")
                           and not (current.blocker or {}).get("admission_retry_used"))
        if current.recovery_used and not admission_retry:
            self._fail(node.id, f"blocker_after_recovery:{report['reason']}")
            return
        versions = {}
        for dep_id in current.deps:
            published = self.repo.published_version(dep_id)
            if published:
                versions[dep_id] = published["version"]
        report["versions"] = versions
        report["remaining_budget"] = remaining
        if base_recovery is not None:
            report["base_recovery"] = base_recovery
        if handoff:
            report["handoff"] = {"consumer": node.id, **handoff}
        saved_drafts = (current.blocker or {}).get("saved_drafts")
        if saved_drafts:
            report["saved_drafts"] = saved_drafts
        with self.repo.db.transaction():
            self.repo.set_blocker(node.id, report)
            self.repo.set_error(node.id, report["reason"])
            self.repo.transition(node.id, "BLOCKED", "reported_to_parent")
        self.trace.record(task_id=node.id, kind="delegate", name="child_blocked",
                          agent_id=agent.id, parent_id=parent.id, **report)
        parent_role = self.roles.get(parent.project_id, parent.role)
        recipient = self._instance(parent, parent_role)
        await self.bus.publish(Message(
            kind="report", project_id=node.project_id, conversation_id=node.conversation_id,
            task_id=parent.id, sender=agent.id, recipients=[recipient.id],
            content=f"{node.id}: {report['reason']} Required: {report['required_input']}",
            meta={"blocker": True, "scope": parent.id},
        ))

    # -- completion ------------------------------------------------------
    async def _submit_result(
        self, node, agent, workspace, args, *, remaining=None, repair_seconds=0,
    ):
        try:
            await self._finish(
                node, workspace, _coerce_result(args),
                repair_budget=remaining, repair_seconds=repair_seconds,
            )
        except MissingBaseDependency as exc:
            current = self.repo.get(node.id)
            if current.recovery_used:
                self._fail(node.id, f"blocker_after_recovery:{exc}")
                return None
            paths = set(exc.detail["paths"])
            policy = getattr(self.context, "input_policy", None)
            if policy is not None:
                jail = LocalJail(workspace)
                for entry in policy.admitted_files(current):
                    relative = entry["path"]
                    path = jail.resolve_artifact(relative)
                    if (path.is_file() and jail.relative(path) == relative
                            and hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]):
                        paths.add(relative)
            drafts = self._save_drafts(workspace, sorted(paths))
            base = exc.detail["base_task"]
            await self._report_blocked(node, agent, {
                "reason": str(exc)[:1000],
                "required_input": f"Use submit_plan.resume with task_id {node.id} and "
                                  f'deps ["{base}"] to import the verified baseline.',
                "progress": "Edited files were saved in the role's private recovery directory. "
                            "Compare them with the imported baseline and resume the same work.",
                "evidence_refs": list(drafts.values())[:8],
            }, remaining=remaining, base_recovery={**exc.detail, "saved_drafts": drafts})
        except InputConflict as exc:
            if node.parent_id is not None or not self.repo.targeted_recovery:
                raise
            return self._reject_submission(node, agent, self._submission_issue(node, exc))
        except InvalidResultFiles as exc:
            result = ToolResult(
                ok=False,
                error=f"invalid_result_files: {str(exc)[:2000]}\n"
                      "Correct the reported issue and call submit_result again.",
            )
            return self._reject_submission(node, agent, result)
        return None

    def _reject_submission(self, node, agent, result: ToolResult) -> ToolResult:
        self.trace.record(task_id=node.id, kind="task", name="result_rejected",
                          agent_id=agent.id, error=result.error)
        return result

    def _conflict_detail(self, node, exc: InputConflict) -> dict:
        consumers = {producer: [] for producer in exc.publishers}
        affected = []
        for child in self.repo.active_descendants(node.id):
            if child.id in exc.publishers:
                continue
            sources = [producer for producer in consumers if self._depends_on(child, producer)]
            if sources:
                affected.append(child.id)
                for producer in sources:
                    consumers[producer].append(child.id)
        return {"kind": "conflicting_input", "path": exc.path,
                "publishers": exc.publishers, "affected_tasks": affected,
                "consumers_by_publisher": consumers}

    @staticmethod
    def _conflict_help(exc, detail):
        affected = " | ".join(f"{task}: {', '.join(children) or 'none'}"
                              for task, children in detail["consumers_by_publisher"].items())
        return (
            f"{exc}. Affected tasks: {affected}. Correct the inheritance with deps or use "
            "submit_plan with replaces for the affected direct assignments. "
            "Retain publications and checks whose inputs are unchanged."
        )

    def _submission_issue(self, node, exc) -> ToolResult:
        detail, message = {}, str(exc)
        if isinstance(exc, InputConflict):
            detail = self._conflict_detail(node, exc)
            message = self._conflict_help(exc, detail)
        elif isinstance(exc, MissingBaseDependency):
            detail = exc.detail
            message += " Call submit_result with the same declaration to save drafts and pause."
        return ToolResult(ok=False, error=message, stdout=json.dumps({
            "valid": False, "issues": message, **detail,
        }, ensure_ascii=False))

    async def _check_delivery(self, args, ctx):
        node = self.repo.get(ctx.task_id)
        freeze = args.get("freeze_candidate")
        required = args.get("open_required_items")
        if freeze is True and required != []:
            return ToolResult(ok=False, error=(
                "Finish mandatory edits and pass open_required_items=[] before freezing."
            ))
        if freeze is False and node.id in self._candidates:
            if not required or not all(isinstance(item, str) and item.strip() for item in required):
                return ToolResult(
                    ok=False, error="List the mandatory items that require reopening.",
                )
            self._clear_candidate(node.id, "required_edits")
        try:
            inputs, checked = self._preflight_submission(node, ctx.workspace, _coerce_result(args))
        except (InvalidResultFiles, HandoffError) as exc:
            return self._submission_issue(node, exc)
        report = {"valid": True, "declared_files": inputs.declared,
                  "acceptance_paths": sorted(set(inputs.own) | set(inputs.upstream)),
                  "versions": {key: value["version"] for key, value in inputs.receipts.items()
                               if value["ok"]},
                  "isolated": inputs.isolated, "issues": ""}
        if freeze is True:
            if not inputs.declared:
                return ToolResult(ok=False, error="Freeze a candidate with declared deliverables.")
            identity = self._candidate_identity(inputs, checked)
            candidate = self._candidates.get(node.id)
            if candidate is None or candidate["identity"] != identity:
                self._clear_candidate(node.id, "candidate_inputs_changed")
                candidate = {"id": uuid.uuid4().hex, "identity": identity}
                self._candidates[node.id] = candidate
                self.trace.record(task_id=node.id, kind="acceptance", name="candidate_frozen",
                                  candidate_id=candidate["id"], **identity)
            report["candidate_id"] = candidate["id"]
            report["sha256"] = {path: checked[path] for path in inputs.declared}
            report["next"] = (
                "Batch mandatory checks against these hashes, reuse evidence for unchanged "
                "files, then call submit_result. Keep required corrections narrowly scoped."
            )
        return ToolResult(ok=True, stdout=json.dumps(report, ensure_ascii=False))

    @staticmethod
    def _candidate_identity(inputs, checked):
        return {"files": sorted(inputs.declared), "own_files": sorted(inputs.own),
                "checked_files": checked, "execute_bits": inputs.execute_bits,
                "versions": {key: value["version"] for key, value in inputs.receipts.items()
                             if value["ok"]}}

    def _clear_candidate(self, task_id, reason):
        if (candidate := self._candidates.pop(task_id, None)) is not None:
            self.trace.record(task_id=task_id, kind="acceptance", name="candidate_invalidated",
                              candidate_id=candidate["id"], reason=reason)

    def _preflight_submission(self, node, workspace, result):
        if missing := self._missing_booking_rules(node):
            raise InvalidResultFiles(
                f"Unconfirmed booking rules: {', '.join(missing)}. Ask the requester.",
            )
        if self.repo.targeted_recovery and node.parent_id is None:
            unresolved = [child for child in self.repo.active_descendants(node.id)
                          if child.status != "DONE"]
            if unresolved:
                details = " | ".join(self.delegation.child_status(child) for child in unresolved)
                raise InvalidResultFiles(f"active dependencies are incomplete: {details}")
        for index, item in enumerate(node.acceptance, 1):
            try:
                validate_check(item)
            except ValueError as exc:
                raise InvalidResultFiles(f"acceptance check {index}: {exc}") from exc
        inputs = self._acceptance_inputs(node, workspace, result)
        self._validate_delivery(node, workspace, result)
        data = {path: content for path, (content, _) in inputs.upstream.items()} | inputs.own
        checked = {path: hashlib.sha256(content).hexdigest() for path, content in data.items()}
        self._validate_base_dependency(node, inputs, checked)
        self._validate_role_evidence(node, inputs, checked)
        return inputs, checked

    def _validate_base_dependency(self, node, inputs, checked) -> None:
        previous = self._previous_teacher_root()
        if (not previous or node.parent_id is None or not inputs.declared
                or self._depends_on(node, previous)):
            return
        publication = self.repo.published_version(previous)
        if not publication:
            return
        declared = set(inputs.declared)
        changed = [
            path for entry in publication["files"]
            if (path := entry["path"]) in declared
            and (checked[path] != entry["sha256"]
                 or (entry.get("execute_bits", inputs.execute_bits[path])
                     != inputs.execute_bits[path]))
        ]
        if changed:
            raise MissingBaseDependency(previous, publication["version"], sorted(changed))

    def _previous_teacher_root(self) -> str | None:
        if not self.stable_role_imports or not self.repo.targeted_recovery:
            return None
        prior = getattr(self.context, "prior_context", {})
        return prior.get("previous_root_id") if prior.get("previous_status") == "DONE" else None

    @staticmethod
    def _draft_write_key(workspace, relative):
        return f"workspace_draft:{workspace}:{relative}"

    def _record_draft_writes(self, node, workspace, result):
        if not self.stable_role_imports or not self.repo.targeted_recovery:
            return
        jail = LocalJail(workspace)
        for relative in result.artifacts:
            relative = relative.removeprefix("./")
            try:
                path = jail.resolve_artifact(relative)
                if (jail.relative(path) != relative or not path.is_file()
                        or path.stat().st_nlink != 1):
                    continue
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                identity = {"task": node.id, "sha256": digest, "execute_bits": _execute_bits(path)}
            except (OSError, ValueError):
                continue  # 无法核实的文件交由现有覆盖检查保护。
            self.repo.db.set_meta(self._draft_write_key(workspace, relative), json.dumps(identity))

    @staticmethod
    def _save_drafts(workspace, paths):
        jail = LocalJail(workspace)
        prefix = f".evaluation/recovery/{uuid.uuid4().hex}"
        drafts = {path: f"{prefix}/{path}" for path in paths}
        moves = []
        for path, saved in drafts.items():
            target = jail.resolve(saved, mutable=True)
            if jail.relative(target) != saved:
                raise HandoffError(f"Draft recovery path aliases another file: {saved}")
            target.parent.mkdir(parents=True, exist_ok=True)
            moves.append((jail.resolve_artifact(path), target))
        completed = []
        try:
            for source, target in moves:
                source.replace(target)
                completed.append((source, target))
        except OSError:
            for source, target in reversed(completed):
                target.replace(source)
            raise
        return drafts

    def _prepare_replacement(self, node, workspace):
        if (not self.stable_role_imports or not node.parent_id
                or not node.recovery_used or node.blocker):
            return node
        retired = next((child for child in self.repo.children(node.parent_id)
                        if child.superseded_by == node.id and child.role == node.role), None)
        if retired is None:
            return node
        prior = self.repo.published_version(retired.id)
        old_files = {entry["path"]: entry for entry in prior["files"]} if prior else {}
        files, receipts = self._dependency_files(node)
        jail = LocalJail(workspace)
        drafts = []
        for relative, (incoming, source) in sorted(files.items()):
            path = jail.resolve_artifact(relative)
            if not path.is_file() or jail.relative(path) != relative or path.stat().st_nlink != 1:
                continue
            raw, bits = path.read_bytes(), _execute_bits(path)
            if raw == incoming and bits == receipts[source.id]["execute_bits"][relative]:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            observed = json.loads(self.repo.db.get_meta(self._draft_write_key(workspace, relative))
                                  or "null")
            if (observed and observed["sha256"] == digest and observed["execute_bits"] == bits):
                owned = observed["task"] == retired.id
            else:
                entry = old_files.get(relative)
                owned = entry and (entry["sha256"], entry.get("execute_bits", bits)) == (
                    digest, bits,
                )
            if owned:
                drafts.append(relative)
        if drafts:
            saved = self._save_drafts(workspace, drafts)
            notice = {
                "resolution": "The previous assignment's saved work is listed in saved_drafts. "
                              "Use the admitted dependencies as the base and compare the drafts.",
                "saved_drafts": saved,
            }
            self.repo.set_blocker(node.id, notice)
            self.trace.record(task_id=node.id, kind="task", name="replacement_drafts_saved",
                              retired_task=retired.id, **notice)
            return self.repo.get(node.id)
        return node

    async def _finish(
        self, node: TaskNode, workspace: Path, result: TaskResult, *,
        repair_budget=None, repair_seconds=0,
    ) -> None:
        node = self.repo.get(node.id) or node
        try:
            inputs, checked_files = self._preflight_submission(node, workspace, result)
        except (InvalidResultFiles, HandoffError):
            self._clear_candidate(node.id, "preflight_failed")
            raise
        candidate = self._candidates.get(node.id)
        if candidate and candidate["identity"] != self._candidate_identity(inputs, checked_files):
            self._clear_candidate(node.id, "candidate_inputs_changed")
            candidate = None
        prior_repair = None
        if self.repo.targeted_recovery:
            prior_repair = next(
                (event.payload for event in self.trace.query(node.id)
                 if event.name == "acceptance_repair_requested"), None,
            )
        if prior_repair and checked_files.get(prior_repair["target"]) == prior_repair["sha256"]:
            target = prior_repair["target"]
            self._clear_candidate(node.id, "unchanged_acceptance_failure")
            self.trace.record(task_id=node.id, kind="acceptance",
                              name="acceptance_repair_unchanged", target=target)
            self.repo.set_result(node.id, result)
            self._fail(node.id, f"local_acceptance_repair_unchanged:{target}")
            return
        declared, receipts = inputs.declared, inputs.receipts
        if node.parent_id is None and not node.acceptance:
            # A root with real subtree evidence is not graded on an empty
            # self-report: attach an aggregate check over its descendants' own
            # machine verdicts. Attached at finish time so the opening prompt is
            # unchanged, and skipped entirely when no descendant declared checks
            # so an otherwise-acceptable root is never failed by an empty
            # aggregate.
            if any(t.acceptance for t in self.repo.active_descendants(node.id)):
                node.acceptance = [Acceptance(kind="subtree_accepted", target="root")]
                self.repo.set_acceptance(node.id, node.acceptance)
        acceptance_root, isolated = self._prepare_acceptance_root(
            node, workspace, result, inputs=inputs,
        )
        jail = LocalJail(acceptance_root)
        check_started = monotonic()
        ok, details = await self._run_acceptance(
            node, acceptance_root, workspace if isolated else None
        )
        if ok:
            # Acceptance commands may clean up or move files. Recheck the whole
            # declaration immediately before publication, still without writing
            # a partial result or artifact batch on failure.
            declared = _declared_artifacts(workspace, result.files)
            # Validate the bytes that were actually accepted, and make sure the
            # working copy still matches them: acceptance may move or rewrite
            # files, and a silent divergence must not be published.
            self._validate_delivery(node, acceptance_root, result)
            self._verify_accepted_bytes(node, acceptance_root, workspace, declared, isolated)
            for relative, digest in checked_files.items():
                path = jail.resolve_artifact(relative)
                if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                        or _execute_bits(path) != inputs.execute_bits[relative]):
                    raise InvalidResultFiles(f"acceptance changed a checked file: {relative}")

        self.trace.record(
            task_id=node.id, kind="acceptance", name="verdict", ok=ok, checks=details,
            workspace=str(acceptance_root), checked_files=checked_files,
            checked_execute_bits=inputs.execute_bits,
            versions={key: item["version"] for key, item in receipts.items() if item["ok"]},
            candidate_id=candidate["id"] if candidate else None,
        )
        if ok:
            with self.repo.db.transaction():
                version = self._publish_version(node, workspace, declared, source=acceptance_root)
                self.repo.set_result(node.id, result)
                self.repo.set_artifacts(node.id, declared)
                self.repo.transition(node.id, "DONE", "accepted")
            node = self.repo.get(node.id) or node
            if declared:
                self.trace.record(
                    task_id=node.id, kind="task", name="declared_files_published",
                    files=declared, version=version,
                )
                self._warn_undeclared(node, list(inputs.own))
            self._candidates.pop(node.id, None)
        else:
            failed = [d for d in details if not d.get("ok")]
            if (
                self.repo.targeted_recovery and repair_budget and len(failed) == 1
                and repair_budget["steps"] >= 1 and repair_budget["tokens"] > 0
                and repair_budget["seconds"] - (monotonic() - check_started) >= repair_seconds
                and failed[0]["kind"] in {"file_contains", "json_key"}
                and failed[0]["target"] in inputs.own
                and failed[0].get("failure_kind", "check_failed") == "check_failed"
                and not prior_repair
            ):
                check = failed[0]
                self._clear_candidate(node.id, "required_acceptance_repair")
                self.trace.record(
                    task_id=node.id, kind="acceptance", name="acceptance_repair_requested",
                    target=check["target"], check_kind=check["kind"],
                    sha256=checked_files[check["target"]],
                )
                raise LocalAcceptanceRepair(
                    f"local_acceptance_repair: {check['kind']} failed in {check['target']}. "
                    f"Expected {check.get('expect')!r}. {check.get('error') or ''} "
                    "Correct this file and resubmit once. Other checks still apply."
                )
            self._candidates.pop(node.id, None)
            self.repo.set_result(node.id, result)
            error = [{"failure_kind": check.get("failure_kind", "check_failed"),
                      "error": str(check.get("error") or "")[:350],
                      "target": str(check.get("target", ""))[:300],
                      "expected_exit_code": check.get("expected_exit_code"),
                      "exit_code": check.get("exit_code"),
                      "stderr": str(check.get("stderr", ""))[-350:]} for check in failed[:3]]
            self.repo.set_error(node.id, json.dumps(error, ensure_ascii=False))
            self.repo.transition(node.id, "FAILED", "acceptance_failed")

    def _validate_role_evidence(self, node, inputs: AcceptanceInputs, checked) -> None:
        declared, upstream = inputs.declared, inputs.upstream
        required = getattr(self.context, "required_delivery_roles", ())
        minimum = getattr(self.context, "minimum_specialists", 0)
        if minimum and node.parent_id is None:
            roles = {child.role for child in self.repo.active_descendants(node.id)
                     if child.status == "DONE" and child.acceptance and child.artifacts}
            if len(roles - {node.role}) < minimum:
                raise InvalidResultFiles(
                    "delivery requires verified results from two specialist roles"
                )
        if not required:
            return
        product = {path: checked[path] for path in declared
                   if Path(path).suffix.lower() not in {".md", ".txt"}}
        executable = any(item.kind in {"pytest", "command"} for item in node.acceptance)
        tests_dev = any(source.role == "dev" for _, source in upstream.values())
        if node.role == "qa" and tests_dev:
            if not declared or not executable:
                raise InvalidResultFiles(
                    "QA must publish evidence and execute a check on dev inputs"
                )
        if node.parent_id is not None and node.role != "release":
            return
        if node.role == "release" and not product:
            return
        root = node
        while root.parent_id is not None:
            root = self.repo.get(root.parent_id)
        active = self.repo.active_descendants(root.id)
        verdicts = self._accepted_specialist_verdicts(active)
        roles = {child.role for child in active if child.id in verdicts}
        if node.parent_id is None and (set(required) - roles or not product):
            raise InvalidResultFiles(
                "delivery requires distinct specialist results and product files"
            )
        dev_files = [path for path, (_, source) in upstream.items()
                     if source.role == "dev" and Path(path).suffix.lower() not in {".md", ".txt"}]
        if node.parent_id is None and (omitted := set(dev_files) - product.keys()):
            raise InvalidResultFiles(
                "final delivery is missing developer product files in files: "
                + ", ".join(sorted(omitted))
            )
        dev_versions, qa_checks = self._software_qa_evidence(active, verdicts)
        uncovered = []
        for verdict in qa_checks:
            hashes = verdict.get("checked_files", {})
            modes = verdict.get("checked_execute_bits", {})
            uncovered.append([
                path for path, digest in product.items()
                if hashes.get(path) != digest
                or (path in modes and modes[path] != inputs.execute_bits[path])
            ])
        if not uncovered or all(uncovered):
            paths = min(uncovered, key=len) if uncovered else list(product)
            versions = ", ".join(f"{task}/{version}" for task, version in dev_versions.items())
            raise InvalidResultFiles(
                "An independent QA verdict must cover the final product bytes. "
                f"Uncovered files: {', '.join(sorted(paths))}. "
                f"Current developer publications: {versions or 'none'}. "
                "Put checks used only during acceptance in acceptance_files. "
                "For actual product files, obtain QA on the submitted version."
            )

    def _accepted_specialist_verdicts(self, active):
        verdicts = {}
        for child in active:
            if child.status != "DONE" or not child.acceptance or not child.artifacts:
                continue
            verdict = next((event.payload for event in reversed(self.trace.query(child.id))
                            if event.kind == "acceptance" and event.name == "verdict"), {})
            if verdict.get("ok") is True:
                verdicts[child.id] = verdict
        return verdicts

    def _software_qa_evidence(self, active, verdicts):
        dev_versions = {
            child.id: publication["version"]
            for child in active if child.role == "dev" and child.id in verdicts
            if (publication := self.repo.published_version(child.id)) and any(
                Path(item["path"]).suffix.lower() not in {".md", ".txt"}
                for item in publication["files"]
            )
        }
        qa_checks = [
            verdicts[child.id] for child in active
            if child.role == "qa" and child.id in verdicts
            and any(verdicts[child.id].get("versions", {}).get(key) == version
                    for key, version in dev_versions.items())
            and any(check.get("kind") in {"pytest", "command"} and check.get("ok")
                    for check in verdicts[child.id].get("checks", []))
        ]
        return dev_versions, qa_checks

    def _validate_delivery(self, node: TaskNode, workspace: Path, result: TaskResult) -> None:
        """Optional teacher-adapter hook; ordinary contexts keep their contract."""
        validator = getattr(self.context, "validate_delivery", None)
        if node.parent_id is None and callable(validator):
            try:
                validator(node, workspace, result)
            except ValueError as exc:
                raise InvalidResultFiles("delivery_quality: " + str(exc)) from exc

    # -- helpers ---------------------------------------------------------
    def _opening(self, node, role, agent, deliveries, hits, *, reason="initial", qa_hint=""):
        try:
            references = collect_assignment_references(self.repo, node)
        except AssignmentReferenceError as exc:
            raise ContextAdmissionError(str(exc)) from exc
        records = self.repo.human_exchanges(node)
        options = {"memory_hits": hits}
        if records:
            # Existing custom builders without recorded questions retain their
            # old call shape. A builder must accept the new keyword to recover
            # actual answers; never silently omit mandatory clarification facts.
            options["human_exchanges"] = records
        dependencies = self._dep_reports(node, deliveries)
        if references:
            options["assignment_references"] = references
            try:
                signature(self.context.build).bind(node, role, dependencies, **options)
            except (TypeError, ValueError) as exc:
                raise ContextAdmissionError(
                    "delegated context builder must accept mandatory assignment_references"
                ) from exc
        ctx = self.context.build(node, role, dependencies, **options)
        if self.repo.targeted_recovery and qa_hint:
            ctx.messages()[0]["content"] += qa_hint
        if references:
            expected = render_assignment_references(node, references)
            messages = ctx.messages()
            if (ctx.pinned < 2 or len(messages) < 2
                    or messages[1].get("role") != "user"
                    or expected not in messages[1].get("content", "")):
                raise ContextAdmissionError(
                    "context builder omitted or changed pinned assignment references"
                )
        self.trace.record(
            task_id=node.id, kind="memory", name="memory_context_selected", agent_id=agent.id,
            role=role.name, project_id=node.project_id, conversation_id=node.conversation_id,
            retrieved_ids=[record.id for record in hits], selected_ids=list(ctx.memory_ids),
            context_reason=reason, recorded_question_count=len(records),
            assignment_reference_ids=[reference.task_id for reference in references],
            assignment_reference_sha256s=[reference.goal_sha256 for reference in references],
            interpretation="context admission only; not observed model use or reward",
        )
        return ctx

    async def _resume_human_questions(self, node, agent):
        history = getattr(self.repo, "human_exchanges", None)
        if not callable(history):
            raise HumanExchangeError("human_exchange_store_unsupported")
        records = history(node, require_answered=False)
        for record in records:
            if record["response"] is not None:
                continue
            request = HumanRequest.model_validate(record["request"])
            resume = getattr(self.human, "resume", None)
            if not callable(resume):
                raise HumanExchangeError(
                    f"pending_human_exchange_requires_resume:{request.id}"
                )
            self.repo.transition(node.id, "WAITING_HUMAN", "question:resume")
            with self.trace.span(
                task_id=node.id, kind="question", name="resume_human", agent_id=agent.id,
            ) as span:
                span.payload["request_id"] = request.id
                span.payload["original_agent_id"] = request.agent_id
                try:
                    answer = await resume(request)
                except (ValueError, TypeError, OSError) as exc:
                    raise HumanExchangeError(
                        f"human_exchange_resume_failed:{request.id}:{exc}"
                    ) from exc
                self._record_human_answer(node, request, answer)
                span.payload["recorded_response_recovered"] = True
            self.repo.transition(node.id, "RUNNING", "human:resumed")

    def _record_human_answer(self, node, request, answer):
        try:
            answer = HumanResponse.model_validate(answer)
        except (ValueError, TypeError) as exc:
            raise HumanExchangeError(f"invalid_human_response:{exc}") from exc
        if answer.request_id != request.id:
            raise HumanExchangeError(f"human_response_request_id_mismatch:{request.id}")
        self.repo.record_human_response(node, answer)
        return answer

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
            if msg.recipients and agent_id not in msg.recipients:
                return
            if msg.kind == "steer":
                self._steer[agent_id].append(msg)
            elif msg.kind == "report" and msg.meta.get("blocker") and msg.task_id:
                parent = self.repo.get(msg.task_id)
                if parent is not None and parent.status == "RUNNING":
                    self._steer[agent_id].append(msg)

        self.bus.subscribe(agent_id, callback)
        self._subscribed.add(agent_id)

    def _drain(self, agent_id: str) -> list[Message]:
        return self._steer.pop(agent_id, [])

    @staticmethod
    def _result_from(answer: str) -> TaskResult:
        return TaskResult(summary=answer or "")

    def _dep_reports(self, node: TaskNode, deliveries: dict | None = None) -> list[dict]:
        reports = []
        for dep_id in node.deps:
            dep = self.repo.get(dep_id)
            if dep is None:
                continue
            verdict = next((event.payload for event in reversed(self.trace.query(dep.id))
                            if event.kind == "acceptance" and event.name == "verdict"), None)
            delivery = (deliveries or {}).get(dep_id)
            publication = {}
            if (node.role == "release" and getattr(self.context, "required_delivery_roles", ())
                    and dep.status == "DONE" and not dep.superseded_by
                    and delivery and delivery.get("ok")):
                publication = self.repo.published_version(dep.id) or {}
                if publication.get("version") != delivery.get("version"):
                    publication = {}
            reports.append(
                {
                    "id": dep.id,
                    "parent_id": dep.parent_id,
                    "acceptance": verdict,
                    "delivery": delivery,
                    **({"publication_files": [
                        entry for entry in publication["files"]
                        if entry["path"] in delivery.get("selected_files", ())
                    ]} if publication else {}),
                    "role": dep.role,
                    "status": dep.status,
                    "blocker": dep.blocker,
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
        recipients = call.args.get("share_with_roles", [])
        allowed = getattr(self.context, "shared_requirement_roles", ())
        if (not isinstance(recipients, list)
                or any(not isinstance(name, str) or name not in allowed for name in recipients)):
            return ToolResult(ok=False, error="share_with_roles must list roles in this team")
        recipients = list(dict.fromkeys(recipients))
        topics = call.args.get("requirement_topics", [])
        allowed_topics = self._booking_topics(node)
        if (not isinstance(topics, list) or any(
            not isinstance(topic, str) or topic not in allowed_topics for topic in topics
        )):
            return ToolResult(ok=False, error="requirement_topics must name rules asked here.")
        if topics:
            recipients = list(dict.fromkeys([
                *recipients, *(role for role in allowed if role != node.role),
            ]))
        explanation = str(call.args.get("why", ""))
        if recipients:
            explanation += "\nThe answer will be shared with: " + ", ".join(recipients)

        request = HumanRequest(
            task_id=node.id,
            agent_id=agent.id,
            kind="question",
            summary=question,
            context=explanation, payload={
                **({"share_with_roles": recipients} if recipients else {}),
                **({"requirement_topics": list(dict.fromkeys(topics))} if topics else {}),
            },
            project_id=node.project_id, conversation_id=node.conversation_id, role=node.role,
        )
        # Park the task the same way an approval does, so a crash mid-question
        # is recoverable instead of leaving the task silently stuck.
        self.repo.record_human_request(node, request)
        self.repo.transition(node.id, "WAITING_HUMAN", "question")
        with self.trace.span(
            task_id=node.id, kind="question", name="ask_human", agent_id=agent.id
        ) as span:
            answer = await self.human.ask(request)
            answer = self._record_human_answer(node, request, answer)
            span.payload["answer"] = answer.text
            span.payload["request_id"] = request.id
        self.repo.transition(node.id, "RUNNING", "human:answered")
        return ToolResult(ok=True, stdout=answer.text)

    def check_handoff_recovery(
        self, node: TaskNode, deps: list[str], *, replacement_id: str | None = None,
    ) -> None:
        if not (node.blocker or {}).get("handoff", {}).get("path"):
            return
        if replacement_id and any(
            (source := self.repo.get(dep_id)) is None or source.status != "DONE" for dep_id in deps
        ):
            return  # 新生产节点发布后，由实际启动检查验证输入。
        role = self.roles.get(node.project_id, node.role)
        workspace = Path(self._instance(node, role).workspace)
        candidate = node.model_copy(update={"id": replacement_id or node.id, "deps": deps})
        try:
            input_policy = getattr(self.context, "input_policy", None)
            if input_policy is not None and replacement_id is None:
                self.context.validate_inputs(candidate, workspace, check_only=True)
            self._materialise_dependencies(candidate, workspace, check_only=True)
        except HandoffError as exc:
            raise DelegationError(f"{node.id}: unresolved handoff: {exc}") from exc

    def _materialise_dependencies(
        self, node: TaskNode, workspace: Path, *, check_only: bool = False,
    ) -> dict:
        if not self.stable_role_imports:
            git_import = getattr(self.workspace, "integrate_published", None)
            _, deliveries = self._dependency_files(node, provider_import=True)
            for source_id, receipt in deliveries.items():
                if not receipt["ok"] or not receipt["files"]:
                    continue
                dep = self.repo.get(source_id)
                live = self.workspace.allocate(dep.project_id, dep.conversation_id,
                                               f"{dep.role}@{dep.id}")
                frozen, _ = self._published_source(live, source_id)
                try:
                    outcome = (
                        git_import(live, frozen, workspace, receipt["files"])
                        if callable(git_import)
                        else self.workspace.integrate(frozen, workspace, receipt["files"])
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    receipt.update(ok=False, error=str(exc), files=[])
                else:
                    receipt.update(ok=outcome.ok, conflicts=outcome.conflicts,
                                   error=outcome.error, files=outcome.copied)
                self.trace.record(task_id=node.id, kind="task", name="materialised_from_dep",
                                  source=source_id, **receipt)
                if dep.parent_id == node.id:
                    name = "integrated_into_parent" if receipt["ok"] else "integration_conflict"
                    self.trace.record(task_id=dep.id, kind="task", name=name,
                                      parent=node.id, **receipt)
            return deliveries
        files, deliveries = self._dependency_files(node)
        if not files:
            return deliveries
        jail = LocalJail(workspace)
        receipt_key = f"workspace_import:{node.project_id}:{node.conversation_id}:{workspace}"
        imported = json.loads(self.repo.db.get_meta(receipt_key) or "{}")
        changes = []
        for relative, (data, source) in files.items():
            deliveries[source.id].setdefault("selected_files", []).append(relative)
            target = jail.resolve_artifact(relative)
            version = deliveries[source.id]["version"]
            if jail.relative(target) != relative:
                raise HandoffError(f"{source.id}/{version}: alias {relative}")
            digest = hashlib.sha256(data).hexdigest()
            existing = target.read_bytes() if target.is_file() else None
            existing_digest = hashlib.sha256(existing).hexdigest() if existing is not None else None
            wanted_bits = deliveries[source.id]["execute_bits"][relative]
            existing_bits = _execute_bits(target) if existing is not None else None
            receipt = imported.get(relative, {})
            if existing == data and existing_bits == wanted_bits:
                pass
            elif (existing is not None and receipt.get("task") == node.id
                  and receipt.get("sha256") == digest
                  and receipt.get("execute_bits", wanted_bits) == wanted_bits):
                # 恢复同一任务时保留它在原输入之上已完成的修改。
                continue
            elif ((target.exists() and existing is None)
                  or (existing is not None and source.role != node.role
                      and (receipt.get("sha256") != existing_digest
                           or receipt.get("execute_bits", existing_bits) != existing_bits))):
                raise HandoffError(
                    f"{source.id}/{version}: workspace conflict: {relative} "
                    f"(target SHA-256: {existing_digest or 'not a regular file'}; "
                    f"last import: {receipt.get('producer', 'unknown')}/"
                    f"{receipt.get('version', 'unknown')})",
                    detail={"kind": "workspace_conflict", "producer": source.id,
                            "producer_role": source.role, "version": version, "path": relative,
                            "target_sha256": existing_digest, "prior_import": receipt or None},
                )
            else:
                changes.append((relative, target, data, wanted_bits))
            imported[relative] = {"task": node.id, "sha256": digest,
                                  "execute_bits": wanted_bits, "producer": source.id,
                                  "version": version}
        if check_only:
            return deliveries
        if changes:
            stage = self._scratch_root(workspace) / "imports" / uuid.uuid4().hex
            try:
                for relative, _, data, bits in changes:
                    copy = stage / relative
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    _write_snapshot_file(copy, data, bits)
                for relative, target, _, _ in changes:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage / relative, target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        self.repo.db.set_meta(receipt_key, json.dumps(imported, ensure_ascii=False))
        recovery = (node.blocker or {}).get("base_recovery") or {}
        baseline = deliveries.get(recovery.get("base_task"))
        if baseline and baseline.get("version") == recovery.get("base_version"):
            baseline["saved_drafts"] = recovery["saved_drafts"]
        for source_id, receipt in deliveries.items():
            if receipt["ok"] and receipt["files"]:
                self.trace.record(task_id=node.id, kind="task", name="materialised_from_dep",
                                  source=source_id, **receipt)
        return deliveries

    def _dependency_files(self, node: TaskNode, *, provider_import=False) -> tuple[dict, dict]:
        files, deliveries = {}, {}
        for dep_id in node.deps:
            dep = self.repo.get(dep_id)
            if (dep is None or dep.project_id != node.project_id
                    or dep.conversation_id != node.conversation_id or dep.superseded_by):
                raise HandoffError(f"{dep_id}: dependency outside the current task scope")
            if dep.status != "DONE":
                deliveries[dep_id] = {"ok": False, "error": dep.error or dep.status}
                continue
            publication = self.repo.published_version(dep_id)
            if publication is None and dep.artifacts:
                raise HandoffError(f"{dep_id}: unknown publication version")
            version = publication["version"] if publication else None
            entries = publication["files"] if publication else []
            if publication and set(dep.artifacts) != {entry["path"] for entry in entries}:
                raise HandoffError(f"{dep_id}/{version}: incomplete publication manifest")
            deliveries[dep_id] = {"ok": True, "files": [item["path"] for item in entries],
                                  "conflicts": [], "version": version, "execute_bits": {}}
            for entry in entries:
                relative = entry["path"]
                data, bits = self._read_published_artifact(dep, version, entry)
                deliveries[dep_id]["execute_bits"][relative] = bits
                if relative in files:
                    previous, producer = files[relative]
                    if self._depends_on(producer, dep_id):
                        continue
                    previous_bits = deliveries[producer.id]["execute_bits"][relative]
                    if ((data != previous or bits != previous_bits)
                            and not self._depends_on(dep, producer.id)
                            and not provider_import):
                        raise InputConflict(relative, {
                            producer.id: deliveries[producer.id]["version"],
                            dep_id: version,
                        })
                files[relative] = (data, dep)
        return files, deliveries

    def _depends_on(self, node: TaskNode, producer: str) -> bool:
        pending, seen = list(node.deps), set()
        while pending:
            task_id = pending.pop()
            if task_id == producer:
                return True
            if task_id not in seen:
                seen.add(task_id)
                dependency = self.repo.get(task_id)
                if dependency is not None:
                    pending.extend(dependency.deps)
        return False

    def _read_published_file(self, producer: TaskNode, version: str, entry: dict) -> bytes:
        data, _ = self._read_published_artifact(producer, version, entry)
        return data

    def _read_published_artifact(
        self, producer: TaskNode, version: str, entry: dict
    ) -> tuple[bytes, int]:
        relative = entry["path"]
        origin = f"{producer.id}/{version} {relative}"
        detail = {"kind": "publication_invalid", "producer": producer.id,
                  "producer_role": producer.role, "version": version, "path": relative}
        live = self.workspace.allocate(producer.project_id, producer.conversation_id,
                                       f"{producer.role}@{producer.id}")
        source = self._scratch_root(live) / "published" / version
        try:
            jail = LocalJail(source)
            path = jail.resolve_artifact(relative)
            if jail.relative(path) != relative:
                raise ValueError("path aliases another file")
            data = path.read_bytes()
            bits = _execute_bits(path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HandoffError(
                f"{origin}: published input unavailable ({exc})", detail=detail,
            ) from exc
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise HandoffError(f"{origin}: published input hash mismatch", detail=detail)
        if bits != entry.get("execute_bits", bits):
            raise HandoffError(
                f"{origin}: published input execute permissions mismatch", detail=detail,
            )
        return data, bits

    async def _chat(self, messages, tool_specs, attempts):
        """Call the LLM, passing the attempt log only when it accepts one.

        Fake/embedded LLM implementations in tests and adapters predate the
        diagnostic parameter; they must keep working unchanged.
        """
        if getattr(self, "_chat_takes_diag", None) is None:
            try:
                params = inspect.signature(self.llm.chat).parameters
                self._chat_takes_diag = "diag" in params
            except (TypeError, ValueError):
                self._chat_takes_diag = False
        if self._chat_takes_diag:
            return await self.llm.chat(messages, tool_specs, diag=attempts)
        return await self.llm.chat(messages, tool_specs)

    def _verify_accepted_bytes(
        self, node: TaskNode, accepted_root: Path, workspace: Path,
        declared: list[str], isolated: bool,
    ) -> None:
        """The published bytes must be the bytes acceptance saw."""
        if not isolated:
            return
        for relative in declared:
            verified = accepted_root / relative
            live = workspace / relative
            try:
                same = verified.is_file() and live.is_file() and (
                    hashlib.sha256(verified.read_bytes()).hexdigest()
                    == hashlib.sha256(live.read_bytes()).hexdigest()
                    and _execute_bits(verified) == _execute_bits(live)
                )
            except OSError:
                same = False
            if not same:
                self.trace.record(
                    task_id=node.id, kind="task", name="accepted_bytes_changed",
                    path=relative,
                )
                raise InvalidResultFiles(
                    "delivery changed after acceptance: "
                    f"{relative}. Re-run the acceptance checks for the version you "
                    "are actually submitting."
                )

    async def _run_acceptance(self, node: TaskNode, root: Path, scan_root: Path | None):
        """Call the checker, passing scan_root only when it accepts one."""
        if getattr(self, "_acceptance_takes_scan", None) is None:
            try:
                params = inspect.signature(self.acceptance.check).parameters
                self._acceptance_takes_scan = (
                    "scan_root" in params
                    or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
                )
            except (TypeError, ValueError):
                self._acceptance_takes_scan = False
        if self._acceptance_takes_scan:
            return await self.acceptance.check(node, root, scan_root=scan_root)
        return await self.acceptance.check(node, root)

    @staticmethod
    def _scratch_root(workspace: Path) -> Path:
        """Framework-managed area for one role's workspace.

        Kept beside the workspace rather than inside it so a git-backed working
        tree stays clean without exclude tricks.
        """
        anchor = workspace
        while anchor.parent != anchor and anchor.name != "workspaces":
            anchor = anchor.parent
        base = anchor if anchor.name == "workspaces" else workspace.parent
        return base / ".framework" / workspace.name

    def _acceptance_inputs(
        self, node: TaskNode, workspace: Path, result: TaskResult, *, dependency_files=None
    ) -> AcceptanceInputs:
        """读取已声明文件和上游发布版本，生成本次验收的文件集合。"""
        provider_import = not self.stable_role_imports
        files, receipts = (dependency_files if dependency_files is not None
                           else self._dependency_files(node, provider_import=provider_import))
        jail = LocalJail(workspace)
        upstream = files
        bits = {
            path: receipts[source.id]["execute_bits"][path]
            for path, (_, source) in files.items()
        }
        if provider_import:
            upstream = {}
            for path, (_, producer) in files.items():
                integrated = jail.resolve_artifact(path)
                if integrated.is_file():
                    upstream[path] = (integrated.read_bytes(), producer)
                    bits[path] = _execute_bits(integrated)
                else:
                    bits.pop(path)
        declared = _declared_artifacts(workspace, result.files)
        try:
            helpers = _declared_artifacts(workspace, result.acceptance_files)
        except InvalidResultFiles as exc:
            raise InvalidResultFiles(f"acceptance_files: {exc}") from exc
        isolated = bool(upstream or (
            self.stable_role_imports and self.repo.targeted_recovery and node.acceptance
        ))
        editable = set(declared) - set(helpers)
        if getattr(self.context, "required_delivery_roles", ()) and node.role != "dev":
            base = self._previous_teacher_root()
            editable = {
                path for path in editable
                if node.parent_id and path in upstream and upstream[path][1].id == base
                and Path(path).suffix.lower() in {".md", ".txt"}
            }
        own = {}
        for relative in dict.fromkeys([*declared, *helpers]):
            path = jail.resolve_artifact(relative)
            data = path.read_bytes()
            mode = _execute_bits(path)
            if (relative in upstream and relative not in editable
                    and (upstream[relative][0] != data or bits[relative] != mode)):
                _, producer = upstream[relative]
                raise InvalidResultFiles(
                    f"{producer.id}/{receipts[producer.id]['version']}: "
                    f"acceptance input conflicts with current files: {relative}"
                )
            own[relative] = data
            bits[relative] = mode
        if isolated:
            paths = set(bits)
            for relative in sorted(paths):
                for parent in Path(relative).parents:
                    if parent.as_posix() in paths:
                        raise InvalidResultFiles(
                            f"acceptance input path conflict: {parent} is a file "
                            f"and a parent directory of {relative}"
                        )
            for item in node.acceptance:
                entrypoint = command_entrypoint(item.target) if item.kind == "command" else None
                if (entrypoint is None or Path(entrypoint).is_absolute()
                        or PureWindowsPath(entrypoint).anchor):
                    continue
                try:
                    script = jail.resolve_artifact(entrypoint)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise InvalidResultFiles(f"invalid acceptance command path {entrypoint}: "
                                             f"{exc}") from exc
                if not script.is_file():
                    continue
                relative = jail.relative(script)
                if relative != Path(os.path.normpath(entrypoint)).as_posix():
                    raise InvalidResultFiles(
                        f"acceptance command references the alias {entrypoint}; "
                        f"use the declared file path {relative}"
                    )
                if relative not in paths:
                    raise InvalidResultFiles(
                        f"acceptance command references {relative}, which is absent from the "
                        "acceptance inputs. Declare it in acceptance_files if it is a check "
                        "helper, or files if it is a deliverable."
                    )
        return AcceptanceInputs(declared, own, upstream, receipts, bits, isolated)

    def _prepare_acceptance_root(
        self, node: TaskNode, workspace: Path, result: TaskResult, *, inputs=None
    ) -> tuple[Path, bool]:
        if inputs is None:
            inputs = self._acceptance_inputs(node, workspace, result)
        if not inputs.isolated:
            return workspace, False

        root = self._scratch_root(workspace) / "acceptance" / uuid.uuid4().hex[:12]
        root.mkdir(parents=True)
        upstream = {path: data for path, (data, _) in inputs.upstream.items()}
        for relative, data in (upstream | inputs.own).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_snapshot_file(target, data, inputs.execute_bits[relative])

        self.trace.record(
            task_id=node.id, kind="task", name="acceptance_root_prepared",
            root=str(root),
            upstream=sorted(upstream), own=sorted(inputs.own),
            versions={key: item["version"] for key, item in inputs.receipts.items() if item["ok"]},
        )
        return root, True

    def _published_source(self, live: Path, task_id: str) -> tuple[Path, str | None]:
        """定位发布目录。需要正式文件时，缺少记录或目录均由调用者报告。"""
        published = self.repo.published_version(task_id)
        if published:
            frozen = self._scratch_root(live) / "published" / str(published["version"])
            return frozen, str(published["version"])
        raise HandoffError(f"{task_id}: unknown publication version")

    def _publish_version(self, node: TaskNode, workspace: Path, declared: list[str],
                         *, source: Path | None = None) -> str:
        """根据实际通过验收的文件封存不可变版本。"""
        jail = LocalJail(source or workspace)
        files = {path: jail.resolve_artifact(path).read_bytes() for path in declared}
        bits = {path: _execute_bits(jail.resolve_artifact(path)) for path in files}
        entries = [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(),
             "bytes": len(data), "execute_bits": bits[path]}
            for path, data in files.items()
        ]

        fingerprint = "\n".join(f"{e['path']}:{e['sha256']}:{e['execute_bits']}" for e in sorted(
            entries, key=lambda item: item["path"]))
        version = hashlib.sha256(fingerprint.encode()).hexdigest()[:16] if entries else "empty"

        root = self._scratch_root(workspace) / "published" / version
        if root.is_dir():
            frozen = LocalJail(root)
            if any(frozen.resolve_artifact(path).read_bytes() != data
                   or _execute_bits(frozen.resolve_artifact(path)) != bits[path]
                   for path, data in files.items()):
                raise HandoffError(f"{node.id}/{version}: existing snapshot was altered")
        elif entries:
            stage = root.with_name(".pending-" + uuid.uuid4().hex)
            try:
                for path, data in files.items():
                    target = stage / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _write_snapshot_file(target, data, bits[path])
                stage.replace(root)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

        self.repo.publish_version(node.id, version, entries)
        self.trace.record(
            task_id=node.id, kind="task", name="published_version",
            version=version, files=entries,
        )
        return version

    def _warn_undeclared(self, node: TaskNode, declared: list[str]) -> str | None:
        """Tell the submitter when a delivered file was left out of the manifest.

        A warning, never a failure: the declared list is the contract, but a
        silently dropped deliverable is the expensive kind of mistake, so it is
        surfaced once, de-duplicated and with a bounded preview.
        """
        written = self._tool_writes.pop(node.id, set())
        if not written:
            return None
        def _norm(value: str) -> str:
            # Strip at most one leading "./"; str.lstrip("./") would eat the
            # dot of a dotfile such as ".evaluation/...".
            return value[2:] if value.startswith("./") else value

        declared_set = {_norm(item) for item in declared}
        missing = []
        for path in sorted(written):
            normalized = _norm(path)
            if normalized in declared_set or normalized.startswith(".evaluation/"):
                continue
            missing.append(normalized)
        if not missing:
            return None
        preview = missing[:8]
        self.trace.record(
            task_id=node.id,
            kind="task",
            name="undeclared_files_warning",
            count=len(missing),
            preview=preview,
            note=(
                "these paths were written by tools and are absent from both result file lists; "
                "only declared files are published or handed to another role"
            ),
        )
        return (
            f"[delivery] {len(missing)} file(s) written by tools are not in your "
            f"declared files: {', '.join(preview)}"
            + (" ..." if len(missing) > len(preview) else "")
            + ". Only declared files are published - add them to submit_result.files "
              "if they are meant to reach another role."
        )

    def _tool_result_chars(self) -> int:
        """First-hop body cap from the run's context policy."""
        policy = getattr(self.compactor, "policy", None)
        return getattr(policy, "max_tool_result_chars", 4_000)

    def _ensure_request_fits(
        self, node, role, agent, ctx, tool_specs: list[dict] | None, *, step: int | None = None
    ) -> int:
        """对完整请求执行一次压缩及发送检查，保留固定开头和近期回合。"""
        policy = self.compactor.policy
        cap = policy.max_request_tokens
        trigger = min(policy.compact_trigger_tokens, cap)
        parts = self.budget.split_request(ctx.messages(), tool_specs)
        if parts["total_tokens"] <= trigger:
            return parts["total_tokens"]

        target = policy.compact_target_tokens
        started = monotonic()
        result = self.compactor.compact_to(
            ctx.messages(), ctx.pinned,
            target=max(1, target - parts["tools_tokens"]), preserve_recent=True,
        ) if parts["tools_tokens"] < cap else None
        after = self.budget.split_request(ctx.messages(), tool_specs)
        applied = result is not None and result.applied
        if applied:
            ctx.compactions += 1
        self.trace.record(
            task_id=node.id, kind="memory", name="compacted" if applied else "compaction_skipped",
            agent_id=agent.id, role=role.name, step=step, cap_tokens=cap,
            trigger_tokens=trigger, target_tokens=target,
            trigger="send_cap" if parts["total_tokens"] > cap else "threshold",
            duration_ms=round((monotonic() - started) * 1_000, 3),
            layer=result.layer if result is not None else None,
            dropped_turns=result.dropped_turns if result is not None else 0,
            before=parts["total_tokens"], after=after["total_tokens"],
            parts_before=parts, parts_after=after, target_met=after["total_tokens"] <= target,
        )
        if after["total_tokens"] > cap:
            raise ContextCapacityError({
                "task": node.id, "role": role.name,
                "total": after["total_tokens"], "cap": cap,
                "messages": after["messages_tokens"], "tools": after["tools_tokens"],
                "tool_results": after["tool_result_tokens"],
            })
        return after["total_tokens"]

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


def _explicit_plan_deps_schema(tool_specs: list[dict]) -> None:
    """Require explicit model inputs without changing Python Plan defaults.

    ToolRegistry produces fresh schema dictionaries for each subset call, so
    this adjusts only the copy sent to this model, never registered tool state.
    """
    for spec in tool_specs:
        function = spec.get("function", {})
        if function.get("name") != "submit_plan":
            continue
        item = (function.get("parameters", {}).get("properties", {})
                .get("subtasks", {}).get("items", {}))
        deps = item.get("properties", {}).get("deps")
        if not isinstance(deps, dict):
            continue
        item["required"] = list(dict.fromkeys([*item.get("required", []), "deps"]))
        deps["description"] = (
            "Use new sibling keys, retained direct child IDs or authorized inherited IDs. "
            "Supply [] only for independent work. A filename in prose creates no handoff."
        )


def _check_explicit_plan_deps(payload: dict) -> None:
    """Reject omitted tool-call edges before role or child creation.

    Direct Python Plan/Delegation callers retain their default-empty contract.
    No dependency is inferred from prose and no existing DAG is rewritten.
    """
    subtasks = payload.get("subtasks") if isinstance(payload, dict) else None
    if not isinstance(subtasks, list):
        return  # Existing Plan validation reports malformed payload structure.
    missing = [str(spec.get("key", index)) for index, spec in enumerate(subtasks)
               if isinstance(spec, dict) and "deps" not in spec]
    if missing:
        raise DelegationError(
            f"subtasks {missing} must explicitly include deps; supply [] only for "
            "independent work, or list the authorized dependencies for required inputs. "
            "Mentioning a filename in a goal does not create a dependency or file handoff."
        )


def _declared_artifacts(workspace: Path, files: list[str]) -> list[str]:
    if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
        raise InvalidResultFiles("files must be a list of relative file paths")
    if not files:
        return []
    try:
        jail = LocalJail(workspace)
        declared = []
        for path in files:
            if Path(path).is_absolute() or PureWindowsPath(path).anchor:
                raise ValueError(f"absolute or drive-qualified path is not allowed: {path!r}")
            resolved = jail.resolve_artifact(path)
            if not resolved.is_file():
                raise ValueError(f"declared path is not an existing file: {path!r}")
            declared.append(jail.relative(resolved))
        return list(dict.fromkeys(declared))
    except (OSError, ValueError, RuntimeError) as exc:
        raise InvalidResultFiles(str(exc)) from exc


def _coerce_result(args: dict) -> TaskResult:
    """返回模型可修正的字段错误。"""
    try:
        return TaskResult.model_validate(args)
    except ValidationError as exc:
        details = [
            f"{'.'.join(map(str, error['loc'])) or 'submit_result'}: {error['msg']}"
            for error in exc.errors()[:8]
        ]
        raise InvalidResultFiles("Invalid result fields: " + " | ".join(details)) from exc


def _execute_bits(path: Path) -> int:
    return path.stat().st_mode & 0o111


def _write_snapshot_file(path: Path, data: bytes, execute_bits: int) -> None:
    path.write_bytes(data)
    if execute_bits:
        path.chmod(path.stat().st_mode | execute_bits)
