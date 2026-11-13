"""Batch evaluation CLI.

    python -m moumou.eval.cli --tasks eval/tasks --repeats 1

Needs a DeepSeek key in ``.env``. Without one, use ``make demo`` instead — that
runs the whole framework against a scripted model and needs no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
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


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
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
    p.add_argument("--hard-timeout", type=_positive_float, default=None, metavar="SECONDS",
                   help="run each attempt in a supervised Windows process with this deadline")
    p.add_argument("--ablation", action="store_true",
                   help="run four fixed long-term-memory / scheduling groups")
    p.add_argument("--plan", action="store_true",
                   help="print the ablation plan without model calls or output creation")
    p.add_argument("--memory", action=argparse.BooleanOptionalAction, default=None,
                   help="enable/disable long-term experience memory; the conclusion board remains")
    p.add_argument("--max-slots", type=_positive_int, default=None,
                   help="maximum concurrent task slots for a single evaluation group")
    p.add_argument("--role-review", action=argparse.BooleanOptionalAction, default=None,
                   help="enable/disable post-run role revision")
    return p.parse_args(argv)


def _print_plan(args, fixtures=None) -> int:
    if not getattr(args, "ablation", False):
        print("--plan requires --ablation", file=sys.stderr)
        return 2
    from moumou.eval.ablation import plan_ablation

    try:
        if fixtures is None:
            fixtures = load_tasks(Path(args.tasks))
            if args.only:
                fixtures = [fixture for fixture in fixtures if fixture.id == args.only]
        plan = plan_ablation(fixtures, repeats=args.repeats or 3)
    except (OSError, ValueError) as exc:
        print(f"cannot plan evaluation: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(plan, indent=2))
    return 0


async def main_async(args: argparse.Namespace) -> int:
    if args.repeats is not None and args.repeats < 1:
        print("repeats must be a positive integer", file=sys.stderr)
        return 2
    hard_timeout = getattr(args, "hard_timeout", None)
    if hard_timeout is not None:
        if not math.isfinite(hard_timeout) or hard_timeout <= 0:
            print("hard timeout must be a finite positive number", file=sys.stderr)
            return 2
        if sys.platform != "win32":
            print("hard process isolation currently requires native Windows", file=sys.stderr)
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
    ablation = getattr(args, "ablation", False)
    if getattr(args, "plan", False):
        return _print_plan(args, fixtures)
    explicit_controls = any(getattr(args, key, None) is not None
                            for key in ("memory", "max_slots", "role_review"))
    if ablation and (explicit_controls or args.eval_id):
        print("ablation groups fix memory, scheduling, role review and nested eval ids",
              file=sys.stderr)
        return 2
    options = None
    if explicit_controls:
        from moumou.eval.options import EvaluationOptions

        try:
            options = EvaluationOptions(
                memory_enabled=args.memory if args.memory is not None else True,
                max_slots=args.max_slots if args.max_slots is not None else 4,
                role_review_enabled=args.role_review if args.role_review is not None else False,
            )
        except ValueError as exc:
            print(f"invalid evaluation controls: {exc}", file=sys.stderr)
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

    def make_client():
        return DeepSeekClient(
            api_key=settings.api_key, base_url=settings.base_url,
            model=args.model or settings.model, timeout_s=settings.timeout_s,
        )

    if ablation:
        from moumou.eval.ablation import run_ablation

        manifest = await run_ablation(
            fixtures, make_client, out, repeats=args.repeats or 3,
            workspace_backend=args.workspace_backend, hard_timeout_s=hard_timeout,
        )
        print(f"ablation {manifest['status']}: {(out / 'report.md').resolve()}")
        return 0 if manifest["status"] == "completed" else 1
    llm = make_client()
    print(f"model: {args.model or settings.model}   tasks: {len(fixtures)}")

    try:
        results = await run_all(
            fixtures, llm, out, repeats=args.repeats, workspace_backend=args.workspace_backend,
            eval_id=args.eval_id,
            hard_timeout_s=hard_timeout,
            **({"options": options} if options is not None else {}),
        )
    except (ValueError, FileExistsError, PermissionError) as exc:
        print(f"evaluation setup failed: {exc}; use a fresh --out directory", file=sys.stderr)
        return 2
    finally:
        await llm.aclose()

    report_path = out / "report.md"
    report = report_path.read_text(encoding="utf-8") if report_path.is_file() \
        else render_markdown(results)
    print(report)
    print(f"report written to {(out / 'report.md').resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.plan:
            return _print_plan(args)
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
