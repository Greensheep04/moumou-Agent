"""Isolated evaluation attempts with durable, inspectable outcomes.

The legacy completion_rate/wall_s/tokens fields retain Engine's current report
semantics. Full delivery-time and token accounting are separate, pending work;
elapsed_s measures this runner's setup, execution and resource-closing interval.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from time import monotonic

from moumou.adapters import ScriptedHuman
from moumou.eval.artifacts import (
    append_result,
    atomic_json,
    atomic_text,
    create_manifest,
    redact,
    utc_now,
)
from moumou.eval.loader import TaskFixture
from moumou.eval.workspace import (
    BACKENDS,
    create_run_dir,
    git_workspace,
    reserve_batch_dir,
    write_setup,
)
from moumou.orchestrator import Engine, Settings


@dataclass
class FixtureResult:
    fixture_id: str
    scenario: str
    repeat: int
    done: int | None
    dead: int | None
    total: int | None
    completion_rate: float | None
    wall_s: float | None
    tokens_in: int | None
    tokens_out: int | None
    run_dir: str
    errors: list[str] = field(default_factory=list)
    schema_version: int = 1
    attempt: int = 1
    eval_id: str | None = None
    status: str = "unverified"
    error_type: str | None = None
    error_message: str | None = None
    root_task_id: str | None = None
    root_status: str | None = None
    root_machine_accepted: bool | None = None
    root_success: bool | None = None
    root_acceptance_checks: list[dict] = field(default_factory=list)
    unfinished: int | None = None
    nodes: list[dict] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_s: float | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    usage_missing_calls: int | None = None
    total_tokens_in: int | None = None
    total_tokens_out: int | None = None
    execution_mode: str = "in_process"
    hard_timeout_s: float | None = None
    worker_pid: int | None = None
    worker_exit_code: int | None = None
    cleanup_s: float | None = None
    cleanup_complete: bool | None = None


def _empty_result(fixture, run_dir, repeat, eval_id) -> FixtureResult:
    return FixtureResult(
        fixture_id=fixture.id, scenario=fixture.scenario, repeat=repeat,
        done=None, dead=None, total=None, completion_rate=None, wall_s=None,
        tokens_in=None, tokens_out=None, run_dir=str(run_dir), eval_id=eval_id,
        started_at=utc_now(),
        artifacts={
            "result": str(run_dir / "result.json"),
            "task_database": str(run_dir / "moumou.db"),
            "memory_database": str(run_dir / "memory.db"),
            "trace_jsonl": str(run_dir / "trace.jsonl"),
            "trace_database": str(run_dir / "trace.db"),
            "workspaces": str(run_dir / "workspaces"),
        },
    )


def _observe(result: FixtureResult, engine, fixture, llm) -> None:
    """Snapshot before close; a missing observation must not replace the run error."""
    try:
        nodes = engine.repo.all_tasks()
        result.nodes = [
            {
                "id": node.id, "parent_id": node.parent_id, "role": node.role,
                "status": node.status, "retries": node.retries,
                "acceptance_count": len(node.acceptance),
                "error": redact(node.error, llm) if node.error else None,
                "artifacts": node.artifacts,
            }
            for node in nodes
        ]
        result.total = len(nodes)
        result.done = sum(node.status == "DONE" for node in nodes)
        result.dead = sum(node.status == "DEAD" for node in nodes)
        result.unfinished = result.total - result.done - result.dead
        result.errors = [redact(node.goal, llm) for node in nodes if node.status == "DEAD"]
        root = next((node for node in nodes if node.id == result.root_task_id), None)
        if root is None:
            return
        result.root_status = root.status
        verdicts = [
            event for event in engine.trace.query(root.id)
            if event.kind == "acceptance" and event.name == "verdict"
        ]
        if verdicts:
            checks = verdicts[-1].payload.get("checks", [])
            result.root_acceptance_checks = [
                {
                    key: redact(check[key], llm) if isinstance(check[key], str) else check[key]
                    for key in ("kind", "target", "ok", "error") if key in check
                }
                for check in checks
            ]
            if fixture.acceptance:
                result.root_machine_accepted = (
                    verdicts[-1].payload.get("ok") is True
                    and len(checks) == len(fixture.acceptance)
                    and all(c.get("kind") != "self_report" and c.get("ok") is True for c in checks)
                )
        if root.status == "DONE":
            result.root_success = result.root_machine_accepted
        else:
            result.root_success = False
        if root.status == "DONE":
            result.status = "succeeded" if result.root_success else "unverified"
        elif root.status == "DEAD":
            result.status = "failed"
            if result.root_machine_accepted is False:
                result.error_type = "acceptance_failed"
            elif root.error and root.error.startswith("crash:"):
                result.error_type = "worker_crash"
            else:
                result.error_type = "task_failed"
            result.error_message = redact(root.error, llm) if root.error else None
        else:
            result.status = "incomplete"
            result.error_type = "incomplete"
    except Exception as exc:
        result.errors.append(f"observation unavailable: {redact(str(exc), llm)}")


async def run_once(
    fixture: TaskFixture, llm, root: Path, repeat: int, *, workspace_backend: str = "local",
    eval_id: str | None = None, _result_path: Path | None = None,
) -> FixtureResult:
    if workspace_backend not in BACKENDS:
        raise ValueError(f"unknown workspace backend: {workspace_backend}")
    run_dir = create_run_dir(root, fixture.id, repeat)
    result_path = Path(_result_path) if _result_path is not None else run_dir / "result.json"
    started = monotonic()
    result = _empty_result(fixture, run_dir, repeat, eval_id)
    phase = "setup"
    engine = None
    try:
        workspace = git_workspace(run_dir, fixture.setup) if workspace_backend == "git" else None
        settings = Settings(
            project_id=fixture.id, conversation_id="c1", root=run_dir, max_slots=4
        )
        human = ScriptedHuman(
            decisions=fixture.human.decisions, default=fixture.human.default,
            answers=fixture.human.answers, default_answer=fixture.human.default_answer,
        )
        try:
            engine = Engine(llm, human=human, workspace=workspace, settings=settings)
            task = engine.submit(fixture.goal, acceptance=fixture.acceptance, budget=fixture.budget)
            result.root_task_id = task.id
            if fixture.setup and workspace_backend == "local":
                parent = engine.workspace.allocate(fixture.id, "c1", f"planner@{task.id}")
                write_setup(parent, fixture.setup)
            phase = "execution"
            report = await engine.run()
            result.completion_rate = report.completion_rate
            result.wall_s = report.wall_s
            result.tokens_in, result.tokens_out = report.tokens_in, report.tokens_out
        finally:
            if engine is not None:
                _observe(result, engine, fixture, llm)
                engine.close()
    except BaseException as exc:
        result.status = (
            "cancelled" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "error"
        )
        result.error_type = "cancelled" if result.status == "cancelled" else (
            "setup_error" if phase == "setup" else "worker_crash"
        )
        result.error_message = f"{type(exc).__name__}: {redact(str(exc), llm)}"
        result.errors.append(result.error_message)
        result.finished_at, result.elapsed_s = utc_now(), round(monotonic() - started, 6)
        atomic_json(result_path, asdict(result))
        raise
    result.finished_at, result.elapsed_s = utc_now(), round(monotonic() - started, 6)
    atomic_json(result_path, asdict(result))
    return result


def _validate_batch(fixtures, repeats, workspace_backend, eval_id):
    if workspace_backend not in BACKENDS:
        raise ValueError(f"unknown workspace backend: {workspace_backend}")
    if not fixtures:
        raise ValueError("evaluation needs at least one fixture")
    if repeats is not None and (
        not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1
    ):
        raise ValueError("repeats must be a positive integer")
    seen = set()
    for fixture in fixtures:
        name = fixture.id
        windows = PureWindowsPath(name)
        if (not name or name in (".", "..") or windows.drive or windows.root
                or "/" in name or "\\" in name or ":" in name
                or windows.is_reserved() or name.endswith((".", " "))):
            raise ValueError("fixture id must be one safe directory name")
        if name.casefold() in seen:
            raise ValueError("duplicate or case-colliding fixture ids")
        if name.casefold() in {".workers", ".batch", "manifest.json", "results.jsonl", "report.md"}:
            raise ValueError("fixture id conflicts with reserved batch metadata")
        seen.add(name.casefold())
    if eval_id is not None and (
        not isinstance(eval_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", eval_id)
        or eval_id.endswith(".") or PureWindowsPath(eval_id).is_reserved()
    ):
        raise ValueError("eval_id must be one safe identifier")


def _attempt_record(fixture, llm, root, repeat, eval_id, exc, started, started_at):
    if isinstance(exc, FileExistsError):
        raise exc
    run_dir = root / fixture.id / f"r{repeat}"
    path = run_dir / "result.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("eval_id") != eval_id or data.get("fixture_id") != fixture.id
                or data.get("repeat") != repeat or not isinstance(data.get("run_dir"), str)
                or Path(data["run_dir"]).resolve() != run_dir.resolve()):
            raise ValueError("existing result does not belong to this batch attempt")
        return FixtureResult(**data)
    # This also records an execution seam that failed before run_once could start.
    # Never replace a colliding run directory or an existing result.
    run_dir.mkdir(parents=True, exist_ok=True)
    result = _empty_result(fixture, run_dir, repeat, eval_id)
    result.started_at = started_at
    result.status = "cancelled" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) \
        else "error"
    result.error_type = "cancelled" if result.status == "cancelled" else "worker_crash"
    result.error_message = f"{type(exc).__name__}: {redact(str(exc), llm)}"
    result.errors = [result.error_message]
    result.finished_at, result.elapsed_s = utc_now(), round(monotonic() - started, 6)
    atomic_json(path, asdict(result))
    return result


async def run_all(
    fixtures: list[TaskFixture], llm, root: Path, repeats: int | None = None,
    *, workspace_backend: str = "local", eval_id: str | None = None,
    hard_timeout_s: float | None = None, worker_factory: str | None = None,
) -> list[FixtureResult]:
    _validate_batch(fixtures, repeats, workspace_backend, eval_id)
    client_spec, client_env = None, None
    if hard_timeout_s is not None:
        from moumou.eval.isolation import prepare_client, validate_timeout

        hard_timeout_s = validate_timeout(hard_timeout_s)
        client_spec, client_env = prepare_client(llm, worker_factory)
    elif worker_factory is not None:
        raise ValueError("worker_factory requires hard_timeout_s; no silent in-process fallback")
    root = reserve_batch_dir(root)
    manifest = create_manifest(root, fixtures, llm, repeats, workspace_backend, eval_id=eval_id)
    manifest["settings"]["worker"] = {
        "execution_mode": "process" if hard_timeout_s is not None else "in_process",
        "hard_timeout_s": hard_timeout_s,
        "cleanup_timeout_s": 5.0 if hard_timeout_s is not None else None,
        "worker_factory": worker_factory,
        "fresh_client_per_attempt": hard_timeout_s is not None,
    }
    if hard_timeout_s is not None:
        manifest["measurement_limits"] = [
            item for item in manifest["measurement_limits"]
            if not item.startswith("Hard process kill recovery")
        ] + [
            "Worker containment is native Windows only, with a separate bounded cleanup grace.",
            "Synchronous OS launch/Job calls and host filesystem/kernel stalls "
            "are not preemptible.",
        ]
        if worker_factory is not None:
            manifest["purpose"] = "custom_worker_validation"
            manifest["model"] = {"client_factory": worker_factory, "provider": None, "model": None}
    atomic_json(root / "manifest.json", manifest)
    results: list[FixtureResult] = []
    try:
        for fixture in fixtures:
            n = repeats if repeats is not None else fixture.repeats
            for repeat in range(n):
                started = monotonic()
                started_at = utc_now()
                try:
                    if hard_timeout_s is None:
                        result = await run_once(
                            fixture, llm, root, repeat, workspace_backend=workspace_backend,
                            eval_id=manifest["eval_id"],
                        )
                    else:
                        from moumou.eval.isolation import run_isolated

                        result = await run_isolated(
                            fixture, llm, root, repeat, workspace_backend=workspace_backend,
                            eval_id=manifest["eval_id"], hard_timeout_s=hard_timeout_s,
                            client_spec=client_spec, client_env=client_env,
                        )
                except BaseException as exc:
                    result = _attempt_record(
                        fixture, llm, root, repeat, manifest["eval_id"], exc, started, started_at
                    )
                    results.append(result)
                    append_result(root, asdict(result))
                    if not isinstance(exc, Exception):
                        raise
                else:
                    results.append(result)
                    append_result(root, asdict(result))
                manifest["completed_runs"] = len(results)
                atomic_json(root / "manifest.json", manifest)
                if result.error_type == "cleanup_failed":
                    raise RuntimeError("Worker cleanup could not be verified; batch stopped")
        manifest["status"] = "completed"
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(
            exc, (asyncio.CancelledError, KeyboardInterrupt)
        ) else "error"
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        manifest["finished_at"] = utc_now()
        manifest["completed_runs"] = len(results)
        atomic_json(root / "manifest.json", manifest)
        atomic_text(root / "report.md", render_markdown(results))
    return results


def aggregate(results: list[FixtureResult]) -> list[dict]:
    """Mean + spread per fixture. One sample is not a measurement."""
    by_id: dict[str, list[FixtureResult]] = {}
    for r in results:
        by_id.setdefault(r.fixture_id, []).append(r)

    rows = []
    for fixture_id, group in by_id.items():
        n = len(group)
        rates = [g.completion_rate for g in group if g.completion_rate is not None]
        walls = [g.wall_s for g in group if g.wall_s is not None]
        mean = sum(rates) / len(rates) if rates else None
        var = sum((x - mean) ** 2 for x in rates) / len(rates) if rates else None
        rows.append(
            {
                "id": fixture_id,
                "scenario": group[0].scenario,
                "runs": n,
                "measured_runs": len(rates),
                "completion_rate": round(mean, 3) if mean is not None else None,
                "std": round(var**0.5, 3) if var is not None else None,
                "wall_s_mean": round(sum(walls) / len(walls), 2) if walls else None,
                "tokens": sum(g.tokens_in + g.tokens_out for g in group)
                if all(g.tokens_in is not None and g.tokens_out is not None for g in group)
                else None,
            }
        )
    return rows


def render_markdown(results: list[FixtureResult]) -> str:
    rows = aggregate(results)
    total_runs = sum(r["runs"] for r in rows)
    measured = sum(r["measured_runs"] for r in rows)
    mean_rate = sum(
        r["completion_rate"] * r["measured_runs"] for r in rows
        if r["completion_rate"] is not None
    ) / measured if measured else None

    def number(value, places=2):
        return f"{value:.{places}f}" if value is not None else "N/A"

    lines = [
        "# Evaluation report",
        "",
        "Diagnostic Engine node metrics; full evaluation metrics are not finalized.",
        "Missing measurements are N/A. Errors, cancellations and unverified runs remain listed.",
        "",
        "| task | scenario | runs | completion | std | wall (s) | tokens |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['scenario']} | {r['runs']} | {number(r['completion_rate'])} "
            f"| {number(r['std'])} | {number(r['wall_s_mean'], 1)} | "
            f"{r['tokens'] if r['tokens'] is not None else 'N/A'} |"
        )

    lines += [
        "",
        f"**Observed Engine node completion: {number(mean_rate, 3)}** over {measured} measured "
        f"runs of {total_runs} recorded runs, {len(rows)} distinct fixtures.",
        "",
        "| fixture | repeat | status | root | machine acceptance | error type | result |",
        "|---|---:|---|---|---|---|---|",
    ]
    for result in results:
        lines.append(
            f"| {result.fixture_id} | {result.repeat} | {result.status} | "
            f"{result.root_status or 'N/A'} | {result.root_machine_accepted} | "
            f"{result.error_type or '-'} | `{Path(result.run_dir) / 'result.json'}` |"
        )
    return "\n".join(lines) + "\n"
