"""Batch evaluation CLI.

    python -m moumou.eval.cli --tasks eval/tasks --repeats 1

Needs a DeepSeek key in ``.env``. Without one, use ``make demo`` instead — that
runs the whole framework against a scripted model and needs no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from moumou.adapters import DeepSeekClient
from moumou.config import LLMSettings
from moumou.eval.loader import load_tasks
from moumou.eval.runner import render_markdown, run_all
from moumou.eval.workspace import BACKENDS, check_batch_dir


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the Moumou evaluation set")
    p.add_argument("--tasks", default="eval/tasks", help="directory of task YAML files")
    p.add_argument("--out", default=None,
                   help="exact batch directory; defaults to a fresh directory under runs/eval")
    p.add_argument("--eval-id", default=None, help="batch identifier saved in the manifest")
    p.add_argument("--repeats", type=_positive_int, default=None, help="override repeats per task")
    p.add_argument("--model", default=None, help="override DEEPSEEK_MODEL")
    p.add_argument("--only", default=None, help="run a single task id")
    p.add_argument("--workspace-backend", choices=BACKENDS, default="local",
                   help="workspace provider; use git to evaluate P1 worktree delivery")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if args.repeats is not None and args.repeats < 1:
        print("repeats must be a positive integer", file=sys.stderr)
        return 2
    try:
        fixtures = load_tasks(Path(args.tasks))
    except (OSError, ValueError) as exc:
        print(f"cannot load evaluation tasks: {exc}", file=sys.stderr)
        return 2
    if args.only:
        fixtures = [f for f in fixtures if f.id == args.only]
        if not fixtures:
            print(f"no task with id {args.only!r}", file=sys.stderr)
            return 2
    if not fixtures:
        print("the evaluation set is empty", file=sys.stderr)
        return 2

    batch_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    out = Path(args.out) if args.out else Path("runs/eval") / batch_name
    try:
        check_batch_dir(out)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        settings = LLMSettings.from_env()
        settings.require_key()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    llm = DeepSeekClient(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=args.model or settings.model,
        timeout_s=settings.timeout_s,
    )
    print(f"model: {args.model or settings.model}   tasks: {len(fixtures)}")

    try:
        results = await run_all(
            fixtures, llm, out, repeats=args.repeats, workspace_backend=args.workspace_backend,
            eval_id=args.eval_id,
        )
    except (ValueError, FileExistsError, PermissionError) as exc:
        print(f"evaluation setup failed: {exc}; use a fresh --out directory", file=sys.stderr)
        return 2
    finally:
        await llm.aclose()

    report = render_markdown(results)
    print(report)
    print(f"report written to {(out / 'report.md').resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(parse_args(argv)))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
