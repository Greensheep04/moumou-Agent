"""Trace 回放（Replay / Visualization）。

从 ``TraceStore``（JSONL 参考实现）或 ``runs/<run>/trace.jsonl`` 读取一次任务
的完整执行轨迹，按时间升序逐条渲染：谁派了活、谁调了什么工具、哪一步卡在
等待人类、最后花了多久。纯渲染，不触碰业务逻辑。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from moumou.contracts import TraceEvent

_KIND_COLOR = {
    "task": "bold cyan",
    "llm": "bold blue",
    "tool": "bold green",
    "approval": "bold red",
    "question": "bold red",
    "steer": "bold yellow",
    "delegate": "bold magenta",
    "role": "bold magenta",
    "acceptance": "bold green",
    "memory": "dim",
}


def load_events(source) -> list[TraceEvent]:
    """Accept a path (JSONL), an iterable of events, or a TraceStore-like object."""
    # TraceStore-like: expose query() / all_events().
    if hasattr(source, "all_events"):
        return list(source.all_events())
    if hasattr(source, "query") and not isinstance(source, (str, Path)):
        # A bare TraceStore without all_events() can still replay a whole run if
        # we are given a task id, but for a full dump we need all_events.
        return list(source.all_events()) if hasattr(source, "all_events") else []

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"trace 文件不存在：{path}")
    events: list[TraceEvent] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(TraceEvent.model_validate_json(line))
    return events


def replay(source, task_id: str | None = None, console: Console | None = None) -> list[TraceEvent]:
    """Render a flat, ascending timeline of every span."""
    console = console or Console()
    events = sorted(load_events(source), key=lambda e: (e.t0, e.span_id))
    if task_id:
        events = [e for e in events if e.task_id == task_id]

    if not events:
        console.print("[dim](没有可回放的 trace 事件)[/]")
        return events

    table = Table(title=f"Trace 回放（{len(events)} 条 span）", show_lines=False)
    table.add_column("时间", no_wrap=True)
    table.add_column("task", no_wrap=True)
    table.add_column("agent", no_wrap=True, max_width=16, overflow="ellipsis")
    table.add_column("kind", no_wrap=True)
    table.add_column("name", no_wrap=True, max_width=26, overflow="ellipsis")
    table.add_column("status", no_wrap=True)
    table.add_column("耗时", justify="right", no_wrap=True)
    table.add_column("tokens", justify="right", no_wrap=True)

    for e in events:
        kind_style = _KIND_COLOR.get(e.kind, "")
        dur = f"{e.duration_ms:.0f}ms" if e.duration_ms is not None else "-"
        tok = e.tokens_in + e.tokens_out
        table.add_row(
            e.t0.strftime("%H:%M:%S.%f")[:-3],
            e.task_id or "-",
            e.agent_id or "-",
            f"[{kind_style}]{e.kind}[/]",
            e.name,
            e.status or "-",
            dur,
            str(tok) if tok else "-",
        )
    console.print(table)

    # 摘要。
    _print_summary(console, events)
    return events


def replay_tree(source, task_id: str, console: Console | None = None) -> None:
    """Render the parent/child span tree for one task."""
    console = console or Console()
    events = [e for e in load_events(source) if e.task_id == task_id]
    if not events:
        console.print(f"[dim](任务 {task_id} 没有 trace)[/]")
        return

    by_id: dict[str, TraceEvent] = {e.span_id: e for e in events}
    children: dict[str | None, list[TraceEvent]] = defaultdict(list)
    for e in events:
        children[e.parent_span].append(e)

    console.print(Panel(f"[bold]任务 {task_id} 的 span 树[/]", border_style="cyan"))

    def walk(parent: str | None, prefix: str = "") -> None:
        for e in sorted(children.get(parent, []), key=lambda x: x.t0):
            dur = f"{e.duration_ms:.0f}ms" if e.duration_ms is not None else "…"
            marker = "+-- " if prefix else ""
            console.print(
                f"{prefix}{marker}[{_KIND_COLOR.get(e.kind, '')}]{e.kind}[/] "
                f"{e.name}  ({dur}, {e.status or '-'})"
            )
            walk(e.span_id, prefix + ("|   " if prefix else "    "))

    # 顶层是 parent_span 不在本任务内的 span。
    roots = [e for e in events if e.parent_span is None or e.parent_span not in by_id]
    for r in sorted(roots, key=lambda e: e.t0):
        walk(r.span_id, "")
    # 孤立根（parent_span=None）。
    for r in sorted(children.get(None, []), key=lambda e: e.t0):
        walk(r.span_id, "")


def _print_summary(console: Console, events: list[TraceEvent]) -> None:
    by_kind: dict[str, int] = defaultdict(int)
    tokens_in = tokens_out = 0
    max_end = None
    for e in events:
        by_kind[e.kind] += 1
        tokens_in += e.tokens_in
        tokens_out += e.tokens_out
        if e.t1 is not None and (max_end is None or e.t1 > max_end):
            max_end = e.t1

    start = min((e.t0 for e in events), default=None)
    wall = (max_end - start).total_seconds() if start and max_end else None

    parts = [f"[bold]{len(events)}[/] 条 span"]
    if wall is not None:
        parts.append(f"总跨度 [bold]{wall:.2f}s[/]")
    parts.append(f"tokens in/out = [bold]{tokens_in}/{tokens_out}[/]")
    kinds = "  ".join(f"{k}×{n}" for k, n in sorted(by_kind.items()))
    console.print(" · ".join(parts))
    console.print(f"  按类型：{kinds}")


def _find_run_trace(run: str | None) -> Path:
    """Resolve a run name to ``runs/<run>/trace.jsonl``."""
    if run is None:
        # 找最近一个含 trace.jsonl 的 run。
        runs = Path("runs")
        if runs.exists():
            for d in sorted(runs.iterdir(), reverse=True):
                if (d / "trace.jsonl").exists():
                    return d / "trace.jsonl"
        return Path("runs/demo/trace.jsonl")
    return Path("runs") / run / "trace.jsonl"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="重放一次任务的 trace")
    parser.add_argument("trace", nargs="?", help="trace.jsonl 路径；缺省则读 runs/")
    parser.add_argument("--task", help="只重放指定 task_id")
    parser.add_argument("--tree", action="store_true", help="以 span 树展示")
    args = parser.parse_args(argv)

    path = Path(args.trace) if args.trace else _find_run_trace(None)
    console = Console()

    if args.tree and args.task:
        replay_tree(path, args.task, console=console)
    else:
        replay(path, task_id=args.task, console=console)


if __name__ == "__main__":
    main(sys.argv[1:])
