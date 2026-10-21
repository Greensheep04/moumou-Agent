"""Batch evaluation CLI.

    python -m moumou.eval.cli --tasks eval/tasks --repeats 1

Needs a DeepSeek key in ``.env``. Without one, use ``make demo`` instead — that
runs the whole framework against a scripted model and needs no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from moumou.adapters import DeepSeekClient
from moumou.config import LLMSettings
from moumou.eval.loader import load_tasks
from moumou.eval.runner import render_markdown, run_all


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the Moumou evaluation set")
    p.add_argument("--tasks", default="eval/tasks", help="directory of task YAML files")
    p.add_argument("--out", default="runs/eval", help="where run artifacts and the report go")
    p.add_argument("--repeats", type=int, default=None, help="override repeats per task")
    p.add_argument("--model", default=None, help="override DEEPSEEK_MODEL")
    p.add_argument("--only", default=None, help="run a single task id")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    fixtures = load_tasks(Path(args.tasks))
    if args.only:
        fixtures = [f for f in fixtures if f.id == args.only]
        if not fixtures:
            print(f"no task with id {args.only!r}", file=sys.stderr)
            return 2

    settings = LLMSettings.from_env()
    try:
        settings.require_key()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    llm = DeepSeekClient(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=args.model or settings.model,
        timeout_s=settings.timeout_s,
    )
    print(f"model: {args.model or settings.model}   tasks: {len(fixtures)}")

    try:
        results = await run_all(fixtures, llm, out, repeats=args.repeats)
    finally:
        await llm.aclose()

    report = render_markdown(results)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"report written to {(out / 'report.md').resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
