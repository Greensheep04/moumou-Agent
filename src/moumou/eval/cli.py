"""Instructor scenarios: persistent four-member teams with real human input.

All formal task evaluation uses this protocol. Agent completion, machine
acceptance and human quality review are distinct.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import math
import os
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from moumou.config import LLM_PROFILES, LLMSettings
from moumou.contracts import Budget, ContextPolicy
from moumou.orchestrator.timing import ROOT_CLOSEOUT_SECONDS, ExecutionClock, ExecutionDeadline
from moumou.sandbox.workspace import workspace_sandbox_available


class _TaskIncomplete(Exception):
    pass


class _QualityRejected(Exception):
    pass


_NODE_TIMEOUT_S = 300
_SCENARIO_CLOSEOUT_S = 1500
_SCENARIO_TIMEOUT_S = 1800
_LEGACY_STREAM_CONTENT_CHARS = 2 * 1024 * 1024


def _execution_policy(scenario_timeout):
    return {
        "phase_time_policy": "lead_estimate",
        "root_closeout_allowance_s": ROOT_CLOSEOUT_SECONDS,
        "scenario_closeout_s": min(_SCENARIO_CLOSEOUT_S, scenario_timeout),
        "scenario_timeout_s": scenario_timeout,
        "human_wait_in_node_budget": False,
        "scenario_timeout_excludes_quality_review": True,
        "scenario_timeout_excludes_human_wait": True,
    }


def _stream_output_options(args, transport):
    """Validate explicit bounds before reading keys or creating a session."""
    requested = {
        "completion_cap": getattr(args, "llm_completion_cap", None),
        "max_content_chars": getattr(args, "llm_max_content_chars", None),
    }
    options = {}
    for name, value in requested.items():
        if value is None:
            continue
        # This is a configuration representation bound, not a provider token limit.
        if type(value) is not int or not 0 < value <= 2**31 - 1:
            raise ValueError(f"{name} must be a positive 32-bit integer")
        options[name] = value
    if options and transport != "stream":
        raise ValueError("explicit output bounds require --llm-transport stream")
    return options


def _opening_context_options(args):
    """The formal CLI uses default P1 limits unless an opening quota is selected."""
    from moumou.eval.team import teacher_opening_policy

    requested = getattr(args, "opening_context_tokens", None)
    policy = teacher_opening_policy(
        requested, policy=ContextPolicy(**(getattr(args, "llm_limits", None) or {}))
    )
    return {
        "requested_tokens": requested,
        "effective_tokens": policy.opening_budget_tokens,
        "prompt_ceiling_tokens": policy.prompt_ceiling,
        "scope": "teacher opening context only; full required context remains mandatory",
        "token_estimator_calibration": "preserved",
    }


def _stream_tool_choice_options(args, transport):
    """A declared request policy, not a retry or provider capability assertion."""
    policy = getattr(args, "llm_tool_choice", None)
    if policy is None:
        return {}
    if type(policy) is not str or policy not in {"auto", "required-when-tools"}:
        raise ValueError("tool choice must be auto or required-when-tools")
    if transport != "stream":
        raise ValueError("explicit tool choice requires --llm-transport stream")
    return {"tool_choice_policy": policy}


def _stream_http_retry_options(args, transport):
    """Opt in before keys/output; omitted preserves old constructor shims."""
    retries = getattr(args, "llm_transient_http_retries", None)
    if retries is None:
        return {}
    if type(retries) is not int or retries not in (0, 1, 2):
        raise ValueError("transient HTTP retries must be an integer from 0 to 2")
    if transport != "stream":
        raise ValueError("explicit HTTP retries require --llm-transport stream")
    return {"transient_http_retries": retries}


def _research_progress_enabled(args):
    enabled = getattr(args, "research_progress", False)
    if type(enabled) is not bool:
        raise ValueError("research_progress must be a boolean")
    return enabled


def _tool_approval(args):
    requested = getattr(args, "approval_mode", "manual")
    sandboxed = requested != "manual" and workspace_sandbox_available()
    if requested == "workspace" and not sandboxed:
        raise ValueError("workspace approval mode requires a working macOS sandbox-exec")
    return {
        "requested": requested,
        "shell_python": "workspace" if sandboxed else "manual",
        "workspace_delete": "manual" if requested == "manual" else "automatic",
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the instructor's continuous team scenarios")
    parser.add_argument("--tasks", default="eval/tasks", help="instructor Markdown directory")
    parser.add_argument("--scenario", default="all", help="scenario id, or all in source order")
    parser.add_argument("--out", default=None, help="fresh suite directory under local runs")
    parser.add_argument("--human", choices=("console", "files"), default="console")
    parser.add_argument(
        "--approval-mode", choices=("auto", "workspace", "manual"), default="auto",
        help="auto-detect bounded macOS execution, require it with workspace, or use manual",
    )
    parser.add_argument(
        "--llm-profile",
        default=None,
        choices=sorted(LLM_PROFILES),
        help="model endpoint: 'deepseek' (official default) or 'hkgai' "
             "(course gateway). Also settable via MOUMOU_LLM_PROFILE.",
    )
    parser.add_argument(
        "--llm-transport", choices=("buffered", "stream"), default="buffered",
        help="explicit optional school SSE transport; default buffered client is unchanged",
    )
    parser.add_argument(
        "--llm-reasoning-effort", choices=("none",), default=None,
        help="explicit school stream reasoning setting; omitted uses the provider default",
    )
    parser.add_argument(
        "--llm-completion-cap", type=int, default=None,
        help="explicit school stream max_tokens request; provider enforcement is not assumed",
    )
    parser.add_argument(
        "--llm-max-content-chars", type=int, default=None,
        help="local ordinary-content Unicode-codepoint bound, separate from tool arguments; "
             "omitted retains the existing 2097152-character bound",
    )
    parser.add_argument(
        "--llm-tool-choice", choices=("auto", "required-when-tools"), default=None,
        help="explicit stream request policy; required-when-tools requests required only "
             "when nonempty tool schemas are offered, preserving tool-free plain calls",
    )
    parser.add_argument(
        "--llm-transient-http-retries", type=int, choices=(0, 1, 2), default=None,
        help="explicit stream-only retries for pre-body HTTP 502/503/504; fixed 5s/15s "
             "backoff shares the call deadline; omitted preserves zero HTTP retries",
    )
    parser.add_argument(
        "--require-declared-inputs", action="store_true",
        help="explicit teacher input manifests and first-admission file checks",
    )
    parser.add_argument(
        "--research-progress", action="store_true",
        help="opt-in formal-session compact evidence indexes, repeat feedback and retry "
             "checkpoint; native source validation, cache, audit and tools stay authoritative",
    )
    parser.add_argument(
        "--execution-progress", action="store_true",
        help="opt-in feedback on successful shell and Python calls repeated without file changes",
    )
    parser.add_argument(
        "--opening-context-tokens", type=int, default=None,
        help="explicit teacher-only opening quota, at least 500 and within the prompt "
             "ceiling; omitted preserves the existing 8000-token opening budget",
    )
    parser.add_argument(
        "--quality-review",
        choices=("human", "deferred"),
        default="human",
        help="real post-delivery quality review; deferred never implies success",
    )
    parser.add_argument("--plan", action="store_true", help="inspect sources without API calls")
    parser.add_argument(
        "--reuse-sources", default=None,
        help="reuse only public search results from a saved terminal teacher run; retains "
             "original retrieval dates and does not import answers or count as new searches",
    )
    parser.add_argument(
        "--search-requests",
        type=int,
        default=6,
        help="new search provider requests per scenario, shared by both tasks",
    )
    parser.add_argument("--max-steps", type=int, default=80, help="per-node safety bound")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=Budget().max_tokens,
        help="per-node cumulative safety bound, not a price ceiling",
    )
    parser.add_argument(
        "--task-timeout",
        type=int,
        default=_NODE_TIMEOUT_S,
        help="execution seconds per node, excluding its waits for human responses",
    )
    parser.add_argument(
        "--scenario-timeout", type=float, default=_SCENARIO_TIMEOUT_S,
        help="execution deadline for both tasks in a scenario, excluding waits for human responses",
    )
    parser.add_argument(
        "--compact-trigger-tokens",
        type=int,
        default=None,
        help="prompt-estimate threshold that starts compaction; defaults to the built-in "
             "guardrail. Choose it from the target group's own request distribution, not "
             "from a guess: the baseline software group ran at a median of ~21k and a 75th "
             "percentile of ~29k estimated tokens per request.",
    )
    parser.add_argument(
        "--max-request-tokens",
        type=int,
        default=None,
        help="hard cap on one request's estimated size, checked after compaction and "
             "before sending; must sit above the compaction target",
    )
    parser.add_argument(
        "--max-tool-result-chars",
        type=int,
        default=None,
        help="longest tool body admitted into the messages on first entry; longer "
             "bodies become a head/tail window plus a re-readable file reference",
    )
    parser.add_argument(
        "--compact-target-tokens",
        type=int,
        default=None,
        help="prompt estimate compaction aims for; must sit below the trigger",
    )
    args = parser.parse_args(argv)
    if args.llm_reasoning_effort is not None and args.llm_transport != "stream":
        parser.error("--llm-reasoning-effort requires --llm-transport stream")
    try:
        # Resolve the endpoint profile up front: its context window fixes the
        # prompt ceiling, and the compaction options below are validated against
        # that ceiling.
        args.llm_settings = LLMSettings.public_from_env(profile=args.llm_profile)
        spec = LLM_PROFILES[args.llm_settings.profile]
        args.llm_profile_name = spec.name
        args.llm_limits = {
            "context_window": spec.context_window,
            "max_output_tokens": spec.max_output_tokens,
        }
        _stream_output_options(args, args.llm_transport)
        _opening_context_options(args)
        _stream_tool_choice_options(args, args.llm_transport)
        _stream_http_retry_options(args, args.llm_transport)
        _research_progress_enabled(args)
        _compact_options(args)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _compact_options(args):
    """Resolve and record one context policy for preview and execution."""
    trigger = args.compact_trigger_tokens
    target = args.compact_target_tokens
    limits = getattr(args, "llm_limits", None) or {}
    policy = ContextPolicy(**limits)
    ceiling = policy.prompt_ceiling
    if getattr(args, "llm_profile_name", None) == "deepseek":
        default_trigger, default_target, default_cap = 600_000, 400_000, 610_000
        default_tool_chars = 8_192
    else:
        default_trigger, default_target, default_cap = (
            policy.compact_trigger_tokens, policy.compact_target_tokens, policy.max_request_tokens
        )
        default_tool_chars = policy.max_tool_result_chars
    cap = args.max_request_tokens if args.max_request_tokens is not None else min(
        default_cap, ceiling
    )
    if type(cap) is not int or cap < 1_000:
        raise ValueError("--max-request-tokens must be an integer >= 1000")
    if cap > ceiling:
        raise ValueError(f"--max-request-tokens exceeds the prompt ceiling ({ceiling})")
    if trigger is None:
        trigger = min(default_trigger, ceiling)
        if args.max_request_tokens is not None:
            trigger = min(trigger, cap)
    if target is None:
        target = min(default_target, cap)
        if target >= trigger:
            target = int(trigger * 0.85)
    if type(trigger) is not int or trigger < 1_000:
        raise ValueError("--compact-trigger-tokens must be an integer >= 1000")
    if type(target) is not int or target < 1_000:
        raise ValueError("--compact-target-tokens must be an integer >= 1000")
    if target >= trigger:
        raise ValueError(
            "--compact-target-tokens must be below --compact-trigger-tokens, otherwise "
            "compaction never creates headroom"
        )
    if trigger > ceiling:
        raise ValueError(
            f"--compact-trigger-tokens exceeds the prompt ceiling ({ceiling})"
        )
    if target > cap:
        raise ValueError("--max-request-tokens must be at least the compaction target")
    tool_chars = args.max_tool_result_chars if args.max_tool_result_chars is not None else (
        default_tool_chars
    )
    if type(tool_chars) is not int or tool_chars < 500:
        raise ValueError("--max-tool-result-chars must be an integer >= 500")
    args.compact_effective = {
        "requested_trigger": args.compact_trigger_tokens,
        "requested_target": args.compact_target_tokens,
        "effective_trigger": trigger,
        "effective_target": target,
        "max_request_tokens": cap,
        "max_tool_result_chars": tool_chars,
        "context_window": policy.context_window,
        "max_output_tokens": policy.max_output_tokens,
        "prompt_ceiling": ceiling,
        "basis": "profile defaults with operator overrides; thresholds include tool schemas",
    }


def _compact_settings(args):
    """Settings kwargs for the policy resolved against the selected endpoint."""
    effective = getattr(args, "compact_effective", None)
    if not effective:
        return {}
    return {
        "compact_trigger_tokens": effective["effective_trigger"],
        "compact_target_tokens": effective["effective_target"],
        "max_request_tokens": effective["max_request_tokens"],
        "max_tool_result_chars": effective["max_tool_result_chars"],
    }


def _now():
    return datetime.now(UTC).isoformat()


def _dataset_kind(scenarios):
    kinds = {scenario.source_kind for scenario in scenarios}
    if len(kinds) != 1 or not kinds <= {"instructor", "supplementary"}:
        raise ValueError("one test suite must use a single declared evaluation-bank source")
    return next(iter(kinds))


def _tree(repo, root_id):
    tasks = repo.all_tasks()
    selected = {root_id}
    while True:
        following = selected | {node.id for node in tasks if node.parent_id in selected}
        if following == selected:
            return [node for node in tasks if node.id in selected]
        selected = following


def _task_record(engine, node, elapsed, phase):
    from moumou.eval.session_evidence import (
        capture_failure_snapshot,
        capture_memory_snapshot,
        capture_node_usage,
    )

    nodes = _tree(engine.repo, node.id)
    active = engine.repo.active_descendants(node.id)
    current = engine.repo.get(node.id)
    events = {item.id: engine.trace.query(item.id) for item in nodes}
    exit_reasons = {}
    for item in nodes:
        if item.is_terminal or item.status == "FAILED":
            transitions = engine.repo.event_log(item.id)
            if transitions:
                exit_reasons[item.id] = transitions[-1]["reason"]
    posted = next((event.t0.isoformat() for event in events[node.id]
                   if event.name == "instructor_task_posted"), None)
    verdicts = [
        event
        for event in events[node.id]
        if event.kind == "acceptance" and event.name == "verdict"
    ]
    has_criteria = bool(current.acceptance)
    machine_accepted = verdicts[-1].payload.get("ok") is True if has_criteria and verdicts else None
    time_plan = next((event.payload for event in reversed(events[node.id])
                      if event.name == "phase_time_planned"), None)
    return {
        "phase": phase,
        "root_task_id": current.id,
        "root_role": current.role,
        "root_status": current.status,
        "instructor_task_posted_at": posted,
        "elapsed_s": round(elapsed, 6),
        "phase_time_plan": time_plan,
        "root_closeout_used_s": round(sum(
            event.payload.get("root_closeout_used_s", 0)
            for event in events[node.id] if event.name == "execution"
        ), 6),
        "nodes": [
            {
                "id": item.id,
                "parent_id": item.parent_id,
                "project_id": item.project_id,
                "conversation_id": item.conversation_id,
                "role": item.role,
                "agent_id": item.role,
                "identity_policy": "instructor_fixed_role",
                "status": item.status,
                "deps": list(item.deps),
                "artifacts": list(item.artifacts),
                "error": item.error,
                "exit_reason": exit_reasons.get(item.id),
                "budget": item.budget.model_dump(mode="json"),
                "llm": capture_node_usage(events[item.id]),
                "retries": item.retries,
                "wakeups": item.wakeups,
                "blocked": item.blocker,
                "superseded_by": item.superseded_by,
                "published_version": engine.repo.published_version(item.id),
                "machine_criteria": len(item.acceptance),
                "result": item.result.model_dump(mode="json") if item.result else None,
            }
            for item in nodes
        ],
        "observed_subtasks": len(nodes) - 1,
        "active_subtasks": len(active),
        "active_subtasks_done": sum(item.status == "DONE" for item in active),
        "observed_subtasks_done": sum(
            item.id != current.id and item.status == "DONE" for item in nodes
        ),
        "root_machine_criteria_present": has_criteria,
        "root_machine_accepted": machine_accepted,
        "agent_declared_complete": current.status == "DONE",
        "quality_status": "awaiting_human_or_independent_review",
        "verified_goal_success": None,
        "memory_snapshot": capture_memory_snapshot(engine, nodes),
        "failure_snapshot": capture_failure_snapshot(engine, nodes),
        "note": "DONE or empty-criteria self-report does not establish instructor task quality.",
    }


def _report(manifest):
    lines = [
        "# Test session",
        "",
        f"Status: {manifest['status']}",
        f"Dataset source: {manifest['dataset_kind']}",
        f"Search provider: {manifest.get('search_provider', 'tavily')}",
        "Protocol: sequential team conversation scenarios.",
        "Agent completion, machine evidence and reviewed quality are reported separately.",
        "",
        "| scenario | phase | root | agent status | machine accepted | quality |",
        "|---|---|---|---|---|---|",
    ]
    for scenario in manifest["scenarios"]:
        for task in scenario.get("tasks", []):
            lines.append(
                f"| {scenario['scenario_id']} | {task['phase']} | "
                f"{task['root_task_id']} | {task['root_status']} | "
                f"{task['root_machine_accepted']} | {task['quality_status']} |"
            )
    lines += [
        "",
        "## Scenario time",
        "",
        "| scenario | status | wall seconds | execution seconds | human wait seconds "
        "| quality-review wait seconds |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for scenario in manifest["scenarios"]:
        lines.append(
            f"| {scenario['scenario_id']} | {scenario.get('status', '-')} | "
            f"{scenario.get('elapsed_s', '-')} | {scenario.get('execution_elapsed_s', '-')} | "
            f"{scenario.get('human_wait_s', '-')} | "
            f"{scenario.get('quality_review_wait_s', '-')} |"
        )
    lines += [
        "",
        "## Model prompt cache",
        "",
        "The rate is weighted by input tokens across calls with provider cache data.",
        "",
        "| scenario | measured calls / all calls | hit tokens | miss tokens | hit rate |",
        "|---|---|---|---|---|",
    ]
    for scenario in manifest["scenarios"]:
        total = scenario.get("llm", {}).get("total", {})
        cache = total.get("prompt_cache", {})
        hit, miss, rate = (cache.get(key) for key in ("hit_tokens", "miss_tokens", "hit_rate"))
        lines.append(
            f"| {scenario['scenario_id']} | {cache.get('measured_calls', 0)}/"
            f"{total.get('calls', 0)} | {hit if hit is not None else 'unknown'} | "
            f"{miss if miss is not None else 'unknown'} | "
            f"{f'{rate:.1%}' if rate is not None else 'unknown'} |"
        )
    lines += [
        "",
        "Full dialogue is in each scenario's dialogue.jsonl; actual human exchanges "
        "are also retained under human/. Task 2 uses the same team, experience and "
        "delivered files. Search cache hits are not fresh provider requests.",
        "Human wait spans are measured separately and can overlap; they are not "
        "subtracted blindly from end-to-end wall time.",
        "",
    ]
    return "\n".join(lines)


def _bind_phase_workspaces(paths, quote, artifact_quote, source_audit, research, *, phase_change):
    quote.bind_workspaces(paths.values(), phase_change=phase_change)
    artifact_quote.bind_workspaces(paths.values(), phase_change=phase_change)
    source_audit.bind_workspaces(paths, phase_change=phase_change)
    if phase_change and research is not None:
        research.bind_workspaces(paths)


async def run_suite(args, scenarios):
    from moumou.adapters import DeepSeekClient
    from moumou.contracts import HumanRequest
    from moumou.eval.artifacts import _code_version, atomic_json, atomic_text, redact
    from moumou.eval.human import ConsoleHuman, FileHuman, RecordingLLM
    from moumou.eval.quote_delivery import capture_artifact_quote_evidence
    from moumou.eval.search_budget import SearchBudgetTool
    from moumou.eval.session_evidence import (
        capture_artifact_snapshot,
        capture_scenario_state,
        sanitize_evidence,
    )
    from moumou.eval.source_evidence import SourceEvidenceStore
    from moumou.eval.source_seed import reuse_public_sources
    from moumou.eval.team import configure_team, submit_teacher_task
    from moumou.orchestrator import Engine, Settings
    from moumou.tools import ToolRegistry, default_tools
    from moumou.tools.artifact_quote import VerifyArtifactQuoteTool
    from moumou.tools.search import WebSearchTool
    from moumou.tools.source import ReadSourceTool, SourceHistoryTool
    from moumou.tools.source_audit import SourceAccessAuditTool
    from moumou.tools.source_quote import VerifySourceQuoteTool

    dataset_kind = _dataset_kind(scenarios)
    approval = _tool_approval(args)
    workspace_execution = approval["shell_python"] == "workspace"
    node_retries = 0 if dataset_kind == "instructor" else 1
    llm_transport = getattr(args, "llm_transport", "buffered")
    reasoning_effort = getattr(args, "llm_reasoning_effort", None)
    if llm_transport not in {"buffered", "stream"}:
        raise ValueError("unknown LLM transport")
    if (reasoning_effort not in (None, "none")
            or (reasoning_effort is not None and llm_transport != "stream")):
        raise ValueError("explicit reasoning effort requires the school stream transport")
    stream_output_options = _stream_output_options(args, llm_transport)
    stream_tool_options = _stream_tool_choice_options(args, llm_transport)
    stream_http_retry_options = _stream_http_retry_options(args, llm_transport)
    research_progress_enabled = _research_progress_enabled(args)
    execution_progress_enabled = getattr(args, "execution_progress", False)
    if type(execution_progress_enabled) is not bool:
        raise ValueError("execution_progress must be a boolean")
    transient_http_retries = stream_http_retry_options.get("transient_http_retries", 0)
    tool_choice_policy = stream_tool_options.get("tool_choice_policy", "auto")
    completion_cap = stream_output_options.get("completion_cap")
    max_content_chars = stream_output_options.get(
        "max_content_chars", _LEGACY_STREAM_CONTENT_CHARS,
    )
    require_inputs = getattr(args, "require_declared_inputs", False)
    if type(require_inputs) is not bool:
        raise ValueError("require_declared_inputs must be a boolean")
    scenario_timeout = getattr(args, "scenario_timeout", _SCENARIO_TIMEOUT_S)
    if not math.isfinite(scenario_timeout) or scenario_timeout <= 0:
        raise ValueError("scenario timeout must be positive and finite")
    opening_context_options = _opening_context_options(args)
    settings = LLMSettings.from_env(profile=getattr(args, "llm_profile_name", args.llm_profile))
    search_backend = WebSearchTool()
    settings.require_key()
    search_backend.api_key = os.getenv(search_backend.key_env)
    root = (Path(args.out) if args.out else Path("runs/test") / uuid4().hex[:10]).resolve()
    if root.exists():
        raise ValueError("test needs a fresh output directory; existing sessions are preserved")
    root.mkdir(parents=True)
    secrets = tuple(value for value in (
        settings.api_key, os.getenv("TAVILY_API_KEY"), os.getenv("SERPAPI_API_KEY"),
    ) if value)
    manifest = {
        "schema_version": 1,
        "kind": "instructor_test",
        "dataset_kind": dataset_kind,
        "source": str(Path(args.tasks).resolve()),
        "status": "running",
        "started_at": _now(),
        "human_mode": args.human,
        "search_provider": search_backend.provider,
        "search_requests_per_scenario": args.search_requests,
        "context_policy": getattr(args, "compact_effective", None),
        "planned_root_goals": sum(len(scenario.tasks) for scenario in scenarios),
        "scenarios": [],
        "code": _code_version(),
        "model": {
            "profile": settings.profile,
            "profile_label": LLM_PROFILES[settings.profile].label,
            "model": settings.model,
            "base_url": settings.base_url,
            "endpoint_host": urlsplit(settings.base_url).hostname,
            "api_key_env": settings.api_key_env or None,
            "timeout_s": settings.timeout_s,
            "max_retries": 2 if llm_transport == "buffered" else None,
            "transport": llm_transport,
        },
        "environment": {"python": sys.version, "platform": platform.platform()},
        "task_budget": {
            "max_steps": args.max_steps,
            "max_tokens": args.max_tokens,
            "max_wall_s": args.task_timeout,
            "max_retries": node_retries,
        },
        "school_llm_policy": "quality first; token bound only guards runaway loops",
        **_execution_policy(scenario_timeout),
        "automatic_quality_pass": False,
        "input_handoff_policy": {
            "required": require_inputs, "scope": "declared files at first admission",
            "omitted_inputs_inferred": False, "final_file_review": None,
        },
        "opening_context_policy": opening_context_options,
        "research_progress_policy": {
            "enabled": research_progress_enabled,
            "scope": "explicit formal-session CLI only; instructor or supplementary bank",
            "refresh": "re-emit native bounded result; native cache policy unchanged",
            "observation": "prepared tool output, not model receipt or quality approval",
        },
        "execution_progress_policy": {"enabled": execution_progress_enabled},
        "quality_review_mode": args.quality_review,
        "tool_approval": approval,
    }
    if llm_transport == "stream":
        manifest["model"].update(
            max_attempts=1 + transient_http_retries, retry_transport=False,
            legacy_max_attempts=1,
            transient_http_retries=transient_http_retries,
            transient_http_retries_explicit=bool(stream_http_retry_options),
            http_retry_statuses=[502, 503, 504],
            http_retry_backoff_s=[5, 15][:transient_http_retries],
            http_retry_shared_deadline_s=settings.timeout_s if transient_http_retries else None,
            completion_cap=completion_cap,
            max_content_chars=max_content_chars, reasoning_effort=reasoning_effort,
            provider_output_limit_compliance=None,
            tool_choice_policy=tool_choice_policy,
            tool_choice_explicit=bool(stream_tool_options),
            provider_tool_choice_compliance=None,
        )
    atomic_json(root / "test.json", manifest)
    budget = Budget(
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_wall_s=args.task_timeout,
        max_retries=node_retries,
    )
    print(f"test session: {root}", flush=True)
    unsuccessful_scenarios = []
    try:
        for scenario in scenarios:
            folder = root / scenario.id
            folder.mkdir()
            transcript = folder / "dialogue.jsonl"
            source_evidence = SourceEvidenceStore(folder / "source-evidence")
            source_reuse = (
                reuse_public_sources(args.reuse_sources, scenario.id, source_evidence)
                if getattr(args, "reuse_sources", None) else None
            )
            search = SearchBudgetTool(
                folder / "search", max_requests=args.search_requests, namespace=scenario.id,
                delegate=search_backend, source_store=source_evidence,
            )

            def accept_response(request, response, *, search_budget=search):
                command = re.fullmatch(
                    r"/grant-search\s+([1-9][0-9]*)", (response.text or "").strip()
                )
                if command and request.kind == "question":
                    search_budget.grant(int(command.group(1)), approval_id=request.id)

            human_class = ConsoleHuman if args.human == "console" else FileHuman
            human = human_class(
                folder / "human",
                secrets=secrets,
                transcript_path=transcript,
                on_response=accept_response,
            )
            transport_attempts = [] if llm_transport == "stream" else None

            def persist_transport(*, attempts=transport_attempts, directory=folder):
                if attempts is None:
                    return
                atomic_json(directory / "stream-transport.json", {
                    "schema_version": 1,
                    "kind": "optional_school_stream_attempts",
                    "transport": "stream",
                    "saved_at": _now(),
                    "configured_model": redact(settings.model, settings),
                    "max_attempts_per_call": 1 + transient_http_retries,
                    "legacy_max_attempts": 1,
                    "retry_transport": False,
                    "transient_http_retries": transient_http_retries,
                    "transient_http_retries_explicit": bool(stream_http_retry_options),
                    "http_retry_statuses": [502, 503, 504],
                    "http_retry_backoff_s": [5, 15][:transient_http_retries],
                    "http_retry_shared_deadline_s": (
                        settings.timeout_s if transient_http_retries else None
                    ),
                    "completion_cap": completion_cap,
                    "max_content_chars": max_content_chars,
                    "provider_output_limit_compliance": None,
                    "tool_choice_policy": tool_choice_policy,
                    "tool_choice_explicit": bool(stream_tool_options),
                    "provider_tool_choice_compliance": None,
                    "reasoning_effort": reasoning_effort,
                    "stream_options_sent": False,
                    "call_count": len({row["call_id"] for row in attempts}),
                    "attempt_count": len(attempts),
                    "http_request_attempt_count": sum(
                        row.get("http_request_started") is True for row in attempts
                    ),
                    "attempts": sanitize_evidence(attempts, secrets),
                    "notes": [
                        "Transport call IDs are independent of dialogue RecordingLLM call IDs; "
                        "no order-based identity or phase mapping is inferred.",
                        "Usage counters are observed per attempt; a missing or failed attempt "
                        "does not become measured zero or a complete billing total.",
                        "Only complete SSE responses enter RecordingLLM; this metadata sidecar "
                        "contains no prompts, content, arguments, authorization or full headers.",
                        "completion_cap is a requested max_tokens value, not proof of provider "
                        "enforcement. max_content_chars is a local ordinary-content bound; "
                        "tool arguments retain their separate client bound. Any limit failure "
                        "discards the response rather than declaring the task complete.",
                        "required-when-tools requests required only for nonempty offered tool "
                        "schemas; a tool-free request retains its native plain format. This "
                        "is policy selection before a request, never a failure fallback. "
                        "A completed required response must contain syntactically valid "
                        "calls to offered tool names; this is not argument-schema validation, "
                        "tool execution, factual truth or task-quality approval.",
                        "HTTP retries apply only to 502/503/504 headers before any body/tool. "
                        "Fixed backoff shares one call deadline; actual request starts and "
                        "backoff outcomes are recorded per attempt. Other failures never "
                        "trigger HTTP retries or auto fallback. A successful retried call "
                        "retains attempt usage here but its complete billing total is unknown.",
                    ],
                })

            if llm_transport == "stream":
                from moumou.eval.school_stream import SchoolStreamClient

                delegate = SchoolStreamClient(
                    api_key=settings.api_key,
                    base_url=settings.base_url,
                    model=settings.model,
                    timeout_s=settings.timeout_s,
                    max_attempts=1,
                    retry_transport=False,
                    reasoning_effort=reasoning_effort,
                    on_attempt=transport_attempts.append,
                    **stream_output_options,
                    **stream_tool_options,
                    **stream_http_retry_options,
                )
            else:
                delegate = DeepSeekClient(
                    api_key=settings.api_key,
                    base_url=settings.base_url,
                    model=settings.model,
                    timeout_s=settings.timeout_s,
                    max_retries=2,
                )
            llm = RecordingLLM(delegate, transcript, secrets=secrets)
            quote_verifier = VerifySourceQuoteTool(source_evidence)
            artifact_verifier = VerifyArtifactQuoteTool(source_evidence)
            source_audit = SourceAccessAuditTool(source_evidence)
            history_tool = SourceHistoryTool(source_evidence)
            read_tool = ReadSourceTool(source_evidence)
            research_progress = None
            if research_progress_enabled:
                from moumou.eval.research_progress import TeacherResearchProgress

                research_progress = TeacherResearchProgress(scenario.id, "instructor-group")
                history_tool = research_progress.wrap(history_tool)
                read_tool = research_progress.wrap(read_tool)
            tools = ToolRegistry()
            tools.register_all(
                *(search if tool.name == "web_search" else tool for tool in default_tools()),
                history_tool,
                read_tool,
                quote_verifier,
                artifact_verifier,
                source_audit,
            )
            try:
                engine = Engine(
                    llm,
                    human=human,
                    tools=tools,
                    settings=Settings(
                        root=folder,
                        project_id=scenario.id,
                        conversation_id="instructor-group",
                        memory_enabled=True,
                        max_slots=4,
                        role_review_enabled=False,
                        execution_progress_enabled=execution_progress_enabled,
                        approval={
                            "git_commit": not workspace_execution,
                            "fs_delete": approval["workspace_delete"] == "manual",
                        },
                        workspace_execution_root=root if workspace_execution else None,
                        **(getattr(args, "llm_limits", None) or {}),
                        **_compact_settings(args),
                    ),
                )
            except BaseException:
                try:
                    await llm.aclose()
                finally:
                    persist_transport()
                raise
            human.waiting = engine.runner.wait_for_human
            entry = {
                "scenario_id": scenario.id,
                "name": scenario.name,
                "root": str(folder),
                "status": "running",
                "tasks": [],
                "source_sha256": scenario.source_sha256,
                "source_kind": scenario.source_kind,
                "source_evidence": str(folder / "source-evidence"),
                "source_reuse": source_reuse,
                "roles": [role.model_dump(mode="json") for role in scenario.roles],
            }
            manifest["scenarios"].append(entry)
            previous = None
            scenario_ready = False
            scenario_began = None
            scenario_clock = None
            phase_clock = None
            active_node = None
            try:
                team_options = {"previous_dialogue_path": transcript}
                if scenario.source_kind == "instructor":
                    team_options["enforce_collaboration"] = True
                if require_inputs:
                    team_options["require_declared_inputs"] = True
                if opening_context_options["requested_tokens"] is not None:
                    team_options["opening_budget_tokens"] = (
                        opening_context_options["requested_tokens"]
                    )
                team = configure_team(engine, scenario, **team_options)
                team.on_workspaces_changed = functools.partial(
                    _bind_phase_workspaces, quote=quote_verifier, artifact_quote=artifact_verifier,
                    source_audit=source_audit, research=research_progress, phase_change=True,
                )
                if research_progress is not None:
                    research_progress.bind(engine.repo, team.workspaces, engine.trace)
                    team.context.research_progress = research_progress
                _bind_phase_workspaces(
                    team.workspaces, quote_verifier, artifact_verifier, source_audit,
                    research_progress, phase_change=False,
                )
                team.context.bind_artifact_receipts(engine.repo, artifact_verifier.audit_records)
                team.context.search_budget_notice = (
                    f"This group initially has {args.search_requests} new search requests "
                    "shared across both tasks and all four roles. Cache hits are separate. "
                    "The school model API is available for quality reasoning; this search "
                    "quota is not a token allowance. A human can grant more searches with "
                    "the explicit command /grant-search N.\n"
                )
                entry["workspaces"] = {role: str(path) for role, path in team.workspaces.items()}
                scenario_ready = True
                for index, goal in enumerate(scenario.tasks, start=1):
                    if scenario_clock is not None and scenario_clock.remaining <= 0:
                        raise ExecutionDeadline("scenario")
                    phase = f"task_{index}"
                    llm.set_phase(phase)
                    human.set_phase(phase)
                    atomic_json(
                        root / "active.json",
                        {
                            "scenario_id": scenario.id,
                            "phase": phase,
                            "scenario_root": str(folder),
                            "human_pending": str(folder / "human/pending"),
                            "manifest": str(root / "test.json"),
                        },
                    )
                    search_before = search.stats()
                    node = submit_teacher_task(
                        engine, scenario, goal, budget, previous_root=previous
                    )
                    entry["workspaces"] = {
                        role: str(path) for role, path in team.workspaces.items()
                    }
                    began = monotonic()
                    if scenario_began is None:
                        scenario_began = began
                        scenario_clock = ExecutionClock(
                            "scenario", scenario_timeout, started=began,
                            closeout_s=min(_SCENARIO_CLOSEOUT_S, scenario_timeout),
                        )
                        entry["instructor_task_posted_at"] = next(
                            event.t0.isoformat() for event in engine.trace.query(node.id)
                            if event.name == "instructor_task_posted"
                        )
                    active_node = node
                    phase_clock = ExecutionClock("phase", float("inf"), started=began)
                    engine.runner.scenario_clock = scenario_clock
                    engine.runner.phase_clock = phase_clock
                    engine.runner.later_tasks = len(scenario.tasks) - index
                    atomic_json(root / "test.json", manifest)
                    try:
                        async with scenario_clock.enforce():
                            await engine.run()
                    except ExecutionDeadline as exc:
                        if engine.repo.get(node.id).status != "DONE":
                            raise
                        # A formally published result survives cancellation during cleanup.
                        entry["deadline_reached_after_publication"] = node.id
                        entry["deadline_after_publication_scope"] = exc.scope
                    task = _task_record(engine, node, monotonic() - began, phase)
                    task["execution_elapsed_s"] = round(phase_clock.active_seconds, 6)
                    task["human_wait_s"] = round(phase_clock.paused_seconds, 6)
                    task["delivery_elapsed_s"] = task["elapsed_s"]
                    task["workspaces"] = entry["workspaces"]
                    capture_started = monotonic()
                    current = engine.repo.get(node.id)
                    artifact_source = Path(entry["workspaces"][current.role])
                    publication = engine.repo.published_version(current.id)
                    if current.status == "DONE" and publication and publication["files"]:
                        artifact_source, _ = engine.runner._published_source(
                            artifact_source, current.id
                        )
                    task["artifact_snapshot"] = capture_artifact_snapshot(
                        folder, artifact_source, current, phase,
                        secrets=secrets,
                    )
                    task["artifact_quote_evidence"] = capture_artifact_quote_evidence(
                        folder, task["artifact_snapshot"], task,
                        artifact_verifier.audit_records(),
                        task_ids={item.id for item in _tree(engine.repo, node.id)},
                        secrets=secrets,
                    )
                    task["evidence_capture_s"] = round(monotonic() - capture_started, 6)
                    task = sanitize_evidence(task, secrets)
                    entry["tasks"].append(task)
                    atomic_json(folder / f"{phase}-result.json", task)
                    atomic_json(root / "test.json", manifest)
                    atomic_text(root / "report.md", _report(manifest))
                    if task["root_status"] == "DONE" and args.quality_review == "human":
                        request = HumanRequest(
                            task_id=node.id,
                            agent_id="instructor-session",
                            kind="review",
                            action="review_instructor_task",
                            summary=f"Review {scenario.name}, Task {index} deliverables",
                            context=(
                                "Inspect the actual output and dialogue before deciding. "
                                "Approval records your quality judgment; it does not create "
                                "a machine acceptance check. Rejection stops later tasks "
                                "in this group; independent groups can still proceed."
                            ),
                            options=["approve", "reject"],
                            payload={"scenario_root": str(folder), "task": task},
                        )
                        paused_before_review = scenario_clock.paused_seconds
                        try:
                            review = await human.ask(request)
                        finally:
                            task["quality_review_wait_s"] = round(
                                scenario_clock.paused_seconds - paused_before_review, 6,
                            )
                        task["quality_status"] = (
                            "human_accepted" if review.decision == "approve" else "human_rejected"
                        )
                        task["human_quality_response"] = review.model_dump(mode="json")
                        task["verified_goal_success"] = (
                            review.decision == "approve"
                            and task["root_machine_accepted"] is not False
                        )
                        task["quality_method"] = "actual_human_review"
                        if index == len(scenario.tasks):
                            entry["through_review_elapsed_s"] = round(
                                monotonic() - scenario_began, 6
                            )
                    task["total_to_review_s"] = round(monotonic() - began, 6)
                    task["llm"] = llm.summary()["by_phase"].get(phase, {})
                    task["human"] = human.summary()["by_phase"].get(phase, {})
                    search_after = search.stats()
                    task["search_scenario_cumulative"] = search_after
                    task["search_delta"] = {
                        key: search_after.get(key, 0) - search_before.get(key, 0)
                        for key in (
                            "tool_calls",
                            "provider_requests",
                            "cache_hits",
                            "deduplicated",
                            "budget_denials",
                        )
                    }
                    previous = node.id
                    task.update(sanitize_evidence(task, secrets))
                    atomic_json(folder / f"{phase}-result.json", task)
                    atomic_json(root / "test.json", manifest)
                    atomic_text(root / "report.md", _report(manifest))
                    print(
                        json.dumps(
                            {
                                "scenario": scenario.id,
                                "phase": phase,
                                "root_status": task["root_status"],
                                "quality_status": task["quality_status"],
                            }
                        ),
                        flush=True,
                    )
                    if task["root_status"] != "DONE":
                        entry["status"] = "incomplete"
                        raise _TaskIncomplete(
                            "instructor task did not finish; later tasks were not posted"
                        )
                    if task["quality_status"] == "human_rejected":
                        entry["status"] = "quality_rejected"
                        raise _QualityRejected("actual human review rejected the delivered task")
                entry["status"] = (
                    "human_accepted"
                    if args.quality_review == "human"
                    else "agent_completed_quality_unreviewed"
                )
            except (ExecutionDeadline, asyncio.CancelledError, KeyboardInterrupt) as exc:
                timed_out = isinstance(exc, ExecutionDeadline)
                entry["status"] = "deadline_exceeded" if timed_out else "interrupted"
                if timed_out:
                    entry["deadline_scope"] = exc.scope
                if active_node is not None and not any(
                    item["root_task_id"] == active_node.id for item in entry["tasks"]
                ):
                    partial = sanitize_evidence(
                        _task_record(engine, active_node, monotonic() - began, phase), secrets
                    )
                    partial["attempt_interrupted"] = True
                    partial["execution_elapsed_s"] = round(phase_clock.active_seconds, 6)
                    partial["human_wait_s"] = round(phase_clock.paused_seconds, 6)
                    entry["tasks"].append(partial)
                    atomic_json(folder / f"{phase}-result.json", partial)
                if not timed_out:
                    raise
                entry["outcome_detail"] = str(exc)
                unsuccessful_scenarios.append(scenario.id)
            except (_TaskIncomplete, _QualityRejected) as exc:
                # Later tasks in this group depend on its earlier delivery.
                # Other instructor groups are independent and still need an attempt.
                entry["status"] = (
                    "quality_rejected" if isinstance(exc, _QualityRejected) else "incomplete"
                )
                entry["outcome_detail"] = str(exc)
                unsuccessful_scenarios.append(scenario.id)
            except Exception as exc:
                if not scenario_ready:
                    entry["status"] = "setup_error"
                    raise
                # Task-stage failure is local to this independent group. Setup
                # and cleanup errors remain infrastructure stops: resource
                # closure has not been established, so another group is unsafe.
                entry["status"] = "error"
                entry["error_type"] = type(exc).__name__
                entry["error_message"] = sanitize_evidence(redact(str(exc), settings), secrets)
                unsuccessful_scenarios.append(scenario.id)
            finally:
                if scenario_began is not None:
                    entry["elapsed_s"] = round(monotonic() - scenario_began, 6)
                    entry["execution_elapsed_s"] = round(scenario_clock.active_seconds, 6)
                    entry["human_wait_s"] = round(scenario_clock.paused_seconds, 6)
                    entry["quality_review_wait_s"] = round(sum(
                        task.get("quality_review_wait_s", 0) for task in entry["tasks"]
                    ), 6)
                entry["search"] = search.stats()
                entry["llm"] = llm.summary()
                entry["human"] = human.summary()
                try:
                    entry["state_snapshot"] = capture_scenario_state(
                        folder, engine, secrets=secrets,
                    )
                except Exception as exc:
                    entry["state_snapshot"] = {
                        "status": "unavailable", "error_type": type(exc).__name__,
                        "reason": "No state snapshot was established; do not infer final state.",
                    }
                try:
                    try:
                        engine.close()
                    finally:
                        await llm.aclose()
                finally:
                    persist_transport()
                    if transport_attempts is not None:
                        entry["stream_transport"] = str(folder / "stream-transport.json")
                atomic_json(root / "test.json", manifest)
        manifest["status"] = (
            "human_accepted" if args.quality_review == "human"
            else "agent_completed_quality_unreviewed"
        )
        if unsuccessful_scenarios:
            if len(scenarios) == 1:
                status = manifest["scenarios"][0]["status"]
                manifest["status"] = status if status in {
                    "quality_rejected", "deadline_exceeded", "error",
                } else "agent_incomplete"
            else:
                manifest["status"] = "partially_completed"
            manifest["unsuccessful_scenarios"] = unsuccessful_scenarios
            return 1
    except (_TaskIncomplete, _QualityRejected) as exc:
        manifest["status"] = (
            "quality_rejected" if isinstance(exc, _QualityRejected) else "agent_incomplete"
        )
        manifest["outcome_detail"] = str(exc)
        return 1
    except BaseException as exc:
        manifest["status"] = (
            "interrupted"
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            else "error"
        )
        manifest["error_type"] = type(exc).__name__
        manifest["error_message"] = sanitize_evidence(redact(str(exc), settings), secrets)
        raise
    finally:
        manifest["finished_at"] = _now()
        tasks = [task for scenario in manifest["scenarios"] for task in scenario["tasks"]]
        successes = sum(task["verified_goal_success"] is True for task in tasks)
        manifest["goal_metrics"] = {
            "planned_root_goals": manifest["planned_root_goals"],
            "persisted_task_deliveries": sum(not task.get("attempt_interrupted") for task in tasks),
            "agent_declared_complete": sum(task["agent_declared_complete"] for task in tasks),
            "reviewed_goals": sum(task["verified_goal_success"] is not None for task in tasks),
            "verified_successes": successes,
            "verified_goal_coverage": successes / manifest["planned_root_goals"],
            "quality_review_method": args.quality_review,
        }
        atomic_json(root / "test.json", manifest)
        atomic_text(root / "report.md", _report(manifest))
    return 0


def main(argv=None):
    args = parse_args(argv)
    if (
        args.search_requests < 0
        or args.max_steps < 1
        or args.max_tokens < 1000
        or args.task_timeout < 10
        or not math.isfinite(args.scenario_timeout)
        or args.scenario_timeout <= 0
    ):
        print("invalid search or task safety bound", file=sys.stderr)
        return 2
    from moumou.eval.cases import load_cases

    try:
        scenarios = load_cases(Path(args.tasks))
        if args.scenario != "all":
            scenarios = [scenario for scenario in scenarios if scenario.id == args.scenario]
        if not scenarios:
            raise ValueError("the requested instructor scenario was not found")
        if args.plan:
            print(
                json.dumps(
                    {
                        "kind": "instructor_test",
                        "dataset_kind": _dataset_kind(scenarios),
                        "human": args.human,
                        "tool_approval": _tool_approval(args),
                        "model": {
                            "profile": args.llm_settings.profile,
                            "model": args.llm_settings.model,
                            "base_url": args.llm_settings.base_url,
                            "timeout_s": args.llm_settings.timeout_s,
                        },
                        "llm_transport": args.llm_transport,
                        "llm_reasoning_effort": args.llm_reasoning_effort,
                        "llm_completion_cap": args.llm_completion_cap,
                        "llm_max_content_chars": args.llm_max_content_chars,
                        "llm_tool_choice": args.llm_tool_choice,
                        "research_progress": args.research_progress,
                        "execution_progress": args.execution_progress,
                        "llm_transient_http_retries": args.llm_transient_http_retries,
                        "llm_max_http_attempts_per_call": (
                            1 + (args.llm_transient_http_retries or 0)
                            if args.llm_transport == "stream" else None
                        ),
                        "llm_http_retry_backoff_s": (
                            [5, 15][:(args.llm_transient_http_retries or 0)]
                            if args.llm_transport == "stream" else None
                        ),
                        "quality_review": args.quality_review,
                        "require_declared_inputs": getattr(
                            args, "require_declared_inputs", False,
                        ),
                        "opening_context_policy": _opening_context_options(args),
                        "context_policy": args.compact_effective,
                        "task_budget": {"max_tokens": args.max_tokens, "max_steps": args.max_steps,
                                        "max_wall_s": args.task_timeout},
                        **_execution_policy(args.scenario_timeout),
                        "search_requests_per_scenario": args.search_requests,
                        "planned_root_goals": sum(len(case.tasks) for case in scenarios),
                        "scenarios": [
                            {
                                "id": case.id,
                                "name": case.name,
                                "roles": [role.name for role in case.roles],
                                "source_sha256": case.source_sha256,
                                "source_kind": case.source_kind,
                                "task_count": len(case.tasks),
                            }
                            for case in scenarios
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        return asyncio.run(run_suite(args, scenarios))
    except (OSError, ValueError, RuntimeError) as exc:
        from moumou.eval.artifacts import redact

        print(f"cannot run instructor test: {redact(str(exc), None)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
