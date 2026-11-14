"""Fair, sequential four-group experiments with independently warmed memory.

Warmup is a separate, explicitly labelled Engine execution. Only its long-term
MemoryRecord objects cross into held-out attempts; task state, workspaces and
the conclusion board never cross that boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.eval.artifacts import (
    _code_version,
    _model_metadata,
    atomic_json,
    atomic_text,
    redact,
    utc_now,
)
from moumou.eval.loader import TaskFixture
from moumou.eval.options import EvaluationOptions, memory_snapshot, memory_snapshot_sha256
from moumou.eval.telemetry import EvalLLM
from moumou.eval.workspace import reserve_batch_dir, write_setup
from moumou.memory import memory_scope
from moumou.orchestrator import Engine, Settings
from moumou.prompts import LEAD_ROLE

GROUPS = (
    ("memory_on_parallel_on", True, 4),
    ("memory_on_parallel_off", True, 1),
    ("memory_off_parallel_on", False, 4),
    ("memory_off_parallel_off", False, 1),
)
PROTOCOL_VERSION = "long-term-memory-and-parallel-v1"


def plan_ablation(fixtures: list[TaskFixture], repeats: int = 3) -> dict:
    """Build the fixed, interleaved schedule without making any model calls."""
    from moumou.eval.runner import _validate_batch

    if type(repeats) is not int or repeats < 1:
        raise ValueError("ablation repeats must be a positive integer")
    _validate_batch(fixtures, repeats, "local", None)
    schedule = []
    for repeat in range(repeats):
        for fixture_index, fixture in enumerate(fixtures):
            offset = (repeat + fixture_index) % len(GROUPS)
            order = GROUPS[offset:] + GROUPS[:offset]
            for name, memory_enabled, slots in order:
                group_index = next(index for index, group in enumerate(GROUPS) if group[0] == name)
                schedule.append({
                    "fixture_id": fixture.id, "repeat": repeat, "group": name,
                    "fixture_index": fixture_index, "group_index": group_index,
                    "relative_dir": f"heldout/r{repeat}/f{fixture_index}/g{group_index}",
                    "memory_enabled": memory_enabled, "max_slots": slots,
                    "role_review_enabled": False,
                })
    return {
        "protocol_version": PROTOCOL_VERSION,
        "repeats": repeats,
        "planned_runs": len(schedule),
        "harness_concurrency": 1,
        "path_layout": "compact-indexed-v1; fixture/group identities are in schedule entries",
        "group_order_policy": "rotate four fixed groups by repeat plus fixture position",
        "groups": [
            {"name": name, "memory_enabled": memory, "max_slots": slots,
             "role_review_enabled": False}
            for name, memory, slots in GROUPS
        ],
        "schedule": schedule,
    }


def _check_path_budget(root, fixtures, plan, workspace_backend):
    """Reject already-known MAX_PATH failures before allocating a paid client.

    This conservative native-Windows policy checks declared files and the
    planner workspace. A model's future role names and output names are not
    statically known; this is not a promise that arbitrary generated paths fit.
    """
    root = Path(root).resolve()
    checks = [(root / "ablation.json", False)]
    by_id = {fixture.id: fixture for fixture in fixtures}

    def declared_paths(workspace, spec):
        checks.append((workspace, True))
        names = set(spec.setup)
        names.update(check.target for check in spec.acceptance if check.kind in {
            "file_exists", "file_not_exists", "file_contains", "json_key",
        })
        for name in names:
            checks.append((workspace / name.replace("\\", "/"), False))

    seen_warmups = set()
    for row in plan["schedule"]:
        fixture = by_id[row["fixture_id"]]
        project = re.sub(r"[^a-zA-Z0-9._@#-]", "_", fixture.id) or "_"
        batch = root / row["relative_dir"]
        run = batch / fixture.id / "r0"
        for filename in ("result.json", "llm-calls.json", "trace.jsonl", "moumou.db"):
            checks.append((run / filename, False))
        checks.append((batch / ".workers" / fixture.id / "r0" / "candidate.json", False))
        conversation = f"h{row['repeat']}-g{row['group_index']}"
        workspace_root = run / "workspaces"
        workspace = (
            workspace_root / "worktrees" / ("0" * 24) if workspace_backend == "git"
            else workspace_root / project / conversation / f"{LEAD_ROLE}@t1"
        )
        declared_paths(workspace, fixture)
        if workspace_backend == "git":
            declared_paths(run / "seed", fixture)
        key = (row["repeat"], row["fixture_index"])
        if fixture.warmup is not None and key not in seen_warmups:
            seen_warmups.add(key)
            warmup = root / "warmups" / f"r{row['repeat']}" / f"f{row['fixture_index']}"
            checks.append((warmup / "memory-snapshot.json", False))
            declared_paths(
                warmup / "workspaces" / project / f"w{row['repeat']}" / f"{LEAD_ROLE}@t1",
                fixture.warmup,
            )
    longest = 0
    for path, directory in checks:
        units = len(str(path).encode("utf-16-le")) // 2
        longest = max(longest, units)
        limit = 247 if directory else 259
        if sys.platform == "win32" and units > limit:
            raise ValueError(
                f"ablation output root is too long for portable Windows paths "
                f"({units} UTF-16 units; limit {limit}); use a shorter --out directory"
            )
    return {
        "native_windows": sys.platform == "win32", "max_known_path_utf16_units": longest,
        "file_limit": 259, "directory_limit": 247,
        "scope": "declared files, planner workspace and harness metadata; generated paths unknown",
    }


async def _close_client(client) -> None:
    close = getattr(client, "aclose", None)
    if callable(close):
        value = close()
        if inspect.isawaitable(value):
            await value


async def _warmup(fixture, client, root, repeat):
    """Run disjoint training, then export only real planner experience."""
    root.mkdir(parents=True, exist_ok=False)
    warmup_id = f"{fixture.id}-r{repeat}-warmup"
    meter = EvalLLM(client, root / "llm-calls.json", phase="warmup")
    settings = Settings(
        project_id=fixture.id, conversation_id=f"w{repeat}", root=root,
        max_slots=4, memory_enabled=True, role_review_enabled=False,
    )
    spec = fixture.warmup
    started = monotonic()
    metadata = {
        "schema_version": 1, "warmup_id": warmup_id,
        "fixture_id": fixture.id, "repeat": repeat,
        "status": "running", "started_at": utc_now(),
        "execution_mode": "in_process", "heldout_hard_timeout_applies": False,
        "cooperative_timeout_s": spec.budget.max_wall_s,
        "timeout_limit": "cooperative awaits only; synchronous calls cannot be forcibly stopped",
        "model": _model_metadata(client),
        "source": spec.model_dump(mode="json"),
        "settings": settings.model_dump(mode="json", exclude={"root"}),
        "copied_state": "MemoryRecord objects only; no roles, tasks, board or workspace",
        "llm_calls": [],
    }
    engine = None
    primary_error = False
    active_tasks = set()
    deadline = started + spec.budget.max_wall_s
    try:
        engine = Engine(meter, settings=settings, human=ScriptedHuman())
        original_run = engine.runner.run

        async def tracked_run(node):
            task = asyncio.current_task()
            active_tasks.add(task)
            try:
                return await original_run(node)
            finally:
                active_tasks.discard(task)

        engine.runner.run = tracked_run
        task = engine.submit(spec.goal, acceptance=spec.acceptance, budget=spec.budget)
        if spec.setup:
            workspace = engine.workspace.allocate(
                fixture.id, settings.conversation_id, f"{LEAD_ROLE}@{task.id}"
            )
            write_setup(workspace, spec.setup)
        async with asyncio.timeout_at(deadline):
            report = await engine.run()
        metadata["report"] = report.model_dump(mode="json")
        node = engine.repo.get(task.id)
        verdicts = [
            event for event in engine.trace.query(task.id)
            if event.kind == "acceptance" and event.name == "verdict"
        ]
        if (node is None or node.status != "DONE" or not spec.acceptance
                or not verdicts or verdicts[-1].payload.get("ok") is not True):
            raise RuntimeError("warmup did not pass its independent machine acceptance")
        scope = memory_scope(fixture.id, LEAD_ROLE)
        records = engine.memory.all(scope)
        if not records:
            role = engine.roles.get(fixture.id, LEAD_ROLE)
            steps = sum(event.kind == "llm" for event in engine.trace.query(task.id))
            metadata["explicit_reflection"] = True
            meter.set_phase("warmup_reflection")
            async with asyncio.timeout_at(deadline):
                await engine.reflection.reflect(node, role, steps=steps)
            records = engine.memory.all(scope)
        else:
            metadata["explicit_reflection"] = False
        if not records:
            raise RuntimeError("warmup produced no reusable planner lesson; no seed was invented")
        metadata.update({
            "status": "completed", "root_task_id": task.id,
            "memory_record_ids": [record.id for record in records],
            "memory_sha256": memory_snapshot_sha256(records),
            "memory_record_count": len(records),
        })
        atomic_json(root / "memory-snapshot.json", memory_snapshot(records))
        return records, metadata
    except BaseException as exc:
        primary_error = True
        metadata["status"] = "interrupted" if isinstance(
            exc, (asyncio.CancelledError, KeyboardInterrupt)
        ) else "error"
        metadata["error_type"] = type(exc).__name__
        metadata["error_message"] = redact(str(exc), client)
        raise
    finally:
        async def cleanup():
            # The current Scheduler cancels but does not await its runners.
            # Drain only this warmup's tracked runners before closing their trace.
            pending = set(active_tasks)
            for task in pending:
                task.cancel()
            if pending:
                finished, pending = await asyncio.wait(pending, timeout=5.0)
                for task in finished:
                    if not task.cancelled():
                        task.exception()
            metadata["cooperative_cleanup_complete"] = not pending
            if engine is not None:
                engine.close()
            close_task = asyncio.create_task(_close_client(client))
            _, close_pending = await asyncio.wait({close_task}, timeout=5.0)
            if close_pending:
                close_task.cancel()
                raise RuntimeError("warmup client close could not be confirmed within 5 seconds")
            await close_task
            if pending:
                raise RuntimeError("warmup cooperative task cleanup could not be confirmed")

        cleanup_task = asyncio.create_task(cleanup())
        cleanup_interrupted = False
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cleanup_interrupted = True
            await cleanup_task
            if cleanup_interrupted and not primary_error:
                metadata["status"] = "interrupted"
                raise asyncio.CancelledError
        except BaseException as exc:
            metadata["cleanup_error"] = type(exc).__name__
            if not primary_error:
                if not isinstance(exc, asyncio.CancelledError):
                    metadata["status"] = "error"
                raise
        finally:
            metadata["finished_at"] = utc_now()
            metadata["elapsed_s"] = round(monotonic() - started, 6)
            metadata["llm_calls"] = meter.calls
            metadata["llm_call_count"] = len(meter.calls)
            metadata["llm_usage"] = meter.summary()
            metadata["usage_unknown_calls"] = metadata["llm_usage"]["usage_missing_calls"]
            metadata["api_calls"] = 0 if isinstance(client, FakeLLM) else len(meter.calls)
            atomic_json(root / "warmup.json", metadata)


def _report(manifest):
    lines = [
        "# Ablation run record", "",
        "This records attempts and mechanism evidence, not a claim of statistical improvement.",
        "Long-term memory is ablated; the conclusion board remains enabled in every group.",
        "Warmup is separate in-process execution; held-out hard deadlines do not protect it.",
        "", "| repeat | task | group | status | root success | result |",
        "|---:|---|---|---|---|---|",
    ]
    for job in manifest["jobs"]:
        results = job.get("results", [])
        outcome = results[0].get("root_success") if results else None
        lines.append(
            f"| {job['repeat']} | {job['fixture_id']} | {job['group']} | "
            f"{job['status']} | {outcome if outcome is not None else 'N/A'} | "
            f"{job['relative_dir']} |"
        )
    return "\n".join(lines) + "\n"


async def run_ablation(
    fixtures, llm_factory, root: Path, repeats: int = 3,
    *, workspace_backend: str = "local", hard_timeout_s: float | None = None,
    before_attempt=None,
) -> dict:
    """Execute a predeclared four-group plan and preserve every nested batch.

    ``llm_factory()`` must return a fresh chat client, directly or awaitably.
    FakeLLM runs are offline mechanism validation, never model-performance data.
    Each repeat/fixture has one disjoint warmup and four independent seed copies.
    """
    from moumou.eval.runner import _validate_batch, run_all

    _validate_batch(fixtures, repeats, workspace_backend, None)
    if not callable(llm_factory):
        raise TypeError("llm_factory must create a new chat client for every attempt")
    if before_attempt is not None and not callable(before_attempt):
        raise TypeError("before_attempt must be a callable integrity guard")
    if hard_timeout_s is not None:
        from moumou.eval.isolation import validate_timeout

        hard_timeout_s = validate_timeout(hard_timeout_s)
    for fixture in fixtures:
        if (fixture.scenario == "use_memory" or fixture.requirements.memory_injection
                ) and fixture.warmup is None:
            raise ValueError(f"{fixture.id}: memory fixture requires independent warmup")
    plan = plan_ablation(fixtures, repeats)
    path_preflight = _check_path_budget(root, fixtures, plan, workspace_backend)
    root = reserve_batch_dir(root)
    manifest = {
        "schema_version": 1, **plan, "started_at": utc_now(), "finished_at": None,
        "code": _code_version(),
        "fixtures": [fixture.model_dump(mode="json") for fixture in fixtures],
        "path_preflight": path_preflight,
        "status": "running", "completed_runs": 0, "jobs": [], "warmups": [],
        "workspace_backend": workspace_backend, "hard_timeout_s": hard_timeout_s,
        "integrity_guard_enabled": before_attempt is not None,
        "memory_ablation_scope": (
            "long-term retrieval, reflection and maintenance; board remains on"
        ),
        "heldout_state": "fresh tasks, conversation, roles, board, trace, databases and workspace",
        "warmup_execution_mode": "in_process; excluded from held-out task measurements",
        "cost_note": "Warmup calls are separate. Missing usage stays unknown; no price is assumed.",
    }
    atomic_json(root / "ablation.json", manifest)
    seen_clients = []

    async def make_client():
        client = llm_factory()
        if inspect.isawaitable(client):
            client = await client
        if not callable(getattr(client, "chat", None)):
            raise TypeError("llm_factory returned no chat client")
        if any(client is previous for previous in seen_clients):
            raise ValueError("llm_factory reused a client; each attempt needs a fresh client")
        seen_clients.append(client)
        return client

    fixtures_by_id = {fixture.id: fixture for fixture in fixtures}
    seeds = {}

    async def check_attempt(stage, planned):
        if before_attempt is not None:
            actual = fixtures_by_id[planned["fixture_id"]]
            verdict = before_attempt({
                "phase": stage, "planned": dict(planned),
                "fixture": actual.model_dump(mode="json"),
                "workspace_backend": workspace_backend, "hard_timeout_s": hard_timeout_s,
            })
            if inspect.isawaitable(verdict):
                verdict = await verdict
            if verdict is False:
                raise ValueError("experiment integrity guard rejected this attempt")

    try:
        for planned in plan["schedule"]:
            fixture = fixtures_by_id[planned["fixture_id"]]
            key = (planned["repeat"], fixture.id)
            warmup_id = None
            if key not in seeds:
                if fixture.warmup is not None:
                    directory = (
                        root / "warmups" / f"r{planned['repeat']}" / f"f{planned['fixture_index']}"
                    )
                    await check_attempt("warmup", planned)
                    client = await make_client()
                    warmup_entry = {
                        "fixture_id": fixture.id, "repeat": planned["repeat"],
                        "relative_dir": directory.relative_to(root).as_posix(),
                        "status": "running",
                    }
                    manifest["warmups"].append(warmup_entry)
                    atomic_json(root / "ablation.json", manifest)
                    try:
                        records, metadata = await _warmup(
                            fixture, client, directory, planned["repeat"]
                        )
                    except BaseException:
                        warmup_entry["status"] = "error"
                        raise
                    warmup_entry.update({
                        "status": "completed", "warmup_id": metadata["warmup_id"],
                        "memory_sha256": metadata["memory_sha256"],
                        "memory_record_count": len(records),
                    })
                    seeds[key] = (records, metadata["warmup_id"])
                else:
                    seeds[key] = ([], None)
            records, warmup_id = seeds[key]
            options = EvaluationOptions(
                memory_enabled=planned["memory_enabled"], max_slots=planned["max_slots"],
                role_review_enabled=False, project_id=fixture.id,
                conversation_id=f"h{planned['repeat']}-g{planned['group_index']}",
                memory_records=records, warmup_id=warmup_id,
            )
            await check_attempt("heldout", planned)
            client = await make_client()
            job = {
                **planned, "status": "running", "started_at": utc_now(),
                "options": options.model_dump(mode="json"),
                "memory_sha256": options.memory_sha256,
                "model": _model_metadata(client), "budget": fixture.budget.model_dump(),
                "purpose": (
                    "offline_validation" if isinstance(client, FakeLLM) else "model_evaluation"
                ),
            }
            manifest["jobs"].append(job)
            atomic_json(root / "ablation.json", manifest)
            try:
                results = await run_all(
                    [fixture], client, root / planned["relative_dir"], repeats=1,
                    workspace_backend=workspace_backend, hard_timeout_s=hard_timeout_s,
                    options=options,
                )
                job["results"] = [asdict(result) for result in results]
                job["status"] = "completed"
                manifest["completed_runs"] += len(results)
            except BaseException as exc:
                job["status"] = "interrupted" if isinstance(
                    exc, (asyncio.CancelledError, KeyboardInterrupt)
                ) else "error"
                job["error_type"] = type(exc).__name__
                job["error_message"] = redact(str(exc), client)
                raise
            finally:
                try:
                    await _close_client(client)
                finally:
                    job["finished_at"] = utc_now()
                    atomic_json(root / "ablation.json", manifest)
                    atomic_text(root / "report.md", _report(manifest))
        manifest["status"] = "completed"
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(
            exc, (asyncio.CancelledError, KeyboardInterrupt)
        ) else "error"
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        manifest["finished_at"] = utc_now()
        atomic_json(root / "ablation.json", manifest)
        atomic_text(root / "report.md", _report(manifest))
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Dedicated ablation entry point; the normal CLI still supports --ablation."""
    from moumou.eval.cli import main as evaluation_main

    return evaluation_main(["--ablation", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
