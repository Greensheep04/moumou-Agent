"""The facade. Everything outside ``orchestrator`` talks to this and nothing else."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, Field

from moumou.adapters import AutoApproveHuman, InMemoryBus, LocalDirs
from moumou.context import Compactor, ConclusionBoard, ContextBuilder, TokenBudget
from moumou.contracts import (
    HUMAN_ID,
    Budget,
    Channel,
    ContextPolicy,
    Message,
    RoleSpec,
    TaskNode,
)
from moumou.memory import Evolution, Reflection, Retriever, SqliteMemoryStore, memory_scope
from moumou.memory import near_duplicate as _near_duplicate
from moumou.orchestrator.acceptance import AcceptanceChecker
from moumou.orchestrator.delegation import Delegation
from moumou.orchestrator.gate import ApprovalPolicy, Gate
from moumou.orchestrator.revision import RoleReviser
from moumou.orchestrator.scheduler import Scheduler
from moumou.orchestrator.state import Database, SqliteRoleRegistry, SqliteTaskRepo
from moumou.orchestrator.worker import TaskRunner
from moumou.prompts import LEAD_ROLE
from moumou.tools import ToolRegistry, default_tools
from moumou.trace import Recorder

BOOTSTRAP_ROLE = LEAD_ROLE


class Settings(BaseModel):
    project_id: str = "default"
    conversation_id: str = "default"
    root: Path = Path("runs")
    max_slots: int = 4
    max_rounds: int = 500
    approval: dict[str, bool] = Field(default_factory=dict)
    workspace_execution_root: Path | None = None
    require_subtask_acceptance: bool = Field(
        default=False,
        description="Reject delegated subtasks without machine acceptance checks; opt-in.",
    )
    execution_progress_enabled: bool = False

    context_window: int = 1_000_000
    max_output_tokens: int = 384_000
    compact_trigger_tokens: int = 600_000
    compact_target_tokens: int = 256_000
    max_request_tokens: int = 300_000
    max_tool_result_chars: int = 4_000
    memory_enabled: bool = True

    role_review_enabled: bool = True
    role_review_every: int = 3          # finished tasks between reviews
    role_review_max_rounds: int = 3     # proposals per review before giving up


class RunReport(BaseModel):
    project_id: str
    wall_s: float
    total: int
    done: int
    dead: int
    unfinished: int
    completion_rate: float
    tokens_in: int
    tokens_out: int
    tasks: list[dict]


class Engine:
    def __init__(
        self,
        llm,
        *,
        human=None,
        bus: Channel | None = None,
        workspace=None,
        tools: ToolRegistry | None = None,
        settings: Settings | None = None,
        memory=None,
    ):
        self.settings = settings or Settings()
        self.llm = llm
        root = Path(self.settings.root)
        root.mkdir(parents=True, exist_ok=True)

        self.db = Database(root / "moumou.db")
        self.repo = SqliteTaskRepo(self.db)
        self.roles = SqliteRoleRegistry(self.db)
        try:
            self.trace = Recorder(root / "trace.jsonl", sqlite_path=root / "trace.db")
        except BaseException:
            self.db.close()
            raise

        self.bus = bus or InMemoryBus()
        self.human = human or AutoApproveHuman()
        self.workspace = workspace or LocalDirs(root / "workspaces")

        self.tools = tools or ToolRegistry()
        if not self.tools.names():
            self.tools.register_all(*default_tools())

        self.gate = Gate(
            self.repo, self.tools, self.human, self.trace, ApprovalPolicy(self.settings.approval),
            execution_root=self.settings.workspace_execution_root,
        )
        self.delegation = Delegation(
            self.repo, self.roles, self.trace, self.tools.names(),
            require_subtask_acceptance=self.settings.require_subtask_acceptance,
        )
        self.acceptance = AcceptanceChecker(
            self.trace, subtree_verdict=self._subtree_verdict,
            execution_root=self.settings.workspace_execution_root,
        )
        self.policy = ContextPolicy(
            context_window=self.settings.context_window,
            max_output_tokens=self.settings.max_output_tokens,
            compact_trigger_tokens=self.settings.compact_trigger_tokens,
            compact_target_tokens=self.settings.compact_target_tokens,
            max_request_tokens=self.settings.max_request_tokens,
            max_tool_result_chars=self.settings.max_tool_result_chars,
        )
        self.budget = TokenBudget(
            correction=float(self.db.get_meta("token_correction", "1.0") or 1.0)
        )
        self.compactor = Compactor(self.budget, self.policy)
        self.board = ConclusionBoard(self.db)

        self._owns_memory = memory is None
        self.memory = memory or SqliteMemoryStore(root / "memory.db")
        self.retriever = Retriever(self.memory, self.policy)
        self.evolution = Evolution(self.memory, self.policy, self.trace)
        self.reflection = Reflection(
            llm, self.memory, self.policy, self.trace, is_duplicate=_near_duplicate
        )
        self.reviser = RoleReviser(
            llm,
            self.roles,
            self.human,
            self.trace,
            max_rounds=self.settings.role_review_max_rounds,
        )

        self.context = ContextBuilder(self.policy, self.budget, board=self.board)

        self.runner = TaskRunner(
            repo=self.repo,
            roles=self.roles,
            llm=llm,
            tools=self.tools,
            gate=self.gate,
            context=self.context,
            trace=self.trace,
            bus=self.bus,
            acceptance=self.acceptance,
            delegation=self.delegation,
            workspace=self.workspace,
            human=self.human,
            compactor=self.compactor,
            budget=self.budget,
            retriever=self.retriever,
            memory_enabled=self.settings.memory_enabled,
            execution_progress_enabled=self.settings.execution_progress_enabled,
        )
        self.scheduler = Scheduler(
            self.repo,
            self.runner,
            self.trace,
            self.settings.max_slots,
            self.settings.max_rounds,
            on_terminal=self._on_terminal,
        )
        self.background_reflection = False
        self._reflections: set[asyncio.Task] = set()

    # -- submitting work -------------------------------------------------
    def _subtree_verdict(self, root_id: str) -> tuple[bool, dict]:
        """Aggregate the machine evidence of a root's subtree into one verdict.

        A root that declares ``subtree_accepted`` is no longer graded on its own
        self-report: it passes only when every descendant finished and every
        descendant that declared criteria actually passed them.
        """
        descendants = self.repo.active_descendants(root_id)
        unfinished = sorted(t.id for t in descendants if t.status != "DONE")
        checked = [t for t in descendants if t.acceptance]
        failed: list[str] = []
        missing: list[str] = []
        for task in checked:
            verdicts = [
                event for event in self.trace.query(task.id)
                if event.kind == "acceptance" and event.name == "verdict"
            ]
            if not verdicts:
                missing.append(task.id)
            elif verdicts[-1].payload.get("ok") is not True:
                failed.append(task.id)
        detail = {
            "descendants": len(descendants),
            "checked_descendants": len(checked),
            "unfinished": unfinished,
            "failed_verdicts": failed,
            "missing_verdicts": missing,
        }
        if not descendants:
            return False, {**detail, "error": "root has no subtasks to aggregate"}
        if unfinished:
            return False, {**detail, "error": "descendants did not finish"}
        if not checked:
            return False, {**detail, "error": "no descendant declared machine criteria"}
        if failed or missing:
            return False, detail
        return True, detail

    def submit(
        self,
        goal: str,
        *,
        acceptance: list | None = None,
        budget: Budget | None = None,
    ) -> TaskNode:
        self._ensure_bootstrap_role()
        seq = self.repo.count_root_tasks() + 1
        node = TaskNode(
            id=f"t{seq}",
            project_id=self.settings.project_id,
            conversation_id=self.settings.conversation_id,
            goal=goal,
            role=BOOTSTRAP_ROLE,
            acceptance=acceptance or [],
            budget=budget or Budget(),
        )
        self.repo.add(node)
        self.repo.transition(node.id, "READY", "submitted")
        return self.repo.get(node.id)

    def _ensure_bootstrap_role(self) -> None:
        if self.roles.get(self.settings.project_id, BOOTSTRAP_ROLE) is not None:
            return
        self.roles.create(
            RoleSpec(
                name=BOOTSTRAP_ROLE,
                title="Task lead",
                description=(
                    "Leads the task: decides how to split it, delegates, integrates "
                    "and verifies."
                ),
                tools=sorted(self.tools.names()),
                created_by="human",
                reason="bootstrap role: someone has to receive work from the human",
                project_id=self.settings.project_id,
            )
        )

    # -- running ---------------------------------------------------------
    async def run(self) -> RunReport:
        self._recover()
        started_dt = datetime.now(UTC)
        started = monotonic()
        try:
            await self.scheduler.run()
            if self._reflections:
                wait_s = max(0.0, min(8.0, self.runner.execution_remaining() - 1))
                await asyncio.wait(self._reflections, timeout=wait_s)
        finally:
            pending = tuple(self._reflections)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        wall = monotonic() - started

        tasks = self.repo.all_tasks()
        ids = {t.id for t in tasks}
        tokens_in = tokens_out = 0
        for event in self.trace.all_events():
            if event.task_id in ids:
                tokens_in += event.tokens_in
                tokens_out += event.tokens_out

        self.db.set_meta("token_correction", str(round(self.budget.correction, 4)))
        self._maintain(tasks, started_dt)
        await self._maybe_review_roles(tasks, started_dt)

        done = sum(1 for t in tasks if t.status == "DONE")
        dead = sum(1 for t in tasks if t.status == "DEAD")
        return RunReport(
            project_id=self.settings.project_id,
            wall_s=round(wall, 3),
            total=len(tasks),
            done=done,
            dead=dead,
            unfinished=len(tasks) - done - dead,
            completion_rate=round(done / len(tasks), 3) if tasks else 0.0,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tasks=[
                {
                    "id": t.id,
                    "parent_id": t.parent_id,
                    "depth": t.depth,
                    "role": t.role,
                    "status": t.status,
                    "retries": t.retries,
                    "goal": t.goal,
                    "artifacts": t.artifacts,
                }
                for t in tasks
            ],
        )

    async def _on_terminal(self, node: TaskNode) -> None:
        """Hand-off note, memory scoring and reflection. None of it is fatal."""
        try:
            self.board.write(node)
            self.trace.record(
                task_id=node.id, kind="memory", name="board_written", outcome=node.status
            )
        except Exception as exc:      # the task is already terminal; do not disturb it
            self.trace.record(
                task_id=node.id,
                kind="memory",
                name="board_write_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        if not self.settings.memory_enabled:
            return

        used = self.runner.citations.get(node.id, [])
        try:
            if used:
                self.evolution.record_use(used, success=node.status == "DONE")

            steps = sum(1 for event in self.trace.query(node.id) if event.kind == "llm")
            if self.reflection.should_reflect(node, steps):
                role = self.roles.get(node.project_id, node.role)
                if role is not None:
                    if self.background_reflection:
                        task = asyncio.create_task(self._reflect_in_background(node, role, steps))
                        self._reflections.add(task)
                        task.add_done_callback(self._reflections.discard)
                    else:
                        await self.reflection.reflect(node, role, steps=steps)
        except Exception as exc:
            self.trace.record(
                task_id=node.id,
                kind="memory",
                name="reflection_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _reflect_in_background(self, node, role, steps):
        with self.trace.span(task_id=node.id, kind="memory", name="background_reflection"):
            try:
                await self.reflection.reflect(node, role, steps=steps)
            except Exception as exc:
                self.trace.record(task_id=node.id, kind="memory", name="reflection_failed",
                                  error=f"{type(exc).__name__}: {exc}")

    async def _maybe_review_roles(self, tasks: list[TaskNode], run_started) -> None:
        """Ask for one role improvement every N finished tasks, never mid-run."""
        if not self.settings.role_review_enabled:
            return

        finished = [t for t in tasks if t.is_terminal and t.updated_at >= run_started]
        pending = int(self.db.get_meta("tasks_since_review", "0") or 0) + len(finished)
        if pending < self.settings.role_review_every:
            self.db.set_meta("tasks_since_review", str(pending))
            return

        self.db.set_meta("tasks_since_review", "0")
        try:
            await self.reviser.review(self.settings.project_id, finished)
        except Exception as exc:
            self.trace.record(
                task_id="-",
                kind="role",
                name="revision:failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _maintain(self, tasks: list[TaskNode], run_started) -> None:
        """Decay what went unused this run, then retire what has faded."""
        if not self.settings.memory_enabled or self.memory is None:
            return
        try:
            used = {i for ids in self.runner.citations.values() for i in ids}
            for scope in {memory_scope(t.project_id, t.role) for t in tasks}:
                self.evolution.decay_scope(scope, used, skip_created_after=run_started)
                self.evolution.evict(scope)
            hard_delete = getattr(self.memory, "hard_delete_expired", None)
            if callable(hard_delete):
                hard_delete(30)
        except Exception as exc:
            self.trace.record(
                task_id="-", kind="memory", name="maintenance_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _recover(self) -> None:
        """Anything left RUNNING died with the previous process."""
        for node in self.repo.by_status("RUNNING"):
            self.repo.transition(node.id, "FAILED", "crashed_recovery")
        # A task parked on a human died with the question it was waiting for;
        # re-run it rather than leaving it stuck forever.
        for node in self.repo.by_status("WAITING_HUMAN"):
            self.repo.transition(node.id, "FAILED", "crashed_while_waiting")
        for node in self.repo.by_status("FAILED"):
            if self.repo.targeted_recovery or node.recovery_used:
                self.repo.transition(node.id, "DEAD", "node_failure_requires_repair")
            elif node.retries < node.budget.max_retries:
                self.repo.bump_retries(node.id)
                self.repo.transition(node.id, "READY", "recovered")
            else:
                self.repo.transition(node.id, "DEAD", "retries_exhausted")

    async def steer(self, text: str, *, recipients: list[str] | None = None) -> None:
        """Nudge a running task. Picked up by the agent at its next step boundary."""
        await self.bus.publish(
            Message(
                project_id=self.settings.project_id,
                conversation_id=self.settings.conversation_id,
                sender=HUMAN_ID,
                recipients=recipients or [],
                kind="steer",
                content=text,
            )
        )

    def abort(self) -> None:
        self.scheduler.abort()
        for task in self._reflections:
            task.cancel()

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.trace.close()
        if self._owns_memory and self.memory is not None:
            self.memory.close()
        self.db.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
