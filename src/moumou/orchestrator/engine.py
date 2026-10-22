"""The facade. Everything outside ``orchestrator`` talks to this and nothing else."""

from __future__ import annotations

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
from moumou.orchestrator.scheduler import Scheduler
from moumou.orchestrator.state import Database, SqliteRoleRegistry, SqliteTaskRepo
from moumou.orchestrator.worker import TaskRunner
from moumou.tools import ToolRegistry, default_tools
from moumou.trace import Recorder

BOOTSTRAP_ROLE = "planner"

PLANNER_PROMPT = (
    "You receive work from a human and you own the outcome. "
    "First decide if the job is small enough to do yourself. If it is not, call "
    "submit_plan to split it into sub-tasks for specialists you define. "
    "Each sub-task goal must be self-contained: a specialist sees only that goal, "
    "the acceptance criteria, and the reports of its own dependencies. "
    "Prefer declaring acceptance criteria that can be executed (a command, a file "
    "existence check) over vague ones."
)


class Settings(BaseModel):
    project_id: str = "default"
    conversation_id: str = "default"
    root: Path = Path("runs")
    max_slots: int = 4
    max_rounds: int = 500
    approval: dict[str, bool] = Field(default_factory=dict)

    context_window: int = 1_000_000
    max_output_tokens: int = 384_000
    compact_trigger_tokens: int = 600_000
    compact_target_tokens: int = 256_000
    memory_enabled: bool = True


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
        root = Path(self.settings.root)
        root.mkdir(parents=True, exist_ok=True)

        self.db = Database(root / "moumou.db")
        self.repo = SqliteTaskRepo(self.db)
        self.roles = SqliteRoleRegistry(self.db)
        self.trace = Recorder(root / "trace.jsonl")

        self.bus = bus or InMemoryBus()
        self.human = human or AutoApproveHuman()
        self.workspace = workspace or LocalDirs(root / "workspaces")

        self.tools = tools or ToolRegistry()
        if not self.tools.names():
            self.tools.register_all(*default_tools())

        self.gate = Gate(
            self.repo, self.tools, self.human, self.trace, ApprovalPolicy(self.settings.approval)
        )
        self.delegation = Delegation(self.repo, self.roles, self.trace, self.tools.names())
        self.acceptance = AcceptanceChecker(self.trace)
        self.policy = ContextPolicy(
            context_window=self.settings.context_window,
            max_output_tokens=self.settings.max_output_tokens,
            compact_trigger_tokens=self.settings.compact_trigger_tokens,
            compact_target_tokens=self.settings.compact_target_tokens,
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

        self.context = ContextBuilder(
            self.policy, self.budget, self.memory, board=self.board
        )

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
        )
        self.scheduler = Scheduler(
            self.repo,
            self.runner,
            self.trace,
            self.settings.max_slots,
            self.settings.max_rounds,
            on_terminal=self._on_terminal,
        )

    # -- submitting work -------------------------------------------------
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
                description=PLANNER_PROMPT,
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
        await self.scheduler.run()
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
                    await self.reflection.reflect(node, role, steps=steps)
        except Exception as exc:
            self.trace.record(
                task_id=node.id,
                kind="memory",
                name="reflection_failed",
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
            if node.retries < node.budget.max_retries:
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
