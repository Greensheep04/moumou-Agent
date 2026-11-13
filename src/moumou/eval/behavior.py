"""Verify collaboration evidence without changing the shared acceptance checker.

Artifact correctness and observed behavior are separate verdicts. In particular,
an ablation's disabled feature is checked as disabled rather than deliberately
scored as a failure. Trace evidence never makes an incorrect artifact succeed.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def expected_behavior_targets(fixture) -> list[str]:
    requirements = fixture.requirements
    targets = [f"approval:{item.tool}:{item.decision}" for item in requirements.approvals]
    targets.extend(f"tool:{name}" for name in requirements.required_tools)
    if requirements.min_children or requirements.parallel_children:
        targets.append("delegation:children")
    if requirements.memory_injection:
        targets.append("memory:injection")
    if requirements.parallel_children:
        targets.append("scheduling:children")
    return targets


def evaluate_behavior(fixture, events, *, nodes, options, memory_observations) -> list[dict]:
    """Return stable, JSON-serializable checks derived from recorded execution.

    ``memory_observations`` is the runner's actual context-citation snapshot,
    either ``{task_id: [memory_id, ...]}`` or a list of dictionaries containing
    ``task_id`` and ``memory_ids``. It is not a model's claim that it used memory.
    Child concurrency uses positive-duration LLM/tool intervals, not parent
    envelopes which could overlap while children merely waited for dispatch.
    """
    requirements = fixture.requirements
    events = list(events)
    nodes = list(nodes)
    checks = []

    def add(target, ok, evidence):
        checks.append({"kind": "behavior", "target": target, "ok": bool(ok),
                       "evidence": evidence})

    for requirement in requirements.approvals:
        approvals = [
            event for event in events
            if _get(event, "kind") == "approval" and _get(event, "name") == requirement.tool
            and _get(event, "status") == "ok"
            and _get(event, "payload", {}).get("decision") == requirement.decision
        ]
        successes = [
            event for event in events
            if _get(event, "kind") == "tool" and _get(event, "name") == requirement.tool
            and _get(event, "status") == "ok"
            and _get(event, "payload", {}).get("ok") is True
        ]
        # A same-task effect must follow approval. A rejected action must have no
        # successful effect anywhere in this attempt, including a later retry.
        approved_effects = [
            effect for effect in successes
            if any(_get(effect, "task_id") == _get(approval, "task_id")
                   and _timestamp(_get(effect, "t0")) is not None
                   and _timestamp(_get(approval, "t1")) is not None
                   and _timestamp(_get(effect, "t0")) >= _timestamp(_get(approval, "t1"))
                   for approval in approvals)
        ]
        ok = bool(approvals) and (
            bool(approved_effects) if requirement.decision == "approve" else not successes
        )
        add(f"approval:{requirement.tool}:{requirement.decision}", ok, {
            "decision_span_ids": [_get(event, "span_id") for event in approvals],
            "successful_tool_span_ids": [_get(event, "span_id") for event in successes],
        })

    for name in requirements.required_tools:
        observed = [
            event for event in events
            if _get(event, "kind") == "tool" and _get(event, "name") == name
            and _get(event, "status") == "ok"
            and _get(event, "payload", {}).get("ok") is True
        ]
        add(f"tool:{name}", bool(observed), {
            "span_ids": [_get(event, "span_id") for event in observed],
        })

    node_map = {_get(node, "id"): node for node in nodes}
    root_ids = {_get(node, "id") for node in nodes if _get(node, "parent_id") is None}
    if not root_ids:
        root_ids = {_get(node, "parent_id") for node in nodes
                    if _get(node, "parent_id") not in node_map}
    child_ids = {_get(node, "id") for node in nodes if _get(node, "parent_id") in root_ids
                 and _get(node, "parent_id") is not None}
    child_ids.discard(None)
    plans = [event for event in events
             if _get(event, "kind") == "delegate" and _get(event, "name") == "plan_applied"
             and _get(event, "status") == "ok"]
    delegated = {child for event in plans for child in _get(event, "payload", {}).get(
        "children", []) if isinstance(child, str)}
    observed_children = child_ids & delegated
    if requirements.min_children or requirements.parallel_children:
        minimum = max(requirements.min_children, 2 if requirements.parallel_children else 0)
        dependencies = {name: _get(node_map[name], "deps", []) for name in observed_children}
        invalid_dependencies = []

        def ancestors(name):
            found, pending = set(), [name]
            while pending:
                current = pending.pop()
                deps = _get(node_map[current], "deps", [])
                if not isinstance(deps, (tuple, list)):
                    invalid_dependencies.append(current)
                    continue
                for dependency in deps:
                    if not isinstance(dependency, str) or dependency not in node_map:
                        invalid_dependencies.append(current)
                    elif dependency not in found:
                        found.add(dependency)
                        pending.append(dependency)
            if name in found:
                invalid_dependencies.append(name)
            return found

        transitive = {name: ancestors(name) for name in observed_children}
        ordered = sorted(observed_children)
        independent = [
            [left, right] for index, left in enumerate(ordered) for right in ordered[index + 1:]
            if _get(node_map[left], "parent_id") == _get(node_map[right], "parent_id")
            and left not in transitive[right] and right not in transitive[left]
        ]
        valid = len(observed_children) >= minimum
        if requirements.parallel_children:
            valid = valid and bool(independent) and not invalid_dependencies
        add("delegation:children", valid, {
            "required": minimum, "child_ids": sorted(observed_children),
            "plan_span_ids": [_get(event, "span_id") for event in plans],
            "child_dependencies": dependencies,
            "independent_sibling_pairs": independent,
            "invalid_dependency_nodes": sorted(set(invalid_dependencies)),
        })

    if requirements.memory_injection:
        if isinstance(memory_observations, dict):
            observations = [{"task_id": task, "memory_ids": ids}
                            for task, ids in memory_observations.items()]
        else:
            observations = list(memory_observations or [])
        injected = []
        unexpected_injections = []
        for item in observations:
            ids = _get(item, "memory_ids", [])
            if isinstance(ids, (list, tuple)):
                ids = [value for value in ids if isinstance(value, str) and value]
                if ids and _get(item, "injected", True) is not False:
                    injected.append({"task_id": _get(item, "task_id"), "memory_ids": ids})
                if ids or _get(item, "injected", False) is True:
                    unexpected_injections.append({
                        "task_id": _get(item, "task_id"), "memory_ids": ids,
                        "injected": _get(item, "injected", bool(ids)),
                    })
        enabled = _get(options, "memory_enabled", True)
        add("memory:injection", bool(injected) if enabled else not unexpected_injections, {
            "memory_enabled": enabled, "injections": injected,
            "observed_injections": unexpected_injections,
            "observation_available": memory_observations is not None,
        })
        if memory_observations is None:
            checks[-1]["ok"] = False

    if requirements.parallel_children:
        intervals = []
        for event in events:
            if (_get(event, "task_id") not in observed_children
                    or _get(event, "kind") not in ("llm", "tool")
                    or _get(event, "status") != "ok"):
                continue
            start, end = _timestamp(_get(event, "t0")), _timestamp(_get(event, "t1"))
            if start is not None and end is not None and end > start:
                intervals.append((_get(event, "task_id"), start, end, _get(event, "span_id")))
        overlaps = [
            [left[3], right[3]]
            for index, left in enumerate(intervals) for right in intervals[index + 1:]
            if left[0] != right[0] and max(left[1], right[1]) < min(left[2], right[2])
        ]
        measured_children = {interval[0] for interval in intervals}
        enabled = _get(options, "max_slots", 4) > 1
        ok = len(measured_children) >= 2 and (bool(overlaps) if enabled else not overlaps)
        add("scheduling:children", ok, {
            "parallel_enabled": enabled, "measured_child_ids": sorted(measured_children),
            "overlap_span_pairs": overlaps[:20],
        })
    assert [check["target"] for check in checks] == expected_behavior_targets(fixture)
    return checks
