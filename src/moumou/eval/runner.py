"""Reference evaluation runner.

P3 owns this module. What is fixed here is the *metric definition*, because the
report has to be comparable across machines:

    completion_rate = tasks reaching DONE / tasks created
    wall_s          = submit -> last terminal state
    tokens          = sum over the run's trace spans

Each repeat gets a fresh database, a fresh workspace tree, and a fresh agent
history, so repeats are independent samples rather than continuations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from moumou.adapters import ScriptedHuman
from moumou.eval.loader import TaskFixture
from moumou.orchestrator import Engine, Settings


@dataclass
class FixtureResult:
    fixture_id: str
    scenario: str
    repeat: int
    done: int
    dead: int
    total: int
    completion_rate: float
    wall_s: float
    tokens_in: int
    tokens_out: int
    run_dir: str
    errors: list[str] = field(default_factory=list)


async def run_once(fixture: TaskFixture, llm, root: Path, repeat: int) -> FixtureResult:
    run_dir = root / fixture.id / f"r{repeat}"
    settings = Settings(
        project_id=fixture.id, conversation_id="c1", root=run_dir, max_slots=4
    )
    human = ScriptedHuman(
        decisions=fixture.human.decisions,
        default=fixture.human.default,
        answers=fixture.human.answers,
        default_answer=fixture.human.default_answer,
    )

    engine = Engine(llm, human=human, settings=settings)
    try:
        task = engine.submit(fixture.goal, acceptance=fixture.acceptance, budget=fixture.budget)

        if fixture.setup:
            workspace = engine.workspace.allocate(fixture.id, "c1", f"planner@{task.id}")
            for rel, content in fixture.setup.items():
                target = workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        report = await engine.run()
        errors = [t["goal"] for t in report.tasks if t["status"] == "DEAD"]

        return FixtureResult(
            fixture_id=fixture.id,
            scenario=fixture.scenario,
            repeat=repeat,
            done=report.done,
            dead=report.dead,
            total=report.total,
            completion_rate=report.completion_rate,
            wall_s=report.wall_s,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            run_dir=str(run_dir),
            errors=errors,
        )
    finally:
        engine.close()


async def run_all(
    fixtures: list[TaskFixture], llm, root: Path, repeats: int | None = None
) -> list[FixtureResult]:
    results: list[FixtureResult] = []
    for fixture in fixtures:
        n = repeats or fixture.repeats
        for repeat in range(n):
            results.append(await run_once(fixture, llm, root, repeat))
    return results


def aggregate(results: list[FixtureResult]) -> list[dict]:
    """Mean + spread per fixture. One sample is not a measurement."""
    by_id: dict[str, list[FixtureResult]] = {}
    for r in results:
        by_id.setdefault(r.fixture_id, []).append(r)

    rows = []
    for fixture_id, group in by_id.items():
        n = len(group)
        rates = [g.completion_rate for g in group]
        walls = [g.wall_s for g in group]
        mean = sum(rates) / n
        var = sum((x - mean) ** 2 for x in rates) / n
        rows.append(
            {
                "id": fixture_id,
                "scenario": group[0].scenario,
                "runs": n,
                "completion_rate": round(mean, 3),
                "std": round(var**0.5, 3),
                "wall_s_mean": round(sum(walls) / n, 2),
                "tokens": sum(g.tokens_in + g.tokens_out for g in group),
            }
        )
    return rows


def render_markdown(results: list[FixtureResult]) -> str:
    rows = aggregate(results)
    total_runs = sum(r["runs"] for r in rows)
    mean_rate = sum(r["completion_rate"] * r["runs"] for r in rows) / total_runs

    lines = [
        "# Evaluation report",
        "",
        "| task | scenario | runs | completion | std | wall (s) | tokens |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['scenario']} | {r['runs']} | {r['completion_rate']:.2f} "
            f"| {r['std']:.2f} | {r['wall_s_mean']:.1f} | {r['tokens']} |"
        )

    lines += [
        "",
        f"**Overall completion rate: {mean_rate:.3f}** over {total_runs} runs, "
        f"{len(rows)} distinct tasks.",
        "",
    ]
    failures = [r for r in results if r.dead]
    if failures:
        lines.append("## Failures")
        for r in failures:
            lines.append(f"- `{r.fixture_id}` run {r.repeat}: {r.dead} task(s) died")
    return "\n".join(lines) + "\n"
