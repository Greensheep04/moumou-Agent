"""Read-only P2 rendering of original traces and P3 instructor replay evidence.

A source is explicit: one JSONL journal, a TraceEvent iterable, an exact-task
TraceStore, a formal run directory, or an instructor_replay JSON export.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC
from os import PathLike
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from moumou.contracts import TraceEvent

_LIMIT = 32 * 1024**2
_KIND_COLOR = {
    "task": "bold cyan", "llm": "bold blue", "tool": "bold green",
    "approval": "bold red", "question": "bold red", "steer": "bold yellow",
    "delegate": "bold magenta", "role": "bold magenta",
    "acceptance": "bold green", "memory": "dim",
}


@dataclass(frozen=True)
class ReplayItem:
    scenario_id: str | None
    event: TraceEvent
    record: dict


@dataclass(frozen=True)
class ReplayView:
    items: tuple[ReplayItem, ...]
    source_kind: str
    evidence: dict | None = None
    scenario_ids: tuple[str, ...] = ()
    task_id: str | None = None
    duplicate_records: int = 0


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate replay field: {key}")
        result[key] = value
    return result


def _invalid(value):
    raise ValueError(f"non-finite replay value: {value}")


def _loads(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid)


def _event(value):
    raw = value.model_dump(mode="json") if isinstance(value, TraceEvent) else value
    if not isinstance(raw, dict):
        raise TypeError("replay events must be TraceEvent objects or saved event objects")
    # Validate saved required fields before Pydantic could supply a new span ID/time.
    from moumou.eval.replay import _validate_trace

    _validate_trace(raw)
    if (not isinstance(raw.get("span_id"), str) or not raw["span_id"]
            or not isinstance(raw.get("task_id"), str) or not raw["task_id"]):
        raise ValueError("replay events require their original non-empty span/task IDs")
    event = TraceEvent.model_validate(raw)
    if event.t1 is not None and event.t1 < event.t0:
        raise ValueError("saved span end precedes its start")
    return event, dict(raw)


def _order(item):
    return item.event.t0.astimezone(UTC), item.event.span_id


def _instructor(model):
    if (not isinstance(model, dict) or type(model.get("schema_version")) is not int
            or model["schema_version"] != 1 or model.get("kind") != "instructor_replay"
            or model.get("read_only") is not True):
        raise ValueError("expected a saved instructor_replay schema_version 1 export")
    scenarios, tasks = model.get("scenarios"), model.get("tasks")
    if not isinstance(scenarios, list) or not isinstance(tasks, list):
        raise ValueError("instructor replay needs original scenario and task arrays")
    items, identities, known_tasks = [], [], set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("saved scenario must be an object")
        identity = scenario.get("scenario_id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("invalid or duplicate scenario identity")
        identities.append(identity)
        trace = scenario.get("trace")
        if not isinstance(trace, dict) or not isinstance(trace.get("events"), list):
            raise ValueError("saved scenario needs its original trace observation")
        for row in trace["events"]:
            if not isinstance(row, dict):
                raise ValueError("saved trace row must be an object")
            event, raw = _event(row.get("record"))
            expected = {"scenario_id": identity, "task_id": event.task_id}
            if row.get("identity") != expected:
                raise ValueError("saved trace scenario/task identity mismatch")
            known_tasks.add((identity, event.task_id))
            items.append(ReplayItem(identity, event, raw))
        state = scenario.get("state_snapshot")
        if isinstance(state, dict) and isinstance(state.get("record"), dict):
            record = state["record"]
            if record.get("scenario_id") != identity:
                raise ValueError("saved diagnostic belongs to another scenario")
            nodes = record.get("nodes")
            if not isinstance(nodes, list):
                raise ValueError("saved diagnostic nodes must be a list")
            for node in nodes:
                if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                        or not node["id"] or node.get("project_id") != identity):
                    raise ValueError("saved diagnostic node identity mismatch")
                known_tasks.add((identity, node["id"]))
    for task in tasks:
        identity = task.get("identity") if isinstance(task, dict) else None
        if (not isinstance(identity, dict) or identity.get("scenario_id") not in identities
                or not isinstance(identity.get("task_id"), str) or not identity["task_id"]):
            raise ValueError("saved task has no valid scenario/task identity")
        node = task.get("node")
        if not isinstance(node, dict) or node.get("id") != identity["task_id"]:
            raise ValueError("saved task node disagrees with its identity")
        known_tasks.add((identity["scenario_id"], identity["task_id"]))
    return items, tuple(identities), known_tasks


def _jsonl(path):
    events = []
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line) > _LIMIT:
                raise ValueError("trace line exceeds 32 MiB")
            try:
                events.append(_event(_loads(line.decode("utf-8-sig"))))
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError(f"invalid original trace at line {number}: {exc}") from exc
    return events


def load_replay(source, task_id: str | None = None, *,
                scenario_id: str | None = None) -> ReplayView:
    """Read exact evidence; an ambiguous cross-scenario task needs a scenario selector.

    Query-only TraceStore input requires task_id. It is one explicitly selected
    store, not a fabricated all-task enumeration. JSON exports retain their
    saved provenance; rendering them does not reverify external artifact bytes.
    """
    for label, value in (("task_id", task_id), ("scenario_id", scenario_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{label} must be a non-empty original identity")
    model, raw_events, source_kind = None, None, "events"
    if isinstance(source, Mapping):
        # Detach the JSON envelope without changing or filling original fields.
        model = _loads(json.dumps(dict(source), ensure_ascii=False, allow_nan=False))
        source_kind = "saved_instructor_export"
    elif isinstance(source, (str, PathLike)):
        path = Path(source)
        if path.is_dir():
            from moumou.eval.replay import build_replay

            model = build_replay(path)
            source_kind = "validated_saved_run"
        elif path.suffix.lower() == ".json":
            if path.stat().st_size > _LIMIT:
                raise ValueError("replay JSON exceeds 32 MiB")
            model = _loads(path.read_text(encoding="utf-8-sig"))
            source_kind = "saved_instructor_export"
        else:
            raw_events = _jsonl(path)
            source_kind = "single_scenario_jsonl"
    elif callable(getattr(source, "query", None)):
        if task_id:
            raw_events = [_event(event) for event in source.query(task_id)]
            if any(event.task_id != task_id for event, _ in raw_events):
                raise ValueError("TraceStore.query returned events from another task")
            source_kind = "exact_task_store"
        elif callable(getattr(source, "all_events", None)):
            raw_events = [_event(event) for event in source.all_events()]
            source_kind = "explicit_all_events_store"
        else:
            raise ValueError("a query-only TraceStore requires task_id")
    elif callable(getattr(source, "all_events", None)):
        raw_events = [_event(event) for event in source.all_events()]
        source_kind = "explicit_all_events_store"
    elif isinstance(source, Iterable):
        raw_events = [_event(event) for event in source]
    else:
        raise TypeError("unsupported replay source")

    if model is not None:
        items, scenarios, known_tasks = _instructor(model)
        if scenario_id is not None and scenario_id not in scenarios:
            raise ValueError(f"unknown scenario: {scenario_id}")
        if task_id and scenario_id is None:
            matching = {scope for scope, task in known_tasks if task == task_id}
            if len(matching) > 1:
                raise ValueError(f"task {task_id} occurs in multiple scenarios; choose scenario_id")
        selected_scenarios = tuple(scope for scope in scenarios
                                   if scenario_id is None or scope == scenario_id)
        # Check original span identities before filtering a task out of the view.
    else:
        if scenario_id is not None:
            raise ValueError("scenario_id applies to a formal run or instructor replay export")
        selected_scenarios = ()
        items = [ReplayItem(None, event, raw) for event, raw in raw_events or []]

    def selected(item):
        return ((model is None or item.scenario_id in selected_scenarios)
                and (task_id is None or item.event.task_id == task_id))

    unique, duplicates = {}, 0
    for item in items:
        key = item.scenario_id, item.event.span_id
        previous = unique.get(key)
        if previous is not None:
            if previous.record != item.record:
                raise ValueError(f"conflicting duplicate span identity: {key}")
            duplicates += int(selected(item))
            continue
        unique[key] = item
    ordered = tuple(sorted(
        (item for item in unique.values() if selected(item)),
        key=lambda item: (
            item.event.t0.astimezone(UTC), item.scenario_id or "", item.event.span_id,
        ),
    ))
    return ReplayView(ordered, source_kind, model, selected_scenarios, task_id, duplicates)


def load_events(source, task_id: str | None = None, *,
                scenario_id: str | None = None) -> list[TraceEvent]:
    """Compatibility list API for one resolved scenario; multi-scenario rows stay scoped."""
    view = load_replay(source, task_id, scenario_id=scenario_id)
    if len({item.scenario_id for item in view.items}) > 1:
        raise ValueError("use load_replay or choose scenario_id for multiple scenarios")
    return [item.event for item in view.items]


def _duration(event):
    return "-" if event.duration_ms is None else f"{event.duration_ms:.0f}ms"


def _cell(value):
    return Text("unknown" if value is None else str(value))


def _print_evidence(console, view):
    model = view.evidence
    if model is None:
        return
    console.print(Text(
        f"保存的整场状态：{model.get('saved_suite_status', 'unknown')} "
        "（不是进程存活检测）",
    ))
    metrics = model.get("metrics")
    if isinstance(metrics, dict) and "suite_wall_time_s" in metrics:
        console.print(Text(f"P3 保存的 suite_wall_time_s：{metrics['suite_wall_time_s']}"))
    table = Table(title="保存的根任务质量与交付证据；不重新判分")
    for title in ("scenario", "phase / root", "agent status", "raw quality", "effective quality",
                  "quality source", "artifact capture", "memory capture"):
        table.add_column(title)
    for scenario in model["scenarios"]:
        if scenario["scenario_id"] not in view.scenario_ids:
            continue
        results = scenario.get("task_results", [])
        if not isinstance(results, list):
            raise ValueError("saved task_results must be a list")
        for saved in results:
            if not isinstance(saved, dict) or not isinstance(saved.get("record"), dict):
                raise ValueError("saved task result must retain its original record")
            record = saved["record"]
            node_ids = {node.get("id") for node in record.get("nodes", [])
                        if isinstance(node, dict)}
            if view.task_id and record.get("root_task_id") != view.task_id \
                    and view.task_id not in node_ids:
                continue
            quality = saved.get("effective_quality") or {}
            if not isinstance(quality, dict):
                raise ValueError("saved effective_quality must be an object")
            artifacts = saved.get("artifact_snapshot") or {}
            memory = saved.get("memory_snapshot") or {}
            if not isinstance(artifacts, dict) or not isinstance(memory, dict):
                raise ValueError("saved artifact/memory captures must be objects")
            reference = artifacts.get("reference")
            capture = artifacts.get("state", "unknown")
            if isinstance(reference, dict):
                capture += (
                    f"; {reference.get('path', 'unknown')}; "
                    f"sha={reference.get('sha256', 'unknown')}"
                )
            table.add_row(
                _cell(scenario["scenario_id"]),
                _cell(f"{record.get('phase', 'unknown')} / {record.get('root_task_id', 'unknown')}"),
                _cell(record.get("root_status")),
                _cell(record.get("quality_status")),
                _cell(quality.get("quality_status")),
                _cell(quality.get("quality_source")),
                _cell(capture), _cell(memory.get("status")),
            )
        state = scenario.get("state_snapshot")
        if isinstance(state, dict) and state.get("state") not in {None, "unknown"}:
            console.print(Text(
                f"{scenario['scenario_id']} 场景诊断：{state.get('state')}，不计为额外交付。",
            ))
    console.print(table)
    console.print(Text(
        "质量、产物引用和来源沿用 P3 保存值；导出文件的展示不重新验证外部文件或推断人工身份。",
    ))


def _print_summary(console, view):
    by_kind = defaultdict(int)
    for item in view.items:
        by_kind[item.event.kind] += 1
    console.print(Text(f"{len(view.items)} 条唯一 span；"
                       + "  ".join(f"{kind}×{count}" for kind, count in sorted(by_kind.items()))))
    if view.duplicate_records:
        console.print(Text(f"相同身份/内容的重复记录 {view.duplicate_records} 条，仅显示一次。"))
    console.print(Text(
        "单个 span 耗时和 trace tokens 仅展示原记录；不据事件跨度重算整场耗时、模型计费或任务质量。",
    ))


def replay(source, task_id: str | None = None, console: Console | None = None, *,
           scenario_id: str | None = None) -> list[TraceEvent]:
    """Render an original timeline, keeping scenario identity visible."""
    console = console or Console()
    view = load_replay(source, task_id, scenario_id=scenario_id)
    _print_evidence(console, view)
    if not view.items:
        console.print(Text("(没有可回放的 trace 事件)", style="dim"))
        return []
    table = Table(title=f"Trace 回放（{len(view.items)} 条 span）", show_lines=False)
    for title in ("scenario", "时间", "task", "agent", "kind", "name / span", "status",
                  "耗时", "trace tokens in/out"):
        table.add_column(title)
    for item in view.items:
        event = item.event
        token_text = "-"
        if event.kind == "llm" and all(key in item.record for key in ("tokens_in", "tokens_out")):
            token_text = f"{item.record['tokens_in']}/{item.record['tokens_out']}"
        table.add_row(
            _cell(item.scenario_id), _cell(event.t0.isoformat()),
            _cell(event.task_id), _cell(event.agent_id),
            Text(event.kind, style=_KIND_COLOR.get(event.kind, "")),
            _cell(f"{event.name} / {event.span_id}"), _cell(event.status),
            _cell(_duration(event)), _cell(token_text),
        )
    console.print(table)
    _print_summary(console, view)
    return [item.event for item in view.items]


def replay_tree(source, task_id: str, console: Console | None = None, *,
                scenario_id: str | None = None) -> None:
    """Print each saved span once; preserve unknown parents and cyclic components."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("replay_tree requires a non-empty original task_id")
    console = console or Console()
    view = load_replay(source, task_id, scenario_id=scenario_id)
    _print_evidence(console, view)
    if not view.items:
        console.print(Text(f"(任务 {task_id} 没有 trace)", style="dim"))
        return
    by_id = {item.event.span_id: item for item in view.items}
    children = defaultdict(list)
    for item in view.items:
        parent = item.event.parent_span
        if parent in by_id:
            children[parent].append(item)
    for rows in children.values():
        rows.sort(key=_order)
    roots = [item for item in view.items
             if item.event.parent_span is None or item.event.parent_span not in by_id]
    console.print(Panel(Text(
        f"任务 {task_id} 的原始 span 树；scenario={scenario_id or view.scenario_ids or 'single source'}",
    ), border_style="cyan"))
    rendered = set()

    def walk(first):
        pending = [(first, 0)]
        while pending:
            item, depth = pending.pop()
            event = item.event
            if event.span_id in rendered:
                continue
            rendered.add(event.span_id)
            parent = "None" if event.parent_span is None else event.parent_span
            missing = ("；父 span 未包含在本任务中" if event.parent_span is not None
                       and event.parent_span not in by_id else "")
            prefix = "  " * min(depth, 20)
            if depth > 20:
                prefix += f"... depth={depth} "
            console.print(Text(
                f"{prefix}{event.kind} {event.name} [span={event.span_id}, parent={parent}] "
                f"({_duration(event)}, {event.status or 'unknown'}){missing}",
                style=_KIND_COLOR.get(event.kind, ""),
            ))
            pending.extend((child, depth + 1)
                           for child in reversed(children.get(event.span_id, [])))

    for root in sorted(roots, key=_order):
        walk(root)
    for item in view.items:
        if item.event.span_id not in rendered:
            console.print(Text("循环/无自然根分量：保留原 parent，以下仅遍历一次。", style="yellow"))
            walk(item)
    _print_summary(console, view)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读回放原始 trace 或 P3 老师会话证据")
    parser.add_argument("source", help="明确的 JSONL、正式 RUN 目录或 instructor_replay JSON")
    parser.add_argument("--task", help="原 task_id；跨场景重复时须同时选 --scenario")
    parser.add_argument("--scenario", help="正式会话/导出中的原 scenario_id")
    parser.add_argument("--tree", action="store_true", help="展示指定任务的 span 树")
    args = parser.parse_args(argv)
    if args.tree and not args.task:
        parser.error("--tree requires --task")
    try:
        if args.tree:
            replay_tree(args.source, args.task, scenario_id=args.scenario)
        else:
            replay(args.source, args.task, scenario_id=args.scenario)
    except (OSError, ValueError, TypeError) as exc:
        Console(stderr=True).print(Text(f"无法回放：{exc}"))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
